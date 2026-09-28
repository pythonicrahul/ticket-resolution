"""FR-14: unattended evaluation run.

Usage:
    uv run python -m evaluation.harness --input PATH --output DIR

Spec: docs/specs/FR-14.md. Takes paths as arguments — never a hardcoded file name, because it
will be pointed at a file nobody has seen (Build Specification §04) — processes every ticket in
isolation so one failure cannot stop the run, logs every decision before acting on it, reconciles
the log against the tickets, and writes the metrics report the Build Specification §04 and the
Evaluation Framework require.

Exit code 0 when every ticket was processed and the log reconciled; 1 when the run could not start
or the log and the tickets disagree. An escalated ticket is not a failure: escalation is a correct
outcome, and until row 14 it is the only one.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ticketing_agent.config import ConfigError, Settings, load_settings
from ticketing_agent.ingest import Ticket, TicketFileError, evaluation_labels, load_tickets
from ticketing_agent.logging_store import DecisionLog, DecisionLogError, InvalidDecision
from ticketing_agent.pipeline import Outcome, Pipeline, StubPipeline
from ticketing_agent.retrieve import RetrievalError, Retriever

#: A segment with fewer tickets than this is flagged: the validation set has 8 enterprise
#: tickets, and a rate over 8 is not a rate anyone should act on (Governance fairness audit).
LOW_CONFIDENCE_SEGMENT = 10

#: The Evaluation Framework's own baselines and targets, so the report compares like with like.
TARGETS: tuple[tuple[str, str, str], ...] = (
    ("First contact resolution (proxy)", "42%", "≥60%"),
    ("Escalation rate", "58%", "≤30%"),
    ("Processing time p95", "—", "<3 s"),
    ("Intent precision (per class)", "—", "≥85%"),
    ("Retrieval hit rate", "—", "—"),
    ("Hallucination rate", "—", "≤5% (human review)"),
    ("Citation accuracy", "—", "≥95% (human review)"),
    ("Private data in outbound text", "—", "0"),
    ("Cross-segment variation", "—", "<5 points"),
)


@dataclass
class TicketResult:
    """One ticket's outcome plus what the report needs about it."""

    ticket: Ticket
    outcome: Outcome
    latency_ms: float
    labels: dict[str, Any]

    @property
    def length_band(self) -> str:
        """The Governance fairness audit segments by ticket length; the PRD's four do not."""
        return "short" if len(self.ticket.text) < 200 else "long_or_complex"

    def segments(self) -> dict[str, str]:
        return {**self.ticket.segments(), "length": self.length_band}


def main(argv: Sequence[str] | None = None) -> int:
    """FR-14: the documented command. Returns the process exit code, never raises."""
    parser = argparse.ArgumentParser(
        prog="python -m evaluation.harness",
        description="Process a ticket file end to end and write a metrics report (FR-14).")
    parser.add_argument("--input", required=True, help="the ticket file to process (any path)")
    parser.add_argument("--output", required=True, help="the directory to write the report into")
    parser.add_argument("--docs", default=None, help="documentation corpus; defaults to DOCS_PATH")
    parser.add_argument("--decision-log", default=None,
                        help="decision log path; defaults to DECISION_LOG_PATH")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--limit", type=int, default=None,
                        help="process only the first N tickets (a smoke run; recorded in the report)")
    parser.add_argument("--no-index-rebuild", action="store_true",
                        help="fail rather than build the documentation index")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 1

    try:
        return run(
            input_path=Path(args.input),
            output_dir=Path(args.output),
            settings=settings,
            docs_path=Path(args.docs) if args.docs else None,
            decision_log_path=Path(args.decision_log) if args.decision_log else None,
            run_id=args.run_id,
            limit=args.limit,
            allow_index_build=not args.no_index_rebuild,
        ).exit_code
    except HarnessError as exc:
        print(f"run failed: {exc}", file=sys.stderr)
        return 1


class HarnessError(Exception):
    """FR-14 §3.1: the run could not start or could not be trusted. Exits non-zero."""


