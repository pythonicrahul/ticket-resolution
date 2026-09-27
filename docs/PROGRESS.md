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

## 2026-09-27 · Decision note · the four FR-07 open questions, answered

Not a backlog row: the author answered the open questions left by row 1. Recorded as `docs/decisions.md`
D-12 … D-15, and `docs/specs/FR-07.md` §7 is now "Resolved questions" rather than open ones.

1. **Duplicate ticket ids** → keep the supplied id and flag the row; the decision log is keyed on a surrogate
   row id with `ticket_id` and `source_index` columns (D-12). Constrains row 3.
2. **Unknown channel** → stays blocking, and the metrics report counts `unknown_channel` tickets so a fifth
   channel shows up as a line rather than an unexplained escalation rate (D-13). Constrains row 6.
3. **The 8000-character cap** → stays, and routing escalates any ticket carrying `text_truncated`; it is not
   made a blocking ingest defect, because `is_malformed` means "no usable representation" while this is a
   routing policy (D-14). Constrains row 9.
4. **Labels** → optional; the harness always computes the label-free metrics and prints
   `scored against labels: N of M` (D-15). `ground_truth_responses.json` is not a fallback (dev ids only).
   Constrains row 6.

Rows 3, 6 and 9 in `docs/BACKLOG.md` now carry these constraints in their item text, so the session that
builds each one sees them without reading this log.

No FR-07 code changed: each answer is either the behaviour already implemented and tested (T-FR07-7,
T-FR07-8, T-FR07-10, T-FR07-11) or a constraint on a later row. `uv run pytest -q` → 32 passed;
`uv run ruff check .` → clean.

**Data note while checking these:** CLAUDE.md records 42 validation tickets duplicating development text;
exact subject+body matching gives 45. The instruction is unchanged (do not tune against individual validation
tickets) but the overlap is slightly larger than the note says, so validation scores flatter the system a
little more than recorded. Worth a line in the Stage 5 revision.

## 2026-09-27 · Row 2 · B-17 Engineered test tickets and drafts · FR-03, FR-07, FR-12

**Why this row exists:** the supplied data has none of the cases these three requirements are about. 0 of 500
development tickets and 0 of 80 validation tickets mention `refund|credit|dispute|chargeback|money back|
reimburse`; none carry PII, an injection attempt or a malformed record. Without engineered tickets, FR-03's
must-escalate half and every FR-12 guardrail are untestable.

**Files changed**
- `docs/specs/FR-03.md` (new): money/date trigger tables grouped into families, the explanatory-billing
  carve-out, the reply-side commitment check shared with FR-12, precedence, 13 acceptance tests, 4 open
  questions.
- `docs/specs/FR-12.md` (new): pre-draft rules (injection, high-risk secrets) and the four post-draft checks,
  the §3.4 precedence table, invariants (no bypass, a raising check is a failure, log before the action), 23
  acceptance tests, 5 open questions.
- `docs/specs/FR-07.md`: T-FR07-23 added for corpus coverage.
- `tests/fixtures/pii_tickets.json` (6), `injection_tickets.json` (8), `money_commitment_tickets.json` (17),
  `draft_replies.json` (11 drafts), `README.md` — all new; `malformed_tickets.json` unchanged from row 1.
- `tests/test_engineered_fixtures.py` (new): 9 contract tests. `tests/test_fr07_ingest.py`: T-FR07-20 widened
  to the whole corpus.
- `docs/decisions.md` D-16 … D-20; `ATTRIBUTION.md`; `docs/BACKLOG.md` rows 9 and 12 annotated with what this
  row decided for them.

**Tests added**: `test_T_FR12_1_corpus_contract`, `_2_every_engineered_ticket_survives_ingest`,
`_3_pii_fixtures_carry_pii_and_lookalikes_do_not`, `_4_injection_fixtures_carry_markers_and_lookalikes_do_not`,
`_5_specs_and_mirrored_tables_agree_with_the_data`, `_17_no_real_credentials_or_addresses_in_the_corpus`,
`_21_declared_reasons_follow_the_precedence_order`, `_22_injection_and_pii_positives_are_diagnostic`,
`_23_draft_fixtures_contract`, `test_T_FR03_1_money_and_date_fixtures_escalate`,
`_2_explanatory_billing_must_still_be_answered`, `_13_every_trigger_family_has_a_fixture`,
`test_T_FR07_23_corpus_covers_every_category_row_2_names`.

