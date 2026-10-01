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
        # R7: a disputed charge, and a compliance-grade data question. This test exists to make
        # a new rank impossible to add without a routed case and a written sentence.
        "money_decision_required": lambda: decide(
            tmp_path, tkt=ticket(body="We were charged twice for the same month.")),
        "compliance_data_question": lambda: decide(
            tmp_path, cls=classification("data_residency"),
            tkt=ticket(body="Where is our data held? Our auditors have asked.")),
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


# --- R7: disputed charges, and compliance-grade data-residency questions ------------------


def test_T_R7_1_a_disputed_charge_escalates_even_without_the_word_dispute(tmp_path):
    """R7 (FR-03): VAL-0072 was auto-answered, and it is a disputed charge.

    "There are charges on our invoice for a service I do not believe we use." FR-03 says a
    dispute must reach a person, and the D-21 trigger table catches the *vocabulary* of a
    dispute — refund, chargeback, dispute — not a customer describing one in their own words.
    A customer disowning a charge is making the same claim without any of those words.
    """
    outcome = decide(tmp_path, tkt=ticket(
        body="There are charges on our invoice for a service I do not believe we use. "
             "Could you explain what these relate to?"))

    assert outcome.escalated
    assert outcome.reason == "money_decision_required"
    assert "money_decision_required" in outcome.all_reasons
    assert "do not believe we use" in (outcome.detail or ""), (
        "the log records which phrase fired, as the money rule already does")


#: One body per phrase, and **exactly one trigger in each**. The R7 review found that four of
#: the seven original bodies carried two triggers at once, so 22 of the 27 phrases could be
#: deleted with the whole suite still green — the same "two code paths look identical" trap this
#: backlog has now hit four times. `test_T_R7_1c` holds this list against the table itself, so a
#: phrase added without a body is a failure rather than a silent gap.
DISPUTE_BODIES = {
    "do not believe we use": "There is a line for a service I do not believe we use.",
    "don't believe we use": "There is a line for a service I don't believe we use.",
    "do not believe we used": "A line appeared for something I do not believe we used.",
    "don't believe we used": "A line appeared for something I don't believe we used.",
    "did not order": "We did not order that add-on.",
    "didn't order": "We didn't order that add-on.",
    "never ordered": "We never ordered that add-on.",
    "never signed up": "We never signed up for that add-on.",
    "do not recognise": "There is an item here I do not recognise.",
    "don't recognise": "There is an item here I don't recognise.",
    "do not recognize": "There is an item here I do not recognize.",
    "don't recognize": "There is an item here I don't recognize.",
    "should not be charged": "We should not be charged for a seat we removed.",
    "shouldn't be charged": "We shouldn't be charged for a seat we removed.",
    "should not have been charged": "We should not have been charged for that seat.",
    "charged twice": "We were charged twice for March.",
    "charged us twice": "You charged us twice for March.",
    "charged me twice": "You charged me twice for March.",
    "billed twice": "We were billed twice for March.",
    "billed us twice": "You billed us twice for March.",
    "billed me twice": "You billed me twice for March.",
    "double charged": "We were double charged for March.",
    "double billed": "We were double billed for March.",
    "duplicate charge": "There is a duplicate charge on the account.",
    "duplicate invoice": "There is a duplicate invoice on the account.",
    "still being billed": "We cancelled last month and are still being billed.",
    "still billed for": "We are still billed for a seat we removed.",
    "overcharged": "I think we have been overcharged this quarter.",
    "over charged": "I think we have been over charged this quarter.",
    "incorrect charge": "There is an incorrect charge on the account.",
    "wrong charge": "There is a wrong charge on the account.",
    "charge is not ours": "That charge is not ours.",
    "charges are not ours": "Those charges are not ours.",
    "line is not ours": "That line is not ours.",
}


