"""FR-12 acceptance tests T-FR12-6 … T-FR12-19, T-FR12-21 (docs/specs/FR-12.md).

Offline: the grounding judge runs through `FakeTransport`, so PR-03's real prompt, its parsing and
its exact-quote check are exercised without a network or a key.

The Governance Framework's line is the standard these hold to: "A guardrail is a check that runs
on every response and can block it. Guardrails that only run in testing are not guardrails; they
are tests."
"""
import json
from pathlib import Path

import pytest

from ticketing_agent.config import Settings
from ticketing_agent.guardrails import (
    CHECK_ORDER,
    OVERLAP_THRESHOLD,
    GroundingJudge,
    Guardrails,
    check_ticket,
)
from ticketing_agent.ingest import normalise_ticket
from ticketing_agent.provider import FakeTransport, ProviderClient, ProviderTimeout
from ticketing_agent.retrieve import Passage

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

PASSAGES = (
    Passage(chunk_id="DOC-BILL-001#2", doc_id="DOC-BILL-001",
            title="Invoices and usage breakdown", heading="Resolution",
            text="Open Billing then Usage breakdown to see the charge for each service. "
                 "The breakdown is generated nightly and invoices are finalised three days "
                 "after the period ends.", score=0.7, rank=1),
    Passage(chunk_id="DOC-BILL-003#1", doc_id="DOC-BILL-003",
            title="Usage limits, overage and spend controls", heading="Overview",
            text="Spend caps apply at the organisation level and stop new usage when reached.",
            score=0.5, rank=2),
)
GROUNDED = "Open Billing then Usage breakdown to see the charge for each service."


def settings(tmp_path, **overrides):
    values = {"llm_api_key": "test-key-not-a-real-one", "model_name": "test-model",
              "llm_cache_path": tmp_path / "llm_cache.sqlite", "llm_max_retries": 0}
    values.update(overrides)
    return Settings(**values)


def ticket(body="Where can I see my invoice breakdown?", **overrides):
    entry = {"ticket_id": "T-1", "channel": "email", "subject": "Invoice question",
             "body": body, "received_at": "2026-05-01T09:00:00Z", "customer_tier": "standard",
             "customer_region": "europe", "language_fluency": "fluent"}
    entry.update(overrides)
    return normalise_ticket(entry, index=0)


def verdicts(*supported: bool, quote: str = GROUNDED) -> str:
    return json.dumps({"results": [
        {"i": i, "supported": ok, "quote": quote if ok else ""}
        for i, ok in enumerate(supported)]})


def payload(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}], "model": "test-model",
            "system_fingerprint": "fp_test"}


def judge(tmp_path, script, **overrides) -> tuple[GroundingJudge, FakeTransport]:
    transport = FakeTransport(script)
    return (GroundingJudge(ProviderClient(settings(tmp_path, **overrides), transport=transport)),
            transport)


def guardrails(tmp_path, script=None, **overrides) -> Guardrails:
    script = list(script) if script is not None else [payload(verdicts(True))]
    return Guardrails(judge=judge(tmp_path, script, **overrides)[0])


def check(tmp_path, reply, sentences=None, citations=("DOC-BILL-001#2",), script=None,
          confidence=0.95, threshold_applied=0.85, retrieved=PASSAGES, **overrides):
    sentences = sentences if sentences is not None else (
        (reply, list(citations)),)
    one = guardrails(tmp_path, script or [payload(verdicts(*([True] * len(sentences))))],
                     **overrides)
    return one.check_draft(ticket=ticket(), reply=reply, sentences=sentences,
                           retrieved=retrieved, citations=tuple(citations),
                           confidence=confidence, threshold_applied=threshold_applied)


# --- T-FR12-6, T-FR12-7: the pre-draft rules, which cost no model call ----------------


