"""Routing acceptance tests: FR-02, FR-09, FR-16 and FR-03's runtime half.

Specs: docs/specs/FR-02.md (the decision and the threshold), FR-09.md (the four always-escalate
intents), FR-16.md (the kill switch), FR-03.md §3 (money and date commitments).

Offline and cheap: routing makes no model call and no retrieval call, so these tests construct
`Classification` objects directly rather than running the classifier.
"""
import json
import math
import os
from pathlib import Path

import pytest

from ticketing_agent.classify import Classification
from ticketing_agent.config import Settings
from ticketing_agent.ingest import normalise_ticket
from ticketing_agent.retrieve import Passage
from ticketing_agent.route import (
    ALWAYS_ESCALATE_INTENTS,
    PRECEDENCE,
    Router,
    RoutingDecision,
)

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "data" / "development_tickets.json"
FIXTURES = ROOT / "tests" / "fixtures"


def settings(tmp_path, **overrides):
    values = {
        "model_name": "test-model",
        "confidence_threshold": 0.80,
        "relevance_threshold": 0.25,
        "kill_switch_file": tmp_path / "KILL_SWITCH",
        "decision_log_path": tmp_path / "decisions.db",
    }
    values.update(overrides)
    return Settings(**values)


def router(tmp_path, **overrides):
    return Router(settings(tmp_path, **overrides))


def ticket(body="Our invoice is higher than usual and I cannot see why.", **overrides):
    entry = {"ticket_id": "T-1", "channel": "email", "subject": "Invoice question",
             "body": body, "received_at": "2026-05-01T09:00:00Z",
             "customer_tier": "standard", "customer_region": "europe",
             "language_fluency": "fluent"}
    entry.update(overrides)
    return normalise_ticket(entry, index=0)


def classification(intent="billing_query", confidence=0.95, urgency="medium"):
    return Classification(
        intent=intent, intent_confidence=confidence,
        intent_alternatives=(("quota_or_overage", 0.02),),
        urgency=urgency, urgency_confidence=0.6,
        urgency_reason="closest to DEV-0001 (medium, 0.80)",
        evidence=(("DEV-0001", "medium", 0.80),), model_fingerprint="test-model-fp")


def passages(count=2):
    return tuple(
        Passage(chunk_id=f"DOC-BILL-001#{n}", doc_id="DOC-BILL-001", title="Invoices",
                heading="Resolution", text="Open the usage breakdown.", score=0.7 - n * 0.1,
                rank=n + 1)
        for n in range(count)
    )


#: `None` is a meaningful value for `cls` (FR-09 §4: no classification at all), so the default
#: cannot be `None` or the case would silently be replaced by a healthy classification.
DEFAULT = object()


def decide(tmp_path, *, tkt=None, cls=DEFAULT, found=None, extra=(), **overrides):
    return router(tmp_path, **overrides).decide(
        tkt if tkt is not None else ticket(),
        classification() if cls is DEFAULT else cls,
        found if found is not None else passages(),
        extra_reasons=extra,
    )


# --- FR-02, the threshold and the decision -------------------------------------------


@pytest.mark.parametrize(("confidence", "expected"), [
    (0.95, "auto_respond"),
    (0.80, "auto_respond"),   # equal to T answers: T is the lowest confidence that may answer
    (0.7999, "escalate"),
    (0.0, "escalate"),
])
def test_T_FR02_1_the_threshold_decides(tmp_path, confidence, expected):
    result = decide(tmp_path, cls=classification(confidence=confidence))
    assert result.decision == expected
    if expected == "escalate":
        assert result.reason == "low_confidence"


@pytest.mark.parametrize("confidence", [None, math.nan, -0.5, 1.5])
def test_T_FR02_2_a_missing_or_absurd_confidence_counts_as_below(tmp_path, confidence):
    """FR-02's own words: "a missing confidence counts as below T"."""
    result = decide(tmp_path, cls=classification(confidence=confidence))
    assert result.decision == "escalate"
    assert result.reason == "low_confidence"
    assert result.confidence == 0.0, "an unusable confidence is 0.0, never high"


