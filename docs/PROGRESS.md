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

## 2026-09-27 · Decision note · the four FR-03 and FR-12 open questions, answered

Not a backlog row. The author answered the questions row 2 left open; recorded as `docs/decisions.md`
D-21 … D-24, with both specs' §7 rewritten from open questions to resolved ones.

**The measurements the answers rest on** (all offline, against `data/`):

| Question | Measured | Result |
|---|---|---|
| Do the conservative money/date triggers over-fire? | all 38 triggers against all 580 tickets | **0 matches**, so zero over-escalation cost on this distribution |
| Does a customer's own email in a reply count as a leak? | emails/phones in the 200 expert reference answers | **0** contain either; 0 of 580 tickets contain an address |
| Would `system:` / `assistant:` fire on real tickets? | both labels, plus `<ticket>`, `ignore all previous`, `you are now`, against 580 tickets | **0** for every one |
| How much content-word overlap counts as grounded? | 997 sentences of the 200 expert answers against the articles they cite | bimodal: 351 pleasantries near 0, 646 claims at p1 0.25 / p5 0.33 / median 0.70 |

1. **Triggers stay as they are** (D-21). `SYN-MONEY-010` remains labelled `known_over_escalation` so the cost
   is visible rather than hidden. Constrains row 9.
2. **Grounding: the exemption list first, threshold 0.3 provisionally** (D-22). A threshold applied to every
   sentence would reject 37–48% of expert-written answers; excluding pleasantries, 0.3 rejects 3.6% of genuine
   claims, 0.5 rejects 19.3%. Set by Claude Code on the author's instruction, not endorsed on the merits, and
   measured against whole articles rather than retrieval chunks — **row 5 re-measures on real chunks**.
   Constrains rows 5 and 12.
3. **A customer's own email in a reply is still a leak** (D-23) — which is what the expert answers already do.
   Constrains row 12.
4. **Role labels anchored to the start of a line** (D-24), the recommended hardening rather than removal.
   Constrains row 12.

**Files changed**: `docs/specs/FR-03.md` §7 and `docs/specs/FR-12.md` §3.1.1, §3.2.2, §7 (the claim-exemption
list and the threshold are now written into the rule, not left to row 12 to invent);
`tests/fixtures/injection_tickets.json` — two fixtures added for D-24: `SYN-INJ-007` (a line-start role label,
the only marker it carries, so it proves the anchoring still catches the attack) and `SYN-INJ-LOOKALIKE-003`
(the same words inline in a pasted log, which must not fire); `tests/test_engineered_fixtures.py` — `markers()`
now implements the anchoring and T-FR12-4 asserts both halves; `tests/fixtures/README.md`; `docs/BACKLOG.md`
rows 5 and 12.

**Result**: `uv run pytest -q` → 45 passed. `uv run ruff check .` → clean. Corpus now 33 engineered tickets
(46 with the row 1 malformed file) and 11 drafts.

**Still open, deliberately**: whether private IP addresses count as private data in a draft (FR-12 §7), and the
chunk-id format the draft fixtures assume, which row 5 owns.

## 2026-09-27 · Row 3 · B-10 Decision log · FR-13

**Files changed**
- `docs/specs/FR-13.md` (new): the reconstructed Governance field set, terminal vs `continue` rows, validation,
  the redaction rule, reconciliation, failure behaviour, 27 acceptance tests, 5 open questions.
- `src/ticketing_agent/logging_store.py`: `DecisionEntry`, `DecisionLog` (SQLite, WAL, autocommit),
  `Reconciliation`, `InvalidDecision`, `DecisionLogUnavailable`, JSONL fallback, redaction.
- `src/ticketing_agent/ingest.py`: `Ticket.log_fields_for_log()`, derived from `log_fields()` so the two
  cannot drift.
- `tests/test_fr13_decision_log.py` (new): T-FR13-1 … T-FR13-27.
- `docs/decisions.md` D-25 … D-27; `docs/BACKLOG.md` (row 3 DONE, row 14 annotated); `ATTRIBUTION.md`.

