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
    # R6: the Dataset Guide says `answerable_from_docs` exists to measure "whether your system
    # correctly recognises questions it cannot ground". There is no pack target for it, so the
    # column reads "—" and the figure is reported rather than graded.
    ("Answered against the label", "—", "—"),
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
    parser.add_argument("--no-cache", action="store_true",
                        help="do not replay recorded provider responses. For a timing run: "
                             "NFR-01 is about the automated path, and a run served from the "
                             "cache measures the cache. Responses are still written.")
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
            read_cache=not args.no_cache,
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
        use_stub: bool = False, read_cache: bool = True) -> RunReport:
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
                    else _build_pipeline(settings, docs_path, allow_index_build,
                                         read_cache=read_cache))

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
                           pipeline_name=type(pipeline).__name__,
                           read_cache=read_cache)
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
                    allow_index_build: bool, read_cache: bool = True) -> Pipeline:
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
        settings.require_api_key()
    except ConfigError as exc:
        raise HarnessError(
            f"{exc} Run with --stub-pipeline to exercise the machinery without a model.") from None
    try:
        classifier = IntentClassifier(TrainedClassifier.load(settings.classifier_path))
    except (ClassifierError, OSError, ValueError) as exc:
        raise HarnessError(
            f"the classifier at {settings.classifier_path} could not be loaded: {exc}. Train it "
            "with scripts/train_classifier.py, or run with --stub-pipeline.") from None

    client = ProviderClient(settings, read_cache=read_cache)
    return SupportPipeline(
        retriever=retriever, classifier=classifier, router=Router(settings),
        drafter=Drafter(client),
        # R9 review / D-74: `JUDGE_MODEL_NAME` existed, was documented as making FR-12's
        # grounding check independent, and reached nothing. PR-03 ran on the drafting model.
        guardrails=Guardrails(judge=GroundingJudge(
            client, model=settings.judge_model_name)),
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
             pipeline_name: str = "unknown", read_cache: bool = True) -> dict[str, Any]:
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

    governance = _governance(results, reconciliation)
    latency = _latency(latencies, [r.latency_ms for r in answered], governance, stub)
    technical = _technical(results, scored, latencies, answered, settings)
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
            # R5 review: a `--no-cache` report was indistinguishable from an ordinary run
            # against a cold cache, so the one configuration that cannot reproduce itself left
            # no trace in its own artefact. A5 and NFR-08 are judged on a run that can.
            "read_cache": read_cache,
            # R9: NFR-07 said zero spend and D-55 amended it to a paid provider within a stated
            # budget. An amendment nobody can check from the report is not an amendment, so the
            # provider and the models are on every run beside the model-call count.
            "provider": _provider(settings),
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
        "latency": latency,
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
                                         settings.kill_switch_on, reasons, latency),
        "gaps": _gaps(results, answered, scored, technical, latency, stub),
    }


def _provider(settings: Settings) -> dict[str, Any]:
    """Which provider and models this run was configured with (R9, NFR-07).

    **The host, never the configured URL.** A base URL is operator-supplied and can carry a
    token in a query string, and CLAUDE.md's rule is that no key reaches code, tests, fixtures
    or history — a report is none of those and all of them. `urlsplit().hostname` drops the
    scheme, any credentials, the path and the query, so only the host can survive into the
    artefact.
    """
    from urllib.parse import urlsplit

    raw = (getattr(settings, "llm_base_url", "") or "").strip()
    try:
        host = urlsplit(raw).hostname
    except ValueError:
        host = None
    return {
        "host": host,
        "model": (getattr(settings, "model_name", "") or None),
        "judge_model": (getattr(settings, "judge_model_name", "") or None),
        # Stated, not implied. Before D-74 the report named a judge model that nothing used,
        # which published a claim of independence the system did not have.
        "grounding_judge_is_independent": bool(
            (getattr(settings, "judge_model_name", "") or "").strip()
            and (getattr(settings, "judge_model_name", "") or "").strip()
            != (getattr(settings, "model_name", "") or "").strip()),
    }


