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
outcome. The stub stays as the thing to run when no model is configured, and a run that used
it says so in its own report (`run.pipeline`).
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
from ticketing_agent.logging_store import (
    DecisionLog,
    DecisionLogError,
    InvalidDecision,
    redact,
)
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
    parser.add_argument("--stub-pipeline", action="store_true",
                        help="run ingest and retrieval only, with no model calls. For exercising "
                             "the run machinery; a gate run must not use it.")
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
            use_stub=args.stub_pipeline,
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
        limit: int | None = None, allow_index_build: bool = True,
        use_stub: bool = False) -> RunReport:
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

    built_here = pipeline is None
    if built_here:
        pipeline = (_build_stub_pipeline(settings, docs_path, allow_index_build) if use_stub
                    else _build_pipeline(settings, docs_path, allow_index_build))

    log_path = decision_log_path or settings.decision_log_path
    results: list[TicketResult] = []
    try:
        with DecisionLog(log_path, run_id=run_id) as log:
            # The rows *inside* one ticket — a draft written, a reply blocked — belong to the
            # component that took the decision (FR-13 §3.1). The terminal row stays here.
            attach = getattr(pipeline, "attach_log", None)
            if callable(attach):
                attach(log)
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
                           time.monotonic() - started, tickets_in_file, log_path, run_id,
                           # Read off the object, not off the flag: a caller that injects the
                           # stub directly is still running a stub, and the report has to
                           # describe what ran (A10).
                           stub=use_stub or isinstance(pipeline, StubPipeline),
                           pipeline_name=type(pipeline).__name__)
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


def _build_pipeline(settings: Settings, docs_path: Path | None,
                    allow_index_build: bool) -> Pipeline:
    """Row 14: the real graph. Every component, or a refusal that names what is missing.

    A missing classifier or an unset `MODEL_NAME` stops the run here rather than quietly running
    something weaker: a gate run that silently escalated everything would look like a result.
    """
    from ticketing_agent.classify import ClassifierError, IntentClassifier, TrainedClassifier
    from ticketing_agent.generate import Drafter
    from ticketing_agent.guardrails import GroundingJudge, Guardrails
    from ticketing_agent.handover import HandoverWriter
    from ticketing_agent.pipeline import SupportPipeline
    from ticketing_agent.provider import ProviderClient
    from ticketing_agent.route import Router

    retriever = _index(settings, docs_path, allow_index_build)
    try:
        settings.require_model()
    except ConfigError as exc:
        raise HarnessError(
            f"{exc} Run with --stub-pipeline to exercise the machinery without a model.") from None
    try:
        classifier = IntentClassifier(TrainedClassifier.load(settings.classifier_path))
    except (ClassifierError, OSError, ValueError) as exc:
        raise HarnessError(
            f"the classifier at {settings.classifier_path} could not be loaded: {exc}. Train it "
            "with scripts/train_classifier.py, or run with --stub-pipeline.") from None

    client = ProviderClient(settings)
    return SupportPipeline(
        retriever=retriever, classifier=classifier, router=Router(settings),
        drafter=Drafter(client), guardrails=Guardrails(judge=GroundingJudge(client)),
        handover_writer=HandoverWriter(client), settings=settings)


def _build_stub_pipeline(settings: Settings, docs_path: Path | None,
                         allow_index_build: bool) -> Pipeline:
    """Row 6's pipeline: ingest and retrieval only, for exercising the run machinery."""
    return StubPipeline(retriever=_index(settings, docs_path, allow_index_build),
                        threshold=settings.relevance_threshold)


def _index(settings: Settings, docs_path: Path | None, allow_index_build: bool) -> Any:
    """The documentation index both pipelines need (FR-10)."""
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
    return retriever


# --- metrics --------------------------------------------------------------------------


