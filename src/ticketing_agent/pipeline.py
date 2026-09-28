"""The end-to-end graph for one ticket. Never raises: every ticket ends answered or escalated.

Spec: docs/specs/FR-14.md §2 for the contract, and each component's own spec for the steps.

Row 6 ships `StubPipeline`, which runs the parts that exist — FR-07 ingest and FR-10 retrieval —
and escalates with an honest reason. That is deliberately not a no-op: it makes the harness
end-to-end today, so reconciliation, the metrics report and retrieval hit rate are all real while
classification (row 8), routing (row 9), drafting (row 11), guardrails (row 12) and the handover
(row 13) are still to come. Row 14 replaces it with the LangGraph pipeline (D-31) and the harness
does not change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from ticketing_agent.ingest import Ticket
from ticketing_agent.logging_store import DecisionEntry
from ticketing_agent.retrieve import Passage, RetrievalError

#: The reason the stub gives while the answering path is unbuilt. A run before row 14 therefore
#: shows a 100% escalation rate, which is correct and must not be read as a tuning result.
PIPELINE_INCOMPLETE = "pipeline_incomplete"


@dataclass(frozen=True)
class Outcome:
    """What happened to one ticket. Its fields are FR-13's columns, so nothing is invented later."""

    ticket: Ticket
    decision: str
    reason: str | None = None
    explanation: str | None = None
    all_reasons: tuple[str, ...] = ()
    stage: str = "routing"
    passages: tuple[Passage, ...] = ()
    draft: str | None = None
    citations: tuple[str, ...] = ()
    guardrail_results: tuple[tuple[str, bool], ...] = ()
    prediction_value: str | None = None
    prediction_confidence: float | None = None
    threshold_applied: float | None = None
    summary: str | None = None
    uncertainty: str | None = None
    detail: str | None = None
    prompt_version: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    model_calls: int = 0
    cache_hits: int = 0
    latency_ms: float | None = None
    requirement_ids: tuple[str, ...] = ("FR-14",)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return self.decision == "auto_respond"

    def to_entry(self) -> DecisionEntry:
        """FR-13: the log row for this outcome, with the ticket's own fields carried through."""
        return DecisionEntry(
            ticket_id=self.ticket.ticket_id,
            source_index=self.ticket.source_index,
            stage=self.stage,
            decision=self.decision,
            reason=self.reason,
            explanation=self.explanation,
            all_reasons=self.all_reasons,
            detail=self.detail,
            summary=self.summary,
            uncertainty=self.uncertainty,
            prediction_value=self.prediction_value,
            prediction_confidence=self.prediction_confidence,
            threshold_applied=self.threshold_applied,
            sources_used=tuple(p.as_source() for p in self.passages),
            citations=self.citations,
            guardrail_results=self.guardrail_results,
            prompt_version=self.prompt_version,
            model_name=self.model_name,
            model_version=self.model_version,
            model_calls=self.model_calls,
            cache_hits=self.cache_hits,
            latency_ms=self.latency_ms,
            requirement_ids=self.requirement_ids,
            **self.ticket.log_fields_for_log(),
        )


class Pipeline(Protocol):
    """FR-14 §2: one ticket in, one outcome out. Row 14's graph and the stub share this."""

    def process(self, ticket: Ticket) -> Outcome:
        ...


class StubPipeline:
    """Ingest and retrieval, then an honest escalation. Replaced by the graph at row 14.

    It does the two things that exist so the harness measures something real, and it applies the
    rules those two requirements already fix: a malformed ticket escalates before anything else
    (FR-07, D-16 rank 4), and an empty retrieval escalates rather than being answered (FR-10).
    """

    def __init__(self, retriever: Any | None = None, threshold: float | None = None) -> None:
        self._retriever = retriever
        self._threshold = threshold

    def process(self, ticket: Ticket) -> Outcome:
        if ticket.is_malformed:
            return Outcome(
                ticket=ticket,
                decision="escalate",
                reason="malformed_ticket",
                explanation=("This ticket could not be read well enough to answer it, so a person "
                             "should look at it."),
                all_reasons=("malformed_ticket",),
                stage="ingest",
                detail="ingest defects: " + ", ".join(ticket.defects),
                requirement_ids=("FR-07", "FR-09", "FR-14"),
            )

        passages: tuple[Passage, ...] = ()
        if self._retriever is not None:
            try:
                passages = self._retriever.search(ticket.text)
            except RetrievalError as exc:
                return Outcome(
                    ticket=ticket,
                    decision="escalate",
                    reason="retrieval_unavailable",
                    explanation=("The documentation search was unavailable, so this ticket goes "
                                 "to a person."),
                    all_reasons=("retrieval_unavailable",),
                    stage="retrieval",
                    detail=f"{type(exc).__name__}",
                    threshold_applied=self._threshold,
                    requirement_ids=("FR-10", "FR-14"),
                )

        if not passages:
            return Outcome(
                ticket=ticket,
                decision="escalate",
                reason="no_retrieval",
                explanation=("No documentation passage was relevant enough to answer from, so a "
                             "person should take this one."),
                all_reasons=("no_retrieval",),
                stage="retrieval",
                threshold_applied=self._threshold,
                requirement_ids=("FR-10", "FR-14"),
            )

        return Outcome(
            ticket=ticket,
            decision="escalate",
            reason=PIPELINE_INCOMPLETE,
            explanation=("Relevant documentation was found, but the answering path is not built "
                         "yet, so this ticket goes to a person."),
            all_reasons=(PIPELINE_INCOMPLETE,),
            stage="routing",
            passages=passages,
            threshold_applied=self._threshold,
            requirement_ids=("FR-14",),
        )