def _latency(latencies: list[float], answered_latencies: list[float],
             governance: dict[str, Any], stub: bool) -> dict[str, Any]:
    """NFR-01, R5: the measured figures, and whether they measured anything.

    `gate-openai-2` reported p95 **95.7 ms** against NFR-01's 3-second target, off **0 model
    calls and 162 cache hits**. The live run of the same 80 tickets measured median 4.4 s and
    p95 **6.5 s**, which misses it. A number that fast beside that target does not read as a
    replay unless the report says so, so a comfortable pass was being printed over a real miss.

    **Three states, not a boolean** (R5 review). `representative` is strict — any replayed
    response makes the figure smaller than the system's real latency — but reusing it to print
    the word *replay* mislabelled two different runs: a 1%-replayed run became a "replay" in
    the headline while its own note said "partly replayed: 1 of 101", and a run that never
    reached the provider at all (every ticket escalating by rule, or the kill switch on) was
    marked a cache replay that never happened, contradicting FR-14 §6 item 44.

    **`answered` as well as `all`** (R5 review). NFR-01 is "p95 end-to-end time per ticket **on
    the automated path**", and a figure over every ticket is not that figure: on the real run
    the two differ (p95 6,532 ms over all, 5,754 ms over the answered), and a run with cheap
    rule escalations could hide a miss behind them. Both are reported, and the one NFR-01 asks
    about is named.
    """
    calls = governance.get("model_calls", 0)
    hits = governance.get("cache_hits", 0)
    responses = calls + hits
    return {
        "median_ms": _round(statistics.median(latencies)) if latencies else None,
        "p95_ms": _round(_percentile(latencies, 0.95)) if latencies else None,
        # The automated path only: what NFR-01 is actually about.
        "automated_path_median_ms": (_round(statistics.median(answered_latencies))
                                     if answered_latencies else None),
        "automated_path_p95_ms": (_round(_percentile(answered_latencies, 0.95))
                                  if answered_latencies else None),
        "automated_path_tickets": len(answered_latencies),
        "nfr01_figure": "automated_path_p95_ms",
        "model_calls": calls,
        "cache_hits": hits,
        "provider_responses_from_cache_pct": _pct(hits, responses) if responses else None,
        "state": _latency_state(calls, hits, stub),
        "cache_replay": bool(hits) and not calls,
        # `and not stub` would be dead here: a stub run makes no model call, so `bool(calls)`
        # already decides it. The state carries the stub case.
        "representative": bool(calls) and not hits,
        "note": _latency_note(calls, hits, stub, len(answered_latencies)),
    }


#: What the latency figures of a run are. `measured` is the only one NFR-01 can be judged on.
_LATENCY_STATES = ("measured", "partly_replayed", "replay", "provider_not_reached", "stub")


def _latency_state(calls: int, hits: int, stub: bool) -> str:
    """One of `_LATENCY_STATES`. Three of the five used to collapse into "replay"."""
    if stub:
        return "stub"
    if hits and not calls:
        return "replay"
    if hits:
        return "partly_replayed"
    if calls:
        return "measured"
    return "provider_not_reached"


#: The suffix the results table puts on the p95 cell, by state. `measured` gets none: a bare
#: number is a claim, and only a measured run is entitled to make it.
_LATENCY_SUFFIX = {
    "replay": " (replay)",
    "partly_replayed": " (partly replayed)",
    "provider_not_reached": " (provider not reached)",
    "stub": " (stub run)",
}


def _latency_note(calls: int, hits: int, stub: bool, answered: int) -> str:
    """One sentence saying what the latency figures are, in this run's own terms."""
    state = _latency_state(calls, hits, stub)
    sample = ("" if answered >= 20 else
              f" The automated path figure rests on {answered} ticket(s), too few to read as a "
              f"percentile." if answered else
              " No ticket took the automated path, so NFR-01's own figure is absent.")
    if state == "stub":
        return ("a stub run makes no model call, so this is the machinery's time and not the "
                "system's")
    if state == "replay":
        return (f"**cache replay**: all {hits} provider responses came from the cache, so these "
                f"figures are replay times and not measurements of the automated path "
                f"(NFR-01).{sample}")
    if state == "partly_replayed":
        return (f"**partly replayed**: {hits} of {calls + hits} provider responses came from "
                f"the cache, so these figures are faster than the automated path really is "
                f"(NFR-01).{sample}")
    if state == "measured":
        return (f"measured over {calls} provider request(s), with no replayed response.{sample}")
    return ("the provider was never reached in this run: no model call and no cache hit, so "
            "this is the time taken by ingest, retrieval and the rules alone")


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
               latencies: list[float], answered: list[TicketResult],
               settings: Settings | None = None) -> dict[str, Any]:
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
        "answered_against_the_labels": _against_the_labels(scored, answered),
        "classification_by_wording": _by_wording(results, scored, settings),
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