def test_T_FR02_3_the_threshold_is_recorded_on_every_decision(tmp_path):
    answered = decide(tmp_path, confidence_threshold=0.5)
    escalated = decide(tmp_path, cls=classification(confidence=0.1), confidence_threshold=0.5)

    assert answered.threshold_applied == 0.5
    assert escalated.threshold_applied == 0.5, "an escalation records what it was measured against"
    assert answered.decision == "auto_respond"


def test_T_FR02_4_the_same_ticket_routes_the_same_way_twice(tmp_path):
    """A5 and NFR-08: the same input gives the same routing decision."""
    one = router(tmp_path)
    first = one.decide(ticket(), classification(), passages())
    second = one.decide(ticket(), classification(), passages())
    assert first == second


def test_T_FR02_5_every_reason_has_a_sentence_a_manager_could_read(tmp_path):
    """Build Spec, Route: "records the reason for the decision in language a support manager
    could read". `low_confidence` is not language."""
    seen = {}
    cases = {
        "kill_switch": lambda: _with_switch(tmp_path),
        "malformed_ticket": lambda: decide(tmp_path, tkt=ticket(body="", subject="")),
        "must_escalate_intent": lambda: decide(tmp_path, cls=classification("security_incident")),
        "money_commitment_requested": lambda: decide(
            tmp_path, tkt=ticket(body="Please refund the overage charge.")),
        "date_commitment_requested": lambda: decide(
            tmp_path, tkt=ticket(body="When will this be fixed? We need a firm date.")),
        "unknown_intent": lambda: decide(tmp_path, cls=classification("not_in_the_taxonomy")),
        "text_truncated": lambda: decide(tmp_path, tkt=ticket(body="x" * 9000)),
        "no_retrieval": lambda: decide(tmp_path, found=()),
        "low_confidence": lambda: decide(tmp_path, cls=classification(confidence=0.1)),
        # FR-12 hands these two in at row 12; their rank and their sentence are fixed here so the
        # guardrails are written against a contract that already holds.
        "private_data_in_ticket": lambda: decide(
            tmp_path, extra=(("private_data_in_ticket", "national_id"),)),
        "instruction_injection_detected": lambda: decide(
            tmp_path, extra=("instruction_injection_detected",)),
    }
    assert set(cases) == set(PRECEDENCE), (
        "every rank in the precedence table needs a routed case and its own sentence: "
        f"{set(PRECEDENCE) - set(cases)} untested")
    for reason, make in cases.items():
        result = make()
        assert result.reason == reason, f"{reason}: got {result.reason}"
        seen[reason] = result.explanation
        assert result.explanation, reason
        assert result.explanation[0].isupper() and result.explanation.endswith("."), reason
        for jargon in (reason, "_", "confidence <", "None", "FR-"):
            assert jargon not in result.explanation, f"{reason}: {result.explanation!r}"
    assert len(set(seen.values())) == len(seen), "each reason needs its own sentence"


def _with_switch(tmp_path):
    (tmp_path / "KILL_SWITCH").write_text("", encoding="utf-8")
    try:
        return decide(tmp_path)
    finally:
        (tmp_path / "KILL_SWITCH").unlink()


def test_T_FR02_6_precedence_is_the_decision_table(tmp_path):
    """D-16: several rules can fire; the primary reason must not depend on evaluation order."""
    malformed_and_money = ticket(body="Please refund this.", channel="sms")  # unknown channel
    result = decide(tmp_path, tkt=malformed_and_money,
                    cls=classification("security_incident", confidence=0.05), found=())

    assert result.reason == "malformed_ticket", "the highest firing rule wins"
    assert set(result.all_reasons) >= {
        "malformed_ticket", "must_escalate_intent", "money_commitment_requested",
        "no_retrieval", "low_confidence"}
    order = [PRECEDENCE.index(r) for r in result.all_reasons]
    assert order == sorted(order), "all_reasons is in precedence order"