def test_T_FR12_6_an_injection_ticket_escalates_before_any_model_call(tmp_path):
    from ticketing_agent.route import Router

    corpus = json.loads((FIXTURES / "injection_tickets.json").read_text("utf-8"))
    attempts = [e for e in corpus if e["synthetic"]["category"] == "injection"]
    assert attempts

    router = Router(Settings(model_name="test-model", confidence_threshold=0.85,
                             kill_switch_file=tmp_path / "absent"))
    transport = FakeTransport([])  # any provider call at all would raise
    for entry in attempts:
        tkt = normalise_ticket(entry, index=0)
        found = check_ticket(tkt)
        assert "instruction_injection_detected" in dict(found), entry["ticket_id"]

        decision = router.decide(tkt, None, PASSAGES, extra_reasons=found)
        assert decision.decision == "escalate"
        assert "instruction_injection_detected" in decision.all_reasons
        # "...and zero model calls": the rule is pure text matching, so a hostile ticket costs
        # nothing and no draft is ever written (NFR-07). The transport proves it.
        assert transport.requests == [], entry["ticket_id"]


def test_T_FR12_6b_a_lookalike_is_not_escalated_by_this_rule(tmp_path):
    corpus = json.loads((FIXTURES / "injection_tickets.json").read_text("utf-8"))
    lookalikes = [e for e in corpus if e["synthetic"]["category"] == "injection_lookalike"]
    assert lookalikes

    for entry in lookalikes:
        found = dict(check_ticket(normalise_ticket(entry, index=0)))
        assert "instruction_injection_detected" not in found, entry["ticket_id"]


def test_T_FR12_7_a_secret_in_the_ticket_escalates_and_is_never_sent(tmp_path):
    corpus = json.loads((FIXTURES / "pii_tickets.json").read_text("utf-8"))
    secrets = [e for e in corpus if e["synthetic"]["category"] == "pii"]
    assert secrets

    transport = FakeTransport([])  # "no provider call is made with that text"
    for entry in secrets:
        tkt = normalise_ticket(entry, index=0)
        found = dict(check_ticket(tkt))
        assert "private_data_in_ticket" in found, entry["ticket_id"]
        assert tkt.body not in found["private_data_in_ticket"], "the detail names the pattern"
    assert transport.requests == []


def test_T_FR12_7b_an_ordinary_email_address_does_not_escalate_a_ticket(tmp_path):
    found = dict(check_ticket(ticket(body="Please reply to dana@example.com about my invoice.")))
    assert "private_data_in_ticket" not in found, (
        "a customer's own address is normal in a ticket; it is the reply that must not carry it")


# --- T-FR12-8, T-FR12-9: private data in the draft -----------------------------------


@pytest.mark.parametrize(("reply", "pattern"), [
    ("Write to dana@example.com and we will sort it out.", "email"),
    ("Call the team on +1 555 0100 for help.", "phone"),
    ("Your card 4111 1111 1111 1111 was declined.", "card_number"),
    ("Your reference is 000-00-0000 on the account.", "national_id"),
    ("Set api_key=EXAMPLEEXAMPLE1234 in the console.", "credential"),
    ("Connect to the host at 10.0.0.5 instead.", "private_ip"),
])
def test_T_FR12_8_private_data_in_a_draft_blocks_it(tmp_path, reply, pattern):
    report = check(tmp_path, reply)
    assert report.passed is False
    assert report.reason == "private_data_in_draft"
    assert "private_data" in report.blocking
    result = next(r for r in report.results if r.name == "private_data")
    assert pattern in result.detail


def test_T_FR12_9_the_detail_names_the_pattern_and_never_the_value(tmp_path):
    """The decision log must not become the leak it was written to prevent (NFR-04)."""
    report = check(tmp_path, "Write to dana@example.com or call +1 555 0100.")
    whole = json.dumps(report.log_fields())

    assert "dana@example.com" not in whole
    assert "555" not in whole
    assert "email" in whole and "phone" in whole