def _against_the_labels(scored: list[TicketResult],
                        answered: list[TicketResult]) -> dict[str, Any]:
    """R6: the answers the supplied labels disagree with, named.

    In `gate-openai-2` (the 2026-09-28 replay), **14 of 42** auto-answers went to tickets
    labelled `expected_route: escalate`, and 13 of those were labelled
    `answerable_from_docs: false` -- the field the Dataset Guide says exists to measure
    "whether your system correctly recognises questions it cannot ground". The harness reported
    nothing on any of it, so the most interesting disagreement between the system and the
    labels was invisible in the one artefact A10 is judged on. (The counts move with the
    provider's text; see D-68.)

    Every figure needs labels, so each row carries **its own** denominator and says how many
    tickets could be scored for it. An unlabelled file gets `not_computable` and no counts: a
    zero meaning "nothing was checked" is worse than an absent figure (D-15, FR-14 section 3.7).
    """
    if not scored:
        return {"not_computable": ("no labels in the input file, so no answer can be compared "
                                   "with what the labels expected")}

    # By outcome, not by ticket id: ids are not unique (`ingest` keeps a duplicate and flags it
    # with `duplicate_ticket_id`, D-12), so an id-based match would name an escalated ticket as
    # an answer that was never sent, and inflate every denominator with it.
    answered_scored = [r for r in scored if r.outcome.decision == "auto_respond"]

    route_known = [r for r in answered_scored if r.labels.get("expected_route")]
    groundable_known = [r for r in answered_scored
                        if isinstance(r.labels.get("answerable_from_docs"), bool)]
    article_known = [r for r in answered_scored if r.labels.get("expected_doc_ids")]
    forbidden_known = [r for r in answered_scored
                       if isinstance(r.labels.get("must_not_auto_respond"), bool)]

    wrong_route = [r for r in route_known if r.labels["expected_route"] == "escalate"]
    # `is False`, not falsy: a missing label is not a claim that the ticket is ungroundable,
    # which is why the denominator above counts only the tickets that carry a boolean.
    ungroundable = [r for r in groundable_known if r.labels["answerable_from_docs"] is False]
    missed_article = [
        r for r in article_known
        if not (set(r.labels["expected_doc_ids"]) & {_doc_of(c) for c in r.outcome.citations})
    ]
    forbidden = [r for r in forbidden_known if r.labels["must_not_auto_respond"] is True]

    routes = ("auto_respond", "escalate")
    matrix: dict[str, dict[str, int]] = {
        expected: dict.fromkeys(routes, 0) for expected in routes}
    other = 0
    for result in scored:
        expected = result.labels.get("expected_route")
        actual = result.outcome.decision
        if expected in matrix and actual in routes:
            matrix[expected][actual] += 1
        else:
            other += 1

    return {
        "labelled_tickets": len(scored),
        "answered_and_labelled": len(answered_scored),
        "answered_but_labelled_escalate": _disagreement(wrong_route, route_known),
        "answered_but_not_answerable_from_docs": _disagreement(ungroundable, groundable_known),
        "answered_citing_no_expected_article": _disagreement(missed_article, article_known),
        # The one label that is a requirement breach rather than a question for a human.
        "answered_but_must_not_auto_respond": _disagreement(forbidden, forbidden_known),
        "confusion_against_expected_route": matrix,
        "outside_the_matrix": other,
        "note": _against_note(wrong_route, ungroundable, matrix, forbidden),
    }


