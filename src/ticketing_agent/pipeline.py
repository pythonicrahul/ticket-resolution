"""The end-to-end graph for one ticket. Never raises: every ticket ends answered or escalated.

Spec: docs/specs/FR-14.md §2 for the contract, and each component's own spec for the steps.

`SupportPipeline` is a LangGraph `StateGraph` over a Pydantic state (D-31). The graph is worth the
dependency for one reason: the order of the steps and the conditions between them are declared in
one place, so "what happens to a ticket" can be read off the edges instead of traced through
branches. What it is *not* is an agent — nothing here lets a model choose the next step. Every
edge is a deterministic condition on the state, because a support system that decides its own
control flow cannot be shown to escalate when it should (A5, NFR-08).

    ingest ─▶ guard ─▶ classify ─▶ retrieve ─▶ route ─┬─▶ draft ─▶ check ─┬─▶ finish
                                                       └─▶ handover ──────┘

`StubPipeline` (row 6) stays: it is what the harness tests use to exercise the run machinery
without the components, and it is the honest thing to run if a model is not configured.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, ConfigDict

from ticketing_agent.guardrails import check_ticket
from ticketing_agent.ingest import Ticket
from ticketing_agent.logging_store import (
    DecisionEntry,
    DecisionLogUnavailable,
    InvalidDecision,
)
from ticketing_agent.retrieve import Passage, RetrievalError

_log = logging.getLogger(__name__)

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
    # R2: what the classifier decided, on the row it decided about. These were empty on every
    # row of every recorded run because `to_entry` never passed them, which quietly turned
    # FR-05's urgency-first queue into an oldest-first one — `/queue` sorts on the log.
    intent: str | None = None
    intent_confidence: float | None = None
    intent_alternatives: tuple[tuple[str, float], ...] = ()
    urgency: str | None = None
    urgency_confidence: float | None = None
    urgency_reason: str | None = None
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
            intent=self.intent,
            intent_confidence=self.intent_confidence,
            intent_alternatives=self.intent_alternatives,
            urgency=self.urgency,
            urgency_confidence=self.urgency_confidence,
            urgency_reason=self.urgency_reason,
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


# --- row 14: the graph ----------------------------------------------------------------


class PipelineState(BaseModel):
    """What one ticket accumulates as it moves through the graph (D-31).

    Pydantic rather than a dict, for typed access and one declaration of what a ticket
    accumulates. `extra="forbid"` guards *construction*, not node updates: LangGraph filters a
    node's returned dict to the declared channels before validating, so an unknown key is
    dropped in silence rather than raising (verified at the row-14 review). A typo in a channel
    name is therefore caught by the tests that assert the counters, not by this config.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    ticket: Any
    extra_reasons: tuple[tuple[str, str], ...] = ()
    classification: Any | None = None
    passages: tuple[Passage, ...] = ()
    decision: Any | None = None
    draft: Any | None = None
    report: Any | None = None
    handover: Any | None = None
    failure: tuple[str, str] | None = None
    model_calls: int = 0
    cache_hits: int = 0
    prompt_version: str | None = None
    model_name: str | None = None