@pytest.mark.parametrize("phrase", sorted(DISPUTE_BODIES))
def test_T_R7_1b_every_dispute_phrase_has_a_body_of_its_own(tmp_path, phrase):
    """R7's table, one phrase at a time, so a regression names the phrase it lost.

    The R7 review proved the first version of this test could not do that: four of its seven
    bodies carried two triggers, so deleting any of 22 phrases left the suite green.
    """
    from ticketing_agent.route import DISPUTE_TRIGGERS, matches_triggers

    body = DISPUTE_BODIES[phrase]
    matched = matches_triggers(body, DISPUTE_TRIGGERS)
    assert phrase in matched, (matched, body)
    strays = [o for o in matched if o != phrase and not matches_triggers(phrase, (o,))]
    assert not strays, (
        f"{phrase!r}: the body also matches {strays}, so deleting {phrase!r} would not "
        f"change this case")
    outcome = decide(tmp_path, tkt=ticket(body=body))
    assert outcome.escalated, body
    assert outcome.reason == "money_decision_required", body


def test_T_R7_1c_the_bodies_and_the_table_cannot_drift(tmp_path):
    """D-18: a phrase added to the table without a body is a gap nobody would see."""
    from ticketing_agent.route import DISPUTE_TRIGGERS

    assert set(DISPUTE_BODIES) == set(DISPUTE_TRIGGERS), (
        f"untested phrases: {set(DISPUTE_TRIGGERS) - set(DISPUTE_BODIES)}; "
        f"bodies for phrases that no longer exist: {set(DISPUTE_BODIES) - set(DISPUTE_TRIGGERS)}")


@pytest.mark.parametrize("body", [
    "SSO was never enabled on our org. How do I turn it on for the first time?",
    "We did not use the deprecated v1 endpoint, yet the warning still appears.",
    "That webhook endpoint is not ours, how do I remove it from the project?",
    "We did not use the full quota last month; how is the included allowance calculated?",
])
def test_T_R7_1d_a_question_that_is_not_about_a_charge_is_not_a_dispute(tmp_path, body):
    """R7 review (high): the first table carried four phrases that name no charge.

    Matched on every ticket, `did not use`, `never enabled` and `not ours` escalated ordinary
    documentation questions as money disputes — and the handover note then told the tier-two
    engineer that the customer was disputing their bill, for an SSO question. Every phrase in
    the table names a charge now, which is what gives the rule its context (the row's decision
    says "a **billing** ticket that disputes or disowns a charge").
    """
    for intent in ("sso_configuration", "api_usage_question", "configuration_help",
                   "quota_or_overage"):
        outcome = decide(tmp_path, cls=classification(intent=intent), tkt=ticket(body=body))
        assert not outcome.escalated, f"[{intent}] {body}"


def test_T_R7_2_a_compliance_grade_data_residency_question_escalates(tmp_path):
    """R7 (FR-09, the PRD's open question on data residency): VAL-0037 was auto-answered.

    "Are backups replicated outside our primary region? A compliance review has raised this and
    I need a definite answer." The PRD's open question says account-specific or
    compliance-grade location questions escalate: the documentation describes the product's
    general policy, and a compliance review needs a statement about *this account* that no
    article can ground.
    """
    outcome = decide(tmp_path, cls=classification(intent="data_residency"), tkt=ticket(
        subject="Question about backup regions",
        body="Are backups replicated outside our primary region? A compliance review has "
             "raised this and I need a definite answer."))

    assert outcome.escalated
    assert outcome.reason == "compliance_data_question"
    # On the segment, not on the whole string: `detail` always begins
    # "compliance_data_question: …", so `"compliance" in detail` was true for *any* escalation
    # with this reason and could not fail — emptying the table entirely left it passing.
    detail = outcome.detail or ""
    assert "account-specific: our primary region" in detail
    assert "compliance: compliance, compliance review" in detail


#: One body per phrase, carrying exactly that trigger — see `DISPUTE_BODIES` for why.
RESIDENCY_BODIES = {
    "our data": "Which region holds our data?",
    "our backups": "Which region holds our backups?",
    "our logs": "Which region stores our logs?",
    "our records": "Which region stores our records?",
    "our account": "Which region was our account created in?",
    "this account": "Which region was this account created in?",
    "our primary region": "Is anything replicated outside our primary region?",
    "our region": "Can you tell me our region?",
    "our customer data": "Which region holds our customer data?",
    "where is our": "Where is our information held?",
    "our files": "Which region stores our files?",
    "compliance": "Does compliance cover this region question?",
    "compliance review": "A compliance review has raised the question of region.",
    "compliance team": "Our compliance team has raised the question of region.",
    "for compliance": "We need the region for compliance purposes.",
    "auditor": "An auditor has raised the question of region.",
    "being audited": "We are being audited and need to state the region.",
    "audit requires": "The audit requires us to state the region.",
    "audit asks": "The audit asks which region is used.",
    "audit is asking": "The audit is asking which region is used.",
    "regulator": "The regulator has asked which region is used.",
    "regulatory requirement": "A regulatory requirement means we must state the region.",
    "legal review": "A legal review has raised the question of region.",
    "legal team": "Our legal team has raised the question of region.",
    "data protection officer": "The data protection officer has asked which region is used.",
    "attestation": "We need an attestation stating which region is used.",
}


