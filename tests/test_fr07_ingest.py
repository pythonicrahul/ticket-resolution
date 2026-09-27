"""FR-07 acceptance tests T-FR07-1 … T-FR07-22 (docs/specs/FR-07.md).

Offline: no network, no API key. Real tickets come from data/, engineered ones from
tests/fixtures/malformed_tickets.json.
"""
import dataclasses
import json
from pathlib import Path

import pytest

from ticketing_agent import ingest
from ticketing_agent.ingest import (
    MAX_TEXT_CHARS,
    Ticket,
    TicketFileError,
    evaluation_labels,
    load_tickets,
    normalise_ticket,
)

ROOT = Path(__file__).resolve().parents[1]
DEV_TICKETS = ROOT / "data" / "development_tickets.json"
FIXTURE = ROOT / "tests" / "fixtures" / "malformed_tickets.json"
CHANNELS = ("email", "chat", "docs_comment", "forum")

ZWSP, RLO, PDF, NUL, BOM = (chr(c) for c in (0x200B, 0x202E, 0x202C, 0x00, 0xFEFF))
ROCKET, LONE_SURROGATE = chr(0x1F680), chr(0xD800)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dev_entries():
    return read_json(DEV_TICKETS)


@pytest.fixture(scope="module")
def synthetic():
    return read_json(FIXTURE)


def by_id(entries, ticket_id):
    return next(e for e in entries if isinstance(e, dict) and e.get("ticket_id") == ticket_id)


def test_T_FR07_1_one_ticket_per_channel_normalises(dev_entries):
    for channel in CHANNELS:
        entry = next(e for e in dev_entries if e["channel"] == channel)
        ticket = normalise_ticket(entry)
        assert ticket.channel == channel
        assert ticket.text.strip()
        assert entry["body"] in ticket.text
        assert not ticket.is_malformed, ticket.defects
        assert ticket.ticket_id == entry["ticket_id"]
        assert ticket.received_at == entry["received_at"]
        assert ticket.segments() == {
            "channel": channel,
            "tier": entry["customer_tier"],
            "region": entry["customer_region"],
            "fluency": entry["language_fluency"],
        }
        assert ticket.log_fields() == {
            "ticket_id": entry["ticket_id"],
            "received_at": entry["received_at"],
            "ingest_defects": [],
            **ticket.segments(),
        }


def test_T_FR07_2_original_text_kept_verbatim(synthetic):
    entry = by_id(synthetic, "SYN-ODD-001")
    ticket = normalise_ticket(entry)
    assert ticket.subject == entry["subject"]
    assert ticket.body == entry["body"]
    assert ticket.raw["body"] == entry["body"]
    assert ticket.text == (
        "Synthetic odd characters in the subject\n\n"
        f"Line one\nLine two with a NUL and a combining accent café {ROCKET} end."
    )


def test_T_FR07_3_body_only_entry_survives(synthetic):
    entry = next(e for e in synthetic if isinstance(e, dict) and set(e) == {"body"})
    ticket = normalise_ticket(entry, index=4)
    assert ticket.ticket_id.startswith("GEN-0004-")
    assert ticket.text == entry["body"]
    assert ticket.customer_tier == ticket.customer_region == "unknown"
    assert ticket.language_fluency == "unknown"
    assert ticket.customer_id is None
    assert ticket.received_at is None
    assert ticket.is_malformed  # no channel: we do not know the text contract
    assert {
        "missing_ticket_id",
        "missing_channel",
        "missing_received_at",
        "unknown_customer_tier",
        "unknown_customer_region",
        "unknown_language_fluency",
    } <= set(ticket.defects)


def test_T_FR07_4_empty_subject_and_body_is_malformed(synthetic):
    ticket = normalise_ticket(by_id(synthetic, "SYN-EMPTY-001"))
    assert ticket.text == ""
    assert "empty_text" in ticket.defects
    assert "empty_body" in ticket.defects
    assert ticket.is_malformed


def test_T_FR07_5_unusual_characters_handled(synthetic):
    ticket = normalise_ticket(by_id(synthetic, "SYN-ODD-001"))
    for ch in (ZWSP, RLO, PDF, NUL, BOM, LONE_SURROGATE, chr(13)):
        assert ch not in ticket.text
    assert "Synthetic" in ticket.text
    assert "caf" in ticket.text
    assert ROCKET in ticket.text
    assert "control_characters_removed" in ticket.defects
    assert ticket.text.encode("utf-8")  # encodable: no lone surrogate survived


@pytest.mark.parametrize("index", [0, 1, 2])
def test_T_FR07_6_non_object_entries_are_not_dropped(synthetic, index):
    entry = synthetic[index]
    assert not isinstance(entry, dict)
    ticket = normalise_ticket(entry, index=index)
    assert isinstance(ticket, Ticket)
    assert "not_an_object" in ticket.defects
    assert ticket.is_malformed
    assert ticket.channel == "unknown"
    assert ticket.ticket_id.startswith(f"GEN-{index:04d}-")


