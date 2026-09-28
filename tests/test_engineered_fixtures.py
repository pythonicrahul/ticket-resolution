"""Contract tests for the engineered ticket corpus (backlog row 2, B-17).

Acceptance tests T-FR03-1, T-FR03-2, T-FR07-23, T-FR12-1 … T-FR12-5, T-FR12-17 from
docs/specs/FR-03.md, docs/specs/FR-07.md and docs/specs/FR-12.md. Offline: no network, no
API key, no model calls.

The corpus is test *data*, so these tests check the data is what the specs say it is: every
positive carries the trigger its category claims, every lookalike carries none, and the
mirrored pattern tables still match the specs. The detectors themselves are rows 9 and 12;
the tables below move into src/ticketing_agent/guardrails.py there (FR-12 §7).
"""
import json
import re
from pathlib import Path

import pytest

from ticketing_agent.guardrails import (
    EMAIL,
    INJECTION_MARKERS,
    markers_in,
    phones_in,
    secrets_in,
)
from ticketing_agent.ingest import load_tickets, normalise_ticket
from ticketing_agent.route import DATE_TRIGGERS, MONEY_TRIGGERS, matches_triggers
from ticketing_agent.route import PRECEDENCE as ROUTE_PRECEDENCE

#: D-16's order, restricted to the reasons a ticket's own text can produce. Taken from the shipped
#: table rather than restated, so a fixture's expected reason cannot drift from the router's.
PRECEDENCE = tuple(r for r in ROUTE_PRECEDENCE
                   if r in {"private_data_in_ticket", "instruction_injection_detected",
                            "money_commitment_requested", "date_commitment_requested"})

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SPEC_FR03 = ROOT / "docs" / "specs" / "FR-03.md"
SPEC_FR12 = ROOT / "docs" / "specs" / "FR-12.md"

#: The files that must satisfy the schema contract. malformed_tickets.json is exempt by
#: design: it holds entries that are deliberately not tickets at all.
CONFORMING_FILES = ("pii_tickets.json", "injection_tickets.json", "money_commitment_tickets.json")
MALFORMED_FILE = "malformed_tickets.json"
DRAFTS_FILE = "draft_replies.json"

TICKET_KEYS = {"ticket_id", "channel", "subject", "body", "received_at", "customer_id",
               "customer_name", "customer_tier", "customer_region", "language_fluency",
               "labels", "history", "synthetic"}
LABEL_KEYS = {"intent", "urgency", "expected_route", "answerable_from_docs",
              "expected_doc_ids", "must_not_auto_respond"}
SYNTHETIC_KEYS = {"category", "why", "expected_reason", "expected_all_reasons",
                  "expected_guardrail", "requirement_ids", "secrets", "contact_details",
                  "markers", "money_triggers", "date_triggers"}
DRAFT_KEYS = {"draft_id", "for_ticket", "text", "citations", "retrieved", "synthetic"}
DRAFT_SYNTHETIC_KEYS = {"category", "why", "expected_passed", "expected_reason",
                        "expected_failures", "requirement_ids"}

# --- the shipped tables, imported rather than mirrored -------------------------------
#
# These used to be copies. FR-12 §7 said row 12 must make `guardrails.py` the single source of
# truth and point the tests here, and the row-11 review showed why: while `phrases()` was its own
# regex, the fixtures agreed with a defect in the rule instead of catching it (D-42). The specs
# are still checked against these tables in both directions by T-FR12-5.

# docs/specs/FR-03.md §3.1 and §3.2, grouped: T-FR03-13 needs one fixture per family.
MONEY_FAMILIES = {
    "refund": ("refund", "refunded", "refunding"),
    "credit": ("credit note", "credit back", "account credit", "service credit", "sla credit"),
    "dispute": ("dispute", "disputing", "disputed charge", "chargeback", "charge back"),
    "reimburse": ("money back", "reimburse", "reimbursement"),
    "compensation": ("compensation", "compensate", "goodwill"),
    "waive": ("waive", "waiver", "write off"),
    "reverse": ("cancel the charge", "reverse the charge", "reversal"),
}
DATE_FAMILIES = {
    "eta": ("eta",),
    "when_fixed": ("when will you fix", "when will this be fixed", "when it will be fixed"),
    "firm_date": ("by when", "firm date", "fix date", "delivery date", "commit to a date",
                  "guarantee a date", "deadline for the fix"),
    "promise": ("promise",),
    "sla": ("sla breach",),
}