**Result**: `uv run pytest -q` → 45 passed (32 before this row). `uv run ruff check .` → clean. No network, no
API key, no model calls.

**Design decisions** (D-16 … D-20)
- One fixed precedence for the pre-model escalation rules, with `reason` plus `all_reasons` in the log (D-16).
- Injection markers are phrases, not bare words; the two lookalike fixtures are the evidence (D-17).
- Fixture expectations are derived from the fixture text, never hand-written (D-18).
- Engineered drafts ship with their retrieved passages so row 12 needs no index and no provider (D-19).
- `must_not_auto_respond` means something narrower in the pack data than in this corpus (D-20).

**Independent review** (reviewer subagent, PR-08 v1.0): 21 findings — 0 severe, 3 high, 9 medium, 9 low.
Fixed, with the corpus or the specs changed accordingly:
- *High 1* — no precedence between the pre-model rules, so the logged reason was implementation-order
  dependent → D-16, the §3.4 table, `expected_all_reasons` on every fixture, and T-FR12-21.
- *High 2* — two of four injection positives also carried money triggers, so they would escalate even with the
  injection rule deleted → four more injection fixtures, three of them injection-only, and T-FR12-22 enforcing
  that at least three positives are diagnostic.
- *High 3* — log-before-action, `prompt_version` and `requirement_ids` had no acceptance test → T-FR12-18 and
  T-FR03-11, with FR-13 (row 3) named as the owner of the general ordering guarantee.
- Mediums: grounding-judge determinism now specified (temperature 0, pinned prompt, cache) with T-FR12-19;
  FR-03 gained a no-bypass test (T-FR03-12); the PII mirror split into pre-draft secrets and draft-side contact
  details, so a `pii` fixture cannot pass on an email address alone; the spec/mirror drift check now runs in
  both directions by parsing the spec's own tables; the safety-net regex now covers `password`/`passwd`; four
  money fixtures and two date fixtures added so every trigger family is exercised (T-FR03-13), including an
  honestly-labelled `known_over_escalation` case; a usage-limits and a retention-period control added, so all
  four explanatory kinds PRD FR-03 permits are covered; `load_tickets` now runs over the new files; engineered
  drafts added (D-19).
- Lows: `instruction_leak_in_draft` added to the reason vocabulary; the hardcoded corpus size became a floor;
  the README's "every id starts with SYN-" corrected for the `GEN-…` ids; the `must_not_auto_respond`
  convention recorded (D-20); `SYN-PII-004` and `SYN-INJ-003` reworded so FR-08 is unlikely to predict
  `compliance_request`/`security_incident` and escalate them by FR-09 before their own rule fires;
  `SYN-INJ-002`'s unfalsifiable claim about `<ticket>` tags replaced with T-FR12-20, which tests the
  delimiting on a benign ticket where a prompt is actually built.

**Not fixed, on purpose**
- The mirrored pattern tables still live in the test module. Row 12 owns moving them into `guardrails.py`;
  until then the bidirectional drift check is what holds them together. Recorded on backlog row 12.
- The `chunk_id` format in the draft fixtures (`DOC-BILL-001#1`) is provisional until row 5 fixes one.
- FR-12's acceptance tests 6 to 16, 18 and 19, and FR-03's 3 to 12, are deferred to rows 9, 11, 12 and 13 by
  design: there is no guardrail or routing code yet. This row's job was the data and the specs.
- The test module hardcodes `data/ground_truth_responses.json`. CLAUDE.md's no-hardcoded-path rule is about
  the harness input; here the ground-truth file *is* the subject of the test. Left as is, noted so the habit
  does not migrate.

**Open questions for the human** (in the two specs)
1. FR-03 §7: keep `dispute`, `promise`, `compensation`, `waive` and bare `eta` as triggers? `SYN-MONEY-010` is
   labelled `known_over_escalation` to show the cost honestly.
2. FR-12 §7: how much content-word overlap counts as grounded — a threshold for the author, from dev data.
3. FR-12 §7: does a customer's own email address in a reply count as a leak? Read strictly (blocked) for now.
4. FR-12 §7: keep `system:`/`assistant:` as markers, given a pasted log line would escalate?