def test_T_FR12_9b_a_block_is_never_a_redaction(tmp_path):
    """NFR-04: the guardrail blocks, it does not redact. No 'cleaned' reply may be offered."""
    from ticketing_agent import guardrails as module

    report = check(tmp_path, "Write to dana@example.com about it.")
    assert not hasattr(report, "redacted"), "a redacted reply is not this module's to produce"
    source = Path(module.__file__).read_text("utf-8")
    for smell in ("def redact", "redact_reply", "scrub_reply", "sanitise_reply"):
        assert smell not in source, smell


# --- T-FR12-10, T-FR12-11: grounding -------------------------------------------------


def test_T_FR12_10_a_citation_that_was_not_retrieved_fails_grounding(tmp_path):
    report = check(tmp_path, GROUNDED, citations=("DOC-BILL-009#1",))
    assert report.passed is False
    assert report.reason == "ungrounded_draft"
    assert "DOC-BILL-009#1" in next(r for r in report.results if r.name == "grounding").detail


def test_T_FR12_11_an_unsupported_claim_fails_and_an_honest_refusal_passes(tmp_path):
    invented = "Invoices can be paid in instalments over six months."
    failed = check(tmp_path, invented, script=[payload(verdicts(False))])
    assert failed.passed is False
    assert failed.reason == "ungrounded_draft"

    # FR-12 §3.2.2's own words: "a draft that says plainly it does not know, with no claims,
    # passes". `GROUNDED` is a supported factual sentence, so asserting this with that string
    # proved the wrong half (D-51).
    refusal = ("I could not find an article that answers this, so I have passed it to a person "
               "who will follow up.")
    honest = check(tmp_path, refusal, script=[payload(verdicts())])
    assert honest.passed is True, honest.reason
    assert honest.model_calls == 0, "a refusal makes no claim, so there is nothing to judge"

    passed = check(tmp_path, GROUNDED)
    assert passed.passed is True, passed.reason


def test_T_FR12_11b_pleasantries_are_not_claims(tmp_path):
    """D-22: applying an overlap floor to every sentence would reject 37-48% of what CloudServe's
    own senior agents wrote. The exemption list is the substance of the check."""
    reply = ("Thank you for getting in touch. " + GROUNDED
             + " Let me know if that does not help.")
    sentences = ((reply, ["DOC-BILL-001#2"]),)
    report = check(tmp_path, reply, sentences=sentences,
                   script=[payload(verdicts(True, True, True))])
    assert report.passed is True, report.reason


def test_T_FR12_11c_the_overlap_floor_is_measured_against_everything_retrieved(tmp_path):
    """D-22 re-measured at row 5: against a single chunk, 0.3 rejects 9% of expert claims. The
    floor is the union of the retrieved passages; the citation check is the strict half."""
    drawing_on_both = ("Spend caps apply at the organisation level and stop new usage when "
                       "reached.")
    report = check(tmp_path, drawing_on_both, citations=("DOC-BILL-001#2",))
    assert report.passed is True, report.reason
    assert 0.0 < OVERLAP_THRESHOLD <= 0.5


def test_T_FR12_11d_an_invented_claim_fails_the_floor_even_if_the_judge_says_yes(tmp_path):
    """The judge is a model; the floor is arithmetic. Either one failing blocks the reply."""
    invented = "Enterprise customers receive a dedicated migration engineer for ninety days."
    report = check(tmp_path, invented, script=[payload(verdicts(True, quote=GROUNDED))])
    assert report.passed is False
    assert report.reason == "ungrounded_draft"
    assert "overlap" in next(r for r in report.results if r.name == "grounding").detail


def test_T_FR12_11e_a_quote_the_passages_do_not_contain_is_not_support(tmp_path):
    """PR-03's own design: code checks the quote is an exact substring of a cited passage."""
    report = check(tmp_path, "Invoices are issued weekly on Mondays.",
                   script=[payload(verdicts(True, quote="Invoices are issued weekly"))])
    assert report.passed is False
    assert report.reason == "ungrounded_draft"


# --- T-FR12-12: commitments ----------------------------------------------------------