# docs/specs/FR-12.md §3.2 check order → the reason each failing check produces.
CHECK_ORDER = ("private_data", "grounding", "instruction_integrity", "commitments")
CHECK_REASON = {"private_data": "private_data_in_draft", "grounding": "ungrounded_draft",
                "instruction_integrity": "instruction_leak_in_draft",
                "commitments": "commitment_in_draft"}

# FR-09's always-escalate intents: a fixture must not hide behind one of them.
ALWAYS_ESCALATE_INTENTS = {"security_incident", "compliance_request", "feature_request",
                           "unclear_request"}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def contact_in(text: str) -> list[str]:
    """FR-12 §3.2.1: contact details, which block a *draft* but do not escalate a ticket.

    The shipped detectors, not a copy: `phones_in` counts digits, because the raw pattern also
    matches a 16-digit invoice reference (D-51).
    """
    found = []
    if re.search(EMAIL, text):
        found.append("email")
    if phones_in(text):
        found.append("phone")
    return found


def pii_patterns(text: str) -> list[str]:
    return secrets_in(text) + contact_in(text)


def phrases(text: str, table) -> list[str]:
    """The shipped matcher, not a copy of it.

    This used to be its own regex, and the duplicate hid a defect: both this helper and the rule
    matched the singular phrase only, so "please issue refunds" derived *no* expected reason and
    the fixtures agreed with the rule that it was answerable (D-42). Deriving the fixtures'
    expectations from the code the fixtures test is a smaller risk than deriving them from a
    second implementation that can drift; the phrase tables are still checked against the spec
    text in both directions by T-FR12-5.
    """
    return matches_triggers(text, table)


def markers(text: str) -> list[str]:
    """The shipped detector (FR-12 §3.1.1 / D-24), not a copy of it."""
    return markers_in(text)


def spec_table(spec_text: str, line_prefix: str, drop: set[str]) -> set[str]:
    """The code-span items listed on one numbered rule line of a spec."""
    line = next(ln for ln in spec_text.splitlines() if ln.strip().startswith(line_prefix))
    return {span for span in re.findall(r"`([^`]+)`", line)} - drop


@pytest.fixture(scope="module")
def corpus():
    """Every conforming engineered entry, with the file it came from."""
    return [(name, entry) for name in CONFORMING_FILES for entry in read_json(FIXTURES / name)]


def of_category(corpus, *categories):
    return [e for _, e in corpus if e["synthetic"]["category"] in categories]


def text_of(entry):
    return f"{entry['subject']} {entry['body']}"


def test_T_FR12_1_corpus_contract(corpus):
    assert len(corpus) >= 21  # a floor, not a fixed count: adding a fixture must not fail here
    seen = set()
    for name, entry in corpus:
        tid = entry["ticket_id"]
        assert tid.startswith("SYN-"), f"{name}: {tid} is not clearly synthetic"
        assert tid not in seen, f"duplicate id {tid}"
        seen.add(tid)
        assert TICKET_KEYS == set(entry), f"{tid}: {TICKET_KEYS ^ set(entry)}"
        assert LABEL_KEYS == set(entry["labels"]), tid
        assert SYNTHETIC_KEYS <= set(entry["synthetic"]), tid
        assert entry["customer_name"] == "Synthetic Tester", tid
        assert entry["synthetic"]["why"].strip(), f"{tid}: no reason recorded"
        assert entry["labels"]["expected_route"] in {"auto_respond", "escalate"}, tid
        assert entry["labels"]["must_not_auto_respond"] == (
            entry["labels"]["expected_route"] == "escalate"
        ), tid
        assert entry["labels"]["intent"] not in ALWAYS_ESCALATE_INTENTS, (
            f"{tid} would escalate by intent (FR-09) and prove nothing about its own rule"
        )
        if entry["synthetic"]["category"] == "pii_echo_risk":
            assert entry["synthetic"]["must_not_appear_in_reply"], tid