@dataclass
class RunReport:
    metrics: dict[str, Any]
    exit_code: int
    output_dir: Path


def run(input_path: Path, output_dir: Path, settings: Settings, *,
        pipeline: Pipeline | None = None, docs_path: Path | None = None,
        decision_log_path: Path | None = None, run_id: str | None = None,
        limit: int | None = None, allow_index_build: bool = True) -> RunReport:
    """FR-14: one unattended run. Returns the report; writes it to `output_dir`."""
    started = time.monotonic()

    try:
        tickets = load_tickets(input_path)
    except TicketFileError as exc:
        raise HarnessError(str(exc)) from None
    tickets_in_file = len(tickets)
    if limit is not None:
        tickets = tickets[:limit]
    if not tickets:
        raise HarnessError(f"{input_path} contains no tickets to process")

    # Check the output directory *before* processing: discovering a bad --output after 80 tickets
    # would throw the whole run away, and A10 is that the report appears without further work.
    try:
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        probe = Path(output_dir) / ".harness-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise HarnessError(f"cannot write the report to {output_dir}: {exc}") from None

    if pipeline is None:
        pipeline = _build_stub_pipeline(settings, docs_path, allow_index_build)

    log_path = decision_log_path or settings.decision_log_path
    results: list[TicketResult] = []
    try:
        with DecisionLog(log_path, run_id=run_id) as log:
            log.start_run(input_path, len(tickets))
            for ticket in tickets:
                results.append(_process_one(ticket, pipeline, log))
            log.finish_run(len(results))
            reconciliation = log.reconcile(tickets)
    except DecisionLogError as exc:
        # A log that cannot be written breaks FR-13 and NFR-05 for every ticket in the run, not
        # one, so this is the one failure that stops the run rather than escalating (D-27).
        raise HarnessError(f"the decision log could not be written: {exc}") from None

    try:
        metrics = _metrics(results, tickets, reconciliation, settings, input_path, limit,
                           time.monotonic() - started, tickets_in_file, log_path, run_id)
        _write_reports(output_dir, metrics, results)
    except Exception as exc:  # noqa: BLE001 - the run happened; losing the report is not allowed
        raise HarnessError(
            f"every ticket was processed and logged, but the report could not be written: "
            f"{type(exc).__name__}: {exc}") from None

    ok = reconciliation.ok and len(results) == len(tickets)
    if not ok:
        print("reconciliation FAILED: the decision log and the tickets processed disagree",
              file=sys.stderr)
    print(f"{len(results)} tickets processed, "
          f"{metrics['volume']['answered_automatically']} answered, "
          f"{metrics['volume']['escalated']} escalated, "
          f"{metrics['volume']['blocked_by_guardrails']} blocked; "
          f"report in {output_dir}")
    return RunReport(metrics=metrics, exit_code=0 if ok else 1, output_dir=Path(output_dir))


def _process_one(ticket: Ticket, pipeline: Pipeline, log: DecisionLog) -> TicketResult:
    """FR-14 §3.3: one ticket, in isolation. **Any** failure here escalates and the run goes on.

    The guard covers the whole of one ticket's handling, not only the pipeline call: building the
    log row and writing it can fail too — an `Outcome` that is not valid for FR-13 (no reason on
    an escalation, an unknown stage, an auto-respond with no threshold) or a pipeline that returns
    something that is not an `Outcome` at all. Those are one ticket's problem. Only a log that
    cannot be written at all is the run's problem (D-27), and that is the one exception allowed
    past here.
    """
    started = time.monotonic()
    outcome = _outcome_or_escalation(ticket, pipeline)
    latency_ms = (time.monotonic() - started) * 1000

    try:
        entry = outcome.to_entry()
        entry = type(entry)(**{**entry.__dict__, "latency_ms": latency_ms})
    except Exception as exc:  # noqa: BLE001 - a malformed outcome is this ticket's failure
        outcome = _pipeline_error(ticket, exc, "the outcome could not be turned into a log row")
        entry = outcome.to_entry()
        entry = type(entry)(**{**entry.__dict__, "latency_ms": latency_ms})

    try:
        # Log before the outcome is recorded anywhere else (FR-13, CLAUDE.md).
        log.perform(entry, lambda: None)
    except InvalidDecision as exc:
        # The *call* was wrong, not the log: escalate this one ticket with a row that is valid by
        # construction. Letting this reach the run-level handler would report a component's bug
        # as an unwritable log and abandon every remaining ticket.
        outcome = _pipeline_error(ticket, exc, "the decision was not valid for the log")
        entry = outcome.to_entry()
        entry = type(entry)(**{**entry.__dict__, "latency_ms": latency_ms})
        log.perform(entry, lambda: None)

    return TicketResult(ticket=ticket, outcome=outcome, latency_ms=latency_ms,
                        labels=dict(evaluation_labels(ticket)))