class SupportPipeline:
    """FR-14 §2: one ticket in, one outcome out — through every requirement in order.

    Never raises. A component that fails is this ticket's problem, not the run's (CLAUDE.md), so
    every node is guarded and every failure still ends in a logged escalation with a handover.
    """

    def __init__(self, *, retriever: Any, classifier: Any, router: Any, drafter: Any,
                 guardrails: Any, handover_writer: Any, settings: Any,
                 log: Any | None = None) -> None:
        self._retriever = retriever
        self._classifier = classifier
        self._router = router
        self._drafter = drafter
        self._guardrails = guardrails
        self._handover = handover_writer
        self._settings = settings
        self._log = log
        self._warned_no_log = False
        self._graph = self._build()

    def attach_log(self, log: Any) -> None:
        """FR-13 §3.1: where the intermediate rows go — a guardrail block, before its escalation.

        The terminal row is the harness's to write (it owns the latency and the reconciliation);
        the rows *inside* one ticket belong to the component that took the decision.
        """
        self._log = log

    # --- the graph ------------------------------------------------------------------

    def _build(self) -> Any:
        graph = StateGraph(PipelineState)
        graph.add_node("guard", self._node_guard)
        graph.add_node("classify", self._node_classify)
        graph.add_node("retrieve", self._node_retrieve)
        graph.add_node("route", self._node_route)
        graph.add_node("draft", self._node_draft)
        graph.add_node("check", self._node_check)
        graph.add_node("handover", self._node_handover)

        graph.set_entry_point("guard")
        graph.add_edge("guard", "classify")
        graph.add_edge("classify", "retrieve")
        graph.add_edge("retrieve", "route")
        # The only branches in the system, and both are conditions on the state rather than a
        # model's choice: answer or hand over, and then whether the draft survived its checks.
        graph.add_conditional_edges("route", self._after_route,
                                    {"draft": "draft", "handover": "handover"})
        graph.add_edge("draft", "check")
        graph.add_conditional_edges("check", self._after_check,
                                    {"handover": "handover", "done": END})
        graph.add_edge("handover", END)
        return graph.compile()

    @staticmethod
    def _after_route(state: PipelineState) -> str:
        decision = state.decision
        if decision is None or decision.escalated or state.failure is not None:
            # A node that failed means a check did not run. FR-12 §3.3: that is a failure, not a
            # pass, so nothing is drafted and nothing is sent to a provider on this ticket.
            return "handover"
        return "draft"

    @staticmethod
    def _after_check(state: PipelineState) -> str:
        report, draft = state.report, state.draft
        blocked = report is None or not report.passed  # no report means the check did not clear it
        unusable = draft is None or not getattr(draft, "usable", False)
        failed = state.failure is not None
        return "handover" if (blocked or unusable or failed) else "done"

    # --- the nodes ------------------------------------------------------------------

    def _node_guard(self, state: PipelineState) -> dict[str, Any]:
        """FR-12 §3.1: the pre-draft rules, before anything is embedded or sent."""
        try:
            return {"extra_reasons": tuple(check_ticket(state.ticket))}
        except Exception as exc:  # noqa: BLE001 - one ticket's problem
            # FR-12 §3.3: a check that raises is a failure, not a pass. `_after_route` sends a
            # ticket with a failure straight to the handover, so nothing is drafted or sent.
            return {"failure": ("guardrail_check_failed", _why(exc))}

    def _node_classify(self, state: PipelineState) -> dict[str, Any]:
        """FR-08: never raises on its own, but a mis-built classifier can."""
        try:
            return {"classification": self._classifier.classify(state.ticket)}
        except Exception as exc:  # noqa: BLE001
            return {"classification": None, "failure": ("classifier_unavailable", _why(exc))}

    def _node_retrieve(self, state: PipelineState) -> dict[str, Any]:
        """FR-10: returning nothing is valid; failing is an escalation, not an empty result."""
        if state.failure is not None or _withheld(state.extra_reasons):
            # FR-12 §3.1: the pre-draft rules run "before anything is embedded or sent". The
            # embedding is local, but a ticket the rules have already condemned has nothing to
            # gain from it, and the ordering should match what the spec says (row-14 review).
            return {"passages": ()}
        try:
            return {"passages": tuple(self._retriever.search(state.ticket.text))}
        except Exception as exc:  # noqa: BLE001
            return {"passages": (), "failure": ("retrieval_unavailable", _why(exc))}

    def _node_route(self, state: PipelineState) -> dict[str, Any]:
        """FR-02: the decision, with FR-12's findings ranked in D-16's table."""
        try:
            decision = self._router.decide(state.ticket, state.classification, state.passages,
                                           extra_reasons=state.extra_reasons)
        except Exception as exc:  # noqa: BLE001
            return {"failure": ("routing_failed", _why(exc))}
        return {"decision": decision}

    def _node_draft(self, state: PipelineState) -> dict[str, Any]:
        """FR-11: only reached when routing said `auto_respond`."""
        try:
            draft = self._drafter.draft(state.ticket, state.passages)
        except Exception as exc:  # noqa: BLE001 - the drafter promises not to, but the graph
            return {"failure": ("drafting_failed", _why(exc))}
        # The intermediate row records *what generation produced*, never the ticket's fate:
        # `DraftResult.log_fields()` says `escalate` when the draft is unusable, and a second
        # terminal row breaks reconciliation — which fails the whole run (exit 1) on the
        # documented normal case of `answerable: false`. The terminal row is the harness's.
        try:
            self._record(state, {**draft.log_fields(), "decision": "continue",
                                 "explanation": None}, action="drafted")
        except InvalidDecision as exc:
            return {"failure": ("generation_row_refused", _why(exc))}
        return {"draft": draft, "model_calls": state.model_calls + draft.model_calls,
                "cache_hits": state.cache_hits + draft.cache_hits,
                "prompt_version": draft.prompt_version, "model_name": draft.model_name}

    def _node_check(self, state: PipelineState) -> dict[str, Any]:
        """FR-12 §3.2: the five checks, on every draft, before anything is sent."""
        draft = state.draft
        if draft is None or not draft.usable:
            return {}
        try:
            report = self._guardrails.check_draft(
                ticket=state.ticket, reply=draft.reply,
                sentences=tuple((s.text, tuple(s.citations)) for s in draft.draft.sentences),
                retrieved=state.passages, citations=draft.citations,
                confidence=state.decision.confidence,
                threshold_applied=state.decision.threshold_applied)
        except Exception as exc:  # noqa: BLE001
            return {"failure": ("guardrails_failed", _why(exc))}
        if not report.passed:
            # FR-13 §3.1: the block is its own row, written before the reply is withheld.
            try:
                self._record(state, report.log_fields(), action="blocked")
            except InvalidDecision as exc:
                return {"report": report, "failure": ("block_row_refused", _why(exc))}
        return {"report": report, "model_calls": state.model_calls + report.model_calls,
                "cache_hits": state.cache_hits + report.cache_hits}

    def _node_handover(self, state: PipelineState) -> dict[str, Any]:
        """FR-01: every escalation carries one, whatever went wrong to get here."""
        decision = state.decision
        if decision is None:
            return {}
        # The handover must describe what actually stopped the reply. Handing it routing's
        # decision meant a ticket blocked for leaking an email address was summarised as
        # "the assistant is sure enough of the answer to send it", with the generic uncertainty —
        # and none of FR-01's nine post-draft sentences was reachable at all.
        decision = _as_terminal(decision, *_terminal_reason(state))
        draft = state.draft
        try:
            note = self._handover.write(
                ticket=state.ticket, classification=state.classification, decision=decision,
                passages=state.passages,
                draft=draft.reply if draft is not None and draft.usable else None)
        except Exception as exc:  # noqa: BLE001 - the writer promises not to, but the graph
            return {"failure": state.failure or ("handover_failed", _why(exc))}
        return {"handover": note, "model_calls": state.model_calls + note.model_calls,
                "cache_hits": state.cache_hits + note.cache_hits,
                # FR-13 refuses a row with model calls and no prompt version, and on an
                # escalation the handover may be the only call made — so the column has to name
                # PR-02, not stay empty because no draft was written (found by the first real run).
                "prompt_version": note.prompt_version or state.prompt_version,
                "model_name": state.model_name}

    # --- running it -----------------------------------------------------------------

    def process(self, ticket: Ticket) -> Outcome:
        """FR-14 §2: one outcome, always. The graph is the happy path; this is the floor."""
        try:
            final = self._graph.invoke(PipelineState(ticket=ticket))
            state = PipelineState(**final) if isinstance(final, dict) else final
            # Inside the guard: building the outcome reads a dozen component objects, and an
            # AttributeError there escaped `process` while the docstring said it could not.
            return self._outcome(state)
        except DecisionLogUnavailable:
            raise  # D-27: the run's problem, not this ticket's
        except Exception as exc:  # noqa: BLE001 - a graph that cannot run is still one ticket
            return _failed_outcome(ticket, "pipeline_error", _why(exc), None)

    def _outcome(self, state: PipelineState) -> Outcome:
        decision, draft, report, note = (state.decision, state.draft, state.report,
                                         state.handover)
        if decision is None:
            reason, detail = state.failure or ("pipeline_error", "the graph produced no decision")
            return _failed_outcome(state.ticket, reason, detail, note,
                                   classification=state.classification)

        answered, reason, detail, all_reasons, stage = _terminal_reason(state)

        # A component failure that routing papered over is still what happened: the classifier
        # raising shows up as `unknown_intent`, which is true but does not say why (row-14 tests).
        if state.failure is not None and not answered:
            cause = f"{state.failure[0]}: {state.failure[1]}"
            detail = f"{detail}; {cause}" if detail else cause

        reply = draft.reply if (answered and draft is not None) else None
        return Outcome(
            ticket=state.ticket,
            decision="auto_respond" if answered else "escalate",
            reason=None if answered else reason,
            explanation=_explanation(answered, reason, decision, note),
            all_reasons=() if answered else tuple(all_reasons),
            stage="routing" if answered else stage,
            passages=state.passages,
            draft=reply,
            citations=draft.citations if (answered and draft is not None) else (),
            guardrail_results=tuple((r.name, r.passed) for r in report.results)
            if report is not None else (),
            # FR-02 §5 wants the prediction on the row whatever the decision was. (This read
            # `decision.reason and None or (...)`, which always evaluates to the right-hand side
            # — correct by accident, and misleading to anyone who "fixed" it.)
            prediction_value=(state.classification.intent
                              if state.classification is not None else None),
            prediction_confidence=decision.confidence,
            **_classification_fields(state.classification),
            threshold_applied=decision.threshold_applied,
            summary=note.summary if note is not None else None,
            uncertainty=note.system_uncertainty if note is not None else None,
            detail=detail,
            # Whichever prompt produced the artefact this row is about: the reply for an answer,
            # the handover note for an escalation. One column cannot hold both, and the terminal
            # artefact is what an auditor is reading the row to understand.
            prompt_version=(draft.prompt_version if (answered and draft is not None)
                            else state.prompt_version),
            model_name=state.model_name,
            model_calls=state.model_calls,
            cache_hits=state.cache_hits,
            requirement_ids=_requirements(answered, decision, report, note),
        )

    def _record(self, state: PipelineState, fields: dict[str, Any], action: str) -> None:
        """Write one intermediate row, before the step it describes takes effect (FR-13 §7).

        A run without a log still works: the harness owns the terminal row, and an unattached log
        means the intermediate rows are not written — never that the step is skipped.
        """
        if self._log is None:
            # Not a mode: a caller that wants the intermediate rows attaches a log. Saying so
            # once is the difference between "not configured" and "silently not recorded".
            if not self._warned_no_log:
                self._warned_no_log = True
                _log.warning("no decision log attached: the %s rows inside each ticket are not "
                             "being written (FR-13 §3.1)", action)
            return
        entry = DecisionEntry(
            ticket_id=state.ticket.ticket_id, source_index=state.ticket.source_index,
            # R2: the classifier's columns first, so a caller that owns one of them still wins.
            # `governance_record` unpacks `alternatives` next to `prediction`, so the prediction
            # goes on too — a populated alternatives list beside a null prediction reads as
            # "alternatives to nothing" in the projection an assessor is given.
            **{**_classification_fields(state.classification),
               **_prediction_fields(state), **fields},
            **state.ticket.log_fields_for_log())
        try:
            self._log.perform(entry, lambda: None)
        except InvalidDecision:
            # The call was wrong, not the log. Loud, because FR-12 §5 requires the block to be
            # recorded and this is the path on which it silently was not.
            _log.error("the %s row for %s was refused by the decision log",
                       action, state.ticket.ticket_id)
            raise
        # DecisionLogUnavailable is deliberately not caught: FR-13 §4 and D-27 make an unwritable
        # log the one failure that stops the run rather than escalating one ticket.