@pytest.mark.parametrize("reply", [
    "A refund has been issued to your account.",
    "The issue has been fixed on our side.",
    "We can give a specific delivery date for a fix.",
    "We have issued a refund for the overage.",
    "This is now fixed, you can retry.",
    "It will be fixed by Friday.",
    "We guarantee this will not happen again.",
])
def test_T_FR12_12_a_commitment_in_the_draft_blocks_it(tmp_path, reply):
    report = check(tmp_path, reply, script=[payload(verdicts(True))])
    assert report.passed is False
    assert "tone_and_scope" in report.blocking
    if report.reason != "commitment_in_draft":
        assert report.blocking.index("tone_and_scope") > 0, (
            f"{reply!r} blocked for {report.reason} before the commitment check")


def test_T_FR12_12c_a_grounded_commitment_blocks_with_its_own_reason(tmp_path):
    """Every reply in the case above fails `grounding` first, so nothing exercised the reason
    `commitment_in_draft` — a typo in CHECK_REASON would have shipped green (D-51)."""
    passage = (Passage(chunk_id="DOC-BILL-001#2", doc_id="DOC-BILL-001", title="Invoices",
                       heading="Resolution",
                       text="A refund has been issued to your account for the duplicate charge.",
                       score=0.7, rank=1),)
    report = check(tmp_path, "A refund has been issued to your account.", retrieved=passage,
                   script=[payload(verdicts(True, quote="A refund has been issued"))])

    assert report.passed is False
    assert report.reason == "commitment_in_draft"
    assert report.blocking == ("tone_and_scope",)
    assert report.log_fields()["all_reasons"] == ["commitment_in_draft"]


def test_T_FR12_12b_the_three_must_not_claim_phrases_come_from_the_data(tmp_path):
    from ticketing_agent.guardrails import MUST_NOT_CLAIM

    demanded = {p for g in json.loads(
        (ROOT / "data" / "ground_truth_responses.json").read_text("utf-8"))
        for p in g["must_not_claim"]}
    assert demanded <= set(MUST_NOT_CLAIM), "the guardrail must carry what the data demands"


# --- T-FR12-13, T-FR12-14, T-FR12-16: the five checks, always, unskippable -----------


def test_T_FR12_13_all_five_checks_report_on_every_draft(tmp_path):
    """Build Specification, Validate: it "records what it checked and what it found, whether or
    not it blocked"."""
    for reply, script in ((GROUNDED, [payload(verdicts(True))]),
                          ("Write to dana@example.com.", [payload(verdicts(True))])):
        report = check(tmp_path, reply, script=script)
        assert tuple(r.name for r in report.results) == CHECK_ORDER
        assert len(CHECK_ORDER) == 5
        assert all(r.detail for r in report.results), "each says what it checked"
        assert list(report.log_fields()["guardrail_results"])


def test_T_FR12_14_a_check_that_raises_is_a_failure_not_a_pass(tmp_path):
    class Exploding:
        def check(self, *args, **kwargs):
            raise RuntimeError("the judge exploded")

    one = Guardrails(judge=Exploding())
    report = one.check_draft(ticket=ticket(), reply=GROUNDED,
                             sentences=((GROUNDED, ["DOC-BILL-001#2"]),),
                             retrieved=PASSAGES, citations=("DOC-BILL-001#2",),
                             confidence=0.95, threshold_applied=0.85)

    assert report.passed is False
    grounding = next(r for r in report.results if r.name == "grounding")
    assert grounding.passed is False
    assert "check_error:RuntimeError" in grounding.detail
    assert report.reason == "check_error", (
        "a check that raised is not a draft that was ungrounded (FR-12 §5)")


def test_T_FR12_14b_a_judge_that_answered_badly_is_not_a_judge_that_was_down(tmp_path):
    """FR-12 §4 separates a provider error from invalid model output. Logging both as
    `provider_unavailable` hid prompt drift behind an availability signal (D-51)."""
    report = check(tmp_path, GROUNDED,
                   script=[payload("not json"), payload("still not json")])

    assert report.passed is False
    assert report.reason == "check_error"
    assert report.model_calls == 2, "both the first call and the repair were real requests"
    assert report.prompt_version == "PR-03 v1.0"