def test_T_FR02_6b_the_code_order_is_the_order_the_specs_wrote_down():
    """The review's finding: row 9 added `unknown_intent` at a rank no table named, which moved
    FR-03's ranks and would have broken row 12's fixtures (T-FR12-21 derives `expected_reason`
    from these tables). D-16 exists to stop that, so the tables and the code are compared here."""
    import re as _re

    def ranks(spec: Path) -> list[str]:
        """Every numbered row of a precedence table, by its rank. The rank column being a digit
        is the only filter — matching on the word "reason" skipped the two rows whose condition
        says `extra_reasons`, which is how this test first passed the wrong tables."""
        rows = []
        for line in spec.read_text(encoding="utf-8").splitlines():
            if not line.startswith("| "):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 3 or not cells[0].isdigit():
                continue
            names = _re.findall(r"`([a-z_]+)`", cells[1])
            if names:
                rows.append((int(cells[0]), names))
        return [name for _, names in sorted(rows) for name in names]

    fr02 = ranks(ROOT / "docs" / "specs" / "FR-02.md")
    fr12 = ranks(ROOT / "docs" / "specs" / "FR-12.md")
    assert fr02, "FR-02 §3's precedence table could not be read"
    assert list(PRECEDENCE) == fr02, f"code vs FR-02 §3: {set(PRECEDENCE) ^ set(fr02)}"
    assert list(PRECEDENCE) == fr12, f"code vs FR-12 §3.4 (D-16): {set(PRECEDENCE) ^ set(fr12)}"


def test_T_FR02_7_nothing_can_turn_an_escalation_into_an_answer():
    import inspect

    from ticketing_agent import route

    params = set(inspect.signature(Router.decide).parameters)
    assert not params & {"force", "force_answer", "override", "ignore", "allow"}
    source = Path(route.__file__).read_text(encoding="utf-8")
    for smell in ("force_answer", "ignore_kill_switch", "skip_rules", "bypass"):
        assert smell not in source, smell


def test_T_FR02_8_a_decision_fits_the_decision_log(tmp_path):
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog

    result = decide(tmp_path)
    tkt = ticket()
    entry = DecisionEntry(
        ticket_id=tkt.ticket_id, source_index=tkt.source_index, stage="routing",
        **result.log_fields(), **tkt.log_fields_for_log(),
    )
    assert entry.decision == "auto_respond"
    assert entry.threshold_applied == 0.80, "FR-13 rejects an auto_respond row without it"
    assert entry.explanation

    with DecisionLog(tmp_path / "decisions.db", run_id="routing") as log:
        assert log.record(entry) == 1


def test_T_FR02_9_routing_needs_no_model_and_no_clock(tmp_path):
    """Routing must keep working during a provider outage (A11) and must not read a clock."""
    from ticketing_agent import route

    source = Path(route.__file__).read_text(encoding="utf-8")
    for smell in ("ProviderClient", "complete(", "time.time", "datetime.now", "random"):
        assert smell not in source, f"routing must not use {smell}"
    assert decide(tmp_path).decision == "auto_respond"


def test_T_FR02_10_every_development_ticket_gets_exactly_one_decision(tmp_path):
    one = router(tmp_path)
    entries = json.loads(DEV.read_text(encoding="utf-8"))
    decisions = []
    for index, entry in enumerate(entries):
        tkt = normalise_ticket(entry, index=index)
        decisions.append(one.decide(tkt, classification(), passages()))

    assert len(decisions) == len(entries)
    assert all(d.decision in {"auto_respond", "escalate"} for d in decisions)
    assert all(isinstance(d, RoutingDecision) for d in decisions)


# --- FR-09, the four intents ---------------------------------------------------------