def _outcome_or_escalation(ticket: Ticket, pipeline: Pipeline) -> Outcome:
    """The pipeline's outcome, or an escalation — never an exception and never a wrong type."""
    try:
        outcome = pipeline.process(ticket)
    except Exception as exc:  # noqa: BLE001 - one ticket failing must never stop a run
        return _pipeline_error(ticket, exc, "handling the ticket raised")
    if not isinstance(outcome, Outcome):
        return _pipeline_error(
            ticket, TypeError(f"the pipeline returned {type(outcome).__name__}, not an Outcome"),
            "the pipeline returned the wrong type")
    return outcome


def _pipeline_error(ticket: Ticket, exc: BaseException, what: str) -> Outcome:
    """FR-14 §3.3: a valid, loggable escalation for a ticket whose handling went wrong."""
    return Outcome(
        ticket=ticket,
        decision="escalate",
        reason="pipeline_error",
        explanation=("Something went wrong while handling this ticket, so it goes to a person "
                     "rather than being answered."),
        all_reasons=("pipeline_error",),
        stage="pipeline",
        detail=f"{what}: {_safe_detail(ticket, exc)}",
        # FR-15 is named when the cause was the model provider, so the log says which requirement
        # the failure belongs to (spec §5).
        requirement_ids=(("FR-14", "FR-15") if _is_provider_failure(exc) else ("FR-14",)),
    )


def _safe_detail(ticket: Ticket, exc: BaseException) -> str:
    """The exception type, and its message only when it cannot be quoting the customer (NFR-04).

    A parse error or a provider error can echo the payload it choked on, and `detail` reaches the
    decision log, so the message is dropped whenever any run of the ticket's own words appears in
    it. The type is always safe and is usually the useful part.
    """
    name = type(exc).__name__
    message = str(exc)[:300]
    words = ticket.text.split()
    for start in range(max(1, len(words) - 3)):
        fragment = " ".join(words[start:start + 4])
        if len(fragment) >= 20 and fragment in message:
            return f"{name} (message withheld: it quotes the ticket)"
    return f"{name}: {message}" if message else name


def _is_provider_failure(exc: BaseException) -> bool:
    return any(base.__name__ == "ProviderFailure" for base in type(exc).__mro__)


def _build_stub_pipeline(settings: Settings, docs_path: Path | None,
                         allow_index_build: bool) -> Pipeline:
    """Row 6's pipeline: the parts that exist, wired together (FR-14 §2)."""
    try:
        retriever = Retriever(settings)
        stats = retriever.build_index(docs_path)
        if stats.rebuilt and not allow_index_build:
            raise HarnessError(
                "--no-index-rebuild was given but the documentation index had to be built "
                "(the corpus, the chunking rules or the embedding model changed). Re-run without "
                "the flag, or point CHROMA_PATH at the index you meant to use.")
        if stats.skipped:
            print(f"warning: {len(stats.skipped)} document(s) were not indexed: {stats.skipped}",
                  file=sys.stderr)
    except (RetrievalError, ConfigError) as exc:
        raise HarnessError(f"the documentation index could not be prepared: {exc}") from None
    return StubPipeline(retriever=retriever, threshold=settings.relevance_threshold)


