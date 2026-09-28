"""FR-02, FR-09, FR-03, FR-16: one routing decision per ticket, and the reasons for it.

Specs: `docs/specs/FR-02.md` (the threshold and `RoutingDecision`), `FR-09.md` (the four intents
that always escalate), `FR-03.md` §3 (money and date commitments), `FR-16.md` (the kill switch).

Four requirements share this module because they are one decision, taken once, before any model
call. Three properties are worth stating because they are what the tests hold in place:

* **Every rule is evaluated, not only the first.** `reason` is the highest-ranked rule that fired
  and `all_reasons` carries every one (D-16), so the log does not depend on evaluation order.
* **Nothing here can turn an escalation into an answer.** There is no parameter, setting or
  environment variable that removes a rule; `decide` reads settings and the ticket, nothing else.
* **No model call, no clock, no chance.** A refund request escalates identically during a provider
  outage (A11), and the same ticket routes the same way every time (A5, NFR-08).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from .classify import Classification
from .config import Settings
from .ingest import Ticket
from .retrieve import Passage

_log = logging.getLogger(__name__)

#: FR-09 §3.1: a constant, not configuration. The PRD's "zero auto-responses" criterion rests on
#: this set, so there is deliberately no way to shorten it from outside the module.
ALWAYS_ESCALATE_INTENTS = frozenset(
    {"security_incident", "compliance_request", "feature_request", "unclear_request"}
)

#: The 22 intents of the supplied taxonomy (FR-08). An intent outside it escalates: an intent the
#: system does not recognise is not one it may answer on (FR-09 §4).
KNOWN_INTENTS = frozenset({
    "account_access", "api_key_issue", "api_usage_question", "authentication_failure",
    "billing_query", "compliance_request", "configuration_help", "data_export",
    "data_residency", "database_issue", "deployment_failure", "feature_request",
    "integration_help", "onboarding", "performance_degradation", "quota_or_overage",
    "rate_limit", "rollback_request", "security_incident", "sso_configuration",
    "unclear_request", "webhook_issue",
})

#: FR-03 §3.1, verbatim. Matched case-insensitively on `ticket.text`, at word boundaries, and
#: allowing a plural and a hyphen (`matches_triggers`): the singular-only list read "please issue
#: refunds" as an answerable billing question (D-42).
MONEY_TRIGGERS = (
    "refund", "refunded", "refunding", "credit note", "credit back", "account credit",
    "service credit", "sla credit", "chargeback", "charge back", "dispute", "disputing",
    "disputed charge", "money back", "reimburse", "reimbursement", "compensation",
    "compensate", "waive", "waiver", "write off", "cancel the charge",
    "reverse the charge", "reversal", "goodwill",
)
#: FR-03 §3.2, verbatim. These target a commitment *we* would be making; a factual date question
#: ("when does my billing period end") matches none of them and stays answerable.
DATE_TRIGGERS = (
    "eta", "when will you fix", "when will this be fixed", "when it will be fixed",
    "by when", "firm date", "fix date", "delivery date", "commit to a date",
    "guarantee a date", "deadline for the fix", "promise", "sla breach",
)

#: D-16, and FR-02 §3's table: the order in which reasons rank. `reason` is the first that fired.
PRECEDENCE = (
    "kill_switch",                      # FR-16
    "private_data_in_ticket",           # FR-12, via extra_reasons
    "instruction_injection_detected",   # FR-12, via extra_reasons
    "malformed_ticket",                 # FR-07
    "must_escalate_intent",             # FR-09
    "money_commitment_requested",       # FR-03
    "date_commitment_requested",        # FR-03
    # D-42: below the money and date rules, so every rank D-16, FR-02 §3 and FR-12 §3.4 already
    # wrote down keeps its place. A refund request from a ticket the classifier could not place is
    # more usefully logged as a refund request than as an unrecognised intent.
    "unknown_intent",                   # FR-09 §4
    "text_truncated",                   # FR-07, D-14
    "no_retrieval",                     # FR-10
    "low_confidence",                   # FR-02
)

AUTO_RESPOND = "auto_respond"
ESCALATE = "escalate"

#: The Build Specification asks routing to record its reason "in language a support manager could
#: read", and `low_confidence` is not language. One written sentence per reason, no codes in them.
EXPLANATIONS = {
    "kill_switch": "Automatic replies are switched off, so this ticket goes to a person.",
    "private_data_in_ticket":
        "The ticket contains sensitive personal data, so a person handles it instead of the "
        "assistant.",
    "instruction_injection_detected":
        "The ticket tries to change how the assistant behaves, so a person reviews it.",
    "malformed_ticket": "The ticket could not be read reliably, so a person looks at it.",
    "unknown_intent":
        "The request is not one of the kinds the assistant is allowed to answer, so a person "
        "takes it.",
    "money_commitment_requested":
        "The customer asks for money back or another billing commitment, which only a person can "
        "promise.",
    "date_commitment_requested":
        "The customer asks us to commit to a date, which only a person can give.",
    "text_truncated": "The ticket was too long to read in full, so a person reads the rest.",
    "no_retrieval":
        "Nothing in the documentation was close enough to this question, so a person answers it.",
    "low_confidence":
        "The assistant was not sure enough of its answer to send it, so a person answers instead.",
}
#: `must_escalate_intent` gets its sentence from the intent, because "a security report" and "a
#: product change request" read differently to the person picking the ticket up (FR-09 §5).
INTENT_EXPLANATIONS = {
    "security_incident": "This is a security report, which always goes to a person.",
    "compliance_request":
        "This is a compliance or data-protection request, which always goes to a person.",
    "feature_request": "This asks for a change to the product, which always goes to a person.",
    "unclear_request":
        "The request was not clear enough to answer safely, so a person picks it up.",
}
ANSWERABLE = (
    "The documentation covers this question and the assistant is sure enough of the answer to "
    "send it."
)

#: Which requirement each reason serves, for `requirement_ids` on the log row (NFR-05).
REASON_REQUIREMENTS = {
    "kill_switch": ("FR-16",),
    "private_data_in_ticket": ("FR-12",),
    "instruction_injection_detected": ("FR-12",),
    "malformed_ticket": ("FR-07",),
    "must_escalate_intent": ("FR-09", "FR-08"),
    "unknown_intent": ("FR-09", "FR-08"),
    "money_commitment_requested": ("FR-03",),
    "date_commitment_requested": ("FR-03",),
    "text_truncated": ("FR-07",),
    "no_retrieval": ("FR-10",),
    "low_confidence": ("FR-02",),
}


@dataclass(frozen=True)
class RoutingDecision:
    """FR-02 §2: what routing decided, why, and everything the decision log needs.

    `auto_respond` is a licence to draft, not a sent reply: FR-11 drafts it and FR-12's guardrails
    can still block, after which the ticket ends as an escalation.
    """

    decision: str
    reason: str | None
    all_reasons: tuple[str, ...]
    explanation: str
    detail: str | None
    threshold_applied: float
    confidence: float
    kill_switch: bool
    requirement_ids: tuple[str, ...]

    @property
    def escalated(self) -> bool:
        return self.decision == ESCALATE

    def log_fields(self, classification: Classification | None = None) -> dict[str, Any]:
        """FR-13: the routing row, ready to splat into a `DecisionEntry`.

        Pass the classification and the row carries FR-02 §5's full set — the prediction, its
        alternatives and the urgency beside the threshold that was applied. Splatting both
        `log_fields()` results side by side instead raises `TypeError` on `detail`, which both
        halves fill; here the two details are joined and nothing is silently dropped.
        """
        fields: dict[str, Any] = {
            "decision": self.decision,
            "reason": self.reason,
            "all_reasons": list(self.all_reasons),
            "explanation": self.explanation,
            "detail": self.detail,
            "threshold_applied": self.threshold_applied,
            # The number the threshold was compared with. A row recording the floor but not the
            # value it was applied to cannot answer "was this decision right?" (FR-13 §3.2).
            "prediction_confidence": self.confidence,
            "kill_switch": self.kill_switch,
            "requirement_ids": list(self.requirement_ids),
            "prompt_version": None,  # FR-02 §5: no model call is made to decide
        }
        if classification is None:
            return fields
        merged = dict(classification.log_fields())
        details = [d for d in (merged.get("detail"), fields["detail"]) if d]
        merged.update(fields)
        merged["detail"] = "; ".join(details) or None
        # Routing states the confidence it actually compared (FR-02 §3.2 floors an unusable one at
        # 0.0); the classification's raw value would contradict the decision on the same row.
        merged["prediction_confidence"] = self.confidence
        return merged


class Router:
    """FR-02, FR-09, FR-03, FR-16: the routing decision. Takes settings and nothing else."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def decide(
        self,
        ticket: Ticket,
        classification: Classification | None,
        passages: Sequence[Passage] = (),
        extra_reasons: Iterable[str | tuple[str, str]] = (),
    ) -> RoutingDecision:
        """FR-02: answer or escalate, with every reason that applies, in precedence order.

        `extra_reasons` is how FR-12's pre-draft findings (private data, injection) take their
        ranked place in the table — wired up by backlog row 12 — without routing having to know how
        they were detected. It can only *add* a reason, and only one that has a rank: there is no
        argument that removes one, and an unranked name is refused rather than ranked last.
        """
        threshold = float(self._settings.confidence_threshold)
        confidence = _usable_confidence(classification)
        fired: dict[str, str | None] = {}

        # FR-16: checked first, freshly, on every decision — that is what makes "immediately" and
        # "without a redeploy" true.
        if self._switch_is_on():
            fired["kill_switch"] = f"switch file present at {self._settings.kill_switch_file}"

        for supplied in extra_reasons:
            name, detail = supplied if isinstance(supplied, tuple) else (supplied, None)
            if name not in PRECEDENCE:
                raise ValueError(
                    f"{name!r} is not a known routing reason; add it to PRECEDENCE with its rank "
                    "so the primary reason stays derivable (D-16)")
            fired.setdefault(name, detail)

        if ticket.is_malformed:
            fired["malformed_ticket"] = "ingest defects: " + ", ".join(ticket.defects)

        intent = classification.intent if classification is not None else None
        if intent in ALWAYS_ESCALATE_INTENTS:
            fired["must_escalate_intent"] = f"intent {intent}"
        elif intent not in KNOWN_INTENTS:
            # No classification at all lands here too, and escalates. FR-09 §4.
            fired["unknown_intent"] = f"intent {intent!r} is not one of the {len(KNOWN_INTENTS)}"

        text = ticket.text or ""
        for reason, table in (("money_commitment_requested", MONEY_TRIGGERS),
                              ("date_commitment_requested", DATE_TRIGGERS)):
            matched = matches_triggers(text, table)
            if matched:
                fired[reason] = "matched triggers: " + ", ".join(matched)

        if "text_truncated" in ticket.defects:
            fired["text_truncated"] = "the ticket text was longer than the cap and was cut"

        if not passages:
            fired["no_retrieval"] = "nothing cleared the relevance threshold"

        if confidence < threshold:
            fired["low_confidence"] = (
                f"calibrated confidence {confidence:.4f} is under the threshold {threshold:.4f}")

        ordered = tuple(reason for reason in PRECEDENCE if reason in fired)
        if not ordered:
            return RoutingDecision(
                decision=AUTO_RESPOND, reason=None, all_reasons=(), explanation=ANSWERABLE,
                detail=f"intent {intent} at confidence {confidence:.4f}, "
                       f"threshold {threshold:.4f}",
                threshold_applied=threshold, confidence=confidence, kill_switch=False,
                requirement_ids=("FR-02",))

        primary = ordered[0]
        _log.debug("%s escalated: %s (all: %s)", ticket.ticket_id, primary, ordered)
        return RoutingDecision(
            decision=ESCALATE,
            reason=primary,
            all_reasons=ordered,
            explanation=_explanation(primary, intent),
            detail="; ".join(f"{r}: {fired[r]}" for r in ordered if fired[r]) or None,
            threshold_applied=threshold,
            confidence=confidence,
            kill_switch="kill_switch" in fired,
            requirement_ids=_requirements(ordered),
        )

    # --- internals ------------------------------------------------------------------

    def _switch_is_on(self) -> bool:
        """FR-16 §3: one implementation of "is the switch on", in `Settings.kill_switch_on`.

        Two copies of this check is one too many: the first one here used `Path.exists()`, which
        reads an unreadable switch as *off* (the opposite of §4), while the copy in `Settings` had
        the same defect. There is now one, and it fails safe.
        """
        return self._settings.kill_switch_on


