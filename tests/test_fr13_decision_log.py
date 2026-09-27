"""FR-13 acceptance tests T-FR13-1 … T-FR13-27 (docs/specs/FR-13.md).

Offline: no network, no API key, no model calls. Every test uses a temporary database path,
so nothing is hardcoded and no test touches storage/.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from ticketing_agent.ingest import evaluation_labels, normalise_ticket
from ticketing_agent.logging_store import (
    MAX_TEXT_FIELD,
    SCHEMA_VERSION,
    DecisionEntry,
    DecisionLog,
    DecisionLogUnavailable,
    InvalidDecision,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def entry(**overrides):
    """A minimal valid entry; tests override the field under test."""
    fields = {
        "ticket_id": "SYN-BILL-001",
        "source_index": 0,
        "stage": "route",
        "decision": "auto_respond",
        "requirement_ids": ("FR-02",),
    }
    fields.update(overrides)
    return DecisionEntry(**fields)


@pytest.fixture
def log(tmp_path):
    with DecisionLog(tmp_path / "nested" / "decisions.db", run_id="run-test") as store:
        yield store


def test_T_FR13_1_schema_created_at_any_path(tmp_path):
    path = tmp_path / "does" / "not" / "exist" / "decisions.db"
    with DecisionLog(path) as store:
        assert path.exists()
        assert store.schema_version == SCHEMA_VERSION
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"decisions", "runs", "meta"} <= tables
    assert {"idx_decisions_ticket", "idx_decisions_run", "idx_decisions_run_index"} <= indexes
    with sqlite3.connect(path) as conn:
        stored = dict(conn.execute("SELECT key, value FROM meta"))
    assert stored["schema_version"] == str(SCHEMA_VERSION)


def test_T_FR13_2_every_field_round_trips(log):
    row_id = log.record(entry(
        decision="escalate",
        reason="money_commitment_requested",
        all_reasons=("instruction_injection_detected", "money_commitment_requested"),
        detail="matched triggers: refund",
        summary="Customer asks for a refund of an overage charge.",
        uncertainty="No policy in the documentation permits a refund commitment.",
        intent="billing_query",
        intent_confidence=0.82,
        urgency="medium",
        urgency_confidence=0.61,
        intent_alternatives=(("quota_or_overage", 0.11), ("account_access", 0.04)),
        retrieved_doc_ids=("DOC-BILL-001", "DOC-BILL-003"),
        citations=("DOC-BILL-001#1",),
        guardrail_results=(("private_data", True), ("grounding", False)),
        prompt_version="PR-01 v1.0",
        model_name="test-model",
        model_calls=2,
        cache_hits=1,
        latency_ms=142.5,
        kill_switch=False,
        ingest_defects=("missing_subject",),
        channel="email", tier="business", region="europe", fluency="fluent",
        received_at="2026-05-07T09:00:00Z",
        requirement_ids=("FR-03", "FR-09"),
    ))
    assert row_id == 1

    row = log.rows()[0]
    assert row["row_id"] == 1
    assert row["run_id"] == "run-test"
    assert row["ticket_id"] == "SYN-BILL-001"
    assert row["all_reasons"] == ["instruction_injection_detected", "money_commitment_requested"]
    assert row["intent_alternatives"] == [["quota_or_overage", 0.11], ["account_access", 0.04]]
    assert row["guardrail_results"] == [["private_data", True], ["grounding", False]]
    assert row["retrieved_doc_ids"] == ["DOC-BILL-001", "DOC-BILL-003"]
    assert row["requirement_ids"] == ["FR-03", "FR-09"]
    assert row["intent_confidence"] == 0.82
    assert row["latency_ms"] == 142.5
    assert row["kill_switch"] is False
    assert row["summary"].startswith("Customer asks")
    assert row["redactions"] == []

    minimal = log.record(entry(ticket_id="SYN-BILL-002", source_index=1))
    bare = log.rows()[1]
    assert minimal == 2
    for column in ("reason", "detail", "summary", "intent", "intent_confidence",
                   "prompt_version", "model_name", "latency_ms"):
        assert bare[column] is None, column
    assert row["logged_at"].endswith("Z")
    assert row["schema_version"] == SCHEMA_VERSION


def test_T_FR13_3_requirement_ids_are_mandatory(log):
    with pytest.raises(InvalidDecision, match="requirement_ids"):
        log.record(entry(requirement_ids=()))
    assert log.rows() == []


def test_T_FR13_4_escalation_needs_a_reason(log):
    with pytest.raises(InvalidDecision, match="reason"):
        log.record(entry(decision="escalate"))
    assert log.rows() == []
    log.record(entry(decision="escalate", reason="low_confidence"))
    assert len(log.rows()) == 1


def test_T_FR13_5_a_model_call_needs_a_prompt_version(log):
    with pytest.raises(InvalidDecision, match="prompt_version"):
        log.record(entry(model_calls=1))
    assert log.rows() == []
    log.record(entry(model_calls=1, prompt_version="PR-01 v1.0"))
    assert len(log.rows()) == 1


@pytest.mark.parametrize(("value", "secret", "pattern"), [
    ("leaked alice@example.com", "alice@example.com", "email"),
    ("card 4111 1111 1111 1111 seen", "4111 1111 1111 1111", "card_number"),
    ("reference 000-00-0000", "000-00-0000", "national_id"),
    ("api_key=SYNTHETIC-EXAMPLE-NOT-A-REAL-KEY-2", "SYNTHETIC-EXAMPLE", "credential"),
])
def test_T_FR13_6_private_data_is_redacted_and_the_row_is_still_written(log, value, secret,
                                                                       pattern):
    """The block must be *recorded* (FR-12) without the value reaching the log (NFR-04)."""
    log.record(entry(detail=value))
    log.record(entry(decision="escalate", reason=value))
    log.record(entry(all_reasons=(value,)))

    rows = log.rows()
    assert len(rows) == 3, "a decision must never be dropped because of what it contains"
    assert secret not in json.dumps(rows), "the private value reached the log"
    assert f"[redacted:{pattern}]" in rows[0]["detail"]
    assert f"[redacted:{pattern}]" in rows[1]["reason"]
    assert f"[redacted:{pattern}]" in rows[2]["all_reasons"][0]
    assert f"detail:{pattern}" in rows[0]["redactions"]
    assert f"reason:{pattern}" in rows[1]["redactions"]
    assert f"all_reasons:{pattern}" in rows[2]["redactions"]


@pytest.mark.parametrize("value", [
    "token: expired",
    "invoice 4 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 items",
    "grounding failed on 3 of 12 sentences",
    "matched triggers: refund, chargeback",
])
def test_T_FR13_20_legitimate_details_are_not_redacted(log, value):
    """A pattern-naming detail is exactly what FR-12 asks for; it must survive intact."""
    log.record(entry(detail=value))
    row = log.rows()[0]
    assert row["detail"] == value
    assert row["redactions"] == []


def test_T_FR13_21_an_over_long_detail_is_truncated_not_rejected(log):
    log.record(entry(detail="x" * (MAX_TEXT_FIELD + 500), summary="y" * (MAX_TEXT_FIELD + 1)))
    row = log.rows()[0]
    assert len(row["detail"]) == MAX_TEXT_FIELD + len(" …[truncated]")
    assert {"truncated:detail", "truncated:summary"} <= set(row["redactions"])


@pytest.mark.parametrize(("overrides", "match"), [
    ({"decision": "send_it"}, "decision"),
    ({"ticket_id": "  "}, "ticket_id"),
    ({"stage": "somewhere"}, "stage"),
])
def test_T_FR13_7_vocabulary_is_enforced(log, overrides, match):
    with pytest.raises(InvalidDecision, match=match):
        log.record(entry(**overrides))
    assert log.rows() == []


def test_T_FR13_8_the_row_is_committed_before_the_action(log):
    """CLAUDE.md: the decision is written before the action is taken."""
    seen = {}

    def action():
        # A separate connection sees only committed data.
        with sqlite3.connect(log.path) as conn:
            conn.row_factory = sqlite3.Row
            seen["rows"] = [dict(r) for r in conn.execute("SELECT * FROM decisions")]
        return "sent"

    result = log.perform(entry(decision="escalate", reason="no_retrieval"), action)
    assert result == "sent"
    assert len(seen["rows"]) == 1, "the action ran before the decision was durable"
    assert seen["rows"][0]["reason"] == "no_retrieval"


def test_T_FR13_9_a_failing_action_leaves_the_decision_logged(log):
    def action():
        raise RuntimeError("sending failed")

    with pytest.raises(RuntimeError, match="sending failed"):
        log.perform(entry(decision="auto_respond"), action)
    assert len(log.rows()) == 1, "the decision was taken, so it stays logged"


def test_T_FR13_10_a_clean_run_reconciles(log):
    tickets = [normalise_ticket(e, index=i) for i, e in
               enumerate(json.loads((FIXTURES / "money_commitment_tickets.json").read_text()))]
    for ticket in tickets:
        escalating = evaluation_labels(ticket).get("expected_route") == "escalate"
        log.record(DecisionEntry(
            ticket_id=ticket.ticket_id,
            source_index=ticket.source_index,
            stage="route",
            decision="escalate" if escalating else "auto_respond",
            reason="money_commitment_requested" if escalating else None,
            requirement_ids=("FR-03",),
            **ticket.log_fields_for_log(),
        ))

    report = log.reconcile(tickets)
    assert report.ok, report
    assert report.tickets_in == len(tickets) == report.terminal_rows
    assert not report.missing and not report.extra and not report.duplicated
    assert sum(report.counts_by_decision.values()) == len(tickets)
    assert report.counts_by_decision["escalate"] >= 1
    assert report.counts_by_decision["auto_respond"] >= 1


def test_T_FR13_11_reconcile_reports_every_discrepancy(log):
    tickets = [normalise_ticket({"ticket_id": f"SYN-R-{i}", "channel": "email",
                                 "subject": "s", "body": "b"}, index=i) for i in range(4)]
    # ticket 0 logged once (fine), ticket 1 twice, ticket 2 not at all, plus a stranger.
    log.record(entry(ticket_id="SYN-R-0", source_index=0))
    log.record(entry(ticket_id="SYN-R-1", source_index=1))
    log.record(entry(ticket_id="SYN-R-1", source_index=1))
    log.record(entry(ticket_id="SYN-R-3", source_index=3))
    log.record(entry(ticket_id="SYN-STRANGER", source_index=99))

    report = log.reconcile(tickets)
    assert not report.ok
    assert report.missing == ("SYN-R-2",)
    assert report.duplicated == ("SYN-R-1",)
    assert "SYN-STRANGER" in report.extra
    assert 2 in report.index_gaps


def test_T_FR13_12_duplicate_input_ids_reconcile_on_source_index(log):
    entries = [{"ticket_id": "SYN-DUP-001", "channel": "email", "subject": "s", "body": "one"},
               {"ticket_id": "SYN-DUP-001", "channel": "email", "subject": "s", "body": "two"}]
    seen: dict[str, int] = {}
    tickets = [normalise_ticket(e, index=i, seen_ids=seen) for i, e in enumerate(entries)]
    for ticket in tickets:
        log.record(entry(ticket_id=ticket.ticket_id, source_index=ticket.source_index))

    report = log.reconcile(tickets)
    assert report.ok, report
    assert report.index_gaps == ()
    assert report.duplicate_input_ids == ("SYN-DUP-001",)  # informational, not a failure
    assert report.duplicated == ()


def test_T_FR13_13_continue_rows_are_not_terminal(log):
    log.record(entry(stage="classify", decision="continue"))
    log.record(entry(stage="retrieve", decision="continue"))
    log.record(entry(stage="route", decision="auto_respond"))

    assert len(log.rows()) == 3
    assert len(log.terminal_rows()) == 1
    tickets = [normalise_ticket({"ticket_id": "SYN-BILL-001", "channel": "email",
                                 "subject": "s", "body": "b"}, index=0)]
    assert log.reconcile(tickets).ok


def test_T_FR13_14_rows_persist_across_reopen(tmp_path):
    path = tmp_path / "decisions.db"
    with DecisionLog(path, run_id="run-one") as first:
        first.record(entry())
    with DecisionLog(path, run_id="run-two") as second:
        rows = second.rows()
        assert len(rows) == 1
        assert rows[0]["run_id"] == "run-one"
        second.record(entry(ticket_id="SYN-BILL-002", source_index=1))
        assert len(second.rows()) == 2
        assert len(second.rows(run_id="run-two")) == 1


def test_T_FR13_15_a_failed_write_falls_back_and_raises(tmp_path):
    path = tmp_path / "decisions.db"
    with DecisionLog(path) as store:
        # Break the database for real rather than patching a seam: a closed connection is
        # what a locked or vanished database looks like from here.
        store._connection.close()
        with pytest.raises(DecisionLogUnavailable):
            store.record(entry(decision="escalate", reason="provider_unavailable"))

    fallback = Path(str(path) + ".fallback.jsonl")
    assert fallback.exists(), "the decision must survive even when the database cannot"
    recovered = json.loads(fallback.read_text(encoding="utf-8").splitlines()[0])
    assert recovered["reason"] == "provider_unavailable"
    assert recovered["requirement_ids"] == ["FR-02"]


def test_T_FR13_16_no_way_to_skip_logging():
    """CLAUDE.md: no flag, env var or except may switch logging off."""
    import inspect

    from ticketing_agent import logging_store

    source = Path(logging_store.__file__).read_text(encoding="utf-8")
    for smell in ("if not enabled", "disable_log", "skip_log", "dry_run", "no_log", "LOG_DISABLED"):
        assert smell not in source, f"logging can be switched off via {smell!r}"

    for name in ("record", "perform"):
        params = inspect.signature(getattr(DecisionLog, name)).parameters
        assert not {"enabled", "skip", "dry_run", "disable"} & set(params), name

    # os.environ is never consulted: the store's behaviour cannot depend on the environment.
    assert "os.environ" not in source and "getenv" not in source

    # Spec §3.3: no `except` may skip a write. Every handler in the module ends in a raise
    # (or retries and then raises), which a name-based smell list cannot check.
    import ast
    for handler in (n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ExceptHandler)):
        raises = any(isinstance(n, ast.Raise) for n in ast.walk(handler))
        returns_false = any(isinstance(n, ast.Return) for n in ast.walk(handler))
        assert raises or returns_false, (
            f"the handler at line {handler.lineno} neither raises nor reports failure"
        )


def test_T_FR13_17_two_stores_on_one_path_both_write(tmp_path):
    path = tmp_path / "decisions.db"
    with DecisionLog(path, run_id="run-a") as a, DecisionLog(path, run_id="run-b") as b:
        a.record(entry(ticket_id="SYN-A", source_index=0))
        b.record(entry(ticket_id="SYN-B", source_index=1))
        a.record(entry(ticket_id="SYN-A2", source_index=2))
        assert len(a.rows()) == 3
        assert len(b.rows()) == 3
        assert {r["run_id"] for r in b.rows()} == {"run-a", "run-b"}


def test_T_FR13_18_the_store_adds_nothing_that_breaks_determinism(tmp_path):
    """NFR-08: the same decision logged in two runs differs only in bookkeeping."""
    volatile = {"row_id", "logged_at", "run_id"}
    snapshots = []
    for run in ("run-1", "run-2"):
        path = tmp_path / f"{run}.db"
        with DecisionLog(path, run_id=run) as store:
            store.record(entry(decision="escalate", reason="low_confidence",
                               intent="billing_query", intent_confidence=0.4,
                               requirement_ids=("FR-02",)))
            snapshots.append({k: v for k, v in store.rows()[0].items() if k not in volatile})
    assert snapshots[0] == snapshots[1]


def test_T_FR13_22_aggregates_feed_the_metrics_report(log):
    """§3.4: NFR-07 counts every model call in the run; NFR-01 times each ticket."""
    log.record(entry(stage="generate", decision="continue", model_calls=2, cache_hits=1,
                     prompt_version="PR-01 v1.0", latency_ms=90.0))
    log.record(entry(stage="route", decision="escalate", reason="low_confidence",
                     model_calls=1, cache_hits=3, prompt_version="PR-01 v1.0",
                     latency_ms=210.5))
    log.record(entry(ticket_id="SYN-BILL-002", source_index=1, decision="auto_respond",
                     latency_ms=110.0))

    tickets = [normalise_ticket({"ticket_id": tid, "channel": "email", "subject": "s",
                                 "body": "b"}, index=i)
               for i, tid in enumerate(("SYN-BILL-001", "SYN-BILL-002"))]
    report = log.reconcile(tickets)
    assert report.ok, report
    assert report.model_calls == 3, "intermediate rows count towards NFR-07"
    assert report.cache_hits == 4
    assert sorted(report.latencies_ms) == [110.0, 210.5], "only terminal rows time a ticket"
    assert report.counts_by_reason == {"low_confidence": 1}
    assert report.counts_by_decision == {"escalate": 1, "auto_respond": 1}


def test_T_FR13_23_the_run_record_cross_checks_the_ticket_count(tmp_path):
    """A ticket lost before the caller built its list must not reconcile as ok."""
    tickets = [normalise_ticket({"ticket_id": f"SYN-N-{i}", "channel": "email",
                                 "subject": "s", "body": "b"}, index=i) for i in range(3)]
    with DecisionLog(tmp_path / "decisions.db", run_id="run-x") as store:
        store.start_run("data/whatever-was-given.json", tickets_in=3)
        for ticket in tickets[:2]:  # the third never reached the caller's list
            store.record(entry(ticket_id=ticket.ticket_id, source_index=ticket.source_index))

        report = store.reconcile(tickets[:2])
        assert not report.ok, "the run recorded 3 tickets in, so 2 terminal rows is not ok"
        assert report.recorded_tickets_in == 3

        store.record(entry(ticket_id="SYN-N-2", source_index=2))
        assert store.reconcile(tickets).ok

        store.finish_run(tickets_out=3)
        run = store.runs()[0]
        assert run["input_path"] == "data/whatever-was-given.json"
        assert (run["tickets_in"], run["tickets_out"]) == (3, 3)
        assert run["started_at"] and run["finished_at"]


def test_T_FR13_24_finish_run_without_start_run_still_records(tmp_path):
    with DecisionLog(tmp_path / "decisions.db", run_id="run-y") as store:
        store.finish_run(tickets_out=7)
        assert store.runs()[0]["tickets_out"] == 7


def test_T_FR13_25_tickets_without_a_source_index_are_reported(log):
    """D-12: index coverage is the reconciliation key, so a missing index must not be silent."""
    tickets = [normalise_ticket({"ticket_id": "SYN-NOIDX", "channel": "email",
                                 "subject": "s", "body": "b"})]
    log.record(entry(ticket_id="SYN-NOIDX", source_index=None))
    report = log.reconcile(tickets)
    assert report.tickets_without_index == ("SYN-NOIDX",)
    assert not report.ok, "an unreconcilable ticket must not pass as ok"


def test_T_FR13_26_durability_settings_are_pinned(log):
    """§3.3: the row is durable on return because of these three settings, so pin them."""
    assert log._connection.isolation_level is None
    mode = log._connection.execute("PRAGMA journal_mode").fetchone()[0]
    synchronous = log._connection.execute("PRAGMA synchronous").fetchone()[0]
    assert mode.lower() == "wal"
    assert synchronous == 2  # FULL


def test_T_FR13_27_none_for_a_sequence_field_becomes_an_empty_list(log):
    log.record(entry(all_reasons=None, citations=None, ingest_defects=None))
    row = log.rows()[0]
    assert row["all_reasons"] == [] and row["citations"] == [] and row["ingest_defects"] == []


def test_T_FR13_19_ingest_fields_reach_the_row_unchanged(log):
    ticket = normalise_ticket({
        "ticket_id": "SYN-SEG-001", "channel": "docs_comment", "subject": "",
        "body": "How is proration calculated?", "received_at": "2026-05-10T10:05:00Z",
        "customer_tier": "business", "customer_region": "asia_pacific",
        "language_fluency": "fluent",
    }, index=7)
    log.record(DecisionEntry(
        ticket_id=ticket.ticket_id, source_index=ticket.source_index, stage="route",
        decision="auto_respond", requirement_ids=("FR-02",), **ticket.log_fields_for_log(),
    ))
    row = log.rows()[0]
    assert (row["channel"], row["tier"], row["region"], row["fluency"]) == (
        "docs_comment", "business", "asia_pacific", "fluent")
    assert row["received_at"] == "2026-05-10T10:05:00Z"
    assert row["ingest_defects"] == ["missing_subject"]
    assert row["source_index"] == 7