# --- metrics --------------------------------------------------------------------------


def _metrics(results: list[TicketResult], tickets: list[Ticket], reconciliation: Any,
             settings: Settings, input_path: Path, limit: int | None,
             wall_seconds: float, tickets_in_file: int, log_path: Path,
             run_id: str | None) -> dict[str, Any]:
    """Every figure the Build Specification §04 and the Evaluation Framework require."""
    scored = [r for r in results if r.labels]
    answered = [r for r in results if r.outcome.decision == "auto_respond"]
    escalated = [r for r in results if r.outcome.decision == "escalate"]
    # FR-12 makes `block` a non-terminal action followed by an escalation, so a ticket whose
    # reply was blocked ends as an escalation. Counting decisions would report 0 for ever.
    blocked = [r for r in results
               if r.outcome.decision == "block"
               or any(not passed for _, passed in r.outcome.guardrail_results)
               or "block" in r.outcome.all_reasons]
    latencies = [r.latency_ms for r in results]

    return {
        "run": {
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "input": str(input_path),
            "tickets_in_file": tickets_in_file,
            "run_id": run_id,
            "decision_log": str(log_path),
            "limit": limit,
            "wall_seconds": round(wall_seconds, 2),
            "scored_against_labels": f"{len(scored)} of {len(results)}",
            "thresholds": {
                "relevance_threshold": settings.relevance_threshold,
                "confidence_threshold": settings.confidence_threshold,
                "retrieval_top_k": settings.retrieval_top_k,
            },
        },
        "volume": {
            "tickets_processed": len(results),
            "answered_automatically": len(answered),
            "escalated": len(escalated),
            "blocked_by_guardrails": len(blocked),
        },
        "business": _business(results, answered, escalated, latencies),
        "technical": _technical(results, scored, latencies),
        "governance": _governance(results, reconciliation),
        "segments": _segments(results),
        "reasons": _counts(r.outcome.reason or "none" for r in results),
        # D-13: an unseen channel escalates by rule, so it gets its own line rather than hiding
        # inside malformed_ticket. Same for the other ingest defects.
        "ingest_defects": _counts(defect for r in results for defect in r.ticket.defects),
        "unknown_channel_tickets": sum(1 for r in results if r.ticket.channel == "unknown"),
        "results_table": _results_table(results, answered, escalated, latencies, scored),
        "gaps": _gaps(_pct(len(escalated), len(results))),
    }


def _business(results: list[TicketResult], answered: list[TicketResult],
              escalated: list[TicketResult], latencies: list[float]) -> dict[str, Any]:
    return {
        # PRD §8 and FR-14 §7: offline this measures correct automated handling, not a confirmed
        # resolution. The name is kept for comparability and the caveat travels with it.
        "first_contact_resolution_proxy_pct": _pct(len(answered), len(results)),
        "escalation_rate_pct": _pct(len(escalated), len(results)),
        "processing_time_ms": {
            "mean": _round(statistics.mean(latencies)) if latencies else None,
            "median": _round(statistics.median(latencies)) if latencies else None,
        },
        "response_time_note": (
            "The data has no first-reply timestamp, so no customer-visible wait can be computed. "
            "What is reported is this system's own processing time per ticket."),
    }


