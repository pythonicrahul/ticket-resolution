# Build progress log

One entry per loop iteration, newest last. Written by the loop, read by humans and by the next iteration.


## 2026-09-27 · Row 1 · B-02 Ingest and normalise four channels · FR-07

**Files changed**
- `docs/specs/FR-07.md` (new): spec written with PR-06 v1.0 before any code — purpose, `Ticket` field table, eight ordered decision rules, failure behaviour, the decision-log fields ingest supplies, 22 acceptance tests, four open questions.
- `src/ticketing_agent/ingest.py`: `Ticket` (frozen dataclass, one representation for all four channels), `normalise_ticket`, `load_tickets`, `evaluation_labels`, `TicketFileError`.
- `tests/test_fr07_ingest.py` (new): T-FR07-1 … T-FR07-22, 32 test cases with parametrisation.
- `tests/fixtures/malformed_tickets.json` (new): 13 clearly synthetic `SYN-*` entries — empty body, body-only, odd characters (zero-width, BiDi override, NUL, BOM, combining accent, emoji, lone surrogate), unsupported channel, 20 000-character body, chat/email without a subject, wrong types, a duplicate id, and three entries that are not objects at all.
- `docs/decisions.md`: D-05 … D-11.
- `.claude/hooks/require_green.py`: the stop hook now opens only for `BLOCKED` (not `HUMAN`, which every checkpoint row will reach), resolves `docs/BACKLOG.md` relative to itself, and fixes two ruff findings left from row B-01 — the loop's gate is `pytest && ruff check .` over the whole repo.

**Tests added** (all offline, no API key)
`test_T_FR07_1_one_ticket_per_channel_normalises`, `_2_original_text_kept_verbatim`, `_3_body_only_entry_survives`, `_4_empty_subject_and_body_is_malformed`, `_5_unusual_characters_handled`, `_6_non_object_entries_are_not_dropped` (3 cases), `_7_unknown_channel_escalates`, `_8_load_tickets_from_any_path`, `_9_normalisation_is_deterministic`, `_10_ground_truth_not_on_the_runtime_representation`, `_11_over_long_body_truncated_in_text_only`, `_12_subject_expected_per_channel`, `_13_received_at_normalised` (6 cases), `_14_duplicate_ticket_id_flagged_and_kept`, `_15_payload_shapes_and_file_errors`, `_16_wrong_types_are_coerced`, `_17_whole_dataset_normalises_without_raising`, `_18_file_encodings`, `_19_normalisation_error_escalates_without_leaking_ground_truth`, `_20_ground_truth_never_reaches_text`, `_21_raw_is_read_only`, `_22_subject_repeating_the_body_is_not_duplicated`.

**Result**: `uv run pytest -q` → 32 passed. `uv run ruff check .` → clean.

**Design decisions** (docs/decisions.md D-05 … D-11)
- Original `subject`/`body` kept verbatim; a cleaned `text` is the only text later components read.
- Defect codes rather than exceptions. Five block (`not_an_object`, `missing_channel`, `unknown_channel`, `empty_text`, `normalisation_error`) and make `is_malformed` true, which routing will turn into an escalation with reason `malformed_ticket` before any model call. An unrecognised channel blocks: the conservative reading of FR-07.
- No `labels`/`history` attribute on `Ticket`; the harness reads ground truth through `evaluation_labels()`, and `raw` is a read-only view so scoring data cannot be written back.
- Generated ids `GEN-{index:04d}-{sha256[:8]}` (or `GEN-api-…`) are deterministic (NFR-08); duplicate ids are flagged, not rewritten.
- An unrecognised wrapper object is a loud `TicketFileError`, never one malformed ticket.

**Independent review** (reviewer subagent, PR-08 v1.0): 15 findings — 2 severe, 2 high, 4 medium, 7 low.
Fixed: 1 (the `normalisation_error` fallback was non-blocking, dropped the supplied id and `raw`, and dumped `labels`/`history` into `text`), 2 (that branch had no test), 3 (unrecognised wrapper key silently became one ticket; a ticket carrying an `items` list became N garbage tickets), 4 (non-UTF-8 file raised a bare `UnicodeDecodeError`; BOM files failed), 5 (`missing_channel` blocking was unasserted), 6 (subject-repeats-body rule untested), 8 (`raw` was the caller's mutable dict), 9 and 10 (vacuous/thin assertions), 11 (`ticket_id`/`channel`/enumerated fields coerced with no `coerced_field` defect), 12 (`index=None` collided with row 0), 13 (`Co` stripping unspec'd; CRLF-only input recorded no defect), 14 (spec drift on `received_at_raw` and `log_fields`; date-only timestamps now carry `imprecise_received_at`).
Finding 7 narrowed rather than closed: the stop hook is dev tooling, and a file edit can still open it (D-11).
Finding 15 accepted as a gap: the second half of the PRD criterion ("malformed test tickets are logged and escalated") cannot be demonstrated until the decision log (row 3), routing (row 9) and the pipeline (row 14) exist. FR-07 supplies `is_malformed` and `log_fields()`; row 14 must prove the end-to-end half.

**Open questions for the human** (also in the spec)
1. Duplicate ticket ids: flag and keep the id (current), or rewrite it so reconciliation keys are unique?
2. Is blocking on an unknown channel right, or should a fifth channel be answerable?
3. The 8000-character `text` cap has no requirement behind it (longest supplied body: 245 characters).
4. `labels`/`history` are treated as optional, since the unseen input file may omit them; the metrics report will have to say which tickets could not be scored.