def test_T_FR07_7_unknown_channel_escalates(synthetic):
    ticket = normalise_ticket(by_id(synthetic, "SYN-CHANNEL-001"))
    assert ticket.channel == "unknown"
    assert "unknown_channel" in ticket.defects
    assert ticket.is_malformed
    assert ticket.raw["channel"] == "sms"


def test_T_FR07_8_load_tickets_from_any_path(tmp_path, dev_entries):
    renamed = tmp_path / "tickets-nobody-has-seen.json"
    renamed.write_text(json.dumps(dev_entries), encoding="utf-8")
    tickets = load_tickets(renamed)
    assert len(tickets) == len(dev_entries)
    assert [t.ticket_id for t in tickets] == [e["ticket_id"] for e in dev_entries]
    assert [t.source_index for t in tickets] == list(range(len(dev_entries)))


def test_T_FR07_9_normalisation_is_deterministic(synthetic):
    for entry in synthetic:
        first = normalise_ticket(entry, index=3)
        second = normalise_ticket(entry, index=3)
        assert first == second


def test_T_FR07_10_ground_truth_not_on_the_runtime_representation(dev_entries):
    entry = dev_entries[0]
    ticket = normalise_ticket(entry)
    names = {f.name for f in dataclasses.fields(Ticket)}
    assert not names & {"labels", "history"}
    assert not hasattr(ticket, "labels")
    assert evaluation_labels(ticket) == entry["labels"]
    assert evaluation_labels(normalise_ticket({"body": "no labels here"})) == {}


def test_T_FR07_11_over_long_body_truncated_in_text_only(synthetic):
    entry = by_id(synthetic, "SYN-LONG-001")
    ticket = normalise_ticket(entry)
    assert len(ticket.text) == MAX_TEXT_CHARS
    assert "text_truncated" in ticket.defects
    assert ticket.body == entry["body"]
    assert len(ticket.body) > MAX_TEXT_CHARS


def test_T_FR07_12_subject_expected_per_channel(synthetic):
    chat = normalise_ticket(by_id(synthetic, "SYN-CHAT-NOSUBJECT-001"))
    assert "missing_subject" not in chat.defects
    assert not chat.is_malformed

    email = normalise_ticket(by_id(synthetic, "SYN-EMAIL-NOSUBJECT-001"))
    assert "missing_subject" in email.defects
    assert not email.is_malformed


@pytest.mark.parametrize(
    ("raw", "expected", "defect"),
    [
        ("2026-04-01T09:00:00Z", "2026-04-01T09:00:00Z", None),
        ("2026-04-03T09:00:00+00:00", "2026-04-03T09:00:00Z", None),
        ("2026-04-04T09:00:00", "2026-04-04T09:00:00Z", None),
        ("2026-04-02 09:00:00", "2026-04-02T09:00:00Z", None),
        ("not a timestamp", None, "unparseable_received_at"),
        (None, None, "missing_received_at"),
    ],
)
def test_T_FR07_13_received_at_normalised(raw, expected, defect):
    entry = {"ticket_id": "SYN-TIME", "channel": "email", "subject": "s", "body": "b"}
    if raw is not None:
        entry["received_at"] = raw
    ticket = normalise_ticket(entry)
    assert ticket.received_at == expected
    assert ticket.received_at_raw == raw
    if defect:
        assert defect in ticket.defects
    else:
        assert not [d for d in ticket.defects if "received_at" in d]


def test_T_FR07_14_duplicate_ticket_id_flagged_and_kept():
    entries = [e for e in read_json(FIXTURE) if isinstance(e, dict)]
    duplicates = [e for e in entries if e.get("ticket_id") == "SYN-DUP-001"]
    assert len(duplicates) == 2
    seen = {}
    tickets = [normalise_ticket(e, index=i, seen_ids=seen) for i, e in enumerate(duplicates)]
    assert [t.ticket_id for t in tickets] == ["SYN-DUP-001", "SYN-DUP-001"]
    assert "duplicate_ticket_id" not in tickets[0].defects
    assert "duplicate_ticket_id" in tickets[1].defects


def test_T_FR07_15_payload_shapes_and_file_errors(tmp_path):
    entry = {"ticket_id": "SYN-SHAPE-001", "channel": "chat", "subject": "", "body": "hello"}

    as_array = tmp_path / "array.json"
    as_array.write_text(json.dumps([entry]), encoding="utf-8")
    assert [t.ticket_id for t in load_tickets(as_array)] == ["SYN-SHAPE-001"]

    for key in ("tickets", "data", "items"):
        wrapped = tmp_path / f"wrapped-{key}.json"
        wrapped.write_text(json.dumps({key: [entry]}), encoding="utf-8")
        assert [t.ticket_id for t in load_tickets(wrapped)] == ["SYN-SHAPE-001"]

    single = tmp_path / "single.json"
    single.write_text(json.dumps(entry), encoding="utf-8")
    assert [t.ticket_id for t in load_tickets(single)] == ["SYN-SHAPE-001"]

    # A ticket that happens to carry a list stays one ticket: the wrapper keys must not win.
    with_items = tmp_path / "ticket-with-items.json"
    with_items.write_text(json.dumps({**entry, "items": [1, 2, 3]}), encoding="utf-8")
    assert [t.ticket_id for t in load_tickets(with_items)] == ["SYN-SHAPE-001"]

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(TicketFileError):
        load_tickets(broken)

    with pytest.raises(TicketFileError):
        load_tickets(tmp_path / "does-not-exist.json")

    scalar = tmp_path / "scalar.json"
    scalar.write_text("42", encoding="utf-8")
    with pytest.raises(TicketFileError):
        load_tickets(scalar)

    # An unrecognised wrapper key must fail the run loudly, never load as one ticket:
    # reading {"validation_tickets": [...]} as a single malformed ticket would drop the rest.
    unknown_wrapper = tmp_path / "unknown-wrapper.json"
    unknown_wrapper.write_text(json.dumps({"validation_tickets": [entry] * 3}), encoding="utf-8")
    with pytest.raises(TicketFileError):
        load_tickets(unknown_wrapper)