def _usable_confidence(classification: Classification | None) -> float:
    """FR-02 §3.2: a missing, NaN or out-of-range confidence counts as below T — never as high."""
    if classification is None:
        return 0.0
    value = classification.intent_confidence
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not 0.0 <= number <= 1.0:  # a NaN fails this comparison, which is the intent
        return 0.0
    return number


def matches_triggers(text: str, table: Sequence[str]) -> list[str]:
    """FR-03 §3: which triggers the text contains, in table order.

    Case-insensitive and bounded by word edges, so `eta` does not fire inside `beta`. Two forms
    beyond the literal phrase count, because the literal-only version let real money requests
    through to an answer (D-42):

    * **a plural** — "please issue refunds", "we are claiming the service credits", "these
      disputes", "we will raise chargebacks" all matched nothing before;
    * **a hyphen where the phrase has a space** — "write-off", "charge-back".

    Nothing else is inferred. The tables in the spec stay the source of truth for *which* phrases
    escalate; this function only decides how a phrase is recognised in running text.
    """
    low = text.lower()
    return [t for t in table if _pattern(t).search(low)]


@lru_cache(maxsize=256)
def _pattern(trigger: str) -> re.Pattern[str]:
    body = r"[\s-]+".join(re.escape(word) for word in trigger.split())
    return re.compile(rf"(?<!\w){body}(?:e?s)?(?!\w)")


def _explanation(reason: str, intent: str | None) -> str:
    if reason == "must_escalate_intent":
        return INTENT_EXPLANATIONS.get(
            intent or "", "This kind of request always goes to a person.")
    return EXPLANATIONS[reason]


def _requirements(reasons: Sequence[str]) -> tuple[str, ...]:
    """FR-02 is always in: it is the component that took the decision."""
    out = ["FR-02"]
    for reason in reasons:
        for requirement in REASON_REQUIREMENTS[reason]:
            if requirement not in out:
                out.append(requirement)
    return tuple(out)