def _classification_fields(classification: Any) -> dict[str, Any]:
    """R2: the classifier's five columns, or nothing when it never produced any.

    An absent classification is not a reason to leave the columns off the *other* rows, so this
    returns the empty mapping rather than a row of `None`s: the dataclass defaults already say
    "not classified", and a caller that splats this cannot accidentally blank them.
    """
    if classification is None:
        return {}
    # `SupportPipeline` types its classifier `Any`, and before R2 the graph read only three
    # attributes off whatever came back. A classification object without `row_fields` used to
    # work; making it raise would turn a *successfully drafted* ticket into a `pipeline_error`
    # escalation, because the AttributeError escapes `_record` into `process`'s broad except.
    fields = getattr(classification, "row_fields", None)
    return fields() if callable(fields) else {}


def _prediction_fields(state: Any) -> dict[str, Any]:
    """The Governance `prediction` block: the intent, and the confidence routing compared.

    Not the classifier's raw number: FR-02 §3.2 floors an unusable one to 0.0, and a row must
    state what the decision actually used (`route.py`). The raw value is on the same row as
    `intent_confidence`, and the two differing is the diagnosis of a broken classifier.
    """
    classification, decision = state.classification, state.decision
    if classification is None:
        return {}
    fields: dict[str, Any] = {"prediction_value": classification.intent}
    if decision is not None:
        fields["prediction_confidence"] = decision.confidence
    return fields