**Tests added**: `test_T_FR13_1_schema_created_at_any_path`, `_2_every_field_round_trips`,
`_3_requirement_ids_are_mandatory`, `_4_escalation_needs_a_reason`, `_5_a_model_call_needs_a_prompt_version`,
`_6_private_data_is_redacted_and_the_row_is_still_written` (4 cases), `_7_vocabulary_is_enforced` (3 cases),
`_8_the_row_is_committed_before_the_action`, `_9_a_failing_action_leaves_the_decision_logged`,
`_10_a_clean_run_reconciles`, `_11_reconcile_reports_every_discrepancy`,
`_12_duplicate_input_ids_reconcile_on_source_index`, `_13_continue_rows_are_not_terminal`,
`_14_rows_persist_across_reopen`, `_15_a_failed_write_falls_back_and_raises`, `_16_no_way_to_skip_logging`,
`_17_two_stores_on_one_path_both_write`, `_18_the_store_adds_nothing_that_breaks_determinism`,
`_19_ingest_fields_reach_the_row_unchanged`, `_20_legitimate_details_are_not_redacted` (4 cases),
`_21_an_over_long_detail_is_truncated_not_rejected`, `_22_aggregates_feed_the_metrics_report`,
`_23_the_run_record_cross_checks_the_ticket_count`, `_24_finish_run_without_start_run_still_records`,
`_25_tickets_without_a_source_index_are_reported`, `_26_durability_settings_are_pinned`,
`_27_none_for_a_sequence_field_becomes_an_empty_list`.

**Result**: `uv run pytest -q` → 80 passed (45 before this row). `uv run ruff check .` → clean.

**Design decisions** (D-25 … D-27)
- The log redacts private-looking content and writes the row anyway; only call-site mistakes raise (D-25).
- Reconciliation counts terminal rows, keyed on `source_index`, cross-checked against `start_run`'s count, with
  unindexed tickets reported rather than skipped (D-26).
- A log that cannot be written stops the run, with a JSONL fallback, and everything raised is a
  `DecisionLogError` so a per-ticket `except` cannot mistake it for one ticket failing (D-27).

**Independent review** (reviewer subagent, PR-08 v1.0): 17 findings — 0 severe, 2 high, 8 medium, 7 low.
Both highs were real and are fixed:
- *High 1* — the fallback write was itself unprotected, so with an unwritable directory (permission denied,
  disk full) the decision was preserved nowhere and a raw `PermissionError` escaped, which a per-ticket
  `except` in the harness would have swallowed and carried on unlogged. Now every failure path returns a
  `DecisionLogError`, the fallback failure is reported in the message, and opening the log and the `runs`
  writes are protected the same way. Verified by reproducing the reviewer's `chmod 500` case.
- *High 2* — `InvalidDecision` discarded the row entirely and was reachable from ticket data: an ungrounded
  sentence containing an email address would have meant a guardrail block with no log row, breaking FR-12's
  own acceptance criterion. Now redacted and recorded (D-25); T-FR13-6 asserts the stronger property and
  T-FR13-20 asserts legitimate details survive.
- Mediums fixed: `reconcile` cross-checks `start_run`'s `tickets_in`; tickets with no `source_index` are
  reported instead of silently skipped; the scrub's false positives (`token: expired`, spaced single digits)
  and its gap (`private key: …`, unscrubbed `all_reasons`) are both closed; run-table writes get retries and
  typed errors, and `finish_run` creates the row if `start_run` never ran; the aggregates
  (`model_calls`, `cache_hits`, `latencies_ms`, `counts_by_reason`) are now asserted, with the
  all-rows/terminal-rows asymmetry documented as intentional; `log_fields_for_log` is derived from
  `log_fields()`; T-FR13-16 now parses the module and asserts every `except` ends in a `raise`.
- Lows fixed: linear regexes with a 4000-character bound (the scrub was 9.3 s on a 60 k detail and is now
  ~4 ms at any size); `None` for a sequence field becomes `[]`; the `stage` rule added to the spec; docstrings
  name their requirements; T-FR13-1 checks the `meta` row and the exact index names; T-FR13-2 checks `None`
  stays `None`; T-FR13-10 reads ground truth through `evaluation_labels()`.

**Not fixed, on purpose**
- `record` stays public, so `perform` is the supported path rather than the only possible one. Making `record`
  private would stop the harness writing intermediate rows. Recorded in FR-13 §7 and on backlog row 14, which
  must assert the pipeline routes every action through `perform`.