@pytest.mark.parametrize("intent", sorted(ALWAYS_ESCALATE_INTENTS))
@pytest.mark.parametrize("confidence", [0.99, 0.01])
def test_T_FR09_1_the_four_intents_always_escalate(tmp_path, intent, confidence):
    result = decide(tmp_path, cls=classification(intent, confidence=confidence))
    assert result.decision == "escalate"
    assert result.reason == "must_escalate_intent", "whatever the confidence"
    assert result.all_reasons[0] == "must_escalate_intent"
    assert intent in (result.detail or "")


def test_T_FR09_2_other_intents_are_not_escalated_by_this_rule(tmp_path):
    for intent in ("billing_query", "deployment_failure", "api_key_issue", "onboarding"):
        result = decide(tmp_path, cls=classification(intent))
        assert "must_escalate_intent" not in result.all_reasons
        assert result.decision == "auto_respond"


def test_T_FR09_3_the_list_cannot_be_shortened():
    import inspect

    from ticketing_agent import route

    assert ALWAYS_ESCALATE_INTENTS == frozenset({
        "security_incident", "compliance_request", "feature_request", "unclear_request"})
    params = set(inspect.signature(Router.__init__).parameters)
    assert not params & {"always_escalate", "escalate_intents", "must_escalate"}
    source = Path(route.__file__).read_text(encoding="utf-8")
    assert "ALWAYS_ESCALATE_INTENTS" in source
    assert "os.environ" not in source and "getenv" not in source


def test_T_FR09_4_zero_auto_responses_for_the_four_intents(tmp_path):
    """The PRD's criterion: zero auto-responses to must-escalate tickets, in any run."""
    one = router(tmp_path)
    entries = json.loads(DEV.read_text(encoding="utf-8"))
    answered_must_escalate = []
    for index, entry in enumerate(entries):
        intent = entry["labels"]["intent"]
        tkt = normalise_ticket(entry, index=index)
        result = one.decide(tkt, classification(intent, confidence=0.99), passages())
        if intent in ALWAYS_ESCALATE_INTENTS and result.decision == "auto_respond":
            answered_must_escalate.append(entry["ticket_id"])
    assert answered_must_escalate == []


def test_T_FR09_5_the_classifier_fallback_escalates_by_rule(tmp_path):
    """FR-08 falls back to unclear_request precisely so this rule catches it."""
    fallback = Classification(
        intent="unclear_request", intent_confidence=0.0, intent_alternatives=(),
        urgency="medium", urgency_confidence=0.0, urgency_reason="fell back: no text",
        fallback=True)
    result = decide(tmp_path, cls=fallback)
    assert result.decision == "escalate"
    assert "must_escalate_intent" in result.all_reasons


def test_T_FR09_6_an_unrecognised_intent_escalates(tmp_path):
    result = decide(tmp_path, cls=classification("something_new_entirely"))
    assert result.decision == "escalate"
    assert "unknown_intent" in result.all_reasons

    from ticketing_agent.route import KNOWN_INTENTS

    labelled = {e["labels"]["intent"] for e in json.loads(DEV.read_text(encoding="utf-8"))}
    assert KNOWN_INTENTS == labelled, "the taxonomy in code drifted from the supplied data"
    assert len(KNOWN_INTENTS) == 22
    assert ALWAYS_ESCALATE_INTENTS <= KNOWN_INTENTS


def test_the_trigger_tables_are_the_ones_row_2_mirrored_from_the_spec():
    """Row 2 checks its mirror against docs/specs/FR-03.md in both directions; this ties the
    implementation to that mirror, so a third copy cannot drift unnoticed."""
    from tests.test_engineered_fixtures import DATE_TRIGGERS, MONEY_TRIGGERS
    from ticketing_agent import route

    assert route.MONEY_TRIGGERS == MONEY_TRIGGERS
    assert route.DATE_TRIGGERS == DATE_TRIGGERS