def _against_note(wrong_route: list[TicketResult], ungroundable: list[TicketResult],
                  matrix: dict[str, dict[str, int]],
                  forbidden: list[TicketResult]) -> str:
    """The sentences a reader needs to interpret the table without guessing.

    Every claim here is **computed**, not asserted. An earlier version stated that the first two
    rows naming the same tickets was "a fact about the labels", on a branch that fired whenever
    the two answered subsets happened to coincide -- including by accident on an unseen file --
    while never examining the labels at all. The same went for "it is the larger number here".
    """
    parts = [
        ("The labels are the pack's, not this system's. A disagreement is a question for a "
         "human -- the label may be wrong, or the answer may be -- and the ids are listed so "
         "it can be answered rather than argued about. `must_not_auto_respond` means something "
         "narrower in the pack data than in this corpus (D-20)."),
    ]
    if forbidden:
        parts.append(
            f"**{len(forbidden)} answered ticket(s) are labelled `must_not_auto_respond`.** "
            f"That is not a difference of opinion about a label: it is the one row here that "
            f"would be an FR-02 breach, and it needs reading before anything else.")
    route_ids = {r.ticket.ticket_id for r in wrong_route}
    ground_ids = {r.ticket.ticket_id for r in ungroundable}
    if route_ids and route_ids == ground_ids:
        parts.append(
            "The first two rows name the same tickets. That is a fact about the labels in this "
            "file rather than a duplicated row: every ticket answered against its route label "
            "is also one the labels call ungroundable.")
    elif route_ids and ground_ids and ground_ids < route_ids:
        parts.append(
            f"Every ticket in the second row is also in the first: {len(ground_ids)} of "
            f"{len(route_ids)}.")
    other_way = matrix["auto_respond"]["escalate"]
    if other_way:
        comparison = ("and it is the larger number here" if other_way > len(wrong_route)
                      else "which the row did not ask about")
        parts.append(
            f"The matrix also shows **{other_way}** ticket(s) the labels expected to be "
            f"answered and this run escalated, {comparison}. The same question applies to them.")
    return " ".join(parts)


def _disagreement(matching: list[TicketResult],
                  population: list[TicketResult]) -> dict[str, Any]:
    """One disagreement: how many, out of how many **could carry this label**, and which.

    The population is per-row, not the whole answered set. `answered_citing_no_expected_article`
    can only be judged on tickets that have an `expected_doc_ids` -- 28 of the 43 answered in
    the validation run -- and dividing by 43 printed 2.3% where the figure is 3.6%. That cell
    sits beside NFR-03's citation-accuracy row and will be read as a citation figure. The
    harness already takes retrieval hit rate's denominator this way; this now matches.
    """
    return {
        "count": len(matching),
        "scored_for_this": len(population),
        "pct_of_scored": _pct(len(matching), len(population)),
        "ticket_ids": sorted(r.ticket.ticket_id for r in matching),
    }


def _doc_of(citation: str) -> str:
    """`DOC-BILL-002#2` -> `DOC-BILL-002`. `expected_doc_ids` are articles, not chunks.

    Stripped, as `retrieve.resolve` strips its input: stray whitespace on a citation would
    otherwise produce a disagreement that is a formatting artefact and not a disagreement.
    """
    return citation.strip().split("#", 1)[0]


#: R8: the honest figure is the one measured on wording the classifier has not seen.
UNSEEN = "unseen_wording"
SEEN = "seen_in_training"
#: Where the cross-validated figures live. Referenced in the report because they are the ones
#: that bear on NFR-03, and because 3 of 22 intents are below 85% in them.
CALIBRATION_REPORT = "evaluation/reports/classifier_calibration.md"


#: D-39's clustering threshold, read from the function that applies it rather than restated.
#: The report prints this number, and a literal `0.85` beside a call that used the function's
#: own default would have gone on printing 0.85 after the default changed.
WORDING_THRESHOLD = 0.85


def _normalised_body(text: str) -> str:
    """The comparison key for "is this wording in the training set?".

    Case-folded with runs of whitespace collapsed, because case and spacing are not wording.

    **On the real corpus this changes nothing**: a raw-string comparison gives the same 62/18
    split. An earlier version of this docstring justified the normalisation with DEV-0106 and
    VAL-0037, claiming they differ only in case and spacing — they do not (VAL-0037 inserts two
    articles and drops a sentence), and normalising does not merge them. The normalisation is
    kept because it is the right key for a file nobody has seen, not because it earns its place
    on this data; `T-R8-3` is the only thing that exercises it.
    """
    return " ".join((text or "").split()).casefold()