def test_T_FR12_21_declared_reasons_follow_the_precedence_order(corpus):
    """FR-12 §3.4 / D-16: the primary reason must be derivable, not a matter of code order."""
    for name, entry in corpus:
        text = text_of(entry)
        matched = {
            "private_data_in_ticket": secrets_in(text),
            "instruction_injection_detected": markers(text),
            "money_commitment_requested": phrases(text, MONEY_TRIGGERS),
            "date_commitment_requested": phrases(text, DATE_TRIGGERS),
        }
        expected = [r for r in PRECEDENCE if matched[r]]
        synthetic = entry["synthetic"]
        assert synthetic["expected_all_reasons"] == expected, (name, entry["ticket_id"])
        assert synthetic["expected_reason"] == (expected[0] if expected else None), (
            entry["ticket_id"]
        )
        if entry["labels"]["expected_route"] == "escalate":
            assert expected, f"{entry['ticket_id']} escalates for no derivable reason"

    # The two-rule fixture the precedence exists for: injection outranks the money rule.
    dual = next(e for _, e in corpus if e["ticket_id"] == "SYN-INJ-001")
    assert dual["synthetic"]["expected_all_reasons"] == [
        "instruction_injection_detected", "money_commitment_requested"]


def test_T_FR12_22_injection_and_pii_positives_are_diagnostic(corpus):
    """A positive must not be escalated by an earlier rule, or it proves nothing (HIGH finding)."""
    injection_only = [e for e in of_category(corpus, "injection")
                      if e["synthetic"]["expected_all_reasons"] == ["instruction_injection_detected"]]
    assert len(injection_only) >= 3, [e["ticket_id"] for e in injection_only]

    for entry in of_category(corpus, "pii"):
        assert entry["synthetic"]["expected_reason"] == "private_data_in_ticket", (
            f"{entry['ticket_id']} would escalate for another reason first: "
            f"{entry['synthetic']['expected_all_reasons']}"
        )


def test_T_FR12_2_every_engineered_ticket_survives_ingest(corpus):
    for name, entry in corpus:
        ticket = normalise_ticket(entry)
        assert ticket.ticket_id == entry["ticket_id"]
        assert ticket.text.strip(), f"{name}: {entry['ticket_id']} has no text"
        assert not ticket.is_malformed, (
            f"{entry['ticket_id']} is malformed, so it would escalate at ingest and never "
            f"reach the rule it was written for: {ticket.defects}"
        )
        assert entry["body"] in ticket.text


def test_T_FR12_3_pii_fixtures_carry_pii_and_lookalikes_do_not(corpus):
    secrets = of_category(corpus, "pii")
    assert len(secrets) >= 3
    for entry in secrets:
        text = text_of(entry)
        found = secrets_in(text)
        assert found, (
            f"{entry['ticket_id']} is a pre-draft PII case but carries no high-risk secret; "
            "FR-12 §3.1.2 escalates on secrets, not on a bare email address"
        )
        assert set(entry["synthetic"]["secrets"]) == set(found), entry["ticket_id"]

    echo = of_category(corpus, "pii_echo_risk")
    assert len(echo) >= 1
    for entry in echo:
        text = text_of(entry)
        assert contact_in(text), entry["ticket_id"]
        assert not secrets_in(text), (
            f"{entry['ticket_id']} is a draft-side echo case, so it must not also carry a "
            "secret that escalates it before a draft exists"
        )
        assert entry["labels"]["expected_route"] == "auto_respond", entry["ticket_id"]

    lookalikes = of_category(corpus, "pii_lookalike")
    assert len(lookalikes) >= 2
    for entry in lookalikes:
        found = pii_patterns(text_of(entry))
        assert not found, f"{entry['ticket_id']} is a lookalike but matches {found}"
        assert entry["labels"]["expected_route"] == "auto_respond", entry["ticket_id"]