@pytest.mark.parametrize("phrase", sorted(RESIDENCY_BODIES))
def test_T_R7_2b_every_residency_phrase_has_a_body_of_its_own(tmp_path, phrase):
    """Each phrase on its own, so deleting one fails a named case."""
    from ticketing_agent.route import (
        ACCOUNT_SPECIFIC_TRIGGERS,
        COMPLIANCE_TRIGGERS,
        matches_triggers,
    )

    body = RESIDENCY_BODIES[phrase]
    matched = (matches_triggers(body, ACCOUNT_SPECIFIC_TRIGGERS)
               + matches_triggers(body, COMPLIANCE_TRIGGERS))
    assert phrase in matched, (matched, body)
    # Some phrases nest — `compliance` is inside `compliance review`, `in writing` inside
    # `confirmation in writing` — so "exactly one trigger" is impossible for the longer ones.
    # What has to hold is that the body is *specifically* about this phrase: every other phrase
    # it matches is one the matcher finds **inside this phrase**, so no sibling is carrying it.
    # Containment is asked of `matches_triggers`, not of `in`: the matcher respects word
    # boundaries, so "we use" does not match "we used" even though one contains the other.
    strays = [o for o in matched if o != phrase and not matches_triggers(phrase, (o,))]
    assert not strays, (
        f"{phrase!r}: the body also matches {strays}, so deleting {phrase!r} would not "
        f"change this case")
    outcome = decide(tmp_path, cls=classification(intent="data_residency"),
                     tkt=ticket(body=body))
    assert outcome.escalated, body
    assert outcome.reason == "compliance_data_question", body


def test_T_R7_2e_the_phrases_that_are_shadowed_by_a_shorter_one_are_named(tmp_path):
    """Which phrases are redundant for *coverage*, and why they are kept anyway.

    `compliance review` can never decide a routing outcome that `compliance` would not already
    decide. It stays because the decision-log `detail` names what matched, and "compliance
    review" tells the tier-two engineer more than "compliance". Asserting the set makes that a
    deliberate, visible choice rather than an accident — and makes a *new* redundant phrase,
    which would be an accident, fail.
    """
    from ticketing_agent.route import (
        ACCOUNT_SPECIFIC_TRIGGERS,
        COMPLIANCE_TRIGGERS,
        DISPUTE_TRIGGERS,
        matches_triggers,
    )

    def shadowed(table):
        """Phrases the matcher would still find via a shorter phrase in the same table.

        Asked of `matches_triggers`, not of `in`: word boundaries mean "do not believe we use"
        does **not** match "do not believe we used", so the two are independent despite one
        containing the other as a string. Using `in` here claimed four phrases were redundant
        that are not.
        """
        return {p for p in table
                if matches_triggers(p, tuple(o for o in table if o != p))}

    assert shadowed(DISPUTE_TRIGGERS) == set(), (
        "no dispute phrase is reachable through another, so each one can decide a case")
    assert shadowed(ACCOUNT_SPECIFIC_TRIGGERS) == set()
    assert shadowed(COMPLIANCE_TRIGGERS) == {
        "compliance review", "compliance team", "for compliance"}


def test_T_R7_2c_the_residency_bodies_and_the_tables_cannot_drift(tmp_path):
    from ticketing_agent.route import ACCOUNT_SPECIFIC_TRIGGERS, COMPLIANCE_TRIGGERS

    table = set(ACCOUNT_SPECIFIC_TRIGGERS) | set(COMPLIANCE_TRIGGERS)
    assert set(RESIDENCY_BODIES) == table, (
        f"untested: {table - set(RESIDENCY_BODIES)}; stale: {set(RESIDENCY_BODIES) - table}")