def _by_wording(results: list[TicketResult], scored: list[TicketResult],
                settings: Settings | None) -> dict[str, Any]:
    """FR-08, NFR-03, R8: classification split by whether the classifier has seen the wording.

    The gate reported **100% per-class precision and recall** while 62 of the 80 validation
    bodies were identical to a development ticket the classifier was trained on. The
    cross-validated development figures in `evaluation/reports/classifier_calibration.md` put
    **3 of 22 intents below NFR-03's 85%**. A headline measured on near-duplicate lookup is not
    evidence of generalisation, and the report said nothing about which it was.

    The training file comes from configuration (`TRAINING_TICKETS_PATH`), never a hardcoded
    name (CLAUDE.md). Absent or unreadable, the split says so and the run completes: this is a
    reporting nicety and losing a run to it would be absurd.
    """
    path = getattr(settings, "training_tickets_path", None) if settings else None
    if path is None:
        return {"not_computable": (
            "TRAINING_TICKETS_PATH is not set, so the harness cannot tell which wording the "
            "classifier was trained on. The cross-validated figures in "
            f"`{CALIBRATION_REPORT}` are the ones that bear on NFR-03.")}
    try:
        trained = {_normalised_body(t.body) for t in load_tickets(Path(path))}
    except (TicketFileError, OSError) as exc:
        return {"not_computable": (
            f"the training file at {path} could not be read ({type(exc).__name__}), so the "
            f"split could not be made. TRAINING_TICKETS_PATH points at it.")}

    buckets: dict[str, list[TicketResult]] = {SEEN: [], UNSEEN: []}
    for result in results:
        key = SEEN if _normalised_body(result.ticket.body) in trained else UNSEEN
        buckets[key].append(result)

    near, skipped = _near_duplicates_of_training(buckets[UNSEEN], trained)

    def block(group: list[TicketResult]) -> dict[str, Any]:
        in_group = {r.ticket.ticket_id for r in group}
        pairs = [(r.labels.get("intent"), r.outcome.prediction_value)
                 for r in scored
                 if r.ticket.ticket_id in in_group and r.outcome.prediction_value]
        return {
            "tickets": len(group),
            "scored": len(pairs),
            "ticket_ids": sorted(r.ticket.ticket_id for r in group),
            "classification": _per_class(pairs) if pairs else {
                "not_computable": "no labelled prediction in this group"},
        }

    novel = len(buckets[UNSEEN]) - len(near)
    return {
        "training_file": str(path),
        "training_bodies": len(trained),
        "headline": UNSEEN,
        "seen_pct": _pct(len(buckets[SEEN]), len(results)),
        SEEN: block(buckets[SEEN]),
        UNSEEN: block(buckets[UNSEEN]),
        # The number that actually matters, and it is much smaller than the one above.
        "unseen_but_near_duplicate": {
            "tickets": len(near),
            "ticket_ids": sorted(near),
            "threshold": WORDING_THRESHOLD,
            "skipped": skipped,
        },
        "genuinely_novel_wording": novel,
        "genuinely_novel_ticket_ids": sorted(
            r.ticket.ticket_id for r in buckets[UNSEEN] if r.ticket.ticket_id not in set(near)),
        "note": (
            f"The headline classification figure is the **{UNSEEN}** one: a score measured on "
            f"wording the classifier was trained on is near-duplicate lookup, not "
            f"generalisation. **But an exact-body comparison overstates it**: of the "
            f"{len(buckets[UNSEEN])} tickets whose body is not in the training file, "
            f"{len(near)} fall in a wording cluster that contains a training body under D-39's "
            f"{WORDING_THRESHOLD} clustering — the clusters are transitive, so this counts a "
            f"paraphrase of a paraphrase, which errs toward calling wording seen. That leaves "
            f"**{novel}** with genuinely novel wording, and a figure over {novel} ticket(s) "
            f"supports nothing either way. The cross-validated figures in "
            f"`{CALIBRATION_REPORT}` group their folds by wording cluster over the whole "
            f"development set and are the ones that bear on NFR-03."),
    }


#: Above this many distinct bodies the clustering is skipped. It is O(n²) difflib — measured
#: 0.08 s at 233 texts, 1.3 s at 1,000, 5 s at 2,000, 20 s at 4,000 — and it runs inside the
#: block whose failure costs a completed run its report (`run`'s "the report could not be
#: written"). A reporting nicety must not be able to do that, so past the cap the split says it
#: was skipped and the run finishes.
MAX_CLUSTERED_BODIES = 3000