def _failed_outcome(ticket: Ticket, reason: str, detail: str, note: Any,
                    model_calls: int = 0, cache_hits: int = 0,
                    classification: Any = None) -> Outcome:
    return Outcome(
        ticket=ticket, decision="escalate", reason=reason,
        model_calls=model_calls, cache_hits=cache_hits,
        **_classification_fields(classification),
        explanation="Something went wrong while handling this ticket, so it goes to a person.",
        all_reasons=(reason,), stage="pipeline", detail=detail,
        summary=note.summary if note is not None else f"{ticket.channel} ticket {ticket.ticket_id}"
        f": handling did not complete, so it goes to a person.",
        uncertainty=note.system_uncertainty if note is not None
        else "The system could not finish handling this ticket.",
        requirement_ids=("FR-14",))


def _explanation(answered: bool, reason: str | None, decision: Any, note: Any) -> str:
    """FR-13 §2: the sentence a support manager reads to answer "why did it do that?".

    Never `decision.explanation` on an escalation the routing decision did not cause: that is
    the *answerable* sentence, so an escalate row read "the assistant is sure enough of the
    answer to send it". Each component owns the sentence for its own reasons.
    """
    if answered:
        return decision.explanation
    if reason == decision.reason:
        return decision.explanation
    from .generate import EXPLANATIONS as DRAFT_EXPLANATIONS
    from .handover import UNCERTAINTY

    written = UNCERTAINTY.get(reason or "") or DRAFT_EXPLANATIONS.get(reason or "")
    return written or PIPELINE_EXPLANATION