@pytest.mark.parametrize("body", [
    "Do you offer an EU region for new projects? I need a definite answer before we pick one.",
    "Which regions are available, and is GDPR covered by the standard terms?",
    "How long is the retention period for audit records, and is it configurable?",
])
def test_T_R7_2d_impatience_and_product_nouns_are_not_a_compliance_process(tmp_path, body):
    """R7 review (medium): `gdpr`, `definite answer` and bare `audit` escalated policy questions.

    FR-09 §3.7 promises that a general policy question stays answerable, and these three broke
    that promise. GDPR is a product-policy noun, a definite answer is impatience, and "audit
    records" is a feature. Third time in this project that a bare word was the wrong unit
    (D-17 injection markers, D-51 phone numbers).
    """
    outcome = decide(tmp_path, cls=classification(intent="data_residency"), tkt=ticket(body=body))
    assert not outcome.escalated, body


def test_T_R7_3_an_explanatory_billing_question_still_answers(tmp_path):
    """R7: the rule is a floor on commitments, not a ban on billing questions.

    FR-03 says explanatory billing questions *may* be answered, and the PRD measured 88% of
    billing_query tickets answerable. A dispute rule that caught "how is proration calculated"
    would cost most of that.
    """
    for body in ("How is proration calculated when we upgrade mid-month?",
                 "Where can I see the breakdown of my invoice by service?",
                 "What is included in the usage limit on the business plan?",
                 "When does my billing period end?"):
        outcome = decide(tmp_path, tkt=ticket(body=body))
        assert not outcome.escalated, body
        assert outcome.reason is None, body


def test_T_R7_4_a_general_data_residency_question_still_answers(tmp_path):
    """R7: a question about the product's published policy is what the docs are for."""
    for body in ("Which regions do you offer for deployment?",
                 "Do you have a data centre in Australia?",
                 "What regions are available on the business plan?"):
        outcome = decide(tmp_path, cls=classification(intent="data_residency"),
                         tkt=ticket(body=body))
        assert not outcome.escalated, body


def test_T_R7_4b_the_new_rules_are_scoped_to_the_intents_they_belong_to(tmp_path):
    """The data-residency rule is about `data_residency` tickets, not about the word "our".

    "Our deployment keeps failing" is a deployment question that happens to say "our", and a
    rule that read it as a compliance request would escalate most of the corpus.
    """
    outcome = decide(tmp_path, cls=classification(intent="deployment_failure"),
                     tkt=ticket(body="Our deployment keeps failing on the health check."))
    assert not outcome.escalated


def test_T_R7_4c_the_dispute_rule_outranks_nothing_it_should_not(tmp_path):
    """D-16: a new reason takes a rank and leaves every existing rank where it was."""
    from ticketing_agent.route import PRECEDENCE

    assert PRECEDENCE.index("money_commitment_requested") < PRECEDENCE.index(
        "money_decision_required")
    assert PRECEDENCE.index("money_decision_required") < PRECEDENCE.index(
        "date_commitment_requested")
    assert PRECEDENCE.index("compliance_data_question") < PRECEDENCE.index("unknown_intent")
    # A refund request that also disowns the charge is logged as the refund request.
    outcome = decide(tmp_path, tkt=ticket(
        body="We were charged twice, so please refund the duplicate."))
    assert outcome.reason == "money_commitment_requested"
    assert "money_decision_required" in outcome.all_reasons, "and neither fact is lost"


def test_T_R7_4d_the_new_reasons_carry_a_sentence_a_manager_can_read(tmp_path):
    """FR-13 §3.2: every reason has one written sentence, and no codes in it."""
    from ticketing_agent.route import EXPLANATIONS

    for reason in ("money_decision_required", "compliance_data_question"):
        assert reason in EXPLANATIONS, reason
        sentence = EXPLANATIONS[reason]
        assert sentence.endswith(".") and sentence[0].isupper(), reason
        assert "_" not in sentence, f"{reason}: a code leaked into the sentence"


