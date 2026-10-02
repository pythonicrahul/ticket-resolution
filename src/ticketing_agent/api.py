"""FastAPI application: submit a ticket, search (FR-04), escalation queue (FR-05), /metrics.

The one surface aimed at **agents** rather than customers, which is why most of the rules here
are about what it does not expose: the agents' own past answers are never searchable (FR-04 §3.1),
a failure never carries its exception message outwards, and every ticket submitted takes the same
path through the same pipeline as a harness run — there is no second, looser way in.

`build_app(settings, retriever=..., pipeline=...)` lets the tests inject both, so the routes are
exercised offline. `app` builds the real thing from the environment, which is what
`uv run uvicorn ticketing_agent.api:app` serves.
"""
from __future__ import annotations

import logging
import threading
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .config import ConfigError, Settings, load_settings
from .ingest import normalise_ticket
from .logging_store import DecisionLog
from .retrieve import RetrievalError, Retriever

_log = logging.getLogger(__name__)

MAX_RESULTS = 20
DEFAULT_RESULTS = 5
#: FR-05: the queue holds tickets a person has to pick up, and nothing else.
QUEUE_DECISION = "escalate"


class SearchHit(BaseModel):
    chunk_id: str
    doc_id: str
    title: str
    heading: str
    text: str
    score: float
    rank: int


class SearchResponse(BaseModel):
    query: str
    count: int
    results: list[SearchHit]


class TicketIn(BaseModel):
    """What a caller may submit. Anything else the pack schema carries is accepted and ignored."""

    model_config = {"extra": "allow"}

    ticket_id: str | None = None
    channel: str = Field(min_length=1)
    subject: str = ""
    body: str = ""
    received_at: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None
    customer_tier: str | None = None
    customer_region: str | None = None
    language_fluency: str | None = None


class TicketOut(BaseModel):
    ticket_id: str
    decision: str
    reason: str | None = None
    explanation: str | None = None
    reply: str | None = None
    citations: list[str] = []
    summary: str | None = None
    uncertainty: str | None = None
    intent: str | None = None
    confidence: float | None = None
    # R2, FR-05: the urgency the classifier produced, so a caller that submits a ticket and a
    # caller that reads `/queue` see the same field without a second request.
    urgency: str | None = None
    urgency_confidence: float | None = None
    threshold_applied: float | None = None


class QueueItem(BaseModel):
    ticket_id: str
    urgency: str | None = None
    urgency_confidence: float | None = None
    urgency_reason: str | None = None
    reason: str | None = None
    #: Why the ticket escalated, in the words routing and the guardrails recorded. A different
    #: question from `urgency_reason`, and it used to be served under that name.
    detail: str | None = None
    summary: str | None = None
    received_at: str | None = None
    channel: str | None = None