def _near_duplicates_of_training(unseen: list[TicketResult],
                                 trained: set[str]) -> tuple[list[str], str | None]:
    """Which 'unseen' tickets are really paraphrases of a training body (R8).

    Uses `classify.wording_clusters` — D-39's own definition of "same wording", the one the
    classifier's cross-validation groups its folds by — rather than a second implementation.

    Returns the ids and, when the clustering was skipped, the reason. Note that D-39's clusters
    are **transitive**: an unseen ticket reachable from a training body through another unseen
    ticket is counted here too. That errs toward calling wording seen, which is the
    self-critical direction, and the report says so.
    """
    if not unseen or not trained:
        return [], None
    total = len(unseen) + len(trained)
    if total > MAX_CLUSTERED_BODIES:
        return [], (f"{total} distinct bodies is past the {MAX_CLUSTERED_BODIES} cap for "
                    f"near-duplicate clustering, so only the exact-body split is reported")
    from ticketing_agent.classify import wording_clusters

    bodies = [_normalised_body(r.ticket.body) for r in unseen]
    training = sorted(trained)
    # One cluster id per input, in input order — `T-R8-7` pins that contract, because a sorted
    # or reordered return would keep the counts plausible and name the wrong tickets.
    clusters = wording_clusters([*bodies, *training], threshold=WORDING_THRESHOLD)
    training_clusters = set(clusters[len(bodies):])
    return [r.ticket.ticket_id
            for r, cluster in zip(unseen, clusters[:len(bodies)], strict=True)
            if cluster in training_clusters], None


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
                   reasons: dict[str, int], latency: dict[str, Any]) -> list[dict[str, Any]]:
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
        # R5: marked, not just footnoted. 95.7 ms beside a "<3 s" target reads as a pass, and
        # the live run of the same tickets measured 6.7 s, which is a miss.
        # R5 review: state-driven. The boolean printed "(replay)" on a run that never
        # reached the provider and on a 1%-replayed one, which is the same false claim R4 was
        # commissioned to remove, in the one cell an assessor reads.
        "Processing time p95": (
            "—" if not latencies else
            f"{_round(_percentile(latencies, 0.95))} ms"
            + _LATENCY_SUFFIX.get(latency["state"], "")),
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
        "Answered against the label": _against_the_labels_cell(technical),
    }
    confidence = {
        "First contact resolution (proxy)": (
            f"proxy: automated handling, not confirmed resolution; n={len(results)}"),
        "Escalation rate": _escalation_confidence(results, answered, stub, kill_switch, reasons),
        "Processing time p95": latency["note"],
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
        "Answered against the label": (
            (technical.get("answered_against_the_labels") or {}).get("not_computable")
            or "the ids are in the Technical section; a disagreement is a question for a human, "
               "not a score"),
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


def _against_the_labels_cell(technical: dict[str, Any]) -> str:
    """One cell: how many answers the labels disagree with, and on what."""
    block = technical.get("answered_against_the_labels") or {}
    if block.get("not_computable"):
        return "not computable"
    route = block["answered_but_labelled_escalate"]
    docs = block["answered_but_not_answerable_from_docs"]
    article = block["answered_citing_no_expected_article"]
    forbidden = block["answered_but_must_not_auto_respond"]
    lead = (f"**{forbidden['count']} labelled must_not_auto_respond**, " if forbidden["count"]
            else "")
    return (f"{lead}{route['count']} labelled escalate, {docs['count']} labelled not answerable "
            f"from docs, {article['count']} citing no expected article, of "
            f"{block['answered_and_labelled']} answered and labelled")


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
        not_tuning = _not_a_tuning_result()
        forced = sum(count for reason, count in reasons.items()
                     if reason in not_tuning)
        if forced == len(results):
            dominant = max(((r, c) for r, c in reasons.items() if r in not_tuning),
                           key=lambda pair: pair[1])[0]
            return (f"{base}; every ticket escalated on a rule or a failure rather than on the "
                    f"threshold ({dominant} dominates), so this is not a tuning result")
        return (f"{base}; every ticket escalated in this run. {forced} of {len(results)} did so "
                f"on a rule or a failure rather than on the threshold — the reasons table "
                f"splits them")
    return base


#: Reasons that make an escalation a *threshold* decision rather than a construction. Everything
#: else in `PRECEDENCE` is a rule or a failure, so the set below is derived rather than listed:
#: R7 added two routing reasons and this frozenset was a second, hand-maintained copy that did
#: not get them — the D-18 shape, in the file whose own row (R4) was about figures contradicting
#: each other. `T-R7-6` holds the derivation against `PRECEDENCE`.
_TUNING_REASONS = frozenset({"low_confidence", "no_retrieval"})
#: Plus the reasons no routing table owns: a component failed, or the provider was unreachable.
_FAILURE_REASONS = frozenset({
    "provider_unavailable", "pipeline_error", "retrieval_unavailable", "pipeline_incomplete",
    "drafting_failed", "guardrails_failed", "handover_failed", "guardrails_did_not_run",
})


def _not_a_tuning_result() -> frozenset[str]:
    """Every escalation reason that is a rule or a failure, not the threshold doing its work."""
    from ticketing_agent.route import PRECEDENCE

    return frozenset(set(PRECEDENCE) - _TUNING_REASONS) | _FAILURE_REASONS


def _gaps(results: list[TicketResult], answered: list[TicketResult],
          scored: list[TicketResult], technical: dict[str, Any],
          latency: dict[str, Any], stub: bool) -> list[str]:
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
    # R5 review: the one requirement this project has measured and **missed** was the one
    # figure the gaps list said nothing about.
    if latency["state"] in {"replay", "partly_replayed", "provider_not_reached", "stub"}:
        gaps.append(
            f"Latency against NFR-01 (p95 under 3 s on the automated path): not measured in "
            f"this run. {latency['note']}")
    elif latency["automated_path_p95_ms"] is not None and latency["automated_path_p95_ms"] > 3000:
        gaps.append(
            f"**NFR-01 is missed.** The automated path's p95 is "
            f"{latency['automated_path_p95_ms']} ms against a 3,000 ms target, over "
            f"{latency['automated_path_tickets']} answered ticket(s). Two provider round trips "
            f"per answered ticket (PR-01 to draft, PR-03 to judge) against a hosted model is "
            f"the cause; NFR-01 was written before the provider was chosen, and the "
            f"requirement itself asks for the measured figure and its cause when it cannot be "
            f"met.")
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
         + ("" if run.get("read_cache", True) else
            ", **--no-cache**: recorded responses neither read nor written")
         + ("  \n  *A stub run is ingest and retrieval only: no classifier, no drafting and no "
            "guardrails, so every ticket escalates by the stub's own contract.*"
            if run["pipeline"] == "stub" else
            "  \n  *Not the real graph: a pipeline was injected, so these figures describe "
            "whatever was injected.*" if run["pipeline"] == "injected" else "  ")),
        (f"- Thresholds in use: relevance **{run['thresholds']['relevance_threshold']}**, "
         f"confidence **{run['thresholds']['confidence_threshold']}**, "
         f"top_k {run['thresholds']['retrieval_top_k']}  "),
        (f"- Provider: **{run['provider']['host'] or 'none configured'}**, model "
         f"`{run['provider']['model'] or '—'}`, grounding judge "
         f"`{run['provider']['judge_model'] or run['provider']['model'] or '—'}`"
         + ("  " if run["provider"]["grounding_judge_is_independent"] else
            "  \n  *The grounding check runs on the drafting model, so it is not an independent "
            "check. Set `JUDGE_MODEL_NAME` to a different model to make it one (D-74).*")),
        f"- Wall time: {run['wall_seconds']} s  ",
    ]
    # R5: above the figures it is about. `gate-openai-2` printed p95 95.7 ms against NFR-01's
    # 3-second target off zero model calls, while the live run of the same 80 tickets measured
    # 6.7 s — a miss. A caveat in a footnote does not stop that reading.
    latency = metrics["latency"]
    if latency["state"] in {"replay", "partly_replayed"} and latency["p95_ms"] is not None:
        lines += ["", f"> **Latency is not a measurement in this run.** {latency['note']}", ""]
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
        # R8 review: this table is the whole-run, mostly in-sample figure, and the section
        # below declares a different one as the headline. Saying so here is the difference
        # between two figures and two claims.
        ("*Over every scored ticket in this run, which on the supplied data is mostly wording "
         "the classifier was trained on. The split by wording is below, and the figure that "
         "bears on NFR-03 is in* "
         f"`{CALIBRATION_REPORT}`*.*"),
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

    # R8: which figure is the honest one, before the per-class table is read.
    wording = technical.get("classification_by_wording") or {}
    lines += ["", "### Classification by wording (R8)", ""]
    if wording.get("not_computable"):
        lines.append(f"*The seen/unseen split could not be made: {wording['not_computable']}*")
    else:
        lines += [
            (f"Training wording read from `{wording['training_file']}` "
             f"({wording['training_bodies']} distinct bodies). "
             f"**{_fmt(wording['seen_pct'])}** of this run's tickets use wording the classifier "
             f"was trained on."),
            "",
            "| group | tickets | scored | overall accuracy | lowest per-class precision |",
            "|---|---|---|---|---|",
        ]
        for key, label in ((UNSEEN, "**unseen body (the headline)**"),
                           (SEEN, "seen in training")):
            block = wording[key]
            per_class = block["classification"]
            accuracy = _fmt(per_class.get("overall_accuracy_pct")) if (
                "per_class" in per_class) else "not computable"
            floor = _per_class_floor({"classification": per_class})
            lines.append(f"| {label} | {block['tickets']} | {block['scored']} | {accuracy} "
                         f"| {_fmt(floor) if floor is not None else '—'} |")
        near = wording["unseen_but_near_duplicate"]
        novel_ids = wording["genuinely_novel_ticket_ids"]
        if near["skipped"]:
            lines += ["", f"*Near-duplicate clustering was skipped: {near['skipped']}.*"]
        else:
            lines += [
                "",
                (f"Of the {wording[UNSEEN]['tickets']} tickets with an unseen body, "
                 f"**{near['tickets']}** are paraphrases of a training body at D-39's "
                 f"{near['threshold']} clustering."),
                "",
                # The ids a human acts on are the novel ones, and the first version of this
                # section listed the fourteen paraphrase ids directly after the number 4 —
                # which reads as though those were the novel ones.
                (f"**{wording['genuinely_novel_wording']} ticket(s) use genuinely novel "
                 f"wording**"
                 + (f": {', '.join(novel_ids)}." if novel_ids
                    else ", so nothing in this run is evidence about unseen wording.")),
                "",
                f"Paraphrases: {', '.join(near['ticket_ids']) or '—'}.",
            ]
        lines += ["", f"*{wording['note']}*"]

    # R6: the ids, in the report a human reads. A count without them cannot be acted on.
    against = technical.get("answered_against_the_labels") or {}
    lines += ["", "### Answered against the labels (R6)", ""]
    if against.get("not_computable"):
        lines.append(f"*Not computable: {against['not_computable']}.*")
    else:
        lines += ["| disagreement | count | of those that could be scored | tickets |",
                  "|---|---|---|---|"]
        for label, key in (
                ("**Answered, label says must_not_auto_respond**",
                 "answered_but_must_not_auto_respond"),
                ("Answered, label says escalate", "answered_but_labelled_escalate"),
                ("Answered, label says not answerable from docs",
                 "answered_but_not_answerable_from_docs"),
                ("Answered, cited no expected article", "answered_citing_no_expected_article")):
            row = against[key]
            ids = ", ".join(row["ticket_ids"]) if row["ticket_ids"] else "—"
            lines.append(f"| {label} | {row['count']} | {_fmt(row['pct_of_scored'])} of "
                         f"{row['scored_for_this']} | {ids} |")
        matrix = against["confusion_against_expected_route"]
        lines += [
            "",
            "Decision against the labelled `expected_route`:",
            "",
            "| expected ↓ / actual → | auto_respond | escalate |",
            "|---|---|---|",
            (f"| auto_respond | {matrix['auto_respond']['auto_respond']} "
             f"| {matrix['auto_respond']['escalate']} |"),
            (f"| escalate | {matrix['escalate']['auto_respond']} "
             f"| {matrix['escalate']['escalate']} |"),
        ]
        if against["outside_the_matrix"]:
            lines.append("")
            lines.append(f"*{against['outside_the_matrix']} ticket(s) fell outside the matrix: "
                         f"no `expected_route` label, or a decision that is neither route.*")
        lines += ["", f"*{against['note']}*"]

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