def test_T_R7_5_the_sweep_reports_what_the_new_rules_cost(tmp_path):
    """R7 asked for this **reported, not asserted** — so the test is of the reporting.

    The answer is a judgement for the author: both rules escalate tickets the labels call
    answerable, and on the validation set the labels contradict themselves on exactly those
    tickets (D-70). A test that asserted agreement would be asserting a contradiction.
    """
    import json as _json

    from scripts.dispute_rule_sweep import main as sweep_main
    from scripts.dispute_rule_sweep import sweep

    source = tmp_path / "sweep.json"
    source.write_text(_json.dumps([
        {"ticket_id": "S-DISPUTE", "channel": "email", "subject": "Invoice",
         "body": "There are charges for a service I do not believe we use.",
         "received_at": "2026-05-01T09:00:00Z",
         "labels": {"intent": "billing_query", "expected_route": "auto_respond"}},
        {"ticket_id": "S-REFUND", "channel": "email", "subject": "Invoice",
         "body": "We were charged twice, so please refund the duplicate.",
         "received_at": "2026-05-01T09:00:00Z",
         "labels": {"intent": "billing_query", "expected_route": "escalate"}},
        {"ticket_id": "S-RESIDENCY", "channel": "email", "subject": "Regions",
         "body": "Where is our data held? Our auditors have asked.",
         "received_at": "2026-05-01T09:00:00Z",
         "labels": {"intent": "data_residency", "expected_route": "escalate"}},
        {"ticket_id": "S-PLAIN", "channel": "email", "subject": "Regions",
         "body": "Which regions do you offer on the business plan?",
         "received_at": "2026-05-01T09:00:00Z",
         "labels": {"intent": "data_residency", "expected_route": "auto_respond"}},
    ]), encoding="utf-8")

    result = sweep(source)
    dispute = result["rules"]["money_decision_required"]
    assert dispute["matched"] == 2
    assert dispute["labels_say_answer"] == 1, "S-DISPUTE: the rule escalates what a label answers"
    assert dispute["labels_agree_escalate"] == 1
    assert dispute["labels_absent"] == 0
    assert result["rules"]["compliance_data_question"]["already_caught_by_the_money_rule"] is None, (
        "money triggers say nothing about a residency ticket, so the field is omitted there "
        "rather than printed as a misleading zero")
    assert dispute["already_caught_by_the_money_rule"] == 1, (
        "S-REFUND says 'refund' too, so the new rule changes nothing for it — reporting that "
        "is the difference between a rule's reach and its effect")

    residency = result["rules"]["compliance_data_question"]
    assert [r["ticket_id"] for r in residency["tickets"]] == ["S-RESIDENCY"], (
        "S-PLAIN asks about the published policy and must not match")

    assert sweep_main(["--input", str(source)]) == 0
    assert sweep_main(["--input", str(tmp_path / "missing.json")]) == 2, (
        "a bad path is a clean exit, not a traceback")


def test_T_R7_5b_the_sweep_reads_the_rules_that_run(tmp_path):
    """D-18: the fixtures' expectations come from the tables, never from a second copy.

    A sweep with its own hardcoded phrase list would keep reporting on a rule that had changed.
    """
    import scripts.dispute_rule_sweep as module
    from ticketing_agent import route

    assert module.DISPUTE_TRIGGERS is route.DISPUTE_TRIGGERS
    assert module.COMPLIANCE_TRIGGERS is route.COMPLIANCE_TRIGGERS
    assert module.ACCOUNT_SPECIFIC_TRIGGERS is route.ACCOUNT_SPECIFIC_TRIGGERS


def test_T_R7_2f_the_written_confirmation_wording_is_deliberately_not_a_trigger(tmp_path):
    """The one place R7 is narrower than a reviewer suggested, pinned so it is a choice.

    A request for a written statement a third party will rely on *is* the archetype of
    "compliance-grade", and adding it would catch 6 tickets the labels agree should escalate.
    But 17 tickets in the supplied data say "written confirmation" and 11 of them are labelled
    answerable, so it buys 6 agreements for 11 disagreements — and R7's decision did not list
    it. Inventing a phrase that costs label agreement is the author's call, not mine. If it is
    added, this test is the one to delete, deliberately.
    """
    outcome = decide(tmp_path, cls=classification(intent="data_residency"), tkt=ticket(
        body="One of our customers has asked for written confirmation of where their data is "
             "physically stored. Could you point me to something I can share with them?"))
    assert not outcome.escalated, (
        "if this now escalates, the phrase was added — check it was a decision and not a drift")