def test_T_FR12_15_an_unavailable_judge_blocks_the_reply(tmp_path):
    report = check(tmp_path, GROUNDED, script=[ProviderTimeout("the judge timed out")])
    assert report.passed is False
    assert report.reason == "provider_unavailable"
    assert next(r for r in report.results if r.name == "grounding").passed is False
    # The criterion says the deterministic half still ran: the other four checks report, and a
    # citation that does not resolve is still caught without any judge at all.
    assert len(report.results) == 5
    assert all(r.detail for r in report.results)
    unresolved = check(tmp_path, GROUNDED, citations=("DOC-NOPE-001#1",),
                       script=[ProviderTimeout("the judge timed out")])
    assert unresolved.reason == "ungrounded_draft", "the quote-free half needs no provider"


def test_T_FR12_16b_grounding_cannot_be_turned_into_a_no_op(tmp_path):
    """D-50 made this the only check standing between an unsupported sentence and a customer, so
    a caller that hands over no sentences — or the wrong ones — must not silently disable it."""
    invented = "Refunds are always processed within one hour, guaranteed by us."

    none_given = check(tmp_path, invented, sentences=(), script=[payload(verdicts())])
    assert none_given.passed is False
    assert none_given.reason == "ungrounded_draft"

    wrong_list = check(tmp_path, invented, sentences=((GROUNDED, ["DOC-BILL-001#2"]),),
                       script=[payload(verdicts(True))])
    assert wrong_list.passed is False
    assert "not in the reply" in next(
        r for r in wrong_list.results if r.name == "grounding").detail


def test_T_FR12_16c_a_leaking_reply_is_never_sent_to_the_provider(tmp_path):
    """§3.1.2's rationale is that a secret must never be sent to a provider. A reply that has
    already failed `private_data` is certainly blocked, so the judge is not asked (D-51)."""
    one = guardrails(tmp_path, [payload(verdicts(True))])
    transport = one._judge._client._transport
    report = one.check_draft(
        ticket=ticket(), reply="Write to dana@example.com about the breakdown.",
        sentences=(("Write to dana@example.com about the breakdown.", ["DOC-BILL-001#2"]),),
        retrieved=PASSAGES, citations=("DOC-BILL-001#2",),
        confidence=0.95, threshold_applied=0.85)

    assert report.reason == "private_data_in_draft"
    assert transport.requests == [], "the secret was not transmitted"
    assert len(report.results) == 5, "every check still reports"


def test_T_FR12_16_nothing_can_skip_a_check(tmp_path):
    import inspect

    from ticketing_agent import guardrails as module

    params = set(inspect.signature(Guardrails.check_draft).parameters)
    assert not params & {"skip", "checks", "only", "strict", "disable", "enabled"}
    source = Path(module.__file__).read_text("utf-8")
    # Identifier-shaped, not substrings: `bypass your` is one of the injection markers the
    # module is *looking for*, and a crude search for "bypass" flagged the table itself.
    for smell in ("disable_guardrails", "strict=False", "skip_checks", "def bypass",
                  "_bypass", "bypass=", "if not enabled", "guardrails_enabled"):
        assert smell not in source, smell


# --- T-FR12-17 (confidence floor), T-FR12-18, T-FR12-19 ------------------------------


def test_T_FR12_13b_the_confidence_floor_needs_evidence_it_was_applied(tmp_path):
    """The framework's words: "a missing confidence score is not a high one"."""
    assert check(tmp_path, GROUNDED, threshold_applied=None).reason == "threshold_not_applied"
    assert check(tmp_path, GROUNDED, confidence=None).reason == "threshold_not_applied"
    assert check(tmp_path, GROUNDED, confidence=0.5, threshold_applied=0.85).reason == (
        "threshold_not_applied"), "a reply below the floor cannot be released"
    assert check(tmp_path, GROUNDED, confidence=0.85, threshold_applied=0.85).passed is True


