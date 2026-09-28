"""FR-01: escalation handover package (PR-02, with template fallback).

*All 108 repeat contacts in the supplied data are on escalated tickets.* That line from the
discovery notes is what this module exists to change: an escalation that arrives without context
is how a ticket gets asked about twice.

The requirement is coverage before quality — "100% of escalated tickets carry a non-empty summary
and uncertainty reason" — so every failure path here ends in a note rather than an exception. The
template is not a degraded mode to be avoided; it is the guaranteed one, and the harness reports
how often it was used so an outage reads as an outage rather than as a quality collapse.

Two kinds of ticket never reach the model at all: one carrying a secret, because FR-12 §3.1.2's
rationale is that a secret must never be sent to a provider and a handover is not an exception;
and one carrying an injection attempt, because text written to manipulate a model is not text to
hand to a model.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .classify import Classification
from .generate import fill_slots
from .guardrails import markers_in, secrets_in
from .ingest import Ticket
from .prompts import Prompt, PromptError, load
from .provider import ProviderClient, ProviderFailure
from .retrieve import Passage
from .route import RoutingDecision
from .schemas import HandoverNote

_log = logging.getLogger(__name__)

PROMPT_ID = "PR-02"
PROMPT_NUMBER = "1.0"
PROMPT_VERSION = f"{PROMPT_ID} v{PROMPT_NUMBER}"
MAX_NOTE_TOKENS = 700
NONE_STATED = "none stated"

#: What the system could not settle, per escalation reason (FR-01 §3.4). Written for the person
#: picking the ticket up, in the same plain language FR-02 §3.8 requires of the decision log:
#: "low_confidence" tells an engineer nothing they can act on.
UNCERTAINTY = {
    "kill_switch":
        "Automatic replies were switched off, so this ticket was not answered by the system.",
    "private_data_in_ticket":
        "The ticket contains sensitive personal data, so the system did not process it further.",
    "instruction_injection_detected":
        "The ticket contains text that tries to change how the assistant behaves, so nothing it "
        "says was acted on.",
    "malformed_ticket":
        "The ticket could not be read reliably, so the system could not tell what was being "
        "asked.",
    "must_escalate_intent":
        "This kind of request is always handled by a person, whatever the system thought of it.",
    "money_commitment_requested":
        "The customer is asking for money back or another billing commitment, which only a "
        "person can promise.",
    "date_commitment_requested":
        "The customer is asking us to commit to a date, which only a person can give.",
    "unknown_intent":
        "The system could not place this request among the kinds it handles.",
    "text_truncated":
        "The ticket was longer than the system reads in full, so part of it was not considered.",
    "no_retrieval":
        "Nothing in the documentation was close enough to this question for the system to work "
        "from.",
    "low_confidence":
        "The system was not sure enough of its reading of this ticket to answer it.",
    # Post-draft reasons (FR-12): a draft existed and was stopped.
    "private_data_in_draft":
        "A reply was drafted and stopped because it contained personal data.",
    "ungrounded_draft":
        "A reply was drafted and stopped because part of it was not supported by the "
        "documentation.",
    "instruction_leak_in_draft":
        "A reply was drafted and stopped because it repeated the assistant's own instructions.",
    "commitment_in_draft":
        "A reply was drafted and stopped because it promised something only a person can "
        "promise.",
    "threshold_not_applied":
        "A reply was drafted and stopped because there was no evidence the confidence floor had "
        "been applied.",
    "empty_draft": "A reply was attempted and came back empty.",
    "check_error": "A safety check could not complete, so the reply was not sent.",
    "provider_unavailable":
        "The system that writes replies could not be reached while this ticket was being handled.",
    "drafting_failed": "The system could not draft a reply for this ticket.",
}
FALLBACK_UNCERTAINTY = "The system did not settle this ticket, so a person is handling it."


@dataclass(frozen=True)
class Handover:
    """FR-01 §2: what a tier-two engineer gets, so they need not re-read the ticket."""

    summary: str
    system_uncertainty: str
    customer_goal: str = ""
    already_tried: tuple[str, ...] = (NONE_STATED,)
    relevant_passages: tuple[str, ...] = ()
    suggested_first_check: str | None = None
    draft_answer: str | None = None
    intent: str | None = None
    intent_confidence: float = 0.0
    urgency: str | None = None
    retrieved_doc_ids: tuple[str, ...] = ()
    source: str = "template"
    prompt_version: str | None = None
    model_calls: int = 0
    cache_hits: int = 0

    def log_fields(self, decision: Any = None) -> dict[str, Any]:
        """FR-13 §5: the two fields FR-01's criterion is measured on, plus the provider counters.

        The handover annotates a terminal escalation row rather than creating one, so pass the
        `RoutingDecision` and the two halves are merged here. Splatting both `log_fields()`
        results side by side instead raises `TypeError` on `prompt_version`, which both fill —
        the same trap the row-11 review found in `generate.py`.
        """
        fields: dict[str, Any] = {
            "summary": self.summary,
            "uncertainty": self.system_uncertainty,
            "prompt_version": self.prompt_version,
            "model_calls": self.model_calls,
            "cache_hits": self.cache_hits,
            # PRD FR-01: the package carries "the predicted intent and urgency with confidence"
            # and "the retrieved articles", and the log row is what the criterion is measured on.
            "intent": self.intent,
            "intent_confidence": self.intent_confidence,
            "urgency": self.urgency,
            "retrieved_doc_ids": list(self.retrieved_doc_ids),
        }
        if decision is None:
            return fields
        merged = dict(decision.log_fields())
        merged.update(fields)
        # The note is written after the decision and is what the provider was used for, so its
        # prompt and counters are the row's. Routing itself makes no model call (FR-02 §5).
        merged["requirement_ids"] = [*merged.get("requirement_ids", []), "FR-01"]
        if self.urgency:
            # FR-05 only when an urgency actually reaches the row, not merely because the
            # dataclass has the field (D-52).
            merged["requirement_ids"].append("FR-05")
        return merged


class HandoverWriter:
    """FR-01: one note per escalation, from PR-02 when it can, from the template when it cannot."""

    def __init__(self, client: ProviderClient, prompt: Prompt | None = None,
                 max_tokens: int = MAX_NOTE_TOKENS) -> None:
        self._client = client
        # Loaded on first use, not here. A missing or reshaped prompt file used to raise at
        # construction, so a pipeline that builds the writer once would abort the whole run —
        # the opposite of "the template is the guaranteed path" (D-52).
        self._given = prompt
        self._max_tokens = max_tokens

    @property
    def _prompt(self) -> Prompt:
        return self._given if self._given is not None else load(PROMPT_ID, PROMPT_NUMBER)

    def write(self, *, ticket: Ticket, classification: Classification | None,
              decision: RoutingDecision, passages: Sequence[Passage],
              draft: str | None = None, allow_model: bool = True) -> Handover:
        """FR-01: the note. Never raises — an escalation without one is the failure being designed
        out, so every path below ends in a `Handover`.

        `allow_model` is not a way to weaken the note: the template is complete by contract, and
        the flag exists for callers that already know a model must not be asked (a run with no
        provider configured, or the harness measuring the template path).
        """
        record = _SystemRecord.of(ticket, classification, decision, passages, draft)
        withheld = self._withhold(ticket, decision)
        if withheld or not allow_model:
            if withheld:
                _log.info("handover for %s is template-only: %s", ticket.ticket_id, withheld)
            return record.template()

        try:
            prompt = self._prompt
            structured = self._client.complete_structured(
                [{"role": "system", "content": prompt.system},
                 {"role": "user", "content": self._render_user(record, prompt)}],
                prompt_id=PROMPT_ID, prompt_version=prompt.label,
                schema=HandoverNote, max_tokens=self._max_tokens)
        except (ProviderFailure, PromptError) as exc:
            _log.warning("handover for %s fell back to the template: %s", ticket.ticket_id, exc)
            return record.template()
        except Exception as exc:  # noqa: BLE001 - FR-01 §3.1: nothing here raises
            _log.warning("handover for %s failed (%s); using the template", ticket.ticket_id, exc)
            return record.template()

        return record.from_model(structured, prompt.label)

    # --- internals ------------------------------------------------------------------

    def _withhold(self, ticket: Ticket, decision: RoutingDecision) -> str | None:
        """FR-01 §3.2 and §3.3: the two kinds of ticket a model must never be shown.

        Checked against **everything that would be sent**, not against `ticket.text`. Those are
        not the same string: FR-07 caps `text` at 8000 characters, while the prompt renders the
        raw `subject` and `body`, so a secret past the cap was invisible to the check and
        transmitted anyway — and written to the response cache on disk (D-52). A handover is the
        one component that always sees truncated tickets, because `text_truncated` escalates.
        """
        sent = self._sent_text(ticket)
        if secrets_in(sent) or "private_data_in_ticket" in decision.all_reasons:
            return "the ticket carries a secret, which must never be sent to a provider"
        if markers_in(sent) or "instruction_injection_detected" in decision.all_reasons:
            return "the ticket is an injection attempt, so its text is not handed to a model"
        return None

    @staticmethod
    def _sent_text(ticket: Ticket) -> str:
        """Every string the prompt can carry, so the check and the payload cannot diverge."""
        return "\n".join(filter(None, (ticket.subject, ticket.body, ticket.text)))

    def _render_user(self, record: _SystemRecord, prompt: Prompt | None = None) -> str:
        """PR-02's USER template, filled from the file so the register stays the source of truth."""
        prompt = prompt if prompt is not None else self._prompt
        alternatives = ", ".join(f"{name} {score}" for name, score in record.alternatives) or "none"
        passages = "\n".join(
            f'<passage id="{_as_data(p.chunk_id)}">{_as_data(p.text)}</passage>'
            for p in record.passages) or "<passage>none retrieved</passage>"
        slots = {
            "{intent}": record.intent or "unknown",
            "{confidence}": f"{record.confidence:.2f}",
            "{alternatives}": alternatives,
            "{urgency}": record.urgency or "unknown",
            "{reason}": record.reason or "unknown",
            "{channel}": _attribute(record.channel),
            "{subject}": _as_data(record.subject),
            "{body}": _as_data(record.body),
        }
        lines: list[str] = []
        for line in self._prompt.user_template.splitlines():
            if line.strip() == "...":
                lines.append(passages)
                continue
            lines.append(fill_slots(line, slots))
        rendered = "\n".join(lines)

        left = sorted(prompt.placeholders() - {k.strip("{}") for k in slots})
        if left:
            raise PromptError(
                f"{prompt.path.name} has placeholders this code does not fill: {left}")
        # The guard that matters more than the slot names: a version that stops wrapping the
        # ticket would concatenate customer text straight into the instruction block, which
        # CLAUDE.md forbids outright. Checked on the rendered text, so it cannot be argued away.
        if rendered.count("<ticket") != 1 or rendered.count("</ticket>") != 1:
            raise PromptError(
                f"{prompt.path.name} does not wrap the ticket in exactly one <ticket> element; "
                "customer text must never be concatenated into the instructions")
        body = rendered.split("<ticket", 1)[1]
        if record.body and _as_data(record.body).split("\n")[0] not in body:
            raise PromptError(
                f"{prompt.path.name} renders the ticket outside its own <ticket> element")
        return rendered