def _technical(results: list[TicketResult], scored: list[TicketResult],
               latencies: list[float]) -> dict[str, Any]:
    predictions = [(r.labels.get("intent"), r.outcome.prediction_value)
                   for r in scored if r.outcome.prediction_value]
    retrieval = [r for r in scored if r.labels.get("expected_doc_ids")]
    hits = sum(1 for r in retrieval
               if set(r.labels["expected_doc_ids"]) & {p.doc_id for p in r.outcome.passages})
    route_agreement = [r for r in scored if r.labels.get("expected_route")]
    agreed = sum(1 for r in route_agreement
                 if r.labels["expected_route"] == r.outcome.decision)

    return {
        "classification": (_per_class(predictions) if predictions else
                           {"not_computable": "no intent prediction yet (row 8)"}),
        "retrieval_hit_rate_pct": _pct(hits, len(retrieval)) if retrieval else None,
        "retrieval_scored_tickets": len(retrieval),
        "route_agreement_pct": _pct(agreed, len(route_agreement)) if route_agreement else None,
        "route_agreement_note": (
            "Agreement with the labelled expected_route. While the answering path is unbuilt "
            "every ticket escalates, so this figure is simply the share of tickets labelled "
            "escalate — it does not measure routing."),
        "retrieval_hit_rate_note": (
            "Share of labelled-answerable tickets whose expected article was retrieved, at the "
            "relevance threshold in use. Tickets with no expected article are excluded, so this "
            "has no false-positive counterpart: it cannot fall when retrieval returns too much."),
        "latency_ms": {
            "median": _round(statistics.median(latencies)) if latencies else None,
            "p95": _round(_percentile(latencies, 0.95)) if latencies else None,
        },
        "citations_that_do_not_resolve": sum(
            1 for r in results for c in r.outcome.citations
            if c not in {p.chunk_id for p in r.outcome.passages}),
    }


def _governance(results: list[TicketResult], reconciliation: Any) -> dict[str, Any]:
    guardrail_failures: dict[str, int] = defaultdict(int)
    for result in results:
        for name, passed in result.outcome.guardrail_results:
            if not passed:
                guardrail_failures[name] += 1
    return {
        "decisions_logged": reconciliation.terminal_rows,
        "reconciles": reconciliation.ok,
        "reconciliation": {
            "tickets_in": reconciliation.tickets_in,
            "terminal_rows": reconciliation.terminal_rows,
            "recorded_tickets_in": reconciliation.recorded_tickets_in,
            "missing": list(reconciliation.missing),
            "extra": list(reconciliation.extra),
            "duplicated": list(reconciliation.duplicated),
            "index_gaps": list(reconciliation.index_gaps),
            "tickets_without_index": list(reconciliation.tickets_without_index),
        },
        "guardrail_activations_by_type": dict(sorted(guardrail_failures.items())),
        "private_data_detections": guardrail_failures.get("private_data", 0),
        "redactions_in_log": reconciliation.redactions,
        "model_calls": reconciliation.model_calls,
        "cache_hits": reconciliation.cache_hits,
    }


def _segments(results: list[TicketResult]) -> dict[str, Any]:
    """NFR-06 and the Governance fairness audit: every metric by segment, with sample sizes."""
    out: dict[str, Any] = {}
    keys = ("tier", "fluency", "region", "channel", "length")
    for key in keys:
        buckets: dict[str, list[TicketResult]] = defaultdict(list)
        for result in results:
            buckets[result.segments()[key]].append(result)
        rows = {}
        for name, group in sorted(buckets.items()):
            answered = [r for r in group if r.outcome.decision == "auto_respond"]
            # NFR-06 is about resolution rate *and quality*, so the retrieval figure is
            # segmented too: a retrieval gap on non-fluent tickets is the one the Governance
            # fairness audit predicts and the one the PRD's Stage 1 disagreement is about.
            scored = [r for r in group if r.labels.get("expected_doc_ids")]
            hits = sum(1 for r in scored
                       if set(r.labels["expected_doc_ids"]) & {p.doc_id for p in r.outcome.passages})
            rows[name] = {
                "tickets": len(group),
                "answered_pct": _pct(len(answered), len(group)),
                "escalated_pct": _pct(len(group) - len(answered), len(group)),
                "retrieval_hit_rate_pct": _pct(hits, len(scored)),
                "retrieval_scored": len(scored),
                "median_latency_ms": _round(statistics.median([r.latency_ms for r in group])),
                "low_confidence": len(group) < LOW_CONFIDENCE_SEGMENT,
            }
        # Every segment with tickets counts towards the figure. Excluding the small ones would
        # drop the 8 enterprise tickets — the segment NFR-06 names — and report the gap as
        # smaller than it is. The low-confidence flags travel with it instead.
        spread = [row["answered_pct"] for row in rows.values() if row["answered_pct"] is not None]
        measurable = len(spread) > 1
        out[key] = {
            "rows": rows,
            "variation_points": _round(max(spread) - min(spread)) if measurable else None,
            "variation_basis": (
                f"{len(spread)} segments" if measurable else "not measurable: fewer than two"),
            "low_confidence_segments": sorted(
                name for name, row in rows.items() if row["low_confidence"]),
        }
    return out