def test_T_FR12_4_injection_fixtures_carry_markers_and_lookalikes_do_not(corpus):
    positives = of_category(corpus, "injection")
    assert len(positives) >= 4
    for entry in positives:
        found = markers(text_of(entry))
        assert found, f"{entry['ticket_id']} claims injection but carries no marker"
        assert set(entry["synthetic"]["markers"]) <= set(found), entry["ticket_id"]
        assert entry["labels"]["expected_route"] == "escalate", entry["ticket_id"]
        assert entry["synthetic"]["expected_reason"] == "instruction_injection_detected"

    lookalikes = of_category(corpus, "injection_lookalike")
    assert len(lookalikes) >= 3
    for entry in lookalikes:
        found = markers(text_of(entry))
        assert not found, f"{entry['ticket_id']} is a lookalike but matches {found}"
        assert entry["labels"]["expected_route"] == "auto_respond", entry["ticket_id"]

    # D-24: the role labels are anchored, and the corpus holds both halves of that decision.
    anchored = [e for e in positives if "system:" in e["synthetic"]["markers"]]
    assert anchored, "no fixture carries a line-start role label"
    for entry in anchored:
        assert re.search(r"(?m)^[\s>]*system:", text_of(entry).lower()), entry["ticket_id"]
    inline = [e for e in lookalikes if "system:" in text_of(e).lower()]
    assert inline, "no fixture carries an inline role label that must not fire"
    for entry in inline:
        assert "system:" not in markers(text_of(entry)), (
            f"{entry['ticket_id']}: an inline role label must not match (D-24)"
        )


def test_T_FR12_5_specs_and_mirrored_tables_agree_with_the_data():
    fr12 = SPEC_FR12.read_text(encoding="utf-8")
    fr03 = SPEC_FR03.read_text(encoding="utf-8")

    # Every must_not_claim phrase the data demands is named in the FR-12 commitment list,
    # so the guardrail written at row 12 cannot drift from the ground truth.
    demanded = {p for g in read_json(ROOT / "data" / "ground_truth_responses.json")
                for p in g["must_not_claim"]}
    assert demanded, "ground truth carries no must_not_claim phrases"
    for phrase in demanded:
        assert phrase in fr12, f"FR-12 spec does not name the must_not_claim phrase {phrase!r}"
        assert phrase in fr03, f"FR-03 spec does not name the must_not_claim phrase {phrase!r}"

    # The mirror and the spec must agree in BOTH directions: a marker added to the spec and
    # not to the mirror would otherwise leave every test green while the mirror went stale.
    assert set(INJECTION_MARKERS) == spec_table(fr12, "1. **Instruction integrity.**",
                                                drop={"ticket.text",
                                                      "instruction_injection_detected"})
    assert set(MONEY_TRIGGERS) == spec_table(fr03, "1. **Money commitment requested",
                                             drop={"ticket.text", "money_commitment_requested"})
    assert set(DATE_TRIGGERS) == spec_table(fr03, "2. **Date commitment requested",
                                            drop={"date_commitment_requested"})