def test_T_FR09_7_the_rule_needs_no_retrieval_and_no_confidence(tmp_path):
    result = decide(tmp_path, cls=classification("security_incident", confidence=None), found=())
    assert result.reason == "must_escalate_intent", "above no_retrieval and low_confidence"


def test_T_FR09_8_an_intent_rule_outranks_the_money_rule(tmp_path):
    result = decide(tmp_path, tkt=ticket(body="Please refund this and also we were breached."),
                    cls=classification("security_incident"))
    assert result.reason == "must_escalate_intent"
    assert "money_commitment_requested" in result.all_reasons, "neither fact is lost"


# --- FR-03's runtime half ------------------------------------------------------------


@pytest.mark.parametrize("body", [
    "Please refund the overage charge on this invoice.",
    "We would like a credit note for the duplicate subscription.",
    "We are disputing these charges and will raise a chargeback.",
    "We are claiming the service credit set out in our contract.",
    "Please waive the late fee and write off the balance.",
    # D-42: every one of these routed to auto_respond before the matcher saw plurals and hyphens.
    "Please issue refunds for both duplicated accounts.",
    "We are claiming the service credits set out in our contract.",
    "These disputes have been open for a week.",
    "We will raise chargebacks with our bank.",
    "We need a write-off of the balance and a charge-back if not.",
])
def test_T_FR03_3_money_requests_escalate(tmp_path, body):
    result = decide(tmp_path, tkt=ticket(body=body))
    assert result.decision == "escalate"
    assert result.reason == "money_commitment_requested"
    assert result.detail, "the matched trigger belongs in the log"


def test_T_FR03_4_an_explanatory_question_with_a_refund_ask_still_escalates(tmp_path):
    """The hard case from row 2: the answerable first half must not rescue it."""
    result = decide(tmp_path, tkt=ticket(
        body="Could you explain how proration was calculated? If it is wrong, please refund "
             "the difference."))
    assert result.decision == "escalate"
    assert result.reason == "money_commitment_requested"


def test_T_FR03_5_a_date_demand_escalates_but_a_date_question_does_not(tmp_path):
    demand = decide(tmp_path, tkt=ticket(
        body="Deploys still fail. When will this be fixed? We need a firm date."))
    assert demand.reason == "date_commitment_requested"

    question = decide(tmp_path, tkt=ticket(
        body="When does my billing period end? The invoice dates do not line up."))
    assert question.decision == "auto_respond", "a factual date question is answerable"


def test_T_FR03_6_the_money_rule_fires_before_any_model_call(tmp_path):
    """FR-03 §3: pure text matching, so a refund request escalates during a provider outage."""
    result = decide(tmp_path, tkt=ticket(body="Refund this please."),
                    cls=classification(confidence=None), found=())
    assert result.reason == "money_commitment_requested"


def test_explanatory_billing_fixtures_are_still_answered(tmp_path):
    """The control case: an over-broad money rule fails here."""
    corpus = json.loads((FIXTURES / "money_commitment_tickets.json").read_text(encoding="utf-8"))
    one = router(tmp_path)
    for index, entry in enumerate(corpus):
        if entry["synthetic"]["category"] != "explanatory_billing":
            continue
        tkt = normalise_ticket(entry, index=index)
        result = one.decide(tkt, classification(entry["labels"]["intent"]), passages())
        assert result.decision == "auto_respond", f"{entry['ticket_id']}: {result.reason}"


def test_the_engineered_money_corpus_routes_as_its_fixtures_declare(tmp_path):
    corpus = json.loads((FIXTURES / "money_commitment_tickets.json").read_text(encoding="utf-8"))
    one = router(tmp_path)
    for index, entry in enumerate(corpus):
        category = entry["synthetic"]["category"]
        if category not in {"money_commitment", "date_commitment", "known_over_escalation"}:
            continue
        tkt = normalise_ticket(entry, index=index)
        result = one.decide(tkt, classification(entry["labels"]["intent"]), passages())
        assert result.decision == "escalate", entry["ticket_id"]
        assert result.reason == entry["synthetic"]["expected_reason"], entry["ticket_id"]