#: The last resort, and never the answerable sentence.
PIPELINE_EXPLANATION = (
    "Something went wrong while handling this ticket, so it goes to a person rather than being "
    "answered.")


def _terminal_reason(state: PipelineState) -> tuple[bool, str | None, str | None,
                                                    tuple[str, ...], str]:
    """What the ticket ended on: answered, or the reason that stopped it — and at which stage.

    Shared by the outcome and the handover, so the note and the row cannot disagree about why a
    ticket escalated (row-14 review).
    """
    decision, draft, report = state.decision, state.draft, state.report
    if decision is None:
        reason, detail = state.failure or ("pipeline_error", "the graph produced no decision")
        return False, reason, detail, (reason,), "pipeline"

    if decision.escalated:
        return False, decision.reason, decision.detail, decision.all_reasons, "routing"
    if report is not None and not report.passed:
        return False, report.reason, report.detail, report.all_reasons, "validation"
    if draft is None or not draft.usable:
        reason = draft.reason if draft is not None else (state.failure or ("drafting_failed",))[0]
        detail = draft.detail if draft is not None else (state.failure or ("", ""))[1]
        return False, reason, detail, (reason,), "generation"
    if state.failure is not None:
        return False, state.failure[0], state.failure[1], (state.failure[0],), "pipeline"
    return True, None, decision.detail, (), "routing"


def _as_terminal(decision: Any, answered: bool, reason: str | None, detail: str | None,
                 all_reasons: tuple[str, ...], stage: str) -> Any:
    """The routing decision as the ticket actually ended, for the handover to describe."""
    if answered:
        return decision
    from dataclasses import replace
    return replace(decision, decision="escalate", reason=reason, detail=detail,
                   all_reasons=tuple(all_reasons),
                   explanation=_explanation(False, reason, decision, None))


def _requirements(answered: bool, decision: Any, report: Any, note: Any) -> tuple[str, ...]:
    out = list(decision.requirement_ids)
    if report is not None and not report.passed:
        out.extend(r for r in ("FR-12",) if r not in out)
    if not answered and report is None and "FR-11" not in out:
        # NFR-05: name the component that actually stopped the reply. A failed draft was
        # attributed to routing alone (row-14 review).
        out.append("FR-11")
    if not answered and note is not None:
        out.append("FR-01")
    if answered:
        out.extend(r for r in ("FR-11", "FR-06") if r not in out)
    out.append("FR-14")
    return tuple(dict.fromkeys(out))


#: The pre-draft findings that stop a ticket before anything else happens (FR-12 §3.1).
WITHHOLDING = ("private_data_in_ticket", "instruction_injection_detected")


def _withheld(extra_reasons: tuple[tuple[str, str], ...]) -> bool:
    return any(reason in WITHHOLDING for reason, _ in extra_reasons)


def _why(exc: BaseException) -> str:
    """The type, and nothing that could be quoting the customer (NFR-04)."""
    return f"{type(exc).__name__}"