def test_T_FR03_1_money_and_date_fixtures_escalate(corpus):
    money = of_category(corpus, "money_commitment")
    dates = of_category(corpus, "date_commitment")
    assert len(money) >= 4 and len(dates) >= 1

    # A ticket may carry both kinds of trigger (an SLA credit claim cites the breach); the
    # category says which must be present, and the recorded evidence must match the text.
    for entry in money:
        text = text_of(entry)
        found = phrases(text, MONEY_TRIGGERS)
        assert found, f"{entry['ticket_id']} claims a money commitment but carries no trigger"
        assert set(entry["synthetic"]["money_triggers"]) == set(found), entry["ticket_id"]
        assert set(entry["synthetic"]["date_triggers"]) == set(phrases(text, DATE_TRIGGERS))
        assert entry["synthetic"]["expected_reason"] == "money_commitment_requested"
        assert entry["labels"]["expected_route"] == "escalate", entry["ticket_id"]

    for entry in dates:
        text = text_of(entry)
        found = phrases(text, DATE_TRIGGERS)
        assert found, f"{entry['ticket_id']} claims a date commitment but carries no trigger"
        assert set(entry["synthetic"]["date_triggers"]) == set(found), entry["ticket_id"]
        assert not phrases(text, MONEY_TRIGGERS), (
            f"{entry['ticket_id']} is a date case but also carries a money trigger, which "
            "outranks it by precedence"
        )
        assert entry["synthetic"]["expected_reason"] == "date_commitment_requested"
        assert entry["labels"]["expected_route"] == "escalate", entry["ticket_id"]

    # The mixed ticket: explanatory question plus a refund ask, which must not rescue it.
    mixed = next(e for e in money if "proration" in text_of(e).lower())
    assert phrases(text_of(mixed), MONEY_TRIGGERS) == ["refund"]
    assert "how proration was calculated" in mixed["body"]
    assert mixed["labels"]["expected_route"] == "escalate"


def test_T_FR03_2_explanatory_billing_must_still_be_answered(corpus):
    explanatory = of_category(corpus, "explanatory_billing")
    assert len(explanatory) >= 3
    for entry in explanatory:
        text = text_of(entry)
        assert not phrases(text, MONEY_TRIGGERS), entry["ticket_id"]
        assert not phrases(text, DATE_TRIGGERS), entry["ticket_id"]
        assert entry["labels"]["expected_route"] == "auto_respond", entry["ticket_id"]
        assert entry["labels"]["answerable_from_docs"] is True, entry["ticket_id"]
        assert entry["labels"]["expected_doc_ids"], entry["ticket_id"]

    # The factual date question is the one that would break an over-broad date rule.
    period = next(e for e in explanatory if "billing period end" in text_of(e).lower())
    assert not phrases(text_of(period), DATE_TRIGGERS)


def test_T_FR03_13_every_trigger_family_has_a_fixture(corpus):
    """FR-03 §7's over-firing question can only be answered if the triggers are exercised."""
    text = " ".join(text_of(e) for _, e in corpus)
    for name, family in MONEY_FAMILIES.items():
        assert phrases(text, family), f"no fixture exercises the money family {name}"
    for name, family in DATE_FAMILIES.items():
        assert phrases(text, family), f"no fixture exercises the date family {name}"

    # The honestly-labelled cost of the conservative rule, kept as evidence rather than hidden.
    accepted = of_category(corpus, "known_over_escalation")
    assert accepted, "no fixture records the accepted over-escalation (FR-03 §7)"
    for entry in accepted:
        assert entry["synthetic"]["accepted_cost"], entry["ticket_id"]
        assert entry["labels"]["expected_route"] == "escalate", entry["ticket_id"]