- The FR-01 `summary` is stored unscrubbed. It is derived from customer text, so it can carry PII, but it is
  also what makes an escalation useful to Daniel. Open question for the author (FR-13 §7).

**Open questions for the human** (in the spec)
1. **The Governance Framework document is not in this repository**, so the field list is a reconstruction from
   the PRD, CLAUDE.md and the FR-01/FR-03/FR-07/FR-12 specs. Check it against the source; if the real framework
   names a field this omits, add it and bump `schema_version`.
2. Should the FR-01 handover `summary` be scrubbed for private data too, or is the log an internal artefact?
3. Retention: nothing says how long the log is kept, and a compliance reviewer will ask.
4. `continue` rows mean 5–8 rows per ticket; fine locally, worth noting at CloudServe's real volume.
5. Confirm that a log failure stopping the run is the trade you want (D-27).

## 2026-09-27 · Alignment · the pack's source documents, read and applied · FR-12, FR-13, FR-03

Not a backlog row. The author pointed at `~/Downloads/FDE_Capstone_Complete/Capstone_Pack`, which contains the
three documents this repo had only been citing second-hand: the Governance Framework, the Build Specification
and the Evaluation Framework. `docs/specs/FR-13.md` §7 had flagged the Governance field list as a
reconstruction and asked for exactly this check.

**The reconstruction was incomplete.** The framework's §1 minimum record required seven things the schema did
not have — `decision_id`, `model.version`, a generic `prediction` block, `threshold_applied`, `sources_used`
with scores, the `block` action, and a human-readable `explanation` separate from the machine `reason` — and
the stage vocabulary was ours (`classify`, `route`, `generate`, `guardrails`) rather than the framework's
(`classification`, `routing`, `generation`, `validation`). `threshold_applied` was the most consequential
omission: without it the Governance confidence-floor guardrail cannot be demonstrated at all.

**Files changed**
- `src/ticketing_agent/logging_store.py`: the seven fields, the framework's stage and action vocabularies, and
  `governance_record()` which projects a row into the framework's exact JSON shape. Two new validation rules:
  an `auto_respond` row must carry `threshold_applied`, and every terminal row must carry `explanation`.
- `docs/specs/FR-13.md`: §1 now cites the framework and A8; §2 documents the new fields; §3.1 adds `block` as a
  non-terminal action; §3.2 adds rules 7 and 8; §6 adds T-FR13-28 … T-FR13-31; §7's first open question is
  resolved.
- `docs/specs/FR-12.md`: **five** guardrails with the framework's names and its own table —
  `commitments` → `tone_and_scope`, plus a new `confidence_floor`. `docs/specs/FR-03.md` follows the rename.
- `tests/test_fr13_decision_log.py`: updated to the framework vocabulary; T-FR13-28 (the minimum record, key
  for key), T-FR13-29 (a block is recorded and is not terminal), T-FR13-30 (the two new rules), T-FR13-31
  (scores travel with sources) added.
- `docs/pack_alignment.md` (new): every difference found, what was done, what Stage 5 must record, and the
  document work only the author can do.
- `docs/BACKLOG.md`: rows 6, 8, 9, 12, 15 and 18 now carry what the pack requires of them.
- `docs/decisions.md` D-28 … D-30.

**Result**: `uv run pytest -q` → 85 passed (80 before). `uv run ruff check .` → clean.

**Two things the author should know**
1. **A12 exists.** The PRD cites A1–A11; the Build Specification lists twelve criteria, and A12 is "tests run
   with a single documented command and pass". The repo satisfies it, but the requirement was untraced.
2. **The pack contradicts itself on the hidden set size** — Build Spec §04 says 120 tickets, §08 and the
   Evaluation Framework say 100. Nothing in the build depends on it (the harness takes a path and processes
   whatever it finds), but the report should not state a number as fact.

**Left for the author** (`docs/pack_alignment.md` §4): the risk register R-01…R-08 with named owners, the
six-step incident response, the governance declaration, the kill-switch answers, a retention policy for the
log, and whether the FR-01 handover summary may carry customer text.