def _metrics(results: list[TicketResult], tickets: list[Ticket], reconciliation: Any,
             settings: Settings, input_path: Path, limit: int | None,
             wall_seconds: float, tickets_in_file: int, log_path: Path,
             run_id: str | None, stub: bool = False,
             pipeline_name: str = "unknown") -> dict[str, Any]:
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

    technical = _technical(results, scored, latencies, answered)
    governance = _governance(results, reconciliation)
    segments = _segments(results)
    reasons = _counts(r.outcome.reason or "none" for r in results)

    return {
        "run": {
            "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "input": str(input_path),
            "tickets_in_file": tickets_in_file,
            "run_id": run_id,
            "decision_log": str(log_path),
            "limit": limit,
            # R4 review: `"full"` was asserted for **any** object that was not the stub,
            # including the fakes every harness test injects. The class name is what is
            # actually known, so the provenance field in the A10 artefact says that; `full` is
            # claimed only for the real graph.
            "pipeline": ("stub" if stub else
                         "full" if pipeline_name == "SupportPipeline" else "injected"),
            "pipeline_class": pipeline_name,
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
        # Computed once above, not twice: the results table reads the same dicts the sections
        # print, so the two cannot drift — which is the defect R4 exists to fix.
        "technical": technical,
        "governance": governance,
        "segments": segments,
        "reasons": reasons,
        # D-13: an unseen channel escalates by rule, so it gets its own line rather than hiding
        # inside malformed_ticket. Same for the other ingest defects.
        "ingest_defects": _counts(defect for r in results for defect in r.ticket.defects),
        "unknown_channel_tickets": sum(1 for r in results if r.ticket.channel == "unknown"),
        "results_table": _results_table(results, answered, escalated, latencies, scored,
                                         technical, governance, segments, stub,
                                         settings.kill_switch_on, reasons),
        "gaps": _gaps(results, answered, scored, technical, stub),
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
               latencies: list[float], answered: list[TicketResult]) -> dict[str, Any]:
    predictions = [(r.labels.get("intent"), r.outcome.prediction_value)
                   for r in scored if r.outcome.prediction_value]
    retrieval = [r for r in scored if r.labels.get("expected_doc_ids")]
    hits = sum(1 for r in retrieval
               if set(r.labels["expected_doc_ids"]) & {p.doc_id for p in r.outcome.passages})
    route_agreement = [r for r in scored if r.labels.get("expected_route")]
    agreed = sum(1 for r in route_agreement
                 if r.labels["expected_route"] == r.outcome.decision)

    return {
        # R4: "not computable" now says which input was missing, not which build row is next.
        # The report used to print "no classifier yet (row 8)" beside a per-class table at 100%.
        "classification": (_per_class(predictions) if predictions else
                           {"not_computable": _why_no_classification(results, scored)}),
        "calibration": _calibration(scored),
        "retrieval_hit_rate_pct": _pct(hits, len(retrieval)) if retrieval else None,
        "retrieval_scored_tickets": len(retrieval),
        "route_agreement_pct": _pct(agreed, len(route_agreement)) if route_agreement else None,
        # R4: the caveat is real and is kept, but it is now conditional on **this run** rather
        # than on a build row. It read "while the answering path is unbuilt", which was true at
        # row 6 and false from row 14 — and was still printed beside 42 sent replies.
        "route_agreement_note": _route_agreement_note(results, scored, answered),
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


def _route_agreement_note(results: list[TicketResult], scored: list[TicketResult],
                          answered: list[TicketResult]) -> str:
    """What the route-agreement figure is worth, given how this run actually split.

    The note read "while the answering path is unbuilt every ticket escalates", which was true
    at row 6 and false from row 14 — and was still printed beside 42 sent replies. R4 made it
    conditional on the run; the R4 review then pointed out that a binary switch flips at the
    *first* answered ticket, so 79 of 80 escalations read as "a real comparison". The share is
    the honest form: a reader can see for themselves how close to a base rate it is.
    """
    base = "Agreement with the labelled expected_route. "
    if not results:
        return base + "No tickets were processed."
    escalated_share = _pct(len(results) - len(answered), len(results))
    labelled_escalate = _pct(
        sum(1 for r in scored if r.labels.get("expected_route") == "escalate"), len(scored))
    if not answered:
        return (base + "Every ticket escalated in this run, so the figure is simply the share "
                "of tickets labelled escalate — it does not measure routing.")
    detail = (f"This run escalated {_fmt(escalated_share)} of tickets"
              + (f" against {_fmt(labelled_escalate)} labelled escalate" if scored else "")
              + ". The closer those two are, the more of this figure is the base rate rather "
                "than agreement, and it says nothing about whether the label or the system is "
                "right on a disagreement.")
    return base + detail


def _why_no_classification(results: list[TicketResult],
                           scored: list[TicketResult]) -> str:
    """Which missing input stopped the figure, in the run's own terms (FR-14 §4)."""
    if not results:
        return "no tickets were processed"
    if not scored:
        return ("no labels in the input file, so predictions cannot be scored against anything")
    return ("no ticket produced an intent prediction, so there is nothing to score against "
            "the labels")


def _calibration(scored: list[TicketResult]) -> dict[str, Any]:
    """NFR-03: stated confidence against observed accuracy, per band, from **this run**.

    The report used to say calibration "needs the classifier's confidences", which stopped
    being true the moment the classifier was wired in.

    Two things the R4 review got right about the inputs, and both of them mattered:

    * **`intent_confidence`, not `prediction_confidence`.** D-61: `prediction_confidence` is the
      number *routing compared*, floored to 0.0 when the classifier's own value is unusable —
      and `_outcome` sets it even when there is no classification at all. Since `0.0 is not
      None`, calibrating it turned a run where the classifier raised on every ticket into six
      rows at stated 0.0% / observed 0.0% and a clean "within NFR-03's 5-point limit", printed
      two lines under the section that correctly said no intent could be scored.
    * **`prediction_value`, the same field the per-class table scores.** Keying correctness off
      `Outcome.intent` instead meant one report could carry two verdicts on the same tickets —
      100% per-class precision beside a 95-point calibration gap — on the classifier path
      `pipeline.py` deliberately tolerates (one without `row_fields`).
    """
    pairs = [(r.outcome.intent_confidence,
              r.labels.get("intent") == r.outcome.prediction_value)
             for r in scored
             if r.outcome.prediction_value and r.outcome.intent_confidence is not None
             and r.labels.get("intent")]
    if not pairs:
        return {"bands": [], "pairs": 0, "not_computable": (
            "no ticket carried both a stated confidence and a labelled intent")}

    from ticketing_agent.classify import MIN_BAND_FOR_CONFIDENCE, calibration_table

    bands = calibration_table([p for p, _ in pairs], [c for _, c in pairs])
    # Only bands with enough predictions to support the claim, which is how
    # `classify.TrainingReport.worst_calibration_gap_points` is computed. Taking the max over
    # every band let one stray prediction in an otherwise empty band decide an NFR-03 verdict
    # that is then appended to the precision cell — and gave this repo two artefacts reporting
    # "worst calibration gap" against the same 5 points by different rules.
    usable = [b.gap_points for b in bands
              if b.gap_points is not None and not b.low_confidence]
    worst = max(usable) if usable else None
    return {
        "bands": [{"lower": b.lower, "upper": b.upper, "count": b.count,
                   "stated_pct": b.mean_confidence, "observed_pct": b.observed_accuracy,
                   "gap_points": b.gap_points, "low_confidence": b.low_confidence}
                  for b in bands],
        "pairs": len(pairs),
        "worst_gap_points": worst,
        "within_5_points": None if worst is None else worst <= 5.0,
        "note": (f"{len(pairs)} of {len(scored)} labelled tickets carried both a stated "
                 f"confidence and a labelled intent. NFR-03 asks for stated confidence within "
                 f"5 points of observed accuracy. A band with fewer than "
                 f"{MIN_BAND_FOR_CONFIDENCE} predictions cannot support that claim and is "
                 f"excluded from the verdict, as in the classifier's own report."),
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
        # The ticket's own total, not the sum of every row: an intermediate `generation` row and
        # the terminal row both carry the calls made so far, so summing all rows counted the same
        # request up to three times. NFR-07's spend figure has to be the real one.
        "model_calls": sum(r.outcome.model_calls for r in results),
        "cache_hits": sum(r.outcome.cache_hits for r in results),
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
                   scored: list[TicketResult], technical: dict[str, Any],
                   governance: dict[str, Any], segments: dict[str, Any],
                   stub: bool, kill_switch: bool,
                   reasons: dict[str, int]) -> list[dict[str, Any]]:
    """The Evaluation Framework's own table: measure, baseline, target, achieved, confidence.

    R4: every cell that can be computed from the run now is. This table printed
    "not computable yet (row 8)" for intent precision while `technical.classification` held a
    full per-class table, and "no reply is sent yet (row 11)" for private data while
    `governance.private_data_detections` held the real count. A table that contradicts the
    section above it is not a summary of the run; it is a summary of an older one.

    Every "confidence in the figure" cell has to be true of *this* run, which is harder than it
    sounds: the R4 review found three cells asserting things the run had no way of knowing.
    """
    precision = _per_class_floor(technical)
    predicted = sum(1 for r in scored if r.outcome.prediction_value and r.labels.get("intent"))
    calibration = technical.get("calibration") or {}
    variation = _worst_variation(segments)
    # FR-12 blocks a reply that fails the `private_data` check, so the number that matters for
    # a measure called "in outbound text" is how many **sent** replies carried a detection —
    # zero while the block holds. `governance.private_data_detections` counts the blocks, which
    # is a guardrail doing its job, and reading it here made that look like a target miss.
    leaked = sum(1 for r in answered
                 if any(name == "private_data" and not passed
                        for name, passed in r.outcome.guardrail_results))
    detections = governance.get("private_data_detections", 0)

    achieved = {
        "First contact resolution (proxy)": _fmt(_pct(len(answered), len(results))),
        "Escalation rate": _fmt(_pct(len(escalated), len(results))),
        "Processing time p95": (f"{_round(_percentile(latencies, 0.95))} ms" if latencies else "—"),
        # NFR-03 asks for ≥85% **per class**, so the cell is the worst class, not the average.
        # 100% over twenty classes and 40% over one meets no requirement, and averages to 97%.
        "Intent precision (per class)": (f"{precision}%" if precision is not None
                                         else "not computable"),
        "Retrieval hit rate": (_fmt(technical.get("retrieval_hit_rate_pct"))
                               if technical.get("retrieval_hit_rate_pct") is not None
                               else "not computable"),
        "Hallucination rate": "needs human review of ≥50 responses (two assessors)",
        "Citation accuracy": "needs human review; the harness counts unresolvable citations only",
        "Private data in outbound text": str(leaked),
        "Cross-segment variation": ("not measurable" if variation is None
                                    else f"{variation[1]} points ({variation[0]})"),
    }
    confidence = {
        "First contact resolution (proxy)": (
            f"proxy: automated handling, not confirmed resolution; n={len(results)}"),
        "Escalation rate": _escalation_confidence(results, answered, stub, kill_switch, reasons),
        "Processing time p95": _latency_confidence(results, governance, stub),
        "Intent precision (per class)": _precision_confidence(precision, predicted, scored,
                                                              results, calibration),
        "Retrieval hit rate": (
            f"n={technical.get('retrieval_scored_tickets', 0)} tickets with an expected "
            f"article; no false-positive counterpart"),
        "Private data in outbound text": _private_data_confidence(answered, detections),
        "Cross-segment variation": (
            f"the worst of the {len(segments)} dimensions; per-dimension figures with sample "
            f"sizes are in the segments section, and small segments are flagged there"
            if variation is not None else "fewer than two segments on every dimension"),
    }
    return [
        {"measure": measure, "baseline": baseline, "target": target,
         "achieved": achieved.get(measure, "—"),
         "confidence": confidence.get(measure, "—")}
        for measure, baseline, target in TARGETS
    ]


def _private_data_confidence(answered: list[TicketResult], detections: int) -> str:
    """What kind of zero this is, which is the whole value of the cell.

    Three different zeros reach this row and a reader cannot tell them apart from the number:
    nothing was generated, nothing was detected, or something was detected and blocked.
    """
    if not answered:
        return ("no reply was generated in this run, so this zero means 'nothing was checked', "
                "not 'nothing leaked'")
    base = "replies that were **sent** carrying a `private_data` detection"
    if detections:
        return (f"{base}. {detections} draft(s) failed that check and were blocked, which is "
                f"FR-12 working; the blocks are in the Governance table")
    return f"{base}; no draft failed that check in this run either"


def _worst_variation(segments: dict[str, Any]) -> tuple[str, float] | None:
    """The dimension with the largest spread, for NFR-06's one row in the results table.

    R4 review: this cell said "see segments.*.variation_points" while every dimension of the
    real run was above the 5-point limit. The summary table an assessor reads gave the fairness
    requirement no achieved value and no verdict; the worst dimension is a one-line max.
    """
    measured = [(name, block["variation_points"]) for name, block in segments.items()
                if block.get("variation_points") is not None]
    return max(measured, key=lambda pair: pair[1]) if measured else None


def _per_class_floor(technical: dict[str, Any]) -> float | None:
    """The lowest per-class precision, or None when there is no per-class table.

    The floor, because NFR-03's target is worded "≥85% per class": it is the only aggregate
    that can fail when one class fails. A class that was never predicted has undefined
    precision and is skipped here; its 0.0 recall is where that failure shows, in the per-class
    table.
    """
    per_class = (technical.get("classification") or {}).get("per_class") or {}
    values = [v["precision_pct"] for v in per_class.values()
              if v.get("precision_pct") is not None]
    return min(values) if values else None


def _precision_confidence(precision: float | None, predicted: int,
                          scored: list[TicketResult], results: list[TicketResult],
                          calibration: dict[str, Any]) -> str:
    """What the precision figure rests on — including the reason not to trust this one.

    R4 review: the cell read "the lowest per-class precision over 80 labelled tickets" when the
    figure is computed only over tickets that produced a prediction, and asserted a clean
    NFR-03 pass with no caveat at all. The project's own evidence says that 100% is leakage:
    review row R8 records that 62 of the 80 validation bodies duplicate a training body, and
    `evaluation/reports/classifier_calibration.md` has three intents below 85% out of fold.
    """
    if precision is None:
        return _why_no_classification(results, scored)
    parts = [
        (f"the lowest per-class precision over the {predicted} of {len(scored)} labelled "
         f"tickets that produced a prediction; the macro figure is in "
         f"technical.classification"),
    ]
    if calibration.get("worst_gap_points") is not None:
        parts.append(f"worst calibration gap {calibration['worst_gap_points']} points "
                     f"(NFR-03 asks for ≤5)")
    parts.append("in-sample caution: much of the supplied validation wording also appears in "
                 "the training set, so a high figure here is not evidence of generalisation — "
                 "the out-of-fold figures in evaluation/reports/classifier_calibration.md are")
    return "; ".join(parts)


def _escalation_confidence(results: list[TicketResult], answered: list[TicketResult],
                           stub: bool, kill_switch: bool, reasons: dict[str, int]) -> str:
    """Why this escalation rate is, or is not, a tuning result (FR-14 §7).

    R4 replaced a constant that blamed unbuilt rows. The R4 review then found the replacement
    asserting the opposite error: "a result rather than a construction" was printed with no
    knowledge of the kill switch (FR-16), a provider outage (FR-15) or a file of nothing but
    must-escalate tickets, in each of which 100% escalation *is* by construction.
    """
    base = f"n={len(results)}"
    if stub:
        return (f"{base}; this was a **stub** run — ingest and retrieval only, no classifier, "
                f"no drafting and no guardrails — so 100% escalation is the stub's own "
                f"contract and not a tuning result")
    if kill_switch:
        return (f"{base}; the kill switch is on (FR-16), so every ticket escalates by "
                f"construction and this is not a tuning result")
    if not answered and results:
        forced = sum(count for reason, count in reasons.items()
                     if reason in _NOT_A_TUNING_RESULT)
        if forced == len(results):
            dominant = max(((r, c) for r, c in reasons.items() if r in _NOT_A_TUNING_RESULT),
                           key=lambda pair: pair[1])[0]
            return (f"{base}; every ticket escalated on a rule or a failure rather than on the "
                    f"threshold ({dominant} dominates), so this is not a tuning result")
        return (f"{base}; every ticket escalated in this run. {forced} of {len(results)} did so "
                f"on a rule or a failure rather than on the threshold — the reasons table "
                f"splits them")
    return base


#: Reasons that make an escalation a construction rather than a tuning outcome: a rule fired,
#: a component failed, or the provider was unreachable. None of them is a threshold decision.
_NOT_A_TUNING_RESULT = frozenset({
    "kill_switch", "must_escalate_intent", "money_commitment_requested",
    "date_commitment_requested", "private_data_in_ticket", "instruction_injection_detected",
    "malformed_ticket", "unknown_intent", "text_truncated", "provider_unavailable",
    "pipeline_error", "retrieval_unavailable", "pipeline_incomplete",
})


def _latency_confidence(results: list[TicketResult], governance: dict[str, Any],
                        stub: bool) -> str:
    """NFR-01 is about the automated path, and a run with no model call is not that path."""
    calls = governance.get("model_calls", 0)
    hits = governance.get("cache_hits", 0)
    if stub:
        return "a stub run makes no model call, so this is the machinery's time, not the system's"
    if not calls and hits:
        return (f"no model call was made in this run — all {hits} provider responses came from "
                f"the cache — so this is a replay time, not a measured one")
    if not calls and results:
        # R4 review: the cache sentence was printed here too, where it is flatly false.
        return ("the provider was never reached in this run: no model call and no cache hit, so "
                "this is the time taken by ingest, retrieval and the rules alone")
    return f"{calls} model call(s) over {len(results)} tickets"


def _gaps(results: list[TicketResult], answered: list[TicketResult],
          scored: list[TicketResult], technical: dict[str, Any], stub: bool) -> list[str]:
    """FR-14 §4: a figure this run could not produce says so, **and says why**.

    R4: every entry used to be a constant naming a build row — "no classifier yet (row 8)",
    "nothing is sent yet (row 11)" — and they were still printed after rows 8 to 14 built
    exactly those things. An entry now appears only when the figure really is absent from this
    run, and names the missing input rather than a date in the project's past.

    R4 review: the classification entry was conditioned on *labels* being absent, so a run with
    labels and a dead classifier printed no gap at all while `technical.classification` read
    "not computable". It is conditioned on the figure now, which is what FR-14 §4 asks.
    """
    classification = technical.get("classification") or {}
    calibration = technical.get("calibration") or {}
    gaps: list[str] = []
    if stub:
        gaps.append(
            "This was a **stub** run: ingest and retrieval only, with no classifier, no "
            "drafting and no guardrails, so no reply was generated. Every ticket escalates by "
            "the stub's own contract, which means the volume, business and classification "
            "figures describe the run machinery and not the system.")
    if classification.get("not_computable"):
        gaps.append(f"Intent precision and recall: {classification['not_computable']}. The "
                    f"decisions are real; only the marking is absent.")
    if calibration.get("not_computable"):
        gaps.append(f"Confidence calibration (NFR-03): {calibration['not_computable']}.")
    elif calibration.get("worst_gap_points") is None:
        gaps.append(
            "Confidence calibration (NFR-03): no band held enough predictions to support a "
            "claim about calibration, so the table is printed without a verdict.")
    if not answered and results and not stub:
        gaps.append(
            "Every ticket escalated in this run, so first contact resolution, the private-data "
            "count and the citation figures have no sent replies behind them. The reasons "
            "table says what stopped each one.")
    gaps.extend([
        ("Hallucination rate and citation accuracy: need human review of at least 50 responses "
         "by two assessors (Evaluation Framework tier two); the harness reports unresolvable "
         "citations as a floor only. `scripts/review_sample.py` writes the sheet from this "
         "run's `outcomes.jsonl`."),
        ("Customer satisfaction: no live customers; the Evaluation Framework's rubric proxy "
         "needs a human sample."),
        "Response time: the data has no first-reply timestamp, so only processing time is real.",
    ])
    if scored:
        gaps.append(
            "Generalisation of the classification figures: much of the supplied validation "
            "wording also appears in the training set, so precision and recall measured here "
            "are partly in-sample. The out-of-fold figures in "
            "`evaluation/reports/classifier_calibration.md` are the ones that bear on NFR-03.")
    if answered:
        gaps.append(
            "Whether an automated answer was *correct*: the harness checks that citations "
            "resolve and that the guardrails passed, which is not the same as the answer being "
            "right. That is what the human review above is for.")
    return gaps


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
            handle.write(json.dumps(_outcome_line(result), sort_keys=True) + "\n")


def _outcome_line(result: TicketResult) -> dict[str, Any]:
    """One line of `outcomes.jsonl` (FR-14, NFR-03).

    R3: this used to carry the decision, the citations and the segments but **not one word of
    what was sent**. The Evaluation Framework measures hallucination rate and citation accuracy
    by human review of the text — two assessors over at least 50 responses — so without the
    reply and the passages it cited, the tier-two review the PRD asks for could not be done at
    all, the demo could not show what went out, and a complaint could not be reconstructed.
    """
    outcome = result.outcome
    cited = set(outcome.citations)
    return {
        "ticket_id": result.ticket.ticket_id,
        "source_index": result.ticket.source_index,
        "decision": outcome.decision,
        "reason": outcome.reason,
        "all_reasons": list(outcome.all_reasons),
        "doc_ids": [p.doc_id for p in outcome.passages],
        "chunk_ids": [p.chunk_id for p in outcome.passages],
        "citations": list(outcome.citations),
        # The exact outbound text, or null. Never a withheld draft: `Outcome.draft` is None on
        # every escalation, and a reviewer must not be shown text no customer received.
        "reply": outcome.draft if outcome.answered else None,
        # The passages the reply cited, with their text — an assessor cannot judge "supported"
        # against a list of chunk ids.
        "cited_passages": [
            {"chunk_id": p.chunk_id, "doc_id": p.doc_id, "title": p.title,
             "heading": p.heading, "text": p.text, "score": round(p.score, 4)}
            for p in outcome.passages if p.chunk_id in cited],
        # FR-01's package, for the escalations: what a tier-two engineer is handed.
        **_handover_fields(outcome),
        "intent": outcome.intent,
        "prediction_value": outcome.prediction_value,
        "intent_confidence": outcome.intent_confidence,
        "urgency": outcome.urgency,
        "latency_ms": round(result.latency_ms, 1),
        "segments": result.segments(),
    }


#: The handover fields that are derived from the customer's own words, and therefore go through
#: the decision log's redaction before they are written to a file (NFR-04). `customer_goal` is
#: literally the first sentence of the body on the template path, and `already_tried` is
#: extracted from it by PR-02.
_CUSTOMER_DERIVED = ("summary", "system_uncertainty", "customer_goal", "suggested_first_check")


def _handover_fields(outcome: Outcome) -> dict[str, Any]:
    """FR-01's package for an escalated ticket, redacted the way the decision log redacts.

    R3 review (high): written raw, this put an email address and a phone number from a ticket
    body straight into a file on disk, and `T-FR14-18` — the test that forbids exactly that —
    could not see it, because it runs a `FakePipeline` that produces no handover at all.

    Whether the *log's* `summary` column may carry customer text is an open question for the
    author (FR-13 §7, FR-01 §7, review row R14). Until it is answered, a new artefact does not
    get a looser policy than the log's scrubbed columns: one policy, applied here, and every
    redaction is named on the line rather than silently applied.
    """
    if outcome.answered:
        return {"handover": None, "handover_redactions": []}
    note: dict[str, Any] = {
        "summary": outcome.summary,
        "system_uncertainty": outcome.uncertainty,
        "customer_goal": outcome.customer_goal,
        "suggested_first_check": outcome.suggested_first_check,
        "already_tried": list(outcome.already_tried),
    }
    redactions: list[str] = []
    for name in _CUSTOMER_DERIVED:
        if note[name]:
            note[name], found = redact(str(note[name]))
            redactions.extend(f"{name}:{pattern}" for pattern in found)
    cleaned = []
    for step in note["already_tried"]:
        text, found = redact(str(step))
        cleaned.append(text)
        redactions.extend(f"already_tried:{pattern}" for pattern in found)
    note["already_tried"] = cleaned
    return {"handover": note, "handover_redactions": sorted(set(redactions))}


def _markdown(metrics: dict[str, Any]) -> str:
    run, volume = metrics["run"], metrics["volume"]
    business, technical, governance = metrics["business"], metrics["technical"], metrics["governance"]
    lines = [
        "# Evaluation run (FR-14)",
        "",
        f"- Generated: {run['generated_at']}  ",
        f"- Input: `{run['input']}` — {run['tickets_in_file']} tickets in the file  ",
        f"- Scored against labels: **{run['scored_against_labels']}**  ",
        # R4 review: a stub run announced itself only in two table cells and a gaps bullet a
        # hundred lines down, and a full run made no positive statement at all. A10's reader
        # looks at the header, so the header says which pipeline produced these figures.
        (f"- Pipeline: **{run['pipeline']}** (`{run['pipeline_class']}`)"
         + ("  \n  *A stub run is ingest and retrieval only: no classifier, no drafting and no "
            "guardrails, so every ticket escalates by the stub's own contract.*"
            if run["pipeline"] == "stub" else
            "  \n  *Not the real graph: a pipeline was injected, so these figures describe "
            "whatever was injected.*" if run["pipeline"] == "injected" else "  ")),
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

    # R4: the calibration table is NFR-03's own measurement and the report used to say it
    # "needs the classifier's confidences", which stopped being true at row 8.
    calibration = technical.get("calibration") or {}
    lines += ["", "### Confidence calibration (NFR-03, Evaluation Framework §3)", ""]
    if calibration.get("bands"):
        lines += ["| band | n | stated | observed | gap |", "|---|---|---|---|---|"]
        for band in calibration["bands"]:
            note = " (low confidence)" if band.get("low_confidence") else ""
            gap = band["gap_points"]
            lines.append(
                f"| {band['lower']:.1f}–{band['upper']:.1f} | {band['count']} | "
                f"{_fmt(band['stated_pct'])} | {_fmt(band['observed_pct'])} | "
                f"{'—' if gap is None else str(gap) + ' pts'}{note} |")
        verdict = calibration.get("within_5_points")
        lines += [
            "",
            (f"Worst gap: **{calibration['worst_gap_points']} points**, "
             f"{'within' if verdict else '**above**'} NFR-03's 5-point limit."
             if calibration.get("worst_gap_points") is not None else
             "No band held enough predictions to measure a gap."),
            "",
            f"*{calibration['note']}*",
        ]
    else:
        lines.append(f"*Not computable: {calibration.get('not_computable', 'no data')}*")

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