@dataclass(frozen=True)
class _SystemRecord:
    """Everything the note is built from, whichever way it is built."""

    ticket_id: str
    channel: str
    subject: str
    body: str
    intent: str | None
    confidence: float
    alternatives: tuple[tuple[str, float], ...]
    urgency: str | None
    reason: str | None
    explanation: str
    escalated: bool
    passages: tuple[Passage, ...]
    draft: str | None

    @staticmethod
    def of(ticket: Ticket, classification: Classification | None, decision: RoutingDecision,
           passages: Sequence[Passage], draft: str | None) -> _SystemRecord:
        confidence = decision.confidence if classification is not None else 0.0
        return _SystemRecord(
            ticket_id=ticket.ticket_id, channel=ticket.channel, subject=ticket.subject,
            body=ticket.body,
            intent=classification.intent if classification is not None else None,
            confidence=float(confidence or 0.0),
            alternatives=(classification.intent_alternatives if classification is not None else ()),
            urgency=classification.urgency if classification is not None else None,
            reason=decision.reason, explanation=decision.explanation,
            escalated=decision.escalated,
            passages=tuple(passages), draft=draft)

    @property
    def uncertainty(self) -> str:
        """FR-01 §3.4: written from the escalation reason, never invented."""
        return UNCERTAINTY.get(self.reason or "", FALLBACK_UNCERTAINTY)

    def _safe(self, text: str) -> str:
        """PR-02 rule 5 as code: name a secret rather than repeat it.

        The summary goes into the decision log, and FR-13 truncates that column but deliberately
        does **not** scrub it (FR-13 §7 leaves that to the author). A card number in a ticket
        subject would otherwise be copied verbatim into the log by the template (D-52).
        """
        return "[secret present in ticket]" if secrets_in(text) else text

    def template(self) -> Handover:
        """The guaranteed note: the system record, in order, with nothing inferred.

        Terse on purpose. Its value is that it works when no model does, so it states what was
        known rather than reading like prose (FR-01 §7).
        """
        subject = self._safe((self.subject or "").strip())
        opening = f"{self.channel} ticket {self.ticket_id}"
        about = f' about "{subject}"' if subject else ""
        intent = (f"Classified as {self.intent} at confidence {self.confidence:.2f}"
                  if self.intent else "The intent could not be determined")
        articles = (", ".join(dict.fromkeys(p.doc_id for p in self.passages))
                    if self.passages else "no article")
        return Handover(
            summary=(f"{opening}{about}. {intent}; "
                     f"{'escalated because' if self.escalated else 'handed over; the system said'} "
                     f"{self.explanation[0].lower() + self.explanation[1:]}"),
            system_uncertainty=self.uncertainty,
            customer_goal=self._safe(_first_sentence(self.body)) or "not stated in the ticket",
            already_tried=(NONE_STATED,),
            relevant_passages=tuple(p.chunk_id for p in self.passages),
            retrieved_doc_ids=tuple(dict.fromkeys(p.doc_id for p in self.passages)),
            suggested_first_check=(f"Start from {articles}." if self.passages else None),
            draft_answer=self.draft,
            intent=self.intent, intent_confidence=self.confidence, urgency=self.urgency,
            source="template", prompt_version=None)

    def from_model(self, structured: Any, prompt_version: str = PROMPT_VERSION) -> Handover:
        """PR-02's note, with the fields FR-01 guarantees filled in from the record if missing."""
        note: HandoverNote = structured.value
        response = structured.response
        allowed = {p.chunk_id for p in self.passages}
        # FR-01 §3.5: a stray id is dropped rather than refusing the note. The reader is an
        # engineer who needs the note to exist, not a customer who would be misled by it.
        cited = tuple(dict.fromkeys(c for c in note.relevant_passages if c in allowed))
        tried = tuple(s.strip() for s in note.already_tried if s.strip())
        return Handover(
            summary=note.summary.strip() or self.template().summary,
            # FR-01 §3.4: the uncertainty is **always** code's sentence, never the model's. The
            # system knows exactly why it escalated; the model can only guess, and on the first
            # real run it guessed by echoing the reason code — "the system flagged the intent as
            # a security incident but required escalation (must_escalate_intent)" is the jargon
            # the PRD's "plain statement" rules out (D-52). The model's note contributes the
            # summary, the goal, what was tried and where to start.
            system_uncertainty=self.uncertainty,
            customer_goal=note.customer_goal.strip() or self.template().customer_goal,
            # "none stated" rather than empty, so an agent can tell "nothing tried" from
            # "not extracted" (PR-02 rule 3).
            already_tried=tried or (NONE_STATED,),
            relevant_passages=cited,
            retrieved_doc_ids=tuple(dict.fromkeys(p.doc_id for p in self.passages)),
            suggested_first_check=note.suggested_first_check,
            draft_answer=self.draft,
            intent=self.intent, intent_confidence=self.confidence, urgency=self.urgency,
            source="model", prompt_version=prompt_version,
            model_calls=response.provider_requests, cache_hits=1 if response.cached else 0)