def test_T_FR12_18_a_block_is_logged_before_the_escalation_is_acted_on(tmp_path):
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog

    report = check(tmp_path, "Write to dana@example.com.")
    tkt = ticket()
    order: list[str] = []

    with DecisionLog(tmp_path / "decisions.db", run_id="guardrails") as log:
        entry = DecisionEntry(ticket_id=tkt.ticket_id, source_index=tkt.source_index,
                              **report.log_fields(), **tkt.log_fields_for_log())
        assert entry.decision == "block", "the framework's third action_taken (FR-13 §3.1)"
        log.perform(entry, lambda: order.append("escalated"))
        written = [r for r in log.rows() if r["decision"] == "block"]

    assert order == ["escalated"], "the row is written first, then the action is taken"
    assert len(written) == 1, "exactly one row per block"
    assert written[0]["reason"] == "private_data_in_draft"
    assert written[0]["stage"] == "validation"
    assert "dana@example.com" not in json.dumps(written[0]), "the log is not the leak (NFR-04)"


def test_T_FR12_19_the_grounding_judge_is_deterministic_and_cached(tmp_path):
    one = guardrails(tmp_path, [payload(verdicts(True))])
    kwargs = {"ticket": ticket(), "reply": GROUNDED,
              "sentences": ((GROUNDED, ["DOC-BILL-001#2"]),), "retrieved": PASSAGES,
              "citations": ("DOC-BILL-001#2",), "confidence": 0.95, "threshold_applied": 0.85}

    first = one.check_draft(**kwargs)
    second = one.check_draft(**kwargs)

    assert first.passed is second.passed is True
    assert first.log_fields()["guardrail_results"] == second.log_fields()["guardrail_results"]
    assert second.cache_hits == 1, "the second verdict is served from the cache (NFR-08)"
    assert first.prompt_version == "PR-03 v1.0"


def test_T_FR12_19b_the_judge_asks_at_temperature_zero_with_the_draft_as_data(tmp_path):
    one_judge, transport = judge(tmp_path, [payload(verdicts(True))])
    one_judge.check(sentences=((GROUNDED, ["DOC-BILL-001#2"]),), retrieved=PASSAGES)

    request = transport.requests[0]
    assert request["temperature"] == 0
    user = next(m["content"] for m in request["messages"] if m["role"] == "user")
    assert "<draft>" in user and GROUNDED in user
    assert "DOC-BILL-001#2" in user


def test_T_FR12_23b_the_engineered_draft_corpus_agrees_with_the_shipped_checks(tmp_path):
    """FR-12 §2 says the draft corpus exists so every post-draft check can be tested offline.
    Nothing ran it through `check_draft`, and it disagreed: both drafts the corpus declares clean
    were blocked (D-51). This is the draft-side half of the table move this row asked for."""
    from ticketing_agent.guardrails import JudgeVerdict

    class Permissive:
        """A judge that agrees with everything, so the deterministic half is what is measured."""

        def check(self, sentences, retrieved, indices=None):
            return JudgeVerdict(unsupported=(), detail="judged", prompt_version="PR-03 v1.0")

    one = Guardrails(judge=Permissive())
    drafts = json.loads((FIXTURES / "draft_replies.json").read_text("utf-8"))
    assert len(drafts) >= 11

    for fixture in drafts:
        retrieved = tuple(
            Passage(chunk_id=r["chunk_id"], doc_id=r["doc_id"], title=r.get("title", ""),
                    heading=r.get("heading", ""), text=r["text"], score=0.5, rank=n + 1)
            for n, r in enumerate(fixture["retrieved"]))
        report = one.check_draft(
            ticket=ticket(), reply=fixture["text"],
            sentences=((fixture["text"], fixture["citations"]),), retrieved=retrieved,
            citations=tuple(fixture["citations"]), confidence=0.95, threshold_applied=0.85)

        expected = fixture["synthetic"]["expected_reason"]
        assert report.passed is (expected is None), (
            f"{fixture['draft_id']}: passed={report.passed}, corpus expects {expected}")
        assert report.reason == expected, fixture["draft_id"]