def _per_class(pairs: list[tuple[str | None, str | None]]) -> dict[str, Any]:
    """Precision and recall per class, which is what the Evaluation Framework asks for."""
    classes = sorted({c for pair in pairs for c in pair if c})
    per_class = {}
    for name in classes:
        tp = sum(1 for truth, pred in pairs if truth == name and pred == name)
        fp = sum(1 for truth, pred in pairs if truth != name and pred == name)
        fn = sum(1 for truth, pred in pairs if truth == name and pred != name)
        per_class[name] = {
            "precision_pct": _pct(tp, tp + fp) if tp + fp else None,
            "recall_pct": _pct(tp, tp + fn) if tp + fn else None,
            "support": tp + fn,
        }
    correct = sum(1 for truth, pred in pairs if truth == pred)
    return {"overall_accuracy_pct": _pct(correct, len(pairs)), "per_class": per_class}


def _results_table(results: list[TicketResult], answered: list[TicketResult],
                   escalated: list[TicketResult], latencies: list[float],
                   scored: list[TicketResult]) -> list[dict[str, Any]]:
    """The Evaluation Framework's own table: measure, baseline, target, achieved, confidence."""
    achieved = {
        "First contact resolution (proxy)": _fmt(_pct(len(answered), len(results))),
        "Escalation rate": _fmt(_pct(len(escalated), len(results))),
        "Processing time p95": (f"{_round(_percentile(latencies, 0.95))} ms" if latencies else "—"),
        "Intent precision (per class)": "not computable yet (row 8)",
        "Retrieval hit rate": "see technical.retrieval_hit_rate_pct",
        "Hallucination rate": "needs human review of ≥50 responses (two assessors)",
        "Citation accuracy": "needs human review; the harness counts unresolvable citations only",
        "Private data in outbound text": "no reply is sent yet (row 11)",
        "Cross-segment variation": "see segments.*.variation_points",
    }
    confidence = {
        "First contact resolution (proxy)": (
            f"proxy: automated handling, not confirmed resolution; n={len(results)}"),
        "Escalation rate": (
            f"n={len(results)}; 100% is by construction until row 14, not a tuning result"),
        "Processing time p95": "no model call in the stub pipeline, so this will rise at row 11",
        "Intent precision (per class)": f"labels available for {len(scored)} tickets",
    }
    return [
        {"measure": measure, "baseline": baseline, "target": target,
         "achieved": achieved.get(measure, "—"), "confidence": confidence.get(measure, "—")}
        for measure, baseline, target in TARGETS
    ]


def _gaps(escalation_rate: float | None) -> list[str]:
    """FR-14 §4: a figure the system cannot produce yet says so, by name."""
    by_construction = []
    if escalation_rate is not None and escalation_rate >= 99.9:
        by_construction = [
            ("Escalation rate and first contact resolution: **100% escalation is by construction**, "
             "not a result. Classification, drafting, guardrails and routing are rows 8 to 13, so "
             "the pipeline escalates every ticket on purpose (FR-14 §7)."),
        ]
    return [
        *by_construction,
        "Intent precision and recall: no classifier yet (row 8).",
        ("Hallucination rate and citation accuracy: need human review of at least 50 responses by "
         "two assessors (Evaluation Framework tier two); the harness reports unresolvable "
         "citations as a floor only."),
        ("Private data in outbound replies: nothing is sent yet (row 11), so zero here means "
         "'nothing was generated', not 'nothing leaked'."),
        ("Customer satisfaction: no live customers; the Evaluation Framework's rubric proxy needs "
         "a human sample."),
        "Confidence calibration: needs the classifier's confidences (row 8).",
        "Response time: the data has no first-reply timestamp, so only processing time is real.",
    ]