# --- FR-16, the kill switch ----------------------------------------------------------


def test_T_FR16_1_the_switch_stops_every_answer(tmp_path):
    before = decide(tmp_path)
    assert before.decision == "auto_respond"

    (tmp_path / "KILL_SWITCH").write_text("", encoding="utf-8")
    after = decide(tmp_path)
    assert after.decision == "escalate"
    assert after.reason == "kill_switch"
    assert after.kill_switch is True
    assert "switched off" in after.explanation


def test_T_FR16_2_the_switch_outranks_every_other_reason(tmp_path):
    (tmp_path / "KILL_SWITCH").write_text("", encoding="utf-8")
    result = decide(tmp_path, cls=classification("security_incident", confidence=0.1), found=())
    assert result.reason == "kill_switch"
    assert set(result.all_reasons) >= {
        "kill_switch", "must_escalate_intent", "no_retrieval", "low_confidence"}


def test_T_FR16_3_nothing_is_dropped_with_the_switch_on(tmp_path):
    (tmp_path / "KILL_SWITCH").write_text("", encoding="utf-8")
    one = router(tmp_path)
    entries = json.loads(DEV.read_text(encoding="utf-8"))[:60]
    results = [one.decide(normalise_ticket(e, index=i), classification(), passages())
               for i, e in enumerate(entries)]

    assert len(results) == len(entries)
    assert all(r.decision == "escalate" for r in results)
    assert all(r.reason == "kill_switch" for r in results)
    assert all(r.kill_switch for r in results)


def test_T_FR16_4_the_switch_takes_effect_without_a_restart(tmp_path):
    """"Without a redeploy" means the same Router object must see the change."""
    one = router(tmp_path)
    assert one.decide(ticket(), classification(), passages()).decision == "auto_respond"

    (tmp_path / "KILL_SWITCH").write_text("", encoding="utf-8")
    assert one.decide(ticket(), classification(), passages()).reason == "kill_switch"

    (tmp_path / "KILL_SWITCH").unlink()
    assert one.decide(ticket(), classification(), passages()).decision == "auto_respond"


def test_T_FR16_5_nothing_overrides_the_switch():
    import inspect

    from ticketing_agent import route

    params = set(inspect.signature(Router.decide).parameters)
    assert not params & {"ignore_kill_switch", "force", "override_kill_switch"}
    source = Path(route.__file__).read_text(encoding="utf-8")
    assert "ignore_kill_switch" not in source


