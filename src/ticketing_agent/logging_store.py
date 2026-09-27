"""FR-13: persistent decision log (Governance Framework schema).

Spec: docs/specs/FR-13.md. Three things are structural rather than conventional, because
CLAUDE.md makes them non-negotiable:

* `perform()` writes and commits the row **before** it calls the action. It is the supported
  way to act on a decision; row 14 wires the pipeline so every action goes through it.
* Nothing here reads the environment and no public function takes a parameter that skips a
  write. Every `except` in this module ends in a `raise`.
* A decision is never lost. If the database cannot take the row it goes to a JSONL fallback,
  and if the fallback cannot be written either, `DecisionLogUnavailable` still says so. If a
  field carries something that looks private it is **redacted to a pattern name and the row is
  still written**, because FR-12's acceptance criterion is that the block is *recorded*.
  Redaction here is about the log; the reply itself is still blocked, never redacted (NFR-04).

The columns implement the Governance Framework's "minimum record" (Capstone_Pack
03_Reference/Governance_Framework.docx §1) field for field. `governance_record()` emits a row
in exactly that document's shape, which is what an assessor opens. The stage and action
vocabularies are the framework's; this module adds a few stages it does not name.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self, TypeVar

SCHEMA_VERSION = 1

#: Terminal decisions end a ticket. `block` and `continue` do not: a blocked reply still ends
#: as an escalation, so it is recorded and then followed by a terminal row (§3.1).
TERMINAL_DECISIONS = frozenset({"auto_respond", "escalate"})
#: The Governance Framework's `action_taken` vocabulary, plus `continue` for the intermediate
#: audit rows it does not name.
DECISIONS = TERMINAL_DECISIONS | {"block", "continue"}
#: The framework names four stages; ingest, retrieval, handover and pipeline are ours.
FRAMEWORK_STAGES = frozenset({"classification", "routing", "generation", "validation"})
STAGES = FRAMEWORK_STAGES | {"ingest", "retrieval", "handover", "pipeline"}

#: Free-text log fields are capped: `detail` should name a pattern, not carry a draft, and an
#: unbounded value would put the scrub below on the write path for no reason (§3.2).
MAX_TEXT_FIELD = 4000

#: A last-ditch scrub of the fields that should never carry customer text (§3.2.6). FR-12's
#: tables are canonical; these exist so the log cannot become the leak. Every pattern is
#: linear: `detail` can contain attacker-influenced text, so a backtracking regex is a stall.
_PRIVATE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[^\s@]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")),
    ("national_id", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("credential", re.compile(
        r"(?i)(?:password|passwd|secret|token|api[_-]?key|private[_-]?key)\s*[:=]\s*\S{12,}")),
    ("private_key", re.compile(r"(?i)private key\b|BEGIN [A-Z ]*PRIVATE KEY")),
)
#: Card-shaped runs: one long run of digits, or groups of two or more. Single spaced digits
#: ("4 1 1 1 …") are not a card number even when they happen to pass Luhn.
_CARD_PATTERNS = (re.compile(r"\b\d{13,19}\b"),
                  re.compile(r"\b\d{2,}(?:[ -]\d{2,}){2,}\b"))

T = TypeVar("T")


class DecisionLogError(Exception):
    """FR-13: base class for decision-log failures, so a caller can catch one thing."""


class InvalidDecision(DecisionLogError):
    """FR-13 §3.2: the call itself is wrong (no requirement ids, an unknown decision …).

    Only reachable from a mistake at the call site, never from ticket data: anything
    data-shaped is redacted or truncated and the row is still written.
    """


class DecisionLogUnavailable(DecisionLogError):
    """FR-13 §4: the log could not be written. The row is in the fallback file, if possible."""


@dataclass(frozen=True)
class DecisionEntry:
    """FR-13: one automated decision, with the fields an audit needs to answer 'why?'."""

    ticket_id: str
    stage: str
    decision: str
    requirement_ids: Sequence[str]
    source_index: int | None = None
    reason: str | None = None
    all_reasons: Sequence[str] = ()
    detail: str | None = None
    explanation: str | None = None
    summary: str | None = None
    uncertainty: str | None = None
    prediction_value: str | None = None
    prediction_confidence: float | None = None
    threshold_applied: float | None = None
    intent: str | None = None
    intent_confidence: float | None = None
    intent_alternatives: Sequence[Sequence[Any]] = ()
    urgency: str | None = None
    urgency_confidence: float | None = None
    sources_used: Sequence[Sequence[Any]] = ()
    retrieved_doc_ids: Sequence[str] = ()
    citations: Sequence[str] = ()
    guardrail_results: Sequence[Sequence[Any]] = ()
    prompt_version: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    model_calls: int = 0
    cache_hits: int = 0
    latency_ms: float | None = None
    kill_switch: bool = False
    ingest_defects: Sequence[str] = ()
    received_at: str | None = None
    channel: str | None = None
    tier: str | None = None
    region: str | None = None
    fluency: str | None = None

    @property
    def is_terminal(self) -> bool:
        """FR-13 §3.1: true when this decision ends the ticket, which reconciliation counts."""
        return self.decision in TERMINAL_DECISIONS


@dataclass(frozen=True)
class Reconciliation:
    """FR-13 §3.4: do the logged decisions account for exactly the tickets processed?"""

    tickets_in: int
    terminal_rows: int
    missing: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()
    duplicated: tuple[str, ...] = ()
    index_gaps: tuple[int, ...] = ()
    tickets_without_index: tuple[str, ...] = ()
    duplicate_input_ids: tuple[str, ...] = ()
    recorded_tickets_in: int | None = None
    counts_by_decision: dict[str, int] = field(default_factory=dict)
    counts_by_reason: dict[str, int] = field(default_factory=dict)
    redactions: int = 0
    model_calls: int = 0
    cache_hits: int = 0
    latencies_ms: tuple[float, ...] = ()

    @property
    def ok(self) -> bool:
        """FR-13: true only when every ticket has exactly one terminal row and nothing is stray.

        `recorded_tickets_in` is the count `start_run` wrote, so a ticket lost before the caller
        built the list it passes here is caught too.
        """
        counts_agree = self.recorded_tickets_in in (None, self.tickets_in)
        return (
            self.tickets_in == self.terminal_rows
            and counts_agree
            and not self.missing
            and not self.extra
            and not self.duplicated
            and not self.index_gaps
            and not self.tickets_without_index
        )


_JSON_COLUMNS = ("requirement_ids", "all_reasons", "intent_alternatives", "sources_used",
                 "retrieved_doc_ids", "citations", "guardrail_results", "ingest_defects",
                 "redactions")
_BOOL_COLUMNS = ("kill_switch",)
_SCRUBBED_COLUMNS = ("reason", "detail", "explanation")
_TRUNCATED_COLUMNS = ("reason", "detail", "explanation", "summary", "uncertainty")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    row_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id       TEXT    NOT NULL UNIQUE,
    run_id            TEXT    NOT NULL,
    logged_at         TEXT    NOT NULL,
    schema_version    INTEGER NOT NULL,
    ticket_id         TEXT    NOT NULL,
    source_index      INTEGER,
    stage             TEXT    NOT NULL,
    decision          TEXT    NOT NULL,
    reason            TEXT,
    all_reasons       TEXT    NOT NULL,
    detail            TEXT,
    explanation       TEXT,
    summary           TEXT,
    uncertainty       TEXT,
    prediction_value  TEXT,
    prediction_confidence REAL,
    threshold_applied REAL,
    intent            TEXT,
    intent_confidence REAL,
    intent_alternatives TEXT  NOT NULL,
    urgency           TEXT,
    urgency_confidence REAL,
    sources_used      TEXT    NOT NULL,
    retrieved_doc_ids TEXT    NOT NULL,
    citations         TEXT    NOT NULL,
    guardrail_results TEXT    NOT NULL,
    prompt_version    TEXT,
    model_name        TEXT,
    model_version     TEXT,
    model_calls       INTEGER NOT NULL DEFAULT 0,
    cache_hits        INTEGER NOT NULL DEFAULT 0,
    latency_ms        REAL,
    kill_switch       INTEGER NOT NULL DEFAULT 0,
    ingest_defects    TEXT    NOT NULL,
    received_at       TEXT,
    channel           TEXT,
    tier              TEXT,
    region            TEXT,
    fluency           TEXT,
    requirement_ids   TEXT    NOT NULL,
    redactions        TEXT    NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_decisions_decision_id ON decisions (decision_id);
CREATE INDEX IF NOT EXISTS idx_decisions_ticket ON decisions (ticket_id);
CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions (run_id);
CREATE INDEX IF NOT EXISTS idx_decisions_run_index ON decisions (run_id, source_index);

CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    input_path  TEXT,
    tickets_in  INTEGER,
    finished_at TEXT,
    tickets_out INTEGER
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class DecisionLog:
    """FR-13: the persistent decision log. A context manager; safe to open twice on one path."""

    def __init__(self, path: str | Path, run_id: str | None = None) -> None:
        self.path = Path(path)
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        self.schema_version = SCHEMA_VERSION
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
            self._connection.row_factory = sqlite3.Row
            self._connection.executescript(_SCHEMA)
            # isolation_level=None means each statement commits as it runs, so a row is durable
            # by the time `record` returns. WAL lets a second process read and write alongside.
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        except (sqlite3.Error, OSError) as exc:
            raise DecisionLogUnavailable(
                f"cannot open the decision log at {self.path}: {exc}"
            ) from exc

    # --- writing ------------------------------------------------------------------

    def record(self, entry: DecisionEntry) -> int:
        """FR-13: validate, scrub, write and commit one decision. Returns its row_id."""
        _validate(entry)
        row = self._row_values(entry)
        columns = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)
        statement = f"INSERT INTO decisions ({columns}) VALUES ({placeholders})"

        for attempt in (1, 2):
            try:
                cursor = self._connection.execute(statement, row)
                return int(cursor.lastrowid)
            except (sqlite3.Error, OSError) as exc:
                if attempt == 1:
                    time.sleep(0.05)  # a lock is the likely cause: one retry, then fail loudly
                    continue
                saved = self._write_fallback(entry, row["logged_at"])
                where = (
                    f"the row was appended to {self.fallback_path}" if saved else
                    "the fallback file could not be written either, so this row survives only "
                    "in this exception"
                )
                raise DecisionLogUnavailable(
                    f"cannot write the decision log at {self.path}: {exc}; {where}"
                ) from exc
        raise AssertionError("unreachable")  # pragma: no cover

    def perform(self, entry: DecisionEntry, action: Callable[[], T]) -> T:
        """FR-13, CLAUDE.md: log the decision, commit it, **then** take the action."""
        self.record(entry)
        return action()

    def start_run(self, input_path: str | Path | None, tickets_in: int | None = None) -> None:
        """FR-13, FR-14: record what this run read, so §3.4 can cross-check the ticket count."""
        self._run_write(
            "INSERT INTO runs (run_id, started_at, input_path, tickets_in) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET input_path=excluded.input_path, "
            "tickets_in=excluded.tickets_in",
            (self.run_id, _now(), str(input_path) if input_path is not None else None, tickets_in),
        )

    def finish_run(self, tickets_out: int | None = None) -> None:
        """FR-13, FR-14: close this run's record. Creates the row if `start_run` never ran."""
        self._run_write(
            "INSERT INTO runs (run_id, started_at, finished_at, tickets_out) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET finished_at=excluded.finished_at, "
            "tickets_out=excluded.tickets_out",
            (self.run_id, _now(), _now(), tickets_out),
        )

    # --- reading ------------------------------------------------------------------

    def rows(self, ticket_id: str | None = None,
             run_id: str | None = None) -> list[dict[str, Any]]:
        """FR-13: every logged decision, oldest first, with the JSON columns decoded."""
        clauses, params = [], []
        if ticket_id is not None:
            clauses.append("ticket_id = ?")
            params.append(ticket_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        cursor = self._connection.execute(
            f"SELECT * FROM decisions{where} ORDER BY row_id", params
        )
        return [_decode(dict(row)) for row in cursor]

    def terminal_rows(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """FR-13 §3.1: the rows that ended a ticket, which is what reconciliation counts."""
        return [r for r in self.rows(run_id=run_id) if r["decision"] in TERMINAL_DECISIONS]

    def runs(self) -> list[dict[str, Any]]:
        """FR-13, FR-14: the run records, oldest first."""
        return [dict(r) for r in
                self._connection.execute("SELECT * FROM runs ORDER BY started_at")]

    # --- reconciliation -----------------------------------------------------------

    def reconcile(self, tickets: Iterable[Any], run_id: str | None = None) -> Reconciliation:
        """FR-13 §3.4: do the terminal rows account for exactly the tickets processed?"""
        tickets = list(tickets)
        run = run_id if run_id is not None else self.run_id
        rows = self.terminal_rows(run_id=run)
        every_row = self.rows(run_id=run)

        ticket_ids = [t.ticket_id for t in tickets]
        logged_ids = [r["ticket_id"] for r in rows]
        expected = set(ticket_ids)

        missing = tuple(sorted({i for i in expected if logged_ids.count(i) == 0}))
        extra = tuple(sorted({i for i in logged_ids if i not in expected}))
        # An id logged twice is only wrong if the input held it once: a file may legitimately
        # repeat an id (FR-07 flags it, D-12 keeps it), and then two rows are correct.
        duplicated = tuple(sorted({
            i for i in logged_ids
            if i in expected and logged_ids.count(i) > ticket_ids.count(i)
        }))
        duplicate_input_ids = tuple(sorted({i for i in ticket_ids if ticket_ids.count(i) > 1}))

        # source_index coverage is authoritative (D-12): it survives duplicate ticket ids. A
        # ticket with no index cannot be reconciled that way, so it is reported, not skipped.
        tickets_without_index = tuple(sorted(
            t.ticket_id for t in tickets if t.source_index is None
        ))
        wanted = {t.source_index for t in tickets if t.source_index is not None}
        logged_indexes = [r["source_index"] for r in rows if r["source_index"] is not None]
        index_gaps = tuple(sorted(i for i in wanted if logged_indexes.count(i) != 1))

        counts_by_decision: dict[str, int] = {}
        counts_by_reason: dict[str, int] = {}
        for row in rows:
            counts_by_decision[row["decision"]] = counts_by_decision.get(row["decision"], 0) + 1
            if row["reason"]:
                counts_by_reason[row["reason"]] = counts_by_reason.get(row["reason"], 0) + 1

        recorded = next((r["tickets_in"] for r in self.runs() if r["run_id"] == run), None)
        return Reconciliation(
            tickets_in=len(tickets),
            terminal_rows=len(rows),
            missing=missing,
            extra=extra,
            duplicated=duplicated,
            index_gaps=index_gaps,
            tickets_without_index=tickets_without_index,
            duplicate_input_ids=duplicate_input_ids,
            recorded_tickets_in=recorded,
            counts_by_decision=counts_by_decision,
            counts_by_reason=counts_by_reason,
            # NFR-07 counts every model call and cache hit in the run, intermediate rows
            # included; NFR-01 measures each ticket's end-to-end latency, which only the
            # terminal row carries.
            redactions=sum(len(r["redactions"]) for r in every_row),
            model_calls=sum(r["model_calls"] for r in every_row),
            cache_hits=sum(r["cache_hits"] for r in every_row),
            latencies_ms=tuple(r["latency_ms"] for r in rows if r["latency_ms"] is not None),
        )

    # --- lifecycle ----------------------------------------------------------------

    @property
    def fallback_path(self) -> Path:
        """FR-13 §4: where a row goes when the database cannot take it."""
        return Path(str(self.path) + ".fallback.jsonl")

    def close(self) -> None:
        """Close the connection. Every row is already committed, so nothing is flushed here."""
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- internals ----------------------------------------------------------------

    def _run_write(self, statement: str, params: tuple[Any, ...]) -> None:
        """Run-table writes get the same treatment as decisions: never a bare sqlite error."""
        for attempt in (1, 2):
            try:
                self._connection.execute(statement, params)
                return
            except (sqlite3.Error, OSError) as exc:
                if attempt == 1:
                    time.sleep(0.05)
                    continue
                raise DecisionLogUnavailable(
                    f"cannot write the run record at {self.path}: {exc}"
                ) from exc

    def _row_values(self, entry: DecisionEntry) -> dict[str, Any]:
        values: dict[str, Any] = {
            "decision_id": f"{self.run_id}:{uuid.uuid4().hex[:12]}",
            "run_id": self.run_id,
            "logged_at": _now(),
            "schema_version": SCHEMA_VERSION,
        }
        redactions: list[str] = []
        for f in fields(entry):
            value = getattr(entry, f.name)
            # Redact first (the scrub caps its own input), then mark the truncation, so the
            # marker is not itself cut off.
            was_long = isinstance(value, str) and len(value) > MAX_TEXT_FIELD
            if f.name in _SCRUBBED_COLUMNS and value:
                value, found = _redact(str(value))
                redactions.extend(f"{f.name}:{name}" for name in found)
            if f.name in _TRUNCATED_COLUMNS and was_long:
                value = str(value)[:MAX_TEXT_FIELD] + " …[truncated]"
                redactions.append(f"truncated:{f.name}")
            if f.name == "all_reasons" and value:
                cleaned = []
                for item in value:
                    scrubbed, found = _redact(str(item))
                    cleaned.append(scrubbed)
                    redactions.extend(f"all_reasons:{name}" for name in found)
                value = cleaned
            if f.name == "retrieved_doc_ids" and not value and entry.sources_used:
                # The framework records sources as doc_id plus score; the flat list of ids is a
                # convenience for querying, so derive it rather than making callers repeat it.
                value = [str(source[0]) for source in entry.sources_used if source]
            if f.name in _JSON_COLUMNS:
                values[f.name] = json.dumps(_plain(value))
            elif f.name in _BOOL_COLUMNS:
                values[f.name] = int(bool(value))
            else:
                values[f.name] = value
        values["redactions"] = json.dumps(sorted(set(redactions)))
        return values

    def _write_fallback(self, entry: DecisionEntry, logged_at: str) -> bool:
        """FR-13 §4: preserve the decision outside the database. Says whether it worked."""
        record = {k: _plain(v) for k, v in asdict(entry).items()}
        for name in _SCRUBBED_COLUMNS:
            if record.get(name):
                record[name] = _redact(str(record[name]))[0]
        record.update(run_id=self.run_id, logged_at=logged_at, schema_version=SCHEMA_VERSION)
        try:
            self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
            with self.fallback_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        except OSError:
            return False
        return True


def governance_record(row: dict[str, Any]) -> dict[str, Any]:
    """FR-13: one logged row in the Governance Framework's "minimum record" shape (§1).

    The stored row is richer (segments, defects, cache hits) because the PRD and the other
    specs need those. This is the projection an assessor reads, with the framework's own field
    names and nesting, so the two can be compared without translation.
    """
    return {
        "decision_id": row["decision_id"],
        "timestamp": row["logged_at"],
        "ticket_id": row["ticket_id"],
        "stage": row["stage"],
        "input_summary": row["summary"],
        "model": {"name": row["model_name"], "version": row["model_version"]},
        "prediction": {"value": row["prediction_value"],
                       "confidence": row["prediction_confidence"]},
        "alternatives": [{"value": value, "confidence": confidence}
                         for value, confidence in row["intent_alternatives"]],
        "sources_used": [{"doc_id": doc_id, "score": score}
                         for doc_id, score in row["sources_used"]],
        "threshold_applied": row["threshold_applied"],
        "action_taken": row["decision"],
        "reason": row["explanation"] or row["reason"],
        "guardrail_results": {name: ("pass" if passed else "fail")
                              for name, passed in row["guardrail_results"]},
        "prompt_version": row["prompt_version"],
        "requirement_ids": row["requirement_ids"],
    }


def _validate(entry: DecisionEntry) -> None:
    """FR-13 §3.2: reject a call that cannot be audited. Never reachable from ticket data."""
    if not entry.requirement_ids:
        raise InvalidDecision(
            f"{entry.ticket_id}: requirement_ids is empty; an unattributed decision cannot "
            "be audited (NFR-05)"
        )
    if not str(entry.ticket_id).strip():
        raise InvalidDecision("ticket_id is blank; the row could not be reconciled")
    if entry.decision not in DECISIONS:
        raise InvalidDecision(
            f"{entry.ticket_id}: decision {entry.decision!r} is not one of {sorted(DECISIONS)}"
        )
    if entry.stage not in STAGES:
        raise InvalidDecision(f"{entry.ticket_id}: stage {entry.stage!r} is not a known stage")
    if entry.decision == "escalate" and not (entry.reason or "").strip():
        raise InvalidDecision(
            f"{entry.ticket_id}: an escalation needs a reason — the log has to answer "
            "'why did it do that?'"
        )
    if entry.model_calls > 0 and not (entry.prompt_version or "").strip():
        raise InvalidDecision(
            f"{entry.ticket_id}: {entry.model_calls} model call(s) with no prompt_version; "
            "the log must say which prompt produced the output"
        )
    if entry.is_terminal and not (entry.explanation or "").strip():
        raise InvalidDecision(
            f"{entry.ticket_id}: a terminal decision needs an explanation a support manager "
            "could read (Build Specification, Route: 'records the reason for the decision in "
            "language a support manager could read')"
        )
    if entry.decision == "auto_respond" and entry.threshold_applied is None:
        raise InvalidDecision(
            f"{entry.ticket_id}: auto_respond with no threshold_applied. The Governance "
            "Framework's confidence floor is only demonstrable if the threshold that was "
            "applied is recorded (FR-02)"
        )


def _redact(text: str) -> tuple[str, list[str]]:
    """FR-13 §3.2.6: replace anything private with its pattern name, keeping the row writable.

    The decision must still be recorded (FR-12's acceptance criterion) and the value must not
    reach the log (NFR-04), so this redacts rather than rejecting. It applies to the log only:
    the reply itself is blocked, never redacted.
    """
    found: list[str] = []
    scrubbed = text[:MAX_TEXT_FIELD]
    for name, pattern in _PRIVATE_PATTERNS:
        scrubbed, count = pattern.subn(f"[redacted:{name}]", scrubbed)
        if count:
            found.append(name)
    for pattern in _CARD_PATTERNS:
        scrubbed, count = pattern.subn(_redact_card, scrubbed)
        if count and "card_number" not in found and "[redacted:card_number]" in scrubbed:
            found.append("card_number")
    return scrubbed, found


def _redact_card(match: re.Match[str]) -> str:
    return "[redacted:card_number]" if _luhn_ok(match.group()) else match.group()


def _luhn_ok(text: str) -> bool:
    digits = [int(c) for c in text if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, digit in enumerate(reversed(digits)):
        doubled = digit * 2
        total += (doubled - 9 if doubled > 9 else doubled) if i % 2 else digit
    return total % 10 == 0


def _decode(row: dict[str, Any]) -> dict[str, Any]:
    for column in _JSON_COLUMNS:
        decoded = json.loads(row[column]) if row.get(column) else []
        row[column] = [] if decoded is None else decoded
    for column in _BOOL_COLUMNS:
        row[column] = bool(row[column])
    return row


def _plain(value: Any) -> Any:
    """Tuples become lists so the JSON round-trip is stable; None becomes an empty list."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