#: Full stops that do not end a sentence. "Dr. Smith said the API is down." used to yield "Dr."
ABBREVIATIONS = ("dr.", "mr.", "mrs.", "ms.", "prof.", "e.g.", "i.e.", "etc.", "vs.", "no.",
                 "approx.", "inc.", "ltd.", "fig.", "ref.")


def _first_sentence(text: str, limit: int = 200) -> str:
    """The customer's own opening line, for the template's `customer_goal`.

    Abbreviations are skipped, a sentence with no terminator is cut on a word boundary with an
    ellipsis rather than mid-word, and text with no letters or digits in it is not a goal (D-52).
    """
    stripped = " ".join((text or "").split())
    if not any(c.isalnum() for c in stripped):
        return ""

    for index, char in enumerate(stripped):
        if char not in ".?!" or index + 1 >= len(stripped) or stripped[index + 1] != " ":
            continue
        candidate = stripped[: index + 1]
        if candidate.split()[-1].lower() in ABBREVIATIONS:
            continue
        return candidate
    if len(stripped) <= limit:
        return stripped
    return stripped[:limit].rsplit(" ", 1)[0] + "…"


def _as_data(text: str) -> str:
    """CLAUDE.md: customer text is data. The same escaping `generate.py` applies to a ticket."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attribute(value: str) -> str:
    """The same, plus quotes, for a value that sits inside an XML-ish attribute."""
    return _as_data(value).replace('"', "&quot;")