def test_T_FR12_23_draft_fixtures_contract():
    """Row 12's post-draft checks need engineered drafts; row 12 depends on this row."""
    drafts = read_json(FIXTURES / DRAFTS_FILE)
    assert len(drafts) >= 10
    ids = set()
    tickets = {e["ticket_id"] for name in CONFORMING_FILES
               for e in read_json(FIXTURES / name)}

    for draft in drafts:
        did = draft["draft_id"]
        assert did.startswith("SYN-DRAFT-"), did
        assert did not in ids
        ids.add(did)
        assert DRAFT_KEYS == set(draft), f"{did}: {DRAFT_KEYS ^ set(draft)}"
        assert DRAFT_SYNTHETIC_KEYS == set(draft["synthetic"]), did
        assert draft["for_ticket"] in tickets, f"{did} references an unknown ticket"

        synthetic = draft["synthetic"]
        failures = synthetic["expected_failures"]
        assert synthetic["expected_passed"] == (not failures), did
        if failures and failures != ["empty"]:
            assert failures == [c for c in CHECK_ORDER if c in failures], (
                f"{did}: failures must be listed in FR-12 §3.2 check order"
            )
            assert synthetic["expected_reason"] == CHECK_REASON[failures[0]], did

        retrieved_ids = {p["chunk_id"] for p in draft["retrieved"]}
        if synthetic["expected_passed"]:
            assert not pii_patterns(draft["text"]), did
            assert not phrases(draft["text"], MONEY_TRIGGERS + DATE_TRIGGERS), did
            assert set(draft["citations"]) <= retrieved_ids, did
            assert "drafted automatically" in draft["text"], f"{did}: no FR-06 disclosure"
        if "private_data" in failures:
            assert pii_patterns(draft["text"]), did
        if "grounding" in failures:
            assert draft["citations"] or draft["text"].strip(), did
        if "commitments" in failures:
            assert phrases(draft["text"], MONEY_TRIGGERS + DATE_TRIGGERS) \
                or "fixed on our side" in draft["text"] or "fixed by" in draft["text"], did

    categories = {d["synthetic"]["category"] for d in drafts}
    assert {"clean", "private_data_in_draft", "ungrounded_draft", "commitment_in_draft",
            "instruction_leak", "empty_draft"} <= categories
    # One draft per must_not_claim phrase the ground truth forbids.
    commitments = " ".join(d["text"] for d in drafts
                          if d["synthetic"]["category"] == "commitment_in_draft").lower()
    for phrase in ("a refund has been issued", "the issue has been fixed on our side",
                   "fixed by friday"):
        assert phrase in commitments, phrase


def test_T_FR07_23_corpus_covers_every_category_row_2_names(corpus):
    categories = {e["synthetic"]["category"] for _, e in corpus}
    assert {"pii", "injection", "money_commitment", "date_commitment"} <= categories
    assert {"pii_lookalike", "injection_lookalike", "explanatory_billing"} <= categories

    # Every engineered file must load through the file-level path too, not only entry by entry.
    for name in CONFORMING_FILES:
        entries = read_json(FIXTURES / name)
        tickets = load_tickets(FIXTURES / name)
        assert len(tickets) == len(entries)
        assert [t.source_index for t in tickets] == list(range(len(entries)))
        assert not [t for t in tickets if t.is_malformed]

    # malformed and empty live in the row 1 file, which is exempt from the schema contract.
    malformed = load_tickets(FIXTURES / MALFORMED_FILE)
    assert [t for t in malformed if "empty_text" in t.defects], "no empty ticket in the corpus"
    assert [t for t in malformed if t.is_malformed], "no malformed ticket in the corpus"
    assert all(t.ticket_id.startswith(("SYN-", "GEN-")) for t in malformed)


def test_T_FR12_17_no_real_credentials_or_addresses_in_the_corpus():
    """NFR-04 and CLAUDE.md: fixtures must contain nothing that could be a real secret."""
    for name in (*CONFORMING_FILES, MALFORMED_FILE, DRAFTS_FILE):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        for address in re.findall(EMAIL, text):
            assert address.endswith("@example.com"), f"{name}: {address} is not a reserved domain"
        assert "BEGIN RSA PRIVATE KEY" not in text
        assert "BEGIN PRIVATE KEY" not in text
        assert not re.search(r"\b(sk|pk)_(live|test)_\w+", text), f"{name}: provider-shaped key"
        assert not re.search(r"\bAKIA[0-9A-Z]{16}\b", text), f"{name}: AWS-shaped key id"
        for match in re.findall(
                r"(?i)(?:api[_-]?key|private[_-]?key|token|secret|password|passwd)\s*[:=]\s*(\S+)",
                                text):
            assert "EXAMPLE" in match.upper(), f"{name}: {match} is not an obvious placeholder"
