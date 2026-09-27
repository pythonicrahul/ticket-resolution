"""FR-07: normalise tickets from all four channels into one internal representation.

Spec: docs/specs/FR-07.md. Pure and offline: no model calls, no I/O beyond reading the
input file, so it cannot be the thing that fails mid-run. Nothing raises per ticket and
nothing is dropped: a ticket we cannot make sense of comes back flagged (`is_malformed`)
so the pipeline logs it and escalates it (FR-09, FR-13).
"""
from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

CHANNELS = frozenset({"email", "chat", "docs_comment", "forum"})
TIERS = frozenset({"standard", "business", "enterprise"})
REGIONS = frozenset({"north_america", "europe", "asia_pacific", "latin_america"})
FLUENCIES = frozenset({"fluent", "non_fluent"})
UNKNOWN = "unknown"

#: Channels that carry a subject line. Every supplied `chat` ticket has none by design,
#: so a missing subject there is not a defect.
CHANNELS_WITH_SUBJECT = frozenset({"email", "docs_comment", "forum"})

#: Cap on the text handed to the classifier, retriever and prompts. The longest supplied
#: body is 245 characters; the cap only bites on engineered input. `body` keeps it all.
MAX_TEXT_CHARS = 8000

#: Defects that mean we must not attempt an answer: there is no usable text, or we do not
#: know what kind of message this is. The conservative reading of FR-07 (docs/specs/FR-07.md).
BLOCKING_DEFECTS = frozenset(
    {"not_an_object", "missing_channel", "unknown_channel", "empty_text", "normalisation_error"}
)

_KEYS_TO_STRIP = frozenset({0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x2060, 0x2061, 0x2062,
                            0x2063, 0x2064, 0xFEFF})
_LIST_WRAPPER_KEYS = ("tickets", "data", "items")
_TICKET_SHAPED_KEYS = frozenset({"ticket_id", "channel", "subject", "body"})


class TicketFileError(Exception):
    """FR-07: the input file itself could not be read as JSON tickets (a run-level failure)."""


@dataclass(frozen=True)
class Ticket:
    """FR-07: one internal representation for email, chat, docs_comment and forum tickets.

    `subject` and `body` are the original text, verbatim. `text` is the cleaned form that
    every later component reads. There is deliberately no `labels` or `history` attribute:
    ground truth reaches the harness through `raw` only, never the runtime path.
    """

    ticket_id: str
    channel: str
    subject: str
    body: str
    text: str
    received_at: str | None
    received_at_raw: Any
    customer_id: str | None
    customer_name: str | None
    customer_tier: str
    customer_region: str
    language_fluency: str
    defects: tuple[str, ...] = ()
    source_index: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_malformed(self) -> bool:
        """FR-07: true when a defect means the ticket must go to a person, not an answer."""
        return bool(BLOCKING_DEFECTS & set(self.defects))

    def segments(self) -> dict[str, str]:
        """FR-14, NFR-06: the segment keys every metric is split by."""
        return {
            "channel": self.channel,
            "tier": self.customer_tier,
            "region": self.customer_region,
            "fluency": self.language_fluency,
        }

    def log_fields(self) -> dict[str, Any]:
        """FR-13: the ticket-side fields of a decision-log row."""
        return {
            "ticket_id": self.ticket_id,
            "channel": self.channel,
            "received_at": self.received_at,
            "ingest_defects": list(self.defects),
            **self.segments(),
        }

    def log_fields_for_log(self) -> dict[str, Any]:
        """FR-13: `log_fields()` as keyword arguments for a `DecisionEntry`.

        Derived from `log_fields()` rather than re-listed, so a field added there reaches the
        decision log instead of being silently dropped. `ticket_id` is passed by the caller
        alongside its own decision, so it is the one key left out.
        """
        return {k: v for k, v in self.log_fields().items() if k != "ticket_id"}


def evaluation_labels(ticket: Ticket) -> Mapping[str, Any]:
    """FR-14: the ground-truth block, for the harness only. Runtime code must not call this."""
    labels = ticket.raw.get("labels") if isinstance(ticket.raw, Mapping) else None
    return labels if isinstance(labels, Mapping) else {}