def test_T_FR07_18_file_encodings(tmp_path):
    entry = {"ticket_id": "SYN-ENC-001", "channel": "email", "subject": "s", "body": "b"}

    with_bom = tmp_path / "bom.json"
    with_bom.write_text(json.dumps([entry]), encoding="utf-8-sig")
    assert [t.ticket_id for t in load_tickets(with_bom)] == ["SYN-ENC-001"]

    not_utf8 = tmp_path / "utf16.json"
    not_utf8.write_bytes(json.dumps([entry]).encode("utf-16"))
    with pytest.raises(TicketFileError):
        load_tickets(not_utf8)


def test_T_FR07_19_normalisation_error_escalates_without_leaking_ground_truth(monkeypatch):
    entry = {
        "ticket_id": "SYN-BOOM-001",
        "channel": "email",
        "subject": "Synthetic normalisation failure",
        "body": "Deployment keeps failing after the last release.",
        "received_at": "2026-04-06T09:00:00Z",
        "labels": {"intent": "deployment_failure", "expected_route": "auto_respond"},
        "history": {"csat_rating": 5},
    }

    def boom(*_args, **_kwargs):
        raise RuntimeError("synthetic failure inside normalisation")

    monkeypatch.setattr(ingest, "_normalise_channel", boom)
    ticket = normalise_ticket(entry, index=2)

    assert "normalisation_error" in ticket.defects
    assert ticket.is_malformed  # a ticket we could not read must go to a person
    assert ticket.ticket_id == "SYN-BOOM-001"  # the operator's id survives, for reconciliation
    assert ticket.channel == "unknown"
    assert ticket.text == entry["subject"] + "\n\n" + entry["body"]
    assert "deployment_failure" not in ticket.text  # no ground truth on the runtime path
    assert "csat_rating" not in ticket.text
    assert evaluation_labels(ticket) == entry["labels"]  # still scorable by the harness


def test_T_FR07_20_ground_truth_never_reaches_text(dev_entries, synthetic):
    tickets = [normalise_ticket(e, index=i) for i, e in enumerate(dev_entries)]
    tickets += [normalise_ticket(e, index=i) for i, e in enumerate(synthetic)]
    for ticket in tickets:
        for marker in ("expected_route", "answerable_from_docs", "must_not_auto_respond",
                       "first_contact_resolution", "csat_rating"):
            assert marker not in ticket.text


def test_T_FR07_21_raw_is_read_only(dev_entries):
    ticket = normalise_ticket(dev_entries[0])
    with pytest.raises(TypeError):
        ticket.raw["body"] = "rewritten"
    assert ticket.raw["body"] == dev_entries[0]["body"]


def test_T_FR07_22_subject_repeating_the_body_is_not_duplicated():
    repeated = normalise_ticket(
        {"channel": "email", "subject": "Login fails", "body": "Login fails when I use SSO."}
    )
    assert repeated.text == "Login fails when I use SSO."

    distinct = normalise_ticket(
        {"channel": "email", "subject": "Login fails", "body": "It started after the SSO change."}
    )
    assert distinct.text == "Login fails\n\nIt started after the SSO change."


def test_T_FR07_16_wrong_types_are_coerced(synthetic):
    ticket = normalise_ticket(by_id(synthetic, "SYN-TYPES-001"))
    assert "12345" in ticket.text
    assert "Synthetic ticket whose body arrived as a list" in ticket.text
    assert ticket.customer_id == "9007"
    assert ticket.customer_tier == "unknown"
    assert {
        "coerced_field:subject",
        "coerced_field:body",
        "coerced_field:customer_id",
        "unknown_customer_tier",
    } <= set(ticket.defects)
    assert not ticket.is_malformed

    numeric_id = normalise_ticket({"ticket_id": 7, "channel": 1, "body": "b"})
    assert numeric_id.ticket_id == "7"
    assert {"coerced_field:ticket_id", "coerced_field:channel"} <= set(numeric_id.defects)


def test_T_FR07_17_whole_dataset_normalises_without_raising(dev_entries):
    tickets = load_tickets(DEV_TICKETS)
    assert len(tickets) == len(dev_entries)
    assert not [t for t in tickets if t.is_malformed]
    assert {t.channel for t in tickets} == set(CHANNELS)