def _counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def _pct(part: int, whole: int) -> float | None:
    """None when there is nothing to measure: "0.0%" would read as a measured zero."""
    return round(100.0 * part / whole, 1) if whole else None


def _fmt(value: float | None, suffix: str = "%") -> str:
    return "—" if value is None else f"{value}{suffix}"


def _round(value: float | None) -> float | None:
    return round(float(value), 1) if value is not None else None


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: ceil(fraction x n) - 1. Rounding instead understates p95."""
    import math

    ordered = sorted(values)
    rank = math.ceil(fraction * len(ordered))
    return ordered[max(0, min(len(ordered) - 1, rank - 1))]


# --- writing --------------------------------------------------------------------------


def _write_reports(output_dir: Path, metrics: dict[str, Any],
                   results: list[TicketResult]) -> None:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    (output_dir / "metrics.md").write_text(_markdown(metrics), encoding="utf-8")
    with (output_dir / "outcomes.jsonl").open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps({
                "ticket_id": result.ticket.ticket_id,
                "source_index": result.ticket.source_index,
                "decision": result.outcome.decision,
                "reason": result.outcome.reason,
                "all_reasons": list(result.outcome.all_reasons),
                "doc_ids": [p.doc_id for p in result.outcome.passages],
                "chunk_ids": [p.chunk_id for p in result.outcome.passages],
                "citations": list(result.outcome.citations),
                "latency_ms": round(result.latency_ms, 1),
                "segments": result.segments(),
            }, sort_keys=True) + "\n")


def _markdown(metrics: dict[str, Any]) -> str:
    run, volume = metrics["run"], metrics["volume"]
    business, technical, governance = metrics["business"], metrics["technical"], metrics["governance"]
    lines = [
        "# Evaluation run (FR-14)",
        "",
        f"- Generated: {run['generated_at']}  ",
        f"- Input: `{run['input']}` — {run['tickets_in_file']} tickets in the file  ",
        f"- Scored against labels: **{run['scored_against_labels']}**  ",
        (f"- Thresholds in use: relevance **{run['thresholds']['relevance_threshold']}**, "
         f"confidence **{run['thresholds']['confidence_threshold']}**, "
         f"top_k {run['thresholds']['retrieval_top_k']}  "),
        f"- Wall time: {run['wall_seconds']} s  ",
    ]
    if run["limit"] is not None:
        lines.append(f"- **PARTIAL RUN: --limit {run['limit']} was used. Not a full-set result.**  ")
    lines += [
        "",
        "## Volume",
        "",
        "| figure | value |",
        "|---|---|",
        f"| Tickets processed | {volume['tickets_processed']} |",
        f"| Answered automatically | {volume['answered_automatically']} |",
        f"| Escalated | {volume['escalated']} |",
        f"| Blocked by guardrails | {volume['blocked_by_guardrails']} |",
        "",
        "## Business outcomes",
        "",
        "| figure | value |",
        "|---|---|",
        f"| First contact resolution (proxy) | {_fmt(business['first_contact_resolution_proxy_pct'])} |",
        f"| Escalation rate | {_fmt(business['escalation_rate_pct'])} |",
        f"| Processing time, mean | {business['processing_time_ms']['mean']} ms |",
        f"| Processing time, median | {business['processing_time_ms']['median']} ms |",
        "",
        f"*{business['response_time_note']}*",
        "",
        "## Technical",
        "",
        "| figure | value |",
        "|---|---|",
        (f"| Retrieval hit rate | {_fmt(technical['retrieval_hit_rate_pct'])} "
         f"(n={technical['retrieval_scored_tickets']}) |"),
        f"| Route agreement with labels | {_fmt(technical['route_agreement_pct'])} |",
        f"| Latency median | {technical['latency_ms']['median']} ms |",
        f"| Latency p95 | {technical['latency_ms']['p95']} ms |",
        f"| Citations that do not resolve | {technical['citations_that_do_not_resolve']} |",
        "",
        f"*Retrieval hit rate: {technical['retrieval_hit_rate_note']}*",
        "",
        f"*Route agreement: {technical['route_agreement_note']}*",
        "",
        "### Classification, per class",
        "",
    ]
    classification = technical["classification"]
    if "per_class" in classification:
        lines += ["| intent | precision | recall | support |", "|---|---|---|---|"]
        for name, row in sorted(classification["per_class"].items()):
            lines.append(f"| {name} | {_fmt(row['precision_pct'])} | {_fmt(row['recall_pct'])} "
                         f"| {row['support']} |")
        lines.append("")
        lines.append(f"Overall accuracy: {_fmt(classification['overall_accuracy_pct'])}")
    else:
        lines.append(f"*Not computable: {classification['not_computable']}*")

    lines += [
        "",
        "## Governance",
        "",
        "| figure | value |",
        "|---|---|",
        f"| Decisions logged | {governance['decisions_logged']} |",
        (f"| Log reconciles with tickets processed | "
         f"{'yes' if governance['reconciles'] else '**NO**'} |"),
        (f"| Guardrail activations by type | "
         f"{governance['guardrail_activations_by_type'] or 'none'} |"),
        f"| Private data detections | {governance['private_data_detections']} |",
        f"| Redactions in the log | {governance['redactions_in_log']} |",
        f"| Model calls / cache hits | {governance['model_calls']} / {governance['cache_hits']} |",
        "",
        "## Why tickets ended where they did",
        "",
        "| reason | tickets |",
        "|---|---|",
    ]
    lines += [f"| {reason} | {count} |" for reason, count in metrics["reasons"].items()]
    lines += [
        "",
        (f"Tickets arriving on an unrecognised channel: **{metrics['unknown_channel_tickets']}** "
         "(D-13: these escalate by rule)."),
        "",
    ]
    if metrics["ingest_defects"]:
        lines += ["| ingest defect | tickets |", "|---|---|"]
        lines += [f"| {defect} | {count} |"
                  for defect, count in metrics["ingest_defects"].items()]
        lines.append("")

    lines += ["", "## Segments (NFR-06, Governance fairness audit)", ""]
    for key, block in metrics["segments"].items():
        lines += [
            f"### By {key}",
            "",
            ("| segment | tickets | answered | escalated | retrieval hit rate "
             "| median latency | note |"),
            "|---|---|---|---|---|---|---|",
        ]
        for name, row in block["rows"].items():
            note = "low confidence (n<10)" if row["low_confidence"] else ""
            lines.append(
                f"| {name} | {row['tickets']} | {_fmt(row['answered_pct'])} "
                f"| {_fmt(row['escalated_pct'])} | {_fmt(row['retrieval_hit_rate_pct'])} "
                f"(n={row['retrieval_scored']}) | {row['median_latency_ms']} ms | {note} |")
        variation = block["variation_points"]
        if variation is None:
            lines += ["", ("Variation across segments: **not measurable** "
                           f"({block['variation_basis']})."), ""]
        else:
            verdict = "within" if variation < 5 else "**above**"
            lines += ["", (f"Variation across segments: **{variation} points**, {verdict} "
                           f"NFR-06's 5-point limit ({block['variation_basis']})."), ""]
        if block["low_confidence_segments"]:
            lines += [("Small segments included in that figure, treat with care: "
                       f"{', '.join(block['low_confidence_segments'])}."), ""]

    lines += ["## Results table (Evaluation Framework)", "",
              "| measure | baseline | target | achieved | confidence in the figure |",
              "|---|---|---|---|---|"]
    for row in metrics["results_table"]:
        lines.append(f"| {row['measure']} | {row['baseline']} | {row['target']} "
                     f"| {row['achieved']} | {row['confidence']} |")

    lines += ["", "## What this run does not measure", ""]
    lines += [f"- {gap}" for gap in metrics["gaps"]]
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