def test_T_FR16_6_a_directory_at_the_switch_path_counts_as_on(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    switch = blocked / "KILL_SWITCH"
    switch.mkdir()  # a directory where a file was expected
    result = decide(tmp_path, kill_switch_file=switch)
    assert result.reason == "kill_switch"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores the permissions this test relies on")
def test_T_FR16_6b_an_unreadable_switch_path_counts_as_on(tmp_path):
    """If we cannot tell whether automation was switched off, the safe reading is that it was.

    The earlier version of this test put a *directory* at the switch path, which `exists()`
    happily reports as present — so it passed while the fail-safe branch was dead code, and
    `Path.exists()` (which is `os.path.exists`, and swallows every OSError) read a switch inside
    an unreadable directory as **off**. This one makes the stat call actually fail.
    """
    blocked = tmp_path / "locked"
    blocked.mkdir()
    switch = blocked / "KILL_SWITCH"
    switch.write_text("", encoding="utf-8")
    os.chmod(blocked, 0o000)
    try:
        with pytest.raises(OSError):
            switch.stat()  # the premise: this environment really does deny the stat
        result = decide(tmp_path, kill_switch_file=switch)
    finally:
        os.chmod(blocked, 0o700)
    assert result.reason == "kill_switch", "an unreadable switch must not read as off"
    assert result.kill_switch is True


def test_T_FR16_6c_one_implementation_decides_whether_the_switch_is_on(tmp_path, monkeypatch):
    """Two copies of this check is how the defect above survived: both used `exists()`.

    Routing must read `Settings.kill_switch_on` and not re-implement it, so that fixing the check
    once fixes it everywhere. Faking that one property has to change the routing decision.
    """
    on = settings(tmp_path, kill_switch_file=tmp_path / "SWITCH")
    assert on.kill_switch_on is False
    (tmp_path / "SWITCH").write_text("", encoding="utf-8")
    assert on.kill_switch_on is True

    monkeypatch.setattr(Settings, "kill_switch_on", property(lambda self: True))
    assert decide(tmp_path).reason == "kill_switch", "routing must ask Settings, not the filesystem"


# --- the fields a decision hands to the log (FR-02 §5) --------------------------------


def test_T_FR02_8b_an_escalation_row_carries_the_confidence_it_was_measured_on(tmp_path):
    """An auditor needs both numbers: the floor that applied and the value compared with it."""
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog

    cls = classification(confidence=0.42)
    result = decide(tmp_path, cls=cls)
    tkt = ticket()
    fields = result.log_fields(cls)
    assert fields["threshold_applied"] == 0.80
    assert fields["prediction_confidence"] == 0.42
    assert fields["prediction_value"] == "billing_query", "the prediction comes from FR-08"
    assert fields["urgency"] == "medium"
    assert cls.log_fields()["detail"] in fields["detail"], "neither detail is dropped"
    assert result.detail in fields["detail"]

    entry = DecisionEntry(ticket_id=tkt.ticket_id, source_index=tkt.source_index,
                          stage="routing", **fields, **tkt.log_fields_for_log())
    assert entry.decision == "escalate" and entry.reason == "low_confidence"
    with DecisionLog(tmp_path / "decisions.db", run_id="routing") as log:
        assert log.record(entry) == 1


def test_T_FR02_8c_an_unusable_confidence_is_logged_as_the_zero_it_was_treated_as(tmp_path):
    cls = classification(confidence=None)
    fields = decide(tmp_path, cls=cls).log_fields(cls)
    assert fields["prediction_confidence"] == 0.0, (
        "the row must state the number the decision used, not one that contradicts it")


# --- the extra_reasons contract FR-12 will use (row 12) --------------------------------


def test_extra_reasons_can_only_add_a_reason(tmp_path):
    result = decide(tmp_path, extra=(("private_data_in_ticket", "national_id"),),
                    cls=classification(confidence=0.99))
    assert result.decision == "escalate"
    assert result.reason == "private_data_in_ticket", "rank 2, above every rule below it"
    assert "national_id" in (result.detail or "")

    both = decide(tmp_path, extra=("private_data_in_ticket", "instruction_injection_detected"),
                  tkt=ticket(body="Please refund this.", channel="sms"))
    assert both.reason == "private_data_in_ticket"
    assert both.all_reasons.index("instruction_injection_detected") < \
        both.all_reasons.index("malformed_ticket")


def test_an_unranked_reason_name_is_refused_rather_than_ranked_last(tmp_path):
    """A reason with no rank would make the primary reason depend on evaluation order (D-16).

    The names come from code, never from ticket data, so this is a programming error and is
    raised. FR-15's `provider_unavailable` and FR-12's draft-side reasons are *not* routing
    reasons: they are recorded on their own rows, which is why they are not ranked here.
    """
    with pytest.raises(ValueError, match="not a known routing reason"):
        decide(tmp_path, extra=("provider_unavailable",))


def test_no_classification_at_all_still_escalates(tmp_path):
    """FR-09 §4: a ticket that could not be classified must not be answerable."""
    result = decide(tmp_path, cls=None)
    assert result.decision == "escalate"
    assert result.reason == "unknown_intent"
    assert result.confidence == 0.0