def build_app(settings: Settings, retriever: Any | None = None,
              pipeline: Any | None = None) -> FastAPI:
    """The app, with its two heavy dependencies injectable so the tests stay offline."""
    app = FastAPI(title="CloudServe support system",
                  description="Submit a ticket, search the documentation, read the queue.",
                  version="1.0")
    # Closed over rather than injected through `Depends`: there is exactly one of each per app,
    # and the tests build the app with both supplied.
    state = _State(settings, retriever, pipeline)

    @app.get("/health")
    def health() -> dict[str, Any]:
        """What is wired, so a deployment can be checked without submitting a ticket.

        **A readiness check, not a liveness one** (R10 review). It used to return `ok: true`
        unconditionally — including with no classifier, which is exactly the state
        `docker compose up` leaves before `docker compose run --rm train`: Docker marks the
        container healthy, Prometheus shows zeroes, and every ticket 500s with nothing logged.
        It now builds nothing itself (the retriever is a lazy property, and a HEALTHCHECK with a
        5-second timeout must not trigger a Chroma index build) and reports what it found.
        """
        indexed = len(getattr(state.built_retriever, "chunks", ()) or ())
        problems = state.configuration_problems
        return {
            # Serving, and configured so a ticket can be handled. Not "has already built the
            # graph": a container that has served no ticket is healthy, and one with no
            # classifier is not, however many tickets it has served.
            "ok": not problems,
            "problems": problems,
            "pipeline": state.pipeline_state,
            "documents_indexed": indexed,
            "model": settings.model_name or None,
            "kill_switch": settings.kill_switch_on,
            "thresholds": {"confidence": settings.confidence_threshold,
                           "relevance": settings.relevance_threshold},
        }

    @app.get("/search", response_model=SearchResponse)
    def search(q: str = Query(..., description="what to look for, in plain words"),
               k: int = Query(DEFAULT_RESULTS, ge=1, le=MAX_RESULTS),
               min_score: float = Query(0.0, ge=0.0, le=1.0)) -> SearchResponse:
        """FR-04: ranked passages from `documentation.json`, and from nothing else."""
        if not q.strip():
            # "Nothing matched" and "you asked nothing" are different answers (FR-04 §3.5).
            raise HTTPException(status_code=400, detail="q is empty: nothing was searched for")
        try:
            passages = state.retriever.search(q, top_k=k, threshold=min_score)
        except RetrievalError as exc:
            # The type, never the message: an index path or a driver error is operational detail.
            _log.warning("search failed: %s", exc)
            raise HTTPException(status_code=503,
                                detail="the documentation index is unavailable") from None
        hits = [SearchHit(chunk_id=p.chunk_id, doc_id=p.doc_id, title=p.title, heading=p.heading,
                          text=p.text, score=round(p.score, 4), rank=p.rank) for p in passages]
        return SearchResponse(query=q, count=len(hits), results=hits)

    @app.post("/tickets", response_model=TicketOut)
    def submit(ticket: TicketIn) -> TicketOut:
        """One ticket through the same pipeline a harness run uses, and logged the same way.

        **Every exit from this function writes a row first** (R10 review). Three did not:

        * A malformed body raised a 400 with no row at all, while the same ticket through the
          harness is escalated as `malformed_ticket` with a row — FR-07's criterion is "logged
          and escalated, not crashed", and this module's own docstring claims there is "no
          second, looser way in". The 400 is right; having no trace of it was not.
        * A pipeline that could not be built (no classifier, no index) 500'd with no row.
        * Anything raised between opening the log and `log.perform` lost the ticket, which is
          how two concurrent requests dropped one entirely (D-78).
        """
        entry = ticket.model_dump()
        normalised = normalise_ticket(entry, index=None)

        with DecisionLog(settings.decision_log_path, run_id="api") as log:
            if normalised.is_malformed:
                # Logged, then refused. FR-13 logs the decision before the action, and refusing
                # a ticket is a decision.
                log.perform(_malformed_outcome(normalised).to_entry(), lambda: None)
                raise HTTPException(
                    status_code=400,
                    detail=f"the ticket could not be read: {', '.join(normalised.defects)}")
            try:
                # The log is attached **before** the ticket is processed, not after. Processing
                # first meant the rows the graph writes as it goes — above all the guardrail
                # `block` row FR-12 §5 requires — were silently never written for a ticket
                # submitted here, while the same ticket through the harness recorded them.
                state.attach(log)
                outcome = state.pipeline.process(normalised)
            except Exception as exc:  # noqa: BLE001 - one ticket's problem, never a lost ticket
                _log.exception("handling %s failed", normalised.ticket_id)
                outcome = _failed_outcome(normalised, exc)
                log.perform(outcome.to_entry(), lambda: None)
                raise HTTPException(
                    status_code=503,
                    # The type, never the message: a build failure names the classifier path
                    # and a provider error can echo the request (NFR-04).
                    detail="the ticket was logged and escalated, but could not be handled: "
                           f"{type(exc).__name__}") from None
            # FR-13, CLAUDE.md: the row is written before the outcome is returned to the caller.
            log.perform(outcome.to_entry(), lambda: None)
        return TicketOut(
            ticket_id=normalised.ticket_id, decision=outcome.decision, reason=outcome.reason,
            explanation=outcome.explanation, reply=outcome.draft,
            citations=list(outcome.citations), summary=outcome.summary,
            uncertainty=outcome.uncertainty, intent=outcome.prediction_value,
            confidence=outcome.prediction_confidence,
            urgency=outcome.urgency, urgency_confidence=outcome.urgency_confidence,
            threshold_applied=outcome.threshold_applied)

    @app.get("/queue")
    def queue(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
        """FR-05: what a person has to pick up, urgency first, then oldest first."""
        from .classify import order_escalation_queue

        with DecisionLog(settings.decision_log_path) as log:
            rows = [r for r in log.rows() if r["decision"] == QUEUE_DECISION]
        ordered = order_escalation_queue(rows)[:limit]
        items = [QueueItem(ticket_id=r["ticket_id"], urgency=r.get("urgency"),
                           urgency_confidence=r.get("urgency_confidence"),
                           # FR-05's reason is the classifier's evidence. This used to read
                           # `detail`, which on a terminal row is the *escalation* detail — a
                           # plausible-looking but wrong answer to "why is this high?".
                           urgency_reason=r.get("urgency_reason"),
                           detail=r.get("detail"), reason=r.get("reason"),
                           summary=r.get("summary"), received_at=r.get("received_at"),
                           channel=r.get("channel")) for r in ordered]
        return {"count": len(items), "items": [i.model_dump() for i in items]}

    @app.get("/metrics/prometheus", response_class=PlainTextResponse)
    def prometheus() -> str:
        """NFR-05: the same facts as `/metrics`, in the format a scraper reads.

        Built from the decision log rather than from in-process counters on purpose: a counter
        resets when the process does, and the requirement is that **100% of decisions** are
        auditable. The log is the record; this is a view of it.
        """
        with DecisionLog(settings.decision_log_path) as log:
            rows = log.rows()
        terminal = [r for r in rows if r["decision"] in ("auto_respond", "escalate")]
        by_reason = Counter(r["reason"] for r in terminal if r["reason"])
        by_stage = Counter(r["stage"] for r in rows)
        guardrails: Counter[str] = Counter()
        for row in rows:
            for result in row["guardrail_results"] or ():
                if len(result) >= 2 and not result[1]:
                    guardrails[str(result[0])] += 1

        lines = [
            *_metric("ticketing_decisions_total", "Terminal decisions recorded.", "counter",
                     [({}, len(terminal))]),
            *_metric("ticketing_decisions_by_outcome_total",
                     "Terminal decisions by outcome.", "counter",
                     [({"outcome": name}, sum(1 for r in terminal if r["decision"] == name))
                      for name in ("auto_respond", "escalate")]),
            *_metric("ticketing_escalations_by_reason_total",
                     "Escalations by the reason recorded on the row.", "counter",
                     [({"reason": reason}, count) for reason, count in sorted(by_reason.items())]),
            *_metric("ticketing_guardrail_blocks_total",
                     "Guardrail checks that failed, by check name.", "counter",
                     [({"check": name}, count) for name, count in sorted(guardrails.items())]),
            *_metric("ticketing_rows_by_stage_total", "Every logged row, by stage.", "counter",
                     [({"stage": stage}, count) for stage, count in sorted(by_stage.items())]),
            *_metric("ticketing_model_calls_total",
                     "Provider requests, as recorded on the rows.", "counter",
                     [({}, sum(r["model_calls"] for r in rows))]),
            *_metric("ticketing_kill_switch", "1 when automatic replies are switched off.",
                     "gauge", [({}, int(settings.kill_switch_on))]),
            *_metric("ticketing_threshold", "The thresholds in force for this deployment.",
                     "gauge", [({"kind": "confidence"}, settings.confidence_threshold),
                               ({"kind": "relevance"}, settings.relevance_threshold)]),
        ]
        return "\n".join(lines) + "\n"

    @app.get("/metrics")
    def metrics() -> dict[str, Any]:
        """What this deployment has decided so far. The harness writes the full report."""
        with DecisionLog(settings.decision_log_path) as log:
            rows = log.rows()
        terminal = [r for r in rows if r["decision"] in ("auto_respond", "escalate")]
        return {
            "decisions": len(terminal),
            "answered": sum(1 for r in terminal if r["decision"] == "auto_respond"),
            "escalated": sum(1 for r in terminal if r["decision"] == "escalate"),
            "blocked": sum(1 for r in rows if r["decision"] == "block"),
            "by_reason": dict(Counter(r["reason"] for r in terminal if r["reason"])),
            "model_calls": sum(r["model_calls"] for r in rows),
            "thresholds": {"confidence": settings.confidence_threshold,
                           "relevance": settings.relevance_threshold},
            "kill_switch": settings.kill_switch_on,
        }

    return app


def _metric(name: str, help_text: str, kind: str,
            samples: list[tuple[dict[str, str], float]]) -> list[str]:
    """One metric in the text exposition format: HELP, TYPE, then its labelled samples."""
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    for labels, value in samples:
        rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
        lines.append(f"{name}{{{rendered}}} {value}" if rendered else f"{name} {value}")
    return lines


def _escape(value: str) -> str:
    """Label values are quoted, so a backslash, a quote or a newline must not end the value."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _malformed_outcome(ticket: Any) -> Any:
    """FR-07, FR-09: the row a refused ticket leaves behind (R10 review).

    The same reason and the same explanation the harness writes for the identical ticket, so
    the two paths agree — this module's docstring claims there is no looser way in, and until
    this existed there was: a 400 and no trace.
    """
    from .pipeline import Outcome

    return Outcome(
        ticket=ticket, decision="escalate", reason="malformed_ticket",
        explanation=("This ticket could not be read well enough to answer it, so a person "
                     "should look at it."),
        all_reasons=("malformed_ticket",), stage="ingest",
        detail="ingest defects: " + ", ".join(ticket.defects),
        summary=f"{ticket.channel} ticket {ticket.ticket_id} could not be read.",
        uncertainty="The ticket could not be read reliably, so a person looks at it.",
        requirement_ids=("FR-07", "FR-09", "FR-13"))


def _failed_outcome(ticket: Any, exc: BaseException) -> Any:
    """FR-14 §3.3's contract, at the API: one ticket's failure is never a lost ticket."""
    from .pipeline import Outcome

    return Outcome(
        ticket=ticket, decision="escalate", reason="pipeline_error",
        explanation="Something went wrong while handling this ticket, so it goes to a person.",
        all_reasons=("pipeline_error",), stage="pipeline",
        # The type only: a build failure names the classifier path and a provider error can
        # echo the request (NFR-04).
        detail=type(exc).__name__,
        summary=f"{ticket.channel} ticket {ticket.ticket_id}: handling did not complete.",
        uncertainty="The system could not finish handling this ticket.",
        requirement_ids=("FR-13", "FR-14"))


class _State:
    """The two heavy things, built once and shared: the index and the graph."""

    def __init__(self, settings: Settings, retriever: Any | None, pipeline: Any | None) -> None:
        self._settings = settings
        self._retriever = retriever
        self._pipeline = pipeline
        # R10 review: two concurrent first requests each built the whole Chroma index against
        # the same `CHROMA_PATH` and each loaded the classifier, last writer winning. One lock
        # over both lazy builds; the work happens once and the loser waits for it.
        #
        # **Reentrant, and that is not a detail.** `pipeline` takes this lock and then reads
        # `self.retriever`, which takes it again — with a plain `Lock` the very first ticket
        # deadlocks, for ever, with no error and no log line. I shipped that in R10 and found it
        # in R11 only by watching a real container serve `/health` happily while a POST never
        # returned. An `RLock` is the honest primitive for a nested lazy build (D-80).
        self._lock = threading.RLock()

    @property
    def retriever(self) -> Any:
        if self._retriever is None:
            with self._lock:
                if self._retriever is None:
                    one = Retriever(self._settings)
                    one.build_index(self._settings.require_path("docs_path"))
                    self._retriever = one
        return self._retriever

    @property
    def pipeline(self) -> Any:
        if self._pipeline is None:
            with self._lock:
                if self._pipeline is None:
                    self._pipeline = _build_pipeline(self._settings, self.retriever)
        return self._pipeline

    @property
    def built_retriever(self) -> Any:
        """Whatever has already been built, without building it.

        `/health` used to read `retriever`, the lazy property, so the first health check built
        the whole index — against a HEALTHCHECK with a five-second timeout (R10 review).
        """
        return self._retriever

    @property
    def pipeline_state(self) -> str:
        """`ready` or `not built` — **without building anything**.

        The R10 review said `/health` must not read the lazy retriever property, because the
        Dockerfile gives the HEALTHCHECK five seconds and a Chroma index build does not fit in
        them. My first fix obeyed the letter of that and broke the spirit: it *built the
        pipeline* as a readiness probe, which loads the classifier, builds the index and
        downloads a 79MB embedding model on a cold container. Running the stack showed `/health`
        simply never answering.

        So it answers from what exists. `not built` is an honest state — a container that has
        served no ticket yet has no graph — and `missing` on a configured path is the condition
        that made `ok: true` a lie before (D-78, D-80).
        """
        return "ready" if self._pipeline is not None else "not built"

    @property
    def configuration_problems(self) -> list[str]:
        """Which configured inputs are absent, checked by looking rather than by loading.

        Cheap enough for a five-second HEALTHCHECK: `stat` on three paths. This is what makes
        `ok` meaningful without making it expensive — `docker compose up` before
        `docker compose run --rm train` reports the missing classifier instead of claiming
        health and then 503ing every ticket.
        """
        problems = []
        for field, label in (("classifier_path", "classifier"), ("docs_path", "documentation")):
            configured = getattr(self._settings, field, None)
            if configured is None:
                problems.append(f"{label}: not configured")
            elif not Path(configured).exists():
                problems.append(f"{label}: no file at the configured path")
        if not (self._settings.model_name or "").strip():
            problems.append("model: MODEL_NAME is not set")
        return problems

    def attach(self, log: DecisionLog) -> None:
        """Give the graph the log, so a guardrail block is recorded here too (FR-13 §3.1).

        Reads `self.pipeline`, the property, not the `_pipeline` attribute. The attribute is
        `None` until the first request builds the graph, so the private read attached the log to
        nothing on the very first ticket — and because the built graph is then cached, it stayed
        attached to nothing for the life of the process. Measured through a running server: five
        tickets produced five rows, with the `generation` and `block` rows the harness writes for
        the same tickets missing entirely.
        """
        attach = getattr(self.pipeline, "attach_log", None)
        if callable(attach):
            attach(log)


def _build_pipeline(settings: Settings, retriever: Any) -> Any:
    """The same graph the harness runs. One way in, so the API cannot be the looser path."""
    from .classify import IntentClassifier, TrainedClassifier
    from .generate import Drafter
    from .guardrails import GroundingJudge, Guardrails
    from .handover import HandoverWriter
    from .pipeline import SupportPipeline
    from .provider import ProviderClient
    from .route import Router

    client = ProviderClient(settings)
    return SupportPipeline(
        retriever=retriever,
        classifier=IntentClassifier(TrainedClassifier.load(settings.classifier_path)),
        router=Router(settings), drafter=Drafter(client),
        guardrails=Guardrails(judge=GroundingJudge(client)),
        handover_writer=HandoverWriter(client), settings=settings)


def _app() -> FastAPI:
    try:
        settings = load_settings()
    except ConfigError as exc:  # pragma: no cover - a start-up failure, not a request
        raise SystemExit(f"configuration error: {exc}") from None
    return build_app(settings)


app = _app()