def test_T_FR12_23c_fr06s_own_lines_are_exempt_by_shape_not_by_a_copied_string():
    """FR-06 may change its wording behind DISCLOSURE_VERSION. While the exemptions were copied
    string literals, the day it changed every reply in a run would have failed grounding."""
    from ticketing_agent.generate import DISCLOSURE, HUMAN_ROUTE
    from ticketing_agent.guardrails import is_claim

    for line in (DISCLOSURE, HUMAN_ROUTE, "Hi Kavya,", "Hello,",
                 "Based on: Invoices and usage breakdown (DOC-BILL-001)",
                 "This reply was drafted automatically.",
                 "If you would like a person to look at it, reply and we will pass it on."):
        assert not is_claim(line), line
    assert is_claim("Spend caps apply at the organisation level.")


def test_T_FR12_18b_a_guardrail_row_projects_into_the_governance_record(tmp_path):
    """FR-12's criterion is that the block is *recorded*. The Governance Framework's projection
    crashed on every guardrail row, because the row carried triples where FR-13 declares pairs."""
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog, governance_record

    report = check(tmp_path, "Write to dana@example.com.")
    tkt = ticket()
    with DecisionLog(tmp_path / "decisions.db", run_id="guardrails") as log:
        log.record(DecisionEntry(ticket_id=tkt.ticket_id, source_index=tkt.source_index,
                                 **report.log_fields(), **tkt.log_fields_for_log()))
        row = next(r for r in log.rows() if r["decision"] == "block")

    record = governance_record(row)
    assert record["action_taken"] == "block"
    # All five names, each a pass or a fail: the framework's minimum record, and the shape FR-13
    # §2 declares. This reply fails two checks — it leaks an address *and* says nothing the
    # passages support — and the record has to carry both.
    assert set(record["guardrail_results"]) == set(CHECK_ORDER)
    assert record["guardrail_results"]["private_data"] == "fail"
    assert record["guardrail_results"]["confidence_floor"] == "pass"
    assert record["reason"] and "dana@example.com" not in json.dumps(record)
    assert row["all_reasons"] == ["private_data_in_draft", "ungrounded_draft"], (
        "a draft failing two checks must not lose one of them (FR-12 §5)")


@pytest.mark.parametrize("reply", [
    "Call Dana on 0207 946 0123 about it.",
    "Call (212) 555-0199 for help.",
    "Your reference is 123456789 on the account.",
])
def test_T_FR12_8b_the_draft_side_patterns_catch_the_ordinary_forms(tmp_path, reply):
    """NFR-04 is "zero", so a false negative costs more than a false positive. The first version
    of these patterns wanted an international dialling code and a dashed SSN (D-51)."""
    assert check(tmp_path, reply).reason == "private_data_in_draft", reply


def test_T_FR12_8c_a_version_number_is_not_an_ip_address(tmp_path):
    """The other direction: escalating every answer that names a 10.x version costs resolution."""
    report = check(tmp_path, "Upgrade to version 10.1.2 of the agent to pick up the fix.",
                   script=[payload(verdicts(True, quote="Open Billing"))])
    assert "private_data" not in report.blocking


def test_the_tables_that_can_drift_are_checked_against_the_spec():
    """T-FR12-5 checks three tables against the spec in both directions; the ones most able to
    drift silently had no such test, which is how five undocumented exemptions got in (D-51)."""
    from ticketing_agent.guardrails import (
        CLAIM_EXEMPT_OPENERS,
        COMMITMENT_PHRASES,
        LEAK_MARKERS,
        MUST_NOT_CLAIM,
        OVERLAP_THRESHOLD,
        REFUSAL_OPENERS,
    )

    spec = (ROOT / "docs" / "specs" / "FR-12.md").read_text("utf-8")
    for phrase in (*MUST_NOT_CLAIM, *COMMITMENT_PHRASES, *LEAK_MARKERS,
                   *CLAIM_EXEMPT_OPENERS, *REFUSAL_OPENERS):
        assert phrase in spec, f"{phrase!r} is in the code and not in FR-12's spec"
    assert f"{OVERLAP_THRESHOLD}" in spec