def load_tickets(path: str | Path) -> list[Ticket]:
    """FR-07: read any ticket file (path given, never hardcoded) into Tickets, in file order.

    One Ticket per input entry, so tickets out always equals entries in. Raises
    TicketFileError only when the file as a whole is unusable; per-entry problems become
    defects on the ticket.
    """
    path = Path(path)
    try:
        # utf-8-sig also accepts a byte-order mark, which exported files often carry.
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise TicketFileError(f"cannot read ticket file {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise TicketFileError(f"ticket file {path} is not UTF-8 text: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TicketFileError(f"ticket file {path} is not valid JSON: {exc}") from exc

    entries = _entries_from_payload(payload, path)
    seen_ids: dict[str, int] = {}
    return [
        normalise_ticket(entry, index=index, seen_ids=seen_ids)
        for index, entry in enumerate(entries)
    ]


def normalise_ticket(
    entry: Any,
    *,
    index: int | None = None,
    seen_ids: dict[str, int] | None = None,
) -> Ticket:
    """FR-07: turn one input entry into a Ticket. Never raises, whatever the entry is."""
    try:
        return _normalise(entry, index, seen_ids)
    except Exception as exc:  # noqa: BLE001 - one bad ticket must not stop a run
        return _failed_ticket(entry, index, exc)


# --- internals ---------------------------------------------------------------------


def _entries_from_payload(payload: Any, path: Path) -> list[Any]:
    """FR-07: accept an array, a single ticket object, or an object wrapping the array.

    A mapping that is neither is a run-level failure, not one ticket: silently reading
    `{"validation_tickets": [...500...]}` as a single malformed ticket would drop 500 of them.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        if _TICKET_SHAPED_KEYS & set(payload):
            return [payload]
        for key in _LIST_WRAPPER_KEYS:
            if isinstance(payload.get(key), list):
                return payload[key]
        raise TicketFileError(
            f"ticket file {path} is an object with keys {sorted(map(str, payload))[:10]}: "
            "no ticket fields and no array under 'tickets', 'data' or 'items'"
        )
    raise TicketFileError(f"ticket file {path} holds {type(payload).__name__}, not tickets")


def _normalise(entry: Any, index: int | None, seen_ids: dict[str, int] | None) -> Ticket:
    if not isinstance(entry, Mapping):
        return _not_an_object_ticket(entry, index)

    defects: set[str] = set()
    subject_raw = entry.get("subject")
    body_raw = entry.get("body")
    subject = _as_text(subject_raw, "subject", defects)
    body = _as_text(body_raw, "body", defects)

    channel = _normalise_channel(entry.get("channel"), defects)
    ticket_id = _normalise_id(entry, index, seen_ids, defects)
    received_at = _normalise_timestamp(entry.get("received_at"), defects)
    text = _build_text(subject, body, channel, defects)

    return Ticket(
        ticket_id=ticket_id,
        channel=channel,
        subject=subject,
        body=body,
        text=text,
        received_at=received_at,
        received_at_raw=entry.get("received_at"),
        customer_id=_optional_text(entry.get("customer_id"), "customer_id", defects),
        customer_name=_optional_text(entry.get("customer_name"), "customer_name", defects),
        customer_tier=_enumerated(entry.get("customer_tier"), TIERS, "customer_tier", defects),
        customer_region=_enumerated(
            entry.get("customer_region"), REGIONS, "customer_region", defects
        ),
        language_fluency=_enumerated(
            entry.get("language_fluency"), FLUENCIES, "language_fluency", defects
        ),
        defects=tuple(sorted(defects)),
        source_index=index,
        raw=_frozen(entry),
    )


def _not_an_object_ticket(entry: Any, index: int | None) -> Ticket:
    """FR-07: an entry that is not even a ticket object still becomes a flagged Ticket."""
    body = entry if isinstance(entry, str) else _dump(entry)
    defects = {"not_an_object", "missing_ticket_id", "missing_channel", "empty_body"}
    text = _build_text("", body, UNKNOWN, defects)
    return _bare_ticket(
        ticket_id=_generated_id(entry, index),
        body=body,
        text=text,
        defects=defects,
        index=index,
        raw={},
    )


def _failed_ticket(entry: Any, index: int | None, exc: Exception) -> Ticket:
    """FR-07: normalisation itself failed. Escalate the ticket; never guess at its content.

    `text` is built from the supplied subject and body only, never from a dump of the entry:
    a dump would carry `labels`/`history` ground truth onto the runtime path.
    """
    defects = {"normalisation_error", f"normalisation_error:{type(exc).__name__}"}
    mapping = entry if isinstance(entry, Mapping) else {}
    ticket_id = str(mapping.get("ticket_id") or "").strip()
    if not ticket_id:
        defects.add("missing_ticket_id")
        ticket_id = _generated_id(entry, index)
    subject = mapping.get("subject") if isinstance(mapping.get("subject"), str) else ""
    body = mapping.get("body") if isinstance(mapping.get("body"), str) else ""
    text = _build_text(subject, body, UNKNOWN, set())
    return _bare_ticket(
        ticket_id=ticket_id,
        subject=subject,
        body=body,
        text=text,
        defects=defects,
        index=index,
        raw=_frozen(mapping),
        received_at_raw=mapping.get("received_at"),
    )


def _bare_ticket(
    *,
    ticket_id: str,
    text: str,
    defects: set[str],
    index: int | None,
    raw: Mapping[str, Any],
    subject: str = "",
    body: str = "",
    received_at_raw: Any = None,
) -> Ticket:
    """FR-07: a Ticket with nothing inferred: everything we could not read is `unknown`."""
    values = {f.name: None for f in fields(Ticket)}
    values.update(
        ticket_id=ticket_id,
        channel=UNKNOWN,
        subject=subject,
        body=body,
        text=text,
        received_at=None,
        received_at_raw=received_at_raw,
        customer_tier=UNKNOWN,
        customer_region=UNKNOWN,
        language_fluency=UNKNOWN,
        defects=tuple(sorted(defects)),
        source_index=index,
        raw=raw,
    )
    return Ticket(**values)


def _frozen(entry: Mapping[str, Any]) -> Mapping[str, Any]:
    """Ticket is frozen, so `raw` is a read-only view: scoring data cannot be written back."""
    return MappingProxyType(dict(entry))


def _normalise_channel(value: Any, defects: set[str]) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        defects.add("missing_channel")
        return UNKNOWN
    if not isinstance(value, str):
        defects.add("coerced_field:channel")
    channel = str(value).strip().lower()
    if channel not in CHANNELS:
        defects.add("unknown_channel")
        return UNKNOWN
    return channel


def _normalise_id(
    entry: Mapping[str, Any],
    index: int | None,
    seen_ids: dict[str, int] | None,
    defects: set[str],
) -> str:
    value = entry.get("ticket_id")
    if value is not None and not isinstance(value, str):
        defects.add("coerced_field:ticket_id")
    ticket_id = str(value).strip() if value is not None else ""
    if not ticket_id:
        defects.add("missing_ticket_id")
        ticket_id = _generated_id(entry, index)
    if seen_ids is not None:
        if ticket_id in seen_ids:
            defects.add("duplicate_ticket_id")
        else:
            seen_ids[ticket_id] = index if index is not None else len(seen_ids)
    return ticket_id


def _generated_id(entry: Any, index: int | None) -> str:
    """FR-07, NFR-08: same entry at the same position always gets the same id.

    A ticket submitted through the API has no position in a file, and says so (`GEN-api-`),
    so it can never collide with row 0 of a run.
    """
    digest = hashlib.sha256(_dump(entry).encode("utf-8", errors="surrogatepass")).hexdigest()
    position = "api" if index is None else f"{index:04d}"
    return f"GEN-{position}-{digest[:8]}"


def _normalise_timestamp(value: Any, defects: set[str]) -> str | None:
    """FR-07, FR-05: a timestamp the queue can sort by, or None plus a defect."""
    if value is None or (isinstance(value, str) and not value.strip()):
        defects.add("missing_received_at")
        return None
    text = str(value).strip().replace(" ", "T")
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except (ValueError, TypeError):
        defects.add("unparseable_received_at")
        return None
    if "T" not in candidate:
        defects.add("imprecise_received_at")  # date only: midnight UTC is our invention
    try:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        defects.add("unparseable_received_at")  # e.g. year 1 with a positive offset
        return None


def _enumerated(value: Any, allowed: frozenset[str], name: str, defects: set[str]) -> str:
    if value is not None and not isinstance(value, str):
        defects.add(f"coerced_field:{name}")
    text = str(value).strip().lower() if value is not None else ""
    if text not in allowed:
        defects.add(f"{UNKNOWN}_{name}")
        return UNKNOWN
    return text


def _optional_text(value: Any, name: str, defects: set[str]) -> str | None:
    if value is None:
        return None
    text = _as_text(value, name, defects).strip()
    return text or None


def _as_text(value: Any, name: str, defects: set[str]) -> str:
    """FR-07: whatever arrived in a text field, keep its content as a string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    defects.add(f"coerced_field:{name}")
    if isinstance(value, (Mapping, list, tuple)):
        return _dump(value)
    return str(value)


def _build_text(subject: str, body: str, channel: str, defects: set[str]) -> str:
    """FR-07: the one text downstream reads: cleaned subject, blank line, cleaned body."""
    clean_subject = _clean(subject, defects)
    clean_body = _clean(body, defects)
    if not clean_body:
        defects.add("empty_body")
    if not clean_subject and channel in CHANNELS_WITH_SUBJECT:
        defects.add("missing_subject")

    parts = [clean_subject, clean_body] if clean_subject else [clean_body]
    if clean_subject and clean_subject.lower() in clean_body.lower():
        parts = [clean_body]  # the subject only repeats the body (5 dev tickets)
    text = "\n\n".join(p for p in parts if p).strip()

    if not text:
        defects.add("empty_text")
    if len(text) > MAX_TEXT_CHARS:
        defects.add("text_truncated")
        text = text[:MAX_TEXT_CHARS]
    return text


def _clean(value: str, defects: set[str]) -> str:
    """FR-07: unusual characters must not fail and must not reach a prompt or an index."""
    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    for char in text:
        if char in "\n\t":
            kept.append(" " if char == "\t" else char)
            continue
        if ord(char) in _KEYS_TO_STRIP or unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co"}:
            continue
        kept.append(char)
    cleaned = "".join(kept)
    if len(cleaned) != len(text) or "\r" in value:
        defects.add("control_characters_removed")
    return _collapse_whitespace(cleaned)


def _collapse_whitespace(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and out[-1:] == [""]:
            continue  # at most one blank line in a row
        out.append(line)
    return "\n".join(out).strip()


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    except (TypeError, ValueError):
        return str(value)