# --- T-FR12-21: the corpus's declared reasons are what the code produces -------------


def test_T_FR12_21_the_corpus_reasons_are_what_the_rules_actually_produce(tmp_path):
    from ticketing_agent.classify import Classification
    from ticketing_agent.route import PRECEDENCE, Router

    # The reasons a ticket's own *text* produces. The rest of D-16's table is about the
    # classification and the retrieval, which these fixtures deliberately hold constant.
    PRECEDENCE = tuple(r for r in PRECEDENCE
                       if r in {"private_data_in_ticket", "instruction_injection_detected",
                                "money_commitment_requested", "date_commitment_requested"})
    router = Router(Settings(model_name="test-model", confidence_threshold=0.5,
                             kill_switch_file=tmp_path / "absent"))
    classification = Classification(intent="billing_query", intent_confidence=0.99,
                                    intent_alternatives=(), urgency="medium",
                                    urgency_confidence=0.5, urgency_reason="fixed for this test")

    for name in ("pii_tickets.json", "injection_tickets.json", "money_commitment_tickets.json"):
        for entry in json.loads((FIXTURES / name).read_text("utf-8")):
            declared = entry["synthetic"]["expected_all_reasons"]
            tkt = normalise_ticket(entry, index=0)

            # The router's own assembly, not a re-implementation of it: the criterion is that
            # the *logged row* matches, and reconstructing the reasons here would stay green if
            # `Router.decide` stopped emitting one (D-51).
            decision = router.decide(tkt, classification, PASSAGES,
                                     extra_reasons=check_ticket(tkt))
            from_ticket = [r for r in decision.all_reasons if r in set(PRECEDENCE)]

            assert from_ticket == declared, f"{entry['ticket_id']}: {from_ticket} != {declared}"
            expected = entry["synthetic"]["expected_reason"]
            if expected is not None:
                assert decision.reason == expected, entry["ticket_id"]
                assert decision.decision == "escalate"


def test_T_FR12_11f_a_judge_quote_joining_two_spans_is_still_support(tmp_path):
    """D-53, from the first real harness run: PR-03 answers "copy the exact words" by copying two
    spans joined with `; `, and the joined string is a substring of nothing. Three of four drafts
    were blocked by that alone — sentences with 0.74 and 1.00 overlap, rejected on punctuation.
    """
    joined = ("Open Billing then Usage breakdown to see the charge for each service; "
              "The breakdown is generated nightly")
    report = check(tmp_path, GROUNDED, script=[payload(verdicts(True, quote=joined))])
    assert report.passed is True, report.reason


def test_T_FR12_11g_a_quote_whose_second_half_is_invented_is_not_support(tmp_path):
    """The check is not loosened: every substantial part still has to be in the passages."""
    half = ("Open Billing then Usage breakdown to see the charge for each service; "
            "refunds are issued automatically within one hour")
    report = check(tmp_path, GROUNDED, script=[payload(verdicts(True, quote=half))])
    assert report.passed is False
    assert report.reason == "ungrounded_draft"


def test_T_FR12_11h_punctuation_and_case_do_not_decide_grounding(tmp_path):
    """A curly apostrophe or a doubled space is not evidence of invention."""
    from ticketing_agent.guardrails import _comparable, _quote_found

    corpus = _comparable("Verify the key's status and expiry on the API keys page.")
    assert _quote_found("Verify the key’s  status and expiry", corpus)
    assert not _quote_found("Verify the key was refunded", corpus)
