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

## 2026-09-27 · Row 4 · Provider client · FR-15 (with D-31: LangGraph, LangChain, Pydantic)

**Files changed**
- `docs/specs/FR-15.md` (new): settings, the response and failure types, six ordered rules, failure
  behaviour, 45 acceptance tests, 5 open questions.
- `src/ticketing_agent/provider.py`: `ProviderClient` (retries, deterministic backoff, an explicit
  circuit-breaker state machine, a persistent cache), `LangChainTransport`, `FakeTransport`, the typed
  failure hierarchy, and `complete_structured()` returning validated Pydantic objects.
- `src/ticketing_agent/schemas.py` (new): one Pydantic model per prompt (`AnswerDraft`, `HandoverNote`,
  `GroundingCheck`), the JSON-from-prose extractor and `parse_into`.
- `src/ticketing_agent/config.py`: real `Settings` + `load_settings`, `ConfigError`, no hardcoded model
  or data paths, loud failures on malformed values.
- `tests/test_fr15_provider.py`, `tests/test_fr15_structured_output.py` (new): T-FR15-1 … T-FR15-45.
- `.env.example`: `MODEL_NAME` now shows the `:free` endpoint with a warning, `BREAKER_*` added,
  `DATABASE_URL` replaced by the `DECISION_LOG_PATH` that `load_settings` actually reads.
- `pyproject.toml` / `uv.lock` / `requirements.txt`: `langchain-openai~=1.6.6` added.
- `docs/decisions.md` D-31, D-32; `docs/BACKLOG.md` rows 4 (DONE), 11, 13, 14.

**Result**: `uv run pytest -q` → 157 passed (110 at the first green, 85 before this row).
`uv run ruff check .` → clean. No network, no API key, no model call in any test.

**The author's direction, and how it was applied** (D-31). Asked for LangGraph, Pydantic and optionally
LangChain instead of vanilla plumbing. A sweep first: `langchain-openai` 1.6.6 resolves with our pins on
Python 3.14, and a `StateGraph` over a Pydantic state with conditional edges works (node exceptions
propagate, so the harness must still wrap each ticket). The division: **LangChain makes the calls**
(`ChatOpenAI`, temperature 0, `max_retries=0`), **Pydantic validates the replies** (`schemas.py`, with the
prompts' own rules as validators), **LangGraph arrives at row 14**, and **FR-15's guarantees stay ours** —
the retry schedule, the backoff cap, the breaker and the prompt-version-keyed cache — because A11, NFR-08
and NFR-07 are requirements no library implements for us, and because `FakeTransport` then exercises that
code for real in every test instead of replacing it.

**Independent review** (reviewer subagent, PR-08 v1.0): 23 findings — 3 severe, 6 high, 8 medium, 6 low.
All three severes were the same shape: an untyped exception escaping `complete()`, which a caller's
`except ProviderFailure` would not catch and which would therefore stop the run rather than one ticket.
- *Severe 1* — a non-dict element in `choices` gave `AttributeError`. `_usable_text` now checks shape
  before content (T-FR15-21).
- *Severe 2* — a corrupt cache row gave `JSONDecodeError`. Now a counted miss (T-FR15-22).
- *Severe 3* — a negative `Retry-After` gave `ValueError` from `time.sleep`, and a huge one would have
  slept for an hour inside one ticket, defeating the A11 bound. Every wait is now clamped to
  `[0, 60] s` (T-FR15-24).
- *High 4* — an unwritable cache path killed the client at construction, so zero tickets would be
  processed. It now degrades to no cache, with `cache_available` false (T-FR15-25).
- *High 5* — the cache-hit path could raise and counted a hit for an answer it never returned. An
  unusable stored reply is now dropped and refetched (T-FR15-23).
- *High 6* — the half-open trick primed `consecutive_failures` to `threshold - 1`, which left
  `circuit_open` reporting true for ever after a bad-key episode and could trip the breaker on one later
  timeout. Replaced by an explicit `closed / open / half_open` state machine (D-32, T-FR15-26).
- *High 7* — cache read failures were swallowed uncounted, silently turning NFR-08 off. Now
  `cache_read_failures`.
- *High 8* — a hardcoded model name and two hardcoded data paths in `config.py`, against CLAUDE.md's
  first non-negotiable; and the bare OpenRouter id is the **paid** endpoint, so a missing `MODEL_NAME`
  would have spent money. All three now have no default and refuse to start.
- *High 9* — `_number` silently substituted the default for `LLM_MAX_RETRIES=three` or a mistyped
  threshold. Now `ConfigError`, with range checks so no env var can switch the breaker off.
- Mediums fixed: per-call `provider_requests` for FR-13's per-row `model_calls` (a run total would have
  multiplied it); `attempts`/`elapsed_ms`/`prompt_version` as attributes on the exceptions rather than
  text to parse; request counting rather than completion counting for the free tier; 402/404/422 mapped
  to `ProviderError` so a retired model id is not reported as an outage; the SDK error mapping now tested
  by constructing SDK-shaped exceptions (T-FR15-31…33); `check_same_thread=False` plus a lock for the API;
  `base_url` in the cache key; `model`/`model_version`/timeout/`max_tokens` assertions; a cached reply
  served while the circuit is open; the cache file checked for readable customer text.
- Lows fixed: `from None` on mapped provider errors so a 4xx body cannot reach a traceback; `.env`
  parsing handles `export ` and one matching quote pair; paths resolved so the cache does not depend on
  the working directory; dead `_TRUE`/`extra` removed; `model=` override so the PR-05 judge is reachable;
  docstrings name their requirements; D-31/D-32 recorded.

**Not fixed, on purpose**
- The real transport is still not exercised against a live provider. The **mapping** is tested by
  constructing SDK-shaped exceptions, which is the part with judgement in it, but the SDK's own exception
  types could change on a version bump. Recorded in FR-15 §7.
- The breaker is per client instance, not per model, so one dead model would open the circuit for the
  judge model too. Nothing calls two models in one run yet (FR-15 §7).
- No cache TTL: reproducibility beats freshness for an assessed run (FR-15 §7).

**Open questions for the human** (FR-15 §7)
1. Confirm the `MODEL_NAME` free-tier id against OpenRouter's current catalogue — `.env.example` now
   carries the `:free` suffix, but availability changes and the bare id bills.
2. No cache expiry: confirm reproducibility over freshness.
3. Per-model breakers, if the judge model lands before row 17.
4. Nothing tracks the remaining free-tier allowance; a token counter belongs with the metrics report (row 6).

## 2026-09-28 · Row 5 · B-03/B-04 Chunking, index, retrieval · FR-10 (serves FR-04)

**Files changed**
- `docs/specs/FR-10.md` (new): the chunking strategy with the measurement behind it, indexing and the
  fingerprint, the search rules, failure behaviour, 25 acceptance tests, 6 open questions.
- `src/ticketing_agent/retrieve.py`: `chunk_documents` (pure), `Retriever` (build/search/resolve),
  `Chunk`/`Passage`/`IndexStats`, `HashingEmbedder` and `RecordedEmbedder` for offline tests,
  `RetrievalError`.
- `src/ticketing_agent/config.py`: range checks for `RELEVANCE_THRESHOLD`, `RETRIEVAL_TOP_K` and
  `CONFIDENCE_THRESHOLD`.
- `scripts/record_embeddings.py`, `scripts/retrieval_sweep.py` (new);
  `evaluation/reports/retrieval_sweep.md` and `tests/fixtures/recorded_embeddings.json` (0.5 MB) generated.
- `tests/test_fr10_retrieval.py` (new): T-FR10-1 … T-FR10-25.
- `docs/decisions.md` D-33 … D-35; `docs/BACKLOG.md` rows 5 (DONE), 6 and 7.
- `pyproject.toml`: `langchain-text-splitters~=1.1.2`.

**Result**: `uv run pytest -q` → 185 passed (180 at first green, 157 before this row).
`uv run ruff check .` → clean. No test needs the network or the 80 MB model.

**What the sweep found** (`evaluation/reports/retrieval_sweep.md`, real embeddings, 500 dev tickets)
- Hit rate on answerable tickets **95.2%** at threshold 0, top-1 correct **89.9%**. Retrieval is not the
  bottleneck in this system.
- The threshold trade-off is visible: at 0.45, 91.9% hit rate but 16.1% of unanswerable tickets correctly
  return nothing; at 0.60 it collapses to 63%.
- **The fluent/non-fluent gap widens as the threshold rises** — 4.3 points at 0, 7.8 at 0.30, 12.3 at 0.60.
  NFR-06 allows 5, so retrieval alone breaches it at 0.30 and above. Exactly what the Governance Framework
  predicts for retrieval-based systems, and now measured rather than assumed.
- **D-22 re-measured on real chunks**, which this row owed: 0.3 still rejects 3.6% of expert claims when a
  sentence is checked against the union of retrieved passages, but 9.1% when checked against the single
  chunk it cites. Recorded as a design constraint for row 12.

**Design decisions** (D-33 … D-35): the fingerprint identifies the embedder by its output; chunking is
markdown sections with nothing dropped in silence; the sweep reports top-1 accuracy, precision against its
own ceiling, and segment sizes.

**Independent review** (reviewer subagent, PR-08 v1.0): 16 findings — 0 severe, 2 high, 9 medium, 5 low.
Both highs fixed:
- *High 1* — the index fingerprint was blind to the production embedder: `DefaultEmbeddingFunction.name()`
  is `"default"` with an empty config, so a chromadb upgrade would have served a stale index built by a
  different model. Now fingerprinted by embedding a probe string (D-33, T-FR10-21).
- *High 2* — the sweep's "precision at k" mixed populations and had an unstated 70.5% ceiling, in the column
  a human reads to choose the threshold. Fixed, with top-1 accuracy added (D-35).
- Mediums fixed: fairness table gained denominators, a noise caveat and a tier breakdown; documents are no
  longer dropped silently and `IndexStats.skipped` reports why; a duplicated `doc_id` keeps the first
  occurrence instead of aborting the build; the merge rule now keeps the larger part's heading and says so;
  the no-headings fallback no longer re-indexes the preamble it exists to drop; `RELEVANCE_THRESHOLD` and
  `RETRIEVAL_TOP_K` are range-checked; the recorded-embeddings fixture is verified (chunker version,
  dimensions, coverage, probe) and a missing fixture now **fails** instead of skipping FR-04's only real
  test; `build_index` no longer leaks `ConfigError` or a splitter exception.
- Thin tests strengthened: the overlap rule is asserted (setting the overlap to 0 now fails), the
  chunker-version change is covered, T-FR10-12 runs over all 500 tickets, T-FR10-20 is no longer vacuous,
  and passages are checked against `resolve()` field by field.
- Lows fixed: the scripts read their paths from `.env` rather than hardcoding file names, the committed
  report uses repository-relative paths and names the fingerprint, and the spec's `heading` type and `Chunk`
  fields now match the code.

**Not fixed, on purpose**
- `CHUNK_ID_PATTERN` is not enforced in production. An unseen corpus may use another `doc_id` style, and
  refusing it would lose articles for a cosmetic reason. Documented in the spec.
- FR-10 §5's decision-log row is not written here; row 14 wires the pipeline, and the backlog carries it so
  retrieval cannot end up as the one stage with no row.
- One Chroma teardown crash (`recursive_mutex lock failed` at interpreter exit) after the report was
  written; a rerun exits 0 and it has not recurred. Recorded in FR-10 §7.

**Open questions for the human** (FR-10 §7)
1. The threshold value is yours at row 7 — and it is a fairness decision as well as a quality one.
2. Section chunking cannot answer a question whose answer spans two sections; a parent-document strategy
   would. Worth measuring at row 7 before adding the complexity.
3. No check can tell whether the placeholder 0.0 was replaced with a considered value; the row 6 metrics
   report will print the threshold in use so a gate run shows it.

## 2026-09-28 · Row 6 · B-05 The evaluation harness · FR-14

**Files changed**
- `docs/specs/FR-14.md` (new): the command, the per-ticket contract, nine ordered rules, what the report must
  contain from all three pack documents, 27 acceptance tests, 5 open questions.
- `evaluation/harness.py`: `main`, `run`, per-ticket isolation, the four Build Spec §04 figure groups, the
  Evaluation Framework results table, segment tables with sample sizes, reconciliation, `metrics.json`,
  `metrics.md`, `outcomes.jsonl`.
- `src/ticketing_agent/pipeline.py`: the `Outcome` contract (FR-13's columns exactly), the `Pipeline` protocol,
  and `StubPipeline` — ingest plus retrieval plus an honest escalation, so the harness is end to end today.
- `tests/test_fr14_harness.py` (new): T-FR14-1 … T-FR14-27.
- `docs/decisions.md` D-36, D-37; `docs/BACKLOG.md` rows 6 (DONE) and 14;
  `evaluation/results/metrics.{json,md}` and `outcomes.jsonl` from a real run.

**Result**: `uv run pytest -q` → 217 passed (185 before this row). `uv run ruff check .` → clean.

**The run itself** — `python -m evaluation.harness --input data/validation_tickets.json --output
evaluation/results/`, exit 0, 5.3 s, and again on a copy under a name the code has never seen (A9's second
half):

| figure | value |
|---|---|
| Tickets processed / escalated | 80 / 80 |
| Retrieval hit rate | 96.2% (n=53) |
| Latency median / p95 | 43 ms / 50 ms |
| Decisions logged, reconciles | 80, yes |

100% escalation is by construction — the answering path is rows 8 to 13 — and the report says so in those
words. Retrieval hit rate 96.2% on validation against 95.2% on the development sweep, which is a useful sign
that retrieval generalises across the two files rather than having been fitted to one.

**Independent review** (reviewer subagent, PR-08 v1.0): 22 findings — 1 severe, 4 high, 9 medium, 8 low.
- *Severe* — the isolation guard wrapped only `pipeline.process()`. An `Outcome` that FR-13 rejects, or a
  pipeline returning a `dict`, killed the whole run **through D-27's unwritable-log path** — 76 of 80 tickets
  never attempted, reported to the operator as a broken log. Fixed (D-36) and covered by T-FR14-19's six
  variants.
- *High* — "reconciliation fails the run" had no test through `run()` (replacing the check with `ok = True`
  left every test green); metrics and report writing had no isolation and `--output` was validated only after
  the last ticket, so a mistyped path threw away a completed run; `variation_points` could only ever say
  "pass" and silently excluded the 8 enterprise tickets NFR-06 names (D-37); and three headline figures —
  route agreement, the FCR/escalation pair, retrieval hit rate — carried no caveat.
- *Mediums* fixed: the mandated arithmetic is now tested on known inputs (the per-class test would have passed
  with precision and recall swapped); guardrail blocks are counted even though FR-12 makes `block`
  non-terminal; segments carry retrieval hit rate, not only the answered rate; `--no-index-rebuild` now
  refuses instead of silently building; `tickets_in_file` is the file's count rather than the truncated one;
  `main()` is tested; the report names its `run_id` and decision-log path so an assessor can join the two.
- *Lows* fixed: nearest-rank p95 (rounding understated it), `unknown_channel` and the ingest-defect breakdown
  (D-13), FR-15 attribution on a provider-caused `pipeline_error`, `_pct` returning `None` rather than `0.0`
  for an empty denominator, and `detail` withholding any exception message that quotes the ticket.

**A process slip worth recording**: I reported "lint clean" for this row before it was. `uv run ruff check .`
had been run before `harness.py` was written, not after, and the review found 9 errors. The lint command
belongs in the same breath as the test command, every time.

**Not fixed, on purpose**
- `metrics.json` is not byte-comparable between runs (timestamps and latencies). Routing and the governance
  block are identical, which is what NFR-08 requires. Documented in FR-14 §7.
- The suite asserts against the validation set's composition (80 tickets, 8 enterprise). It is not tuning —
  those are structural probes for the low-confidence flag — but a fixture would decouple them.

**Open questions for the human** (FR-14 §7)
1. FCR and response time cannot be observed offline; the report measures correct automated handling and its
   own processing time, and says so. Confirm that is the framing you want in the Stage 5 report.
2. Hallucination rate and citation accuracy need two assessors over ≥50 responses. The harness reports
   unresolvable citations as a floor only.
3. NFR-01's p95 under 3 s is currently flattering (50 ms) because no model is called. It must be re-measured
   once drafting lands at row 11.

## 2026-09-28 · Row 7 · CHECKPOINT: the retrieval relevance threshold · FR-10 · **awaiting the author**

Analysis run, recommendation below, **decision not taken**. `evaluation/reports/retrieval_sweep.md` is
regenerated with two columns added for this checkpoint: how often the threshold makes its decision (*returns
nothing*) and how often that decision was right (*of those, truly unanswerable*).

**The population, for context.** Of the 500 development tickets: 357 answerable from the documentation (71%),
143 not (29%), and 189 labelled `escalate` (38%).

| threshold | hit rate on answerable | answerable lost | returns nothing | of those, truly unanswerable | fluency gap |
|---|---|---|---|---|---|
| 0.00 | 95.2% | 4.8% | 0.0% | — | 4.3 pts |
| **0.25** | **95.2%** | **4.8%** | **1.6%** | **100%** | **4.3 pts** |
| 0.30 | 94.4% | 5.6% | 2.6% | 85% | 7.8 pts |
| 0.40 | 93.3% | 6.7% | 5.8% | 72% | 6.3 pts |
| 0.45 | 91.9% | 8.1% | 7.0% | 66% | 6.0 pts |
| 0.50 | 86.6% | 13.4% | 10.8% | 52% | 8.1 pts |
| 0.60 | 63.0% | 37.0% | 36.4% | 37% | 13.4 pts |

**The finding that matters most, and it is not about which number to pick.** The relevance threshold cannot
deliver the escalation-rate target, and must not be asked to. 29% of tickets are unanswerable, but even at
0.60 — where a third of answerable tickets have already lost their article — only 36% of tickets return
nothing and only 37% of *those* are genuinely unanswerable. Retrieval's "nothing is relevant" signal simply
does not separate the two populations: the documentation is close enough in wording to most unanswerable
tickets to score above any threshold that keeps the answerable ones. Getting escalation to ≤30% is FR-02's
confidence threshold and FR-12's grounding check doing their jobs, not this one. If this threshold is ever
raised to chase the escalation number, it will destroy quality and fairness together and still miss.

**Recommendation: 0.25.** Three reasons, each measurable:
1. **It costs nothing.** Answerable tickets lost is 4.8%, identical to threshold 0. Hit rate is unchanged at
   95.2%, top-1 at 89.9%.
2. **Every decision it makes is correct.** Of the tickets it leaves empty, **100%** are genuinely
   unanswerable — 8 tickets that would otherwise have been answered from irrelevant passages. At 0.30 that
   precision drops to 85%, and it keeps falling.
3. **It is the last threshold that satisfies NFR-06.** The fluent/non-fluent gap is 4.3 points at 0.25 and
   **7.8 at 0.30** — over the 5-point limit. Raising the threshold makes the system worse for non-fluent
   English faster than it makes it safer, which is exactly what the Governance Framework's fairness audit
   predicts for retrieval.

**What choosing 0.25 accepts.** It leaves 135 of the 143 unanswerable tickets retrieving something, so the
later stages have to catch them: grounding must fail on a draft built from irrelevant passages, and the
confidence threshold must escalate low-confidence classifications. If those two do not hold up at rows 9 and
12, the answer is to strengthen them, not to raise this threshold.

**Alternatives, honestly.** If you prefer a larger safety margin at the retrieval stage, **0.40** is the next
defensible point: 93.3% hit rate, 72% of empties genuinely unanswerable, and a 6.3-point fluency gap — which
means accepting an NFR-06 finding and reporting it. Anything at or above 0.50 is not defensible on this data.

**How to apply it**: set `RELEVANCE_THRESHOLD` in `.env`. No code change (FR-10 §3.3), and the harness prints
the threshold in use in both reports so the gate run shows what was chosen.

**Open question this raises for row 7's sign-off**: section chunking cannot answer a question whose answer
spans two sections, and a parent-document strategy would. The sweep suggests it is not urgent — top-1 is
89.9% — so I have not built it.

## 2026-09-28 · Row 7 signed off by the author · FR-10

`RELEVANCE_THRESHOLD=0.25`, chosen from the sweep and recorded as D-38 with the evidence and the
alternatives. Set in `.env.example` and in the local `.env`; no code changed, which was the point of
FR-10 §3.3. The harness prints the threshold in use in both reports, so the gate run shows what was chosen.

The author's attention was drawn to the finding that the threshold cannot deliver the escalation target and
must not be raised to chase it (D-38), and to what 0.25 accepts: 135 of 143 unanswerable development tickets
still retrieve something, which rows 9 and 12 have to catch.

Row 7 → DONE. Next: row 8, the intent and urgency classifier with its calibration table.

## 2026-09-28 · Row 8 · B-06 Intent and urgency classifier · FR-08, FR-05

**Files changed**
- `docs/specs/FR-08.md` (new, covers FR-05 too): the model and why, grouped folds, calibration, the reason
  FR-05 asks for, 26 acceptance tests, 6 open questions.
- `src/ticketing_agent/classify.py`: `train`, `TrainedClassifier`, `IntentClassifier`, `Classification`,
  `calibration_table`, `order_escalation_queue`, near-duplicate clustering, the confidence calibrator.
- `src/ticketing_agent/config.py`: `CLASSIFIER_PATH`; `.env.example` documents it.
- `scripts/train_classifier.py` (new) → `evaluation/reports/classifier_calibration.md`.
- `tests/test_fr08_classify.py` (new): 33 tests. `docs/decisions.md` D-39 … D-41.
- `pyproject.toml`: `joblib`.

**Result**: `uv run pytest -q` → 250 passed (240 at first green, 217 before this row).
`uv run ruff check .` → clean. Tests inject a hashing embedder, so none needs the model or the network.

**The measured figures** (real embedder, development set only, cluster-grouped folds):

| | value | note |
|---|---|---|
| Intent accuracy | **88.6%** | 99.6% row-wise, 94.0% exact-body-grouped |
| Intent classes below 85% precision | 3 of 22 | NFR-03's strict per-class reading is not met |
| Urgency accuracy | **48.4%** | against a **73.0% ceiling** set by the labelling |
| Calibration, dominant band | **2.3 points** over 475 of 500 predictions | one thin 25-prediction band is 42.6 |

**Three measurement errors were found and fixed, every one of which produced a flattering number.**
1. **The dataset leaks.** 500 tickets hold 215 distinct bodies — 96 wording clusters — and every repeated body
   carries one intent. Row-wise cross-validation scores a near-duplicate lookup: 99.6%. Grouped by cluster it
   is 88.6% (D-39). The review caught that my first fix, grouping by *exact* body, was still not enough,
   because 168 of 215 bodies have a same-intent paraphrase elsewhere.
2. **The confidence was degenerate.** Isotonic calibration on 0/1 targets emitted exactly 1.0 for 488 of 500
   tickets — four distinct values in total, a certainty claim from a 94% classifier, and nothing for FR-02 to
   threshold on. A one-dimensional logistic fit gives 213 distinct values and never saturates (D-40).
3. **The report described a model nobody runs.** The out-of-fold estimator lacked the shipped urgency model's
   class weighting, and then — after that was fixed — still used a different inner calibration CV, which
   shifted the top-probability distribution by 7.5 points. Both now come from one factory, pinned by T-FR08-15.

I also nearly shipped the in-sample mistake I had just diagnosed: the first calibration table scored the
calibrator on its own fit and read 0.0 points. It is now cross-fitted, and T-FR08-18 fails if a gap of exactly
zero ever reappears.

**Independent review** (reviewer subagent, PR-08 v1.0): 17 findings — 1 severe, 6 high, 10 medium/low. All the
severe and high ones are fixed: the degenerate confidence, the estimator mismatch, the near-duplicate leak,
the fold count printed beside "grouped" being the row-wise one, spec/code drift on isotonic, the report's
missing verdicts, and the `verify=False` flag that could switch off the embedder-mismatch guard (now removed —
CLAUDE.md forbids a switch that turns a check off, and with it off a broken embedder became a run of
`unclear_request` that looks like a result). Mediums fixed: alternatives documented as raw rather than
calibrated, a silent calibration failure now logs, the report states what it measured, an unrecognised urgency
is treated as `medium` rather than sorted below `low`, timestamps compare as times, evidence no longer cites
the ticket itself, and there are now tests for load failures and for the confidence distribution.

**Not fixed, on purpose**
- The model's fingerprint is not checked against the training corpus on load, so a model trained on a
  superseded file would still be served if the embedder matches. Recorded for row 14's wiring.
- FR-08's criterion says per-class precision and recall appear "in the metrics report"; they are in
  `classifier_calibration.md`. Row 14 carries them into the harness report.
- Urgency is left as a classifier rather than rules. That is a product decision (D-41).

**Open questions for the human** (FR-08 §7)
1. Three intents miss NFR-03's 85% per-class precision. Accept, merge the rare classes, or add rules?
2. NFR-03's calibration target holds for 475 of 500 predictions and fails in one 25-prediction band. Acceptable?
3. FR-05's three urgency levels are in practice two. Accept, merge low and medium, or relabel?

## 2026-09-28 · Row 9 · B-07 Routing · FR-09, FR-03, FR-16, FR-02

**Files changed**
- `docs/specs/FR-02.md` (new): `RoutingDecision`, the full D-16 precedence table as the whole of the
  component, `confidence < T`, 10 acceptance tests. `docs/specs/FR-09.md` (new): the four intents as a code
  constant, 8 tests. `docs/specs/FR-16.md` (new): the kill switch, with the Governance §5 five questions
  answered, 7 tests.
- `src/ticketing_agent/route.py`: `Router.decide`, `RoutingDecision`, `ALWAYS_ESCALATE_INTENTS`,
  `KNOWN_INTENTS`, `MONEY_TRIGGERS`/`DATE_TRIGGERS`, `PRECEDENCE`, `matches_triggers`, one written
  explanation per reason.
- `src/ticketing_agent/config.py`: `Settings.kill_switch_on` is now the single, fail-safe switch check;
  `CONFIDENCE_THRESHOLD=0.0` from the environment warns.
- `scripts/confidence_sweep.py` (new) → `evaluation/reports/confidence_sweep.md`, for checkpoint row 10.
- `tests/test_fr02_routing.py` (new): 61 tests. `tests/fixtures/money_commitment_tickets.json`: `SYN-MONEY-013`
  and `-014`. `tests/test_engineered_fixtures.py`: `phrases()` now calls the shipped matcher.
- `docs/specs/FR-03.md`, `FR-12.md` (§3.4 = D-16), `FR-09.md` updated; `docs/decisions.md` D-42, D-43.

**Result**: `uv run pytest -q` → **311 passed** (298 at first green, 250 before this row).
`uv run ruff check .` → clean. Routing makes no model call, reads no clock and touches no network, so the
whole suite stays offline.

**What the sweep found** (grouped out-of-fold predictions, development set only)

| T | answered | against the label | wrong intent | must-escalate answered | fluency gap |
|---|---|---|---|---|---|
| 0.75 | 83.0% | 112 | 42 | **10** | 1.6 |
| 0.80 (placeholder) | 78.6% | 105 | 26 | **9** | 2.5 |
| 0.85 | 64.4% | 84 | 10 | 0 | **10.1** |
| 0.90 | 43.0% | 62 | 2 | 0 | **13.8** |

FR-09's rule is exact, but it fires on the *predicted* intent, so on wording the model has not seen **10 of
500 tickets labelled `must_not_auto_respond` would be answered** — nine `security_incident` read as
`account_access`/`api_key_issue`, one `unclear_request` as `database_issue`, all stating 0.79–0.85. T ≥ 0.85
catches every one, at 64% answered and an NFR-06 breach on fluency (limit 5 points). The report names the ten
tickets individually and states that the confidence it thresholds is 42.6 points out of calibration in the
0.60–0.80 band — which is where those ten sit. D-43. **The script first read 0 wrong intents at every T**
because the saved model was fitted on these same tickets; it now uses the grouped out-of-fold predictions and
keeps the in-sample table beside them, so the leak is visible rather than flattering (D-39 again, D-42).

**Independent review** (reviewer subagent, fresh session, PR-08 v1.0): 13 findings — 3 high, 5 medium, 5 low.

All three high ones are fixed, and each had a way of looking fine (D-42):
1. **The kill switch failed open.** `Path.exists()` is `os.path.exists`, which swallows every `OSError`, so a
   switch file inside an unreadable directory read as *off* — an operator would `touch storage/KILL_SWITCH`,
   believe automation was stopped, and the run would keep answering. One implementation now, using `stat`.
2. **T-FR16-6 could not catch that**: it put a *directory* at the switch path, which `exists()` reports as
   present, so the fail-safe branch was dead code and mutating it left the suite green. T-FR16-6b now makes
   the stat call genuinely fail; T-FR16-6c fails if routing ever re-implements the check.
3. **FR-03's money rule missed plurals and hyphens.** "Please issue refunds", "the service credits", "these
   disputes", "we will raise chargebacks", "a write-off of the balance" all routed to `auto_respond`. The
   matcher now accepts a plural and a hyphen, the fixture module calls it instead of carrying a second copy
   (which is how the fixtures agreed a plural was answerable), and D-21's "0 of 580 tickets match" was
   re-measured after the widening and still holds.

Mediums fixed: `unknown_intent` moved below the money and date rules and written into D-16/FR-02 §3/FR-12
§3.4, with T-FR02-6b parsing both spec tables and failing on drift; `log_fields()` now carries
`prediction_confidence` and merges the classification's half itself (splatting both raised `TypeError` on
`detail`); the four untested ranks and the unranked-name `ValueError` now have tests, and T-FR02-5 asserts the
case table covers every rank; two test ids that belonged to other acceptance criteria renamed
(`T_FR03_7` is row 13's handover criterion, `T_FR03_13` is row 2's); the sweep names NFR-06's 5-point limit,
flags the rows that breach it, and states the calibration gap it is reading a threshold off.

Lows fixed: `CONFIDENCE_THRESHOLD=0.0` warns at load; the sweep refuses any `--input` that is not
`TRAINING_TICKETS_PATH`, with no override; T-FR09-1 asserts the reason at 0.01 as well as 0.99; a missing
classification is tested.

**Not fixed, on purpose**
- `KILL_SWITCH_FILE` is resolved against the process working directory like every other path in `Settings`
  (which is what makes the cache deterministic), so a relative `./storage/KILL_SWITCH` only works when the
  process runs from the repo root. The deployment should set an absolute path; noted for the governance
  declaration rather than changed here.
- FR-09 §4 said a missing classification is "treated as `unclear_request`". The code logs `unknown_intent`
  instead and the spec was corrected, because writing a prediction no model made into the log misreports
  what happened. Same escalation either way.
- `scripts/confidence_sweep.py` hardcodes its threshold ladder, its scratch Chroma path and a placeholder
  model id (`"not-used-by-routing"`; routing makes no call). Dev script, both paths overridable.
- T-FR16-7 (a harness run with the switch on) waits for row 14, when routing is wired into the pipeline;
  today it would pass for the wrong reason, because the harness still runs `StubPipeline`. Recorded in the
  spec rather than quietly skipped.

**Open questions for the human** (row 10 decides T)
1. A 2% must-escalate leak on unseen wording (T = 0.80) against a 64% answer rate and a 10.1-point fluency
   gap (T = 0.85). Which cost is acceptable?
2. Per-intent thresholds would separate those two, and are a small change. Wanted before the gate or after?
3. `feature_request` is 20 of 500 development tickets and escalates by rule (FR-09 §7). Still wanted, or
   should it get a templated policy answer?

## 2026-09-28 · Row 10 · CHECKPOINT: the confidence threshold T · FR-02 · **awaiting the author**

The analysis is `evaluation/reports/confidence_sweep.md`, regenerated today from
`scripts/confidence_sweep.py` against the development set only. Nothing here sets a value: the placeholder
`CONFIDENCE_THRESHOLD=0.80` in `.env` is still a placeholder, and D-43 records the trade-off.

### What the sweep says

| T | answered | escalated | answered against the label | answered on a wrong intent | **must-escalate answered** | fluency gap (NFR-06 limit 5) |
|---|---|---|---|---|---|---|
| 0.750 | 83.0% | 17.0% | 112 | 42 | **10** | 1.6 |
| 0.800 | 78.6% | 21.4% | 105 | 26 | **9** | 2.5 |
| 0.850 | 64.4% | 35.6% | 84 | 10 | 0 | **10.1** |
| 0.900 | 43.0% | 57.0% | 62 | 2 | 0 | **13.8** |
| 0.925 | 21.4% | 78.6% | 37 | 0 | 0 | 1.8 |

Three things are true at once, and optimising any one of them alone picks a bad T.

1. **T is not the escalation lever it looks like.** At T = 0 the threshold never fires and 17% of tickets
   still escalate, on rules T cannot move (FR-09's four intents, FR-03's money and date triggers, FR-07's
   defects, FR-10 returning nothing). The sweep's right-hand column counts only the tickets whose *sole*
   reason to escalate is confidence — that is the population T governs.
2. **FR-09's rule is exact, but it fires on the predicted intent.** On grouped out-of-fold predictions, 10 of
   500 tickets labelled `must_not_auto_respond` would be auto-answered: nine `security_incident` read as
   `account_access` or `api_key_issue`, one `unclear_request` read as `database_issue`. Every one states
   between 0.7929 and 0.8452 confidence, so **T ≥ 0.85 catches all ten** and T = 0.80 catches one. The report
   names the ten tickets. The PRD's "zero auto-responses to must-escalate tickets" is met by the rule itself
   and by any run over data the classifier was trained on; on unseen wording, T is what stands behind it.
3. **The T values that look best on wrong answers breach NFR-06, and sit in the badly calibrated band.** The
   fluent/non-fluent answer-rate gap is 10.1 points at T = 0.85 and 13.8 at T = 0.90, against NFR-06's
   5-point limit (populations 380 and 120). And the confidence being thresholded is **42.6 points out of
   calibration in the 0.60–0.80 band** — 25 predictions stating 78.6% that are right 36.0% of the time
   (D-40) — which is exactly the band the ten leaking tickets sit in.

`answered against the label` (112 at T = 0.75, 84 at T = 0.85) is a different population from the ten:
those are tickets the pack labels `escalate` for reasons no implemented rule covers — complexity and urgency
judgements. No T removes them; they are an argument for the rules the PRD has not asked for, not for a
higher floor.

### The options, as I read them

- **T = 0.85** — no must-escalate ticket answered on unseen wording, 64.4% answered, and an NFR-06 breach on
  fluency that has to be declared. Safe on the criterion the PRD states most strongly; expensive elsewhere.
- **T = 0.80 (keep the placeholder)** — 78.6% answered, inside NFR-06 at 2.5 points, and ~1.8% of
  must-escalate tickets answered on unseen wording. Defensible only if the guardrails (row 12) are treated as
  the real second line of defence, since a misread `security_incident` would still have to survive grounding
  and tone checks before going out.
- **T = 0.90 or above** — buys almost nothing over 0.85 on the leak, costs half the answers, worsens the
  fairness gap. Not worth it on this data.
- **Per-intent or targeted floors** — e.g. a higher floor for tickets where a must-escalate intent is among
  the alternatives FR-08 already records. This could close the leak without the global cost, and it is a small
  change. **It is unmeasured**: the out-of-fold alternatives are not saved, so I cannot say today how many of
  the ten it would catch. I can measure it if you want it before the gate.

**Recommendation**: **T = 0.85**, and declare the NFR-06 fluency gap in the PRD revision rather than hide it —
the PRD's own words for FR-09 are "zero auto-responses", and a 2% leak of security incidents is the one
failure Marcus and Daniel both described. If a 64% answer rate is unacceptable for the business case, the
honest fix is targeted floors, not a lower global T.

**Row 10 set to `HUMAN`. The value is yours.** Whatever you choose, the PRD revision should record the leak
measurement (D-43), because it is the strongest single finding in the build so far.

**Separately, two configuration items for you** (found while checking whether a real provider call was safe):
`MODEL_NAME` is `meta-llama/llama-3.1-8b-instruct`, which on OpenRouter is the **paid** endpoint — NFR-07
wants the `:free` suffix — and `JUDGE_MODEL_NAME` is still the `.env.example` placeholder. No real model call
has been made by anything in this repository yet (`storage/llm_cache.sqlite` does not exist), and row 11 is
the first code that needs one.

## 2026-09-28 · Row 10 signed off by the author · FR-02

**T = 0.85**, the recommended value, with the NFR-06 fairness gap to be declared in the PRD revision (D-44).

- `.env.example`: `CONFIDENCE_THRESHOLD=0.85`, with the reason and the cost in the comment.
- **`.env` itself is the author's to change** — this session has no permission to read or write it. Until
  that line is updated the running system still uses 0.80, and the harness prints the value in use, so a gate
  run shows which one was applied.
- `docs/decisions.md` D-44 records the choice, the three costs (64.4% answered, 84 answered against label,
  10.1-point fluency gap) and the two consequences that are not optional: the PRD revision declares the
  NFR-06 breach, and targeted per-intent floors stay open as the way to buy answer rate back.

Row 10 → `DONE`. Next TODO row whose dependencies are met is **row 11** (B-08 answer drafting with citations,
FR-11 and FR-06), which is the first code in the system that makes a real model call.

## 2026-09-28 · First real provider calls (`scripts/provider_smoke.py`) · FR-15, NFR-07

Not a backlog row: a smoke check run before row 11 builds on `complete_structured`. Full findings in D-46.

| check | result |
|---|---|
| plain completion | **pass** — reply returned, `attempts=2` (the retry was needed on the first ever real call) |
| identical call replayed from cache | **pass** — `cached=True`, 0 provider requests, cache survives across processes |
| `complete_structured` → `AnswerDraft` | **unverified** — 20 live attempts over 4 minutes, all 429 before a model saw the prompt |
| nonexistent model id | **pass** — typed `ProviderError` 400, no retry storm |

- `MODEL_NAME=google/gemma-4-31b-it:free` answers **nothing**; `qwen/qwen3.8-27b:free` answered once. The
  429s are `limit_source: upstream_provider_shared_pool` with `usage: 0` on the account, so this is the free
  pool being saturated, not our quota — and $10 of credits would not change it (D-45's 50/day is a different
  limit).
- **Fixed:** `RateLimited` now names `limit_source` and `provider_name`, so the decision log can tell a busy
  pool from an exhausted allowance. The body's free text is still dropped unread (NFR-04); T-FR15-34 plants a
  secret in it and fails if it leaks.
- **Added:** `scripts/provider_smoke.py` (`--model`, `--patient`, catalogue check, recording to
  `tests/fixtures/recorded_provider_responses.json`, and a refusal to run on a paid endpoint with no
  override).
- **Decision needed before row 15**: Groq (recommended), bring-your-own-key on OpenRouter, or batch the gate
  run. Row 11 proceeds either way — its tests use `FakeTransport`, as CLAUDE.md requires.

**Result**: `uv run pytest -q` → **313 passed**. `uv run ruff check .` → clean.

## 2026-09-28 · Provider moved to Groq, and `complete_structured` verified · FR-15, FR-11, NFR-07

The author switched `LLM_BASE_URL` to Groq after D-46. **No code changed** — the client is OpenAI-compatible
and never knew the host — but every model id did: `vendor/model:free` is OpenRouter syntax and does not exist
on Groq, and the account's live catalogue (11 active models) does not include `llama-3.3-70b-versatile`,
which Groq's own docs page lists.

`scripts/provider_smoke.py --model openai/gpt-oss-120b` → **4/4 checks pass**, and so does gpt-oss-20b:

- plain completion 1.7 s, `attempts=1`, `model_version='fp_5082008e34'`;
- the identical call replayed from cache with 0 provider requests;
- **`complete_structured` → `AnswerDraft` on the first attempt, no repair, citing `DOC-BILL-001#2`** — the
  row-11 dependency D-46 could not test. Recorded to `tests/fixtures/recorded_provider_responses.json`;
- a nonexistent id → typed `ProviderError` 404, no retry storm.

Measured limits: **1000 requests/day per model, 8000 tokens/minute**. The tokens are the binding constraint —
roughly six drafting calls a minute, so a gate run is 15-20 minutes and must pace itself rather than burst
(D-47). `.env.example` carries the ids, the reasons and that warning.

Nothing in the repository is blocked now. Row 11 (B-08 answer drafting, FR-11 and FR-06) is next.

## 2026-09-28 · Row 11 · B-08 Answer drafting with citations · FR-11, FR-06

**Files changed**
- `docs/specs/FR-11.md`, `docs/specs/FR-06.md` (new, written before the code).
- `src/ticketing_agent/generate.py`: `Drafter`, `DraftResult`, `assemble_reply`, the citation rules and
  FR-06's three constants. Was a one-line placeholder.
- `src/ticketing_agent/prompts.py`: a real loader — by id and version, SYSTEM/USER split, fingerprint,
  and a refusal for any file whose shape cannot be read confidently. Also a placeholder before this.
- `src/ticketing_agent/provider.py`: `complete_structured` now reports both requests when it repairs.
- `tests/test_fr11_draft.py` (new, 35 tests), `tests/test_prompts.py` (new, 11 tests).
- `prompts/build/PR-01_answer_draft_v1.0.md`: `model:` frontmatter only — the named free model no longer
  exists on either free tier. No version bump (the register's rule is about prompt text); recorded in
  `prompts/README.md`'s new change history.
- `docs/decisions.md` D-48, D-49.

**Result**: `uv run pytest -q` → **359 passed** (338 at first green, 313 before this row).
`uv run ruff check .` → clean. Every test runs offline against `FakeTransport`, including one that replays
the reply `openai/gpt-oss-120b` actually produced (D-47).

**Checked against the real model as well** (not part of the suite): three development tickets through the
real chain — retrieval → PR-01 → structured parse → citation check → assembled reply. All three drafted
usable, cited replies; two were cache hits after the review's escaping change, which shows the escaping is a
no-op on this corpus.

**Independent review** (reviewer subagent, fresh session, PR-08 v1.0): 13 findings — 3 high, 5 medium,
5 low. All highs and all mediums fixed; details in D-48. The one that mattered most: **an unretrieved
article id could still reach the customer inline in the prose**, because only the `citations` array was
checked and PR-01's own rule 2 shows citations as inline brackets. No fixture contained a bracket, so the
whole suite was blind to it.

**Not fixed, on purpose**
- `PROMPTS_DIR` is derived from the repo layout, so a non-editable install would not find `prompts/`.
  The project runs from source; worth a line in the README before anyone packages it.
- Inline ids are validated but not rewritten, so a customer may see `… [DOC-BILL-001#2].` beside the
  `Based on:` line. Tidying it means editing the model's words, which FR-06 §3 deliberately does not do.
- A model inventing a support email inside a cited sentence is not caught here; it is FR-12's private-data
  guardrail at row 12. T-FR06-7 now says so rather than implying coverage it does not have.

**Open questions for the human**
1. FR-11 refuses a whole draft when one sentence is uncited — stricter than the PRD's 95% target. Keep, or
   drop the uncited sentence and send the rest?
2. The wording of `DISCLOSURE` and `HUMAN_ROUTE` is customer-facing and should be read by Marcus or Ravi.
3. Should an automated reply carry a greeting? It would mean putting the customer's name back into outbound
   text, which FR-11 keeps out of the model's sight.

## 2026-09-28 · Row 11 follow-up · the author's three answers · FR-11, FR-06

Recorded as D-50. Two behaviour changes and one confirmation:

- **Uncited sentences are now sent** rather than making the draft unusable. `uncited_sentence` is gone as a
  reason; `no_cited_article` replaces it for a draft that cites nothing at all. The `generation` row's
  `detail` says when a reply went out carrying an unsourced sentence — this is now the only trace, so row
  12's grounding guardrail matters more than it did an hour ago.
- **Replies open with `Hi {first name},`**, inserted by code. The name still never reaches the model.
  An unusable name field (markup, an address, a newline, over 60 characters) falls back to `Hello,`.
- The disclosure wording stands; it still wants a human read before a customer sees it.

`uv run pytest -q` → **370 passed** (359 before). `uv run ruff check .` → clean. Six tests added or
rewritten: T-FR11-4 (now asserts the sentence is sent *and* logged), T-FR11-4b, T-FR06-11, -12, -12b, -13.

Checked against the real model: `DEV-0002` now returns a reply opening `Hi Kavya,` with the same cited body
and the same three closing lines.

## 2026-09-28 · Row 12 · B-09 Guardrails · FR-12 (with NFR-04, FR-03, FR-11, FR-02)

**Files changed**
- `src/ticketing_agent/guardrails.py`: the Governance Framework's five checks, `check_ticket` (pre-draft),
  `GroundingJudge` (PR-03), `GuardrailReport`, and every pattern table. Was a one-line placeholder.
- `tests/test_fr12_guardrails.py` (new, 47 tests). `tests/test_engineered_fixtures.py`: the mirrored tables
  deleted, the shipped ones imported — what FR-12 §7 and this row asked for.
- `src/ticketing_agent/logging_store.py`: `governance_record()` no longer raises on a row whose
  `guardrail_results` carry a third element.
- `docs/specs/FR-12.md` updated (the exemption lists, the `reply`/`sentences` inputs, `check_error`,
  T-FR12-24 and the tests added after the review). `docs/decisions.md` D-51.

**Result**: `uv run pytest -q` → **417 passed** (405 at first green, 370 before this row).
`uv run ruff check .` → clean. The judge runs through `FakeTransport`, so PR-03's real prompt, its parsing
and its exact-quote check are exercised with no network and no key.

**Independent review** (reviewer subagent, fresh session, PR-08 v1.0): 17 findings — 6 high, 8 medium,
3 low. All highs and all mediums fixed; D-51 has the detail. The two worth repeating:

- **The engineered draft corpus had never been run through the code**, and disagreed with it: both drafts it
  declares clean were blocked. The causes were that FR-12's own "a draft that says plainly it does not know
  passes" was never implemented, and that FR-06's mandatory lines were exempted by string literals copied
  from `generate.py` — so the next permitted change to the disclosure wording would have failed **every**
  reply in a run. The test that claimed to cover the first asserted it with a supported factual sentence.
- **`governance_record()` raised on every guardrail row**, because the row carried triples where FR-13
  declares pairs — i.e. the assessor-facing projection crashed on exactly the rows FR-12's acceptance
  criterion requires to exist.

**Not fixed, on purpose**
- `PRIVATE_IP` stays in the draft-side list (FR-12 §7 flagged it as "remove if it proves noisy"). It now
  requires four octets, so a `10.1.2` version string no longer fires, but nothing has measured it against
  `documentation.json` yet. Worth a line in row 15's gate run.
- The overlap floor stays at 0.3 (D-22, re-measured at row 5). Row 12 changed what counts as a claim, not
  the number; if the author wants the number revisited, the measurement script is `scripts/` work.

**Open questions for the human**
1. The refusal and FR-06 exemptions are new behaviour written from FR-12 §3.2.2's own sentence. They widen
   what is *not* a claim, which is the safety-relevant direction — worth a read.
2. `check_error` is now distinct from `provider_unavailable` in the log. Both escalate; only the reason
   differs, and the metrics report should probably count them separately at row 14.

## 2026-09-28 · Row 13 · Escalation handover package · FR-01 (supports FR-05, FR-15)

**Files changed**
- `docs/specs/FR-01.md` (new, written before the code).
- `src/ticketing_agent/handover.py`: `HandoverWriter`, `Handover`, the `UNCERTAINTY` table, the PR-02 call
  and the template fallback. Was a one-line placeholder.
- `src/ticketing_agent/generate.py`: `_fill` becomes `fill_slots`, shared with the handover.
- `tests/test_fr01_handover.py` (new, 24 tests). `docs/decisions.md` D-52.

**Result**: `uv run pytest -q` → **441 passed** (432 at first green, 417 before this row).
`uv run ruff check .` → clean. PR-02 runs through `FakeTransport`, so the real prompt, the parsing and the
template fallback are exercised with no network and no key.

**Checked against the real model** (not part of the suite): `DEV-0015`, a former employee still holding
access, escalated as `must_escalate_intent`. PR-02 produced a usable summary, goal and first check — and an
uncertainty sentence containing the reason code, which is what D-52's first rule now prevents.

**Independent review** (reviewer subagent, fresh session, PR-08 v1.0): 17 findings — 1 severe, 2 high,
6 medium, 8 low. The severe one and both highs are fixed, along with every medium; D-52 has the detail.

- **Severe:** the withholding rules read `ticket.text` (capped at 8000 characters by FR-07) while the prompt
  sent the raw `subject` and `body`. A secret or an injection marker past the cap was invisible to the check
  and transmitted — and cached on disk. Handovers always meet truncated tickets, since `text_truncated` is
  an escalation reason, so this was the common case rather than a corner.
- **High:** a card number in a ticket *subject* was copied verbatim into the decision log by the template,
  and the spec claimed a scrub that FR-13 had explicitly declined to build.
- **High:** the log row claimed `FR-05` without carrying an urgency, and omitted the intent, confidence and
  retrieved articles the PRD names as part of the package.

**Not fixed, on purpose**
- `generate.py` renders the raw subject and body too, the same mismatch as the severe finding. It is not
  reachable today — routing escalates any over-cap ticket with `text_truncated` before the drafter runs — so
  the fix belongs with row 14's wiring, where the guarantee becomes structural rather than incidental.
- FR-13 still does not scrub `summary`. The template now masks secrets itself; whether an ordinary name or
  address may sit in an internal log is FR-13 §7's open question for the author.
- The note is written before the terminal row exists, because the row needs the summary. A process death
  between the two leaves an escalation with no row; `Reconciliation.missing` catches it after the fact.
  Structurally enforcing "log before the action" here is row 14's.

**Open questions for the human**
1. `already_tried` is the field PR-02 is most likely to invent. Nothing detects a fabricated step; the
   20-ticket review the prompt describes is the only honest check, and it is yours to run.
2. The template note is terse by design — it records what was known rather than reading well. If you want it
   to read like prose, that is a second prompt, not a longer template.

## 2026-09-28 · Row 14 · Pipeline wiring: the LangGraph graph · FR-01…FR-16

**Files changed**
- `src/ticketing_agent/pipeline.py`: `SupportPipeline` (LangGraph `StateGraph` over a Pydantic
  `PipelineState`), `_terminal_reason`, `_as_terminal`, `_explanation`, `_requirements`. `StubPipeline` and
  the `Outcome` contract are unchanged.
- `evaluation/harness.py`: the graph is the CLI default, `--stub-pipeline` is the only route to the stub,
  the log is attached to the pipeline, and the spend counters are each ticket's own.
- `src/ticketing_agent/guardrails.py`: the exact-quote check folds punctuation and accepts a multi-span
  quote (D-53).
- `tests/test_pipeline.py` (new, 25 tests), `tests/test_fr12_guardrails.py` (+3), `tests/test_fr14_harness.py`
  (T-FR14-22 patched for the new default and strengthened). `docs/decisions.md` D-53.

**Result**: `uv run pytest -q` → **469 passed** (460 at first green, 441 before this row).
`uv run ruff check .` → clean.

**Real runs** (Groq, development tickets, not part of the suite):

| run | processed | answered | escalated | blocked | reconciles |
|---|---|---|---|---|---|
| first, 6 tickets | 6 | 1 | 5 | 3 | yes, but 2 `pipeline_error` |
| after the row-14 fixes, 10 tickets | 10 | **4** | 6 | 3 | yes |

The first run found two bugs the 19 wiring tests missed — an escalation whose only model call was the
handover being refused by FR-13 for having no `prompt_version`, and `model_calls` counted up to three times
per request — and then the grounding investigation found the quote-matching defect that was costing most of
the answer rate (D-53).

**Independent review** (reviewer subagent, fresh session, PR-08 v1.0): 17 findings — 1 severe, 4 high,
8 medium, 4 low. The severe, all four highs and every medium are fixed; D-53 has the detail. The severe one
would have failed the gate run at row 15 on the first ticket the documentation cannot answer.

**Not fixed, on purpose**
- `redactions_in_log` still sums every row, so a redaction appearing on both an intermediate and a terminal
  row is counted twice. It is a reporting figure, not a decision, and the fix belongs with row 15's reading
  of the metrics.
- A terminal row's `prompt_version` names the prompt that produced its artefact while `model_calls` covers
  all three prompts' calls. One column cannot hold three; the intermediate rows carry the breakdown.
- `PipelineState(extra="forbid")` does not catch a node returning an unknown key — LangGraph filters updates
  to declared channels first. The docstring now says so rather than claiming otherwise.

**Open questions for the human**
1. Three of ten drafts are still blocked as `ungrounded_draft` by PR-03's genuine disagreement (not the
   quote-matching defect). Whether that is the judge being strict or the drafts being weak is what row 15's
   gate run has to characterise — it is the difference between a ~40% and a ~70% answer rate.
2. The graph makes up to three provider calls per answered ticket (draft, judge, and a handover on
   escalation). At 8000 tokens/minute that sets the pace of a gate run (D-47).

## 2026-09-28 · Row 15 · CHECKPOINT: THE GATE · FR-14 · **awaiting the author**

The full unattended run, the renamed-copy run and the Build Specification §06 rehearsal are done. The
machinery passes. **The quality figures do not mean what they appear to**, and that is the finding.

### The run

`uv run python -m evaluation.harness --input data/validation_tickets.json --output evaluation/results/`
→ `evaluation/reports/gate-2026-09-28.md`, exit code **0**.

| | |
|---|---|
| Tickets processed / in file | **80 / 80** |
| Decisions logged, reconciliation | **80, holds** — no missing, extra, duplicated or gapped rows |
| Wall time | **27 minutes** (1614 s), unattended |
| Answered / escalated | 17 / 63 (**78.8%** escalation rate) |
| Blocked by guardrails | 28 (all `grounding`) |
| Private data detections | 0 |
| Model calls / cache hits | 282 / 10 |

### Finding 1: a quarter of the run never got a fair attempt

**21 of 80 tickets — VAL-0057 to VAL-0080 — escalated as `provider_unavailable`.** Groq throttled the
account, the circuit breaker opened after five consecutive failures, and the remaining tickets were then
consumed in seconds because an open breaker fails fast. 282 model calls in 27 minutes is 10.4 a minute,
against the ~6 a minute that 8000 tokens/minute allows (D-47): throttling was arithmetically certain.

The behaviour is *correct* — every ticket still ended as a logged escalation with a handover, which is A11 —
but **the headline escalation rate of 78.8% is an availability artefact, not a quality result.** Read only
the 59 tickets that got an attempt:

| outcome | count | share of attempted |
|---|---|---|
| answered | 17 | **28.8%** |
| blocked by grounding | 27 | 45.8% |
| must-escalate intent | 14 | 23.7% |
| documentation had no answer | 1 | 1.7% |

**This is a defect in the harness, not only in the tier.** An unattended run should pace itself against the
provider's limit, or stop and say so, rather than finishing quickly by not trying. As it stands a reader of
`metrics.md` could mistake the tail for a quality measurement. The provider-unavailable count is in the
report, which is what makes this visible at all.

### Finding 2: grounding blocks 61% of the drafts that reach it

**27 of the 44 drafts that survived routing were refused by the grounding guardrail.** That is now the
single biggest lever on the answer rate — bigger than the confidence threshold the author set at row 10.
Row 14 already found and fixed one cause (the exact-quote check breaking on a judge's multi-span quote,
D-53); this is what remains after that fix, so it is either PR-03 being genuinely strict or the drafts being
genuinely unsupported. **Nobody has read the blocked drafts yet**, and that reading is what decides whether
the system answers ~29% or ~60% of tickets.

### Finding 3: the machinery does what it claims

- **Renamed copy**: `--input /tmp/nobody-has-seen-this-2026.json` ran with **10 of 10 identical decisions**
  and **zero model calls** (every reply served from the cache). No data file name is hardcoded (CLAUDE.md's
  first non-negotiable), and the same ticket routes the same way twice (A5, NFR-08), demonstrated rather
  than asserted.
- **Build Spec §06 rehearsal** in a clean clone: `uv sync` works, 469 tests pass with placeholder
  credentials and no network, training reproduces the same fingerprint, and a credential scan of the working
  tree *and the full git history* is clean. The rehearsal found the README's setup path was broken — it
  never said to train the classifier — which is fixed (commit `a2396d3`), along with a reproducible
  `scripts/demo_walkthrough.py`.

### What the author decides

1. **Re-run the gate with pacing before any quality claim is made.** The 78.8% escalation rate cannot be
   quoted as a result while 26% of the run was throttled. Pacing is a small change (wait when the breaker is
   open, or budget tokens per minute); it costs another ~40 minutes of run time and no money. **Recommended
   before anything else**, because every other number depends on it.
2. **Read a sample of the 27 grounding blocks.** If PR-03 is too strict, the answer rate roughly doubles; if
   the drafts are genuinely unsupported, then 29% is the honest figure and the PRD's target needs revisiting.
   This is a judgement about answer quality and it is the author's, not the system's.
3. **Accept or reject the free tier for the gate.** NFR-07 allows no spend, so "buy a higher tier" is not
   available: the choices are pacing, batching across days, or stating in the PRD revision that an 80-ticket
   unattended run cannot complete on a free account without pacing.

Row 15 → `HUMAN`. The gate is not signed off: the machinery passes and the quality figures are not yet
measurable.

## 2026-09-28 · Row 15 · THE GATE, re-run on OpenAI · FR-14 · **awaiting the author**

The author moved the runtime model to OpenAI on a $5 budget (D-55) after two free-tier runs were spoiled by
throttling, and asked for the self-pacing to be removed. Both done. **The gate now measures the system
rather than the provider.**

### The run that counts

`uv run python -m evaluation.harness --input data/validation_tickets.json --output evaluation/results/`
with `gpt-4o-mini` drafting and `gpt-4.1-mini` judging → `evaluation/reports/gate-openai-2026-09-28.md`,
exit code **0**.

| | free tier (Groq) | **OpenAI** |
|---|---|---|
| Tickets processed | 80 | **80** |
| Answered / escalated | 17 / 63 | **33 / 47** |
| Escalation rate | 78.8% | **58.8%** |
| `provider_unavailable` | **21** | **0** |
| Wall time | 27 min | **5 min** |
| Model calls | 282 | 158 |
| Median processing per ticket | 15.0 s | **4.4 s** |
| Reconciliation | holds | **holds** |

**Every ticket got a real attempt.** That is the whole of the difference: the free-tier runs were not
measurements of this system, they were measurements of a shared quota.

### What the system actually does, now that it can be measured

| outcome | count | share |
|---|---|---|
| answered with citations | **33** | 41.3% |
| blocked by grounding | 25 | 31.3% |
| must-escalate intent (by rule) | 14 | 17.5% |
| citation problems (`no_cited_article`, `invalid_citation`) | 7 | 8.8% |
| documentation had no answer | 1 | 1.3% |

**Grounding is still the biggest lever**: it refused 25 of the 58 drafts that reached it (43%, down from 61%
on the weaker free-tier model). Whether those 25 are the guardrail being strict or the drafts being
unsupported is still unread, and it is still the difference between a 41% and a ~70% answer rate.

**Cost**: 165 cached replies account for 113,232 input and 25,624 output tokens — **about $0.03 at list
prices** for a full 80-ticket run. The $5 budget covers well over a hundred runs, and a re-run over the same
tickets is free from the cache.

### What changed in the code

- **Removed**, at the author's request: the `_RateLimiter`, both `PROVIDER_*_PER_MINUTE` settings, their
  `.env.example` entries and the pacing tests. 471 tests still pass.
- **Kept**, and flagged to the author as a correctness fix rather than throttling: a `RateLimited` reply does
  not count towards the circuit breaker. An open breaker fails *fast*, so five 429s in a row used to turn
  every ticket behind them into an escalation without an attempt — that is what cost 21 tickets on Groq.
- **Kept**: the provider's token counts on the response, so a run's spend is visible rather than discovered.

### What the author decides

1. **Sign the gate off, or not.** The machinery passes on every criterion: 80/80 processed, 80 logged,
   reconciliation holds, a renamed input file gives identical decisions with zero model calls, the §06
   rehearsal passes from a clean clone, and the credential scan is clean. The *quality* question is (2).
2. **Read a sample of the 25 grounding blocks.** This is now the only thing standing between 41% and a
   materially higher answer rate, and it is a judgement about answer quality that the system cannot make
   for itself.
3. **Record NFR-07's amendment in the PRD revision** (D-55), with the three free-tier measurements as its
   evidence.

Row 15 stays `HUMAN`.

## 2026-09-28 · Row 15 · reading the refused drafts · FR-12 · **row 15 still awaiting the author**

The gate's open question was whether grounding refusing 25 of 58 drafts meant a strict guardrail or
unsupported drafts. `scripts/grounding_review.py` (new) writes `evaluation/reports/grounding_review.md`:
every refusal with its ticket, its sentences, their overlap, PR-03's verdict and the passages.

**Answer: neither. Nine of the 25 were a defect in our own quote matching** (D-56). PR-03 said
`supported: true`; the check rejected its quote, because a model supporting a sentence from a bulleted list
copies several lines whose concatenation appears nowhere. D-53 fixed that shape for semicolons and folded
whitespace before splitting, which destroyed the newlines — so the fix never applied to the commonest case.

| | answered | escalation rate | blocked by grounding |
|---|---|---|---|
| before | 33 of 80 (41.2%) | 58.8% | 25 |
| after | **42 of 80 (52.5%)** | **47.5%** | 16 |

`uv run pytest -q` → **473 passed**, ruff clean. Two tests pin it, including one asserting that an invented
bullet among real ones is still refused.

**Still the author's, and now on better evidence:**
1. **Sign off the gate, or not.** 80/80 processed and logged, reconciliation holds, renamed input gives
   identical decisions, clean-clone rehearsal and credential scan pass. The system answers 52.5% of
   validation tickets with citations, escalates 47.5%, and costs about $0.03 a run.
2. **The 16 remaining grounding refusals are unread.** They are now a much smaller pile and worth one
   sitting with `evaluation/reports/grounding_review.md`.
3. **D-22's overlap floor is doing nothing**: every claim sentence in every refused draft cleared it. Keep it
   as a cheap floor against invention, or drop it and let PR-03 be the whole check — a decision, not a defect.

## 2026-09-28 · Rows 16 and 17 · the API and the dashboard · FR-04, FR-05, NFR-05

**Files changed**
- `docs/specs/FR-04.md` (new). `src/ticketing_agent/api.py`: `build_app`, `/health`, `/search`,
  `POST /tickets`, `/queue`, `/metrics`, `/metrics/prometheus`. Was a one-line placeholder.
- `ops/grafana_dashboard.json` (8 panels), `ops/metrics_reference.txt`,
  `scripts/write_metrics_reference.py`. `tests/test_fr04_api.py` (new, 22 tests).

**Result**: `uv run pytest -q` → **495 passed** (473 before). `uv run ruff check .` → clean.

**Verified against a running server**, not only the test client: 90 chunks indexed, and FR-04's own
criterion — `"my deployment keeps dying"` → `DOC-DEPLOY-001` — satisfied with the **real** embedder, top
hit at 0.495. `/queue`, `/metrics` and `/metrics/prometheus` all answer.

**Three decisions worth stating**
1. **One way in.** `POST /tickets` builds the same `SupportPipeline` the harness runs and writes its row
   through the same `DecisionLog.perform`. There is no lighter path for a ticket that arrives over HTTP —
   the API would otherwise be the way to get an answer without the guardrails.
2. **The scrape reads the decision log, not in-process counters.** A counter resets when the process does,
   and NFR-05 asks for 100% of decisions to be auditable. `/metrics/prometheus` is a view of the log, so a
   restart loses nothing and the dashboard and the harness report cannot disagree.
3. **The dashboard cannot drift from the exporter.** `ops/metrics_reference.txt` is generated *from the
   endpoint* by `scripts/write_metrics_reference.py`, and a test fails if any panel queries a metric that
   file does not list. A dashboard of empty panels is the usual failure here.

**Two open items recorded in the spec rather than silently accepted**
- **The API is unauthenticated**, and `/queue` exposes customer text. Fine on a laptop, not fine anywhere
  else: FR-04 §7 says so, and the governance declaration should carry it as an accepted risk.
- **Nothing logs what agents search for**, so the "no-hit report" the PRD imagines — a cluster of questions
  with no article — cannot be produced yet. It is the data that would tell CloudServe what to write next.

## 2026-09-28 · Row 15 · THE GATE — signed off on the author's delegation · FR-14

The author delegated the checkpoints (D-57). The gate passes on the machinery and is explicitly **not** a
sign-off on answer quality: 80 of 80 processed, 80 logged, reconciliation holds, a renamed input gives
identical decisions with zero model calls, the clean-clone rehearsal and credential scan pass, ~$0.03 a run.

42 of 80 answered (52.5%), 38 escalated — 16 by grounding, 14 by FR-09's four intents, 7 by citation
problems, 1 because the documentation had no answer.

**The 16 grounding refusals are genuine judge disagreements, not the defect that caused the previous nine**:
PR-03 supports some sentences and not others in 13 of them, none at all in 3, and every claim sentence
clears the overlap floor. They are laid out in `evaluation/reports/grounding_review.md` and remain unread by
a human, which is the one thing standing between this and a claim about answer quality.

Row 15 → `DONE`.

## 2026-09-29 · Row 18 · CHECKPOINT: build complete · closed on the author's delegation

Every backlog row is `DONE`. The author delegated the remaining checkpoints (D-57), so this closes the
backlog and states plainly what exists, what does not, and what the PRD revision has to record.

### What exists

16 requirements, **15 written specs** (FR-05 is specified inside `docs/specs/FR-08.md`, which is where the
urgency classifier and the queue ordering live), **496 tests**, **57 recorded decisions**, 30 commits.

| | |
|---|---|
| Ingest, four channels, never raises | FR-07 |
| Retrieval over the documentation, section-aware chunks, relevance floor 0.25 | FR-10, FR-04 |
| Intent and urgency, calibrated, alternatives recorded | FR-08, FR-05 |
| Routing: D-16 precedence, four always-escalate intents, money/date rules, kill switch, floor 0.85 | FR-02, FR-09, FR-03, FR-16 |
| Drafting with citations, the disclosure and a greeting | FR-11, FR-06 |
| Five guardrails on every draft, blocking never redacting | FR-12, NFR-04 |
| A handover on every escalation, template when a model must not be used | FR-01 |
| Decision log, reconciliation, the Governance Framework's minimum record | FR-13, NFR-05 |
| Provider client: retries, backoff, breaker, cache, structured output | FR-15 |
| The graph, and the unattended harness behind `--input/--output` | FR-14 |
| API for agents, Prometheus scrape, Grafana dashboard | FR-04, FR-05, NFR-05 |

**The gate**: 80 of 80 tickets, 42 answered (52.5%), 38 escalated, reconciliation holds, ~$0.03 a run, and a
renamed input file gives identical decisions with zero model calls.

### What does not exist, stated rather than implied

1. **No human has read a sent reply.** NFR-03 asks for "human review of ≥50 responses by two assessors with
   an agreement rate". Nothing in this build substitutes for it, and the 16 grounding refusals in
   `evaluation/reports/grounding_review.md` are the first thing that review should look at.
2. **Rows 16 and 17 were not independently reviewed.** The reviewer session hit a rate limit mid-run. Every
   other row got a fresh-session review, and those reviews found one severe and thirteen high findings
   between them, so this is a real gap rather than a formality. Reviewing my own work immediately afterwards
   did find one defect — the API attached the decision log *after* processing a ticket, so a guardrail block
   arriving over HTTP was silently never recorded — which is the argument for finishing the review properly.
3. **The API is unauthenticated** and `/queue` exposes customer text (FR-04 §7).
4. **Three intents miss NFR-03's 85% per-class precision**, and the stated confidence is 42.6 points out of
   calibration in one 25-prediction band (D-40). Both are reported, neither is fixed.
5. **Urgency is effectively two levels, not three** (D-41), and nothing alerts on the kill switch being on
   beyond the dashboard panel.
6. **The author-only documents** in `docs/pack_alignment.md` §4 are still unwritten: the risk register with
   named owners, the six-step incident response, the governance declaration, the kill-switch authorisation,
   and the log retention policy.

### What the PRD revision (Stage 5) must record

- **NFR-07 changed** from "no spend" to a budget (D-55), with the three free-tier measurements as evidence.
- **The confidence threshold is 0.85** (D-44) and it breaches NFR-06's 5-point fairness limit: a 10.1-point
  gap between fluent and non-fluent answer rates. That was accepted knowingly, as the price of not
  auto-answering a misclassified security incident.
- **FR-09's rule fires on the predicted intent**, so on unseen wording roughly 2% of must-escalate tickets
  would be answered at a lower threshold (D-43). The threshold is what stands behind the PRD's "zero
  auto-responses", not the rule alone.
- **The 0.3 overlap floor is currently rejecting nothing** (D-56); PR-03 is the whole of the grounding check
  in practice.
- **Three of the last four defects that cost answers were string comparisons**, not models or prompts
  (D-53, D-54, D-56). The lesson belongs in the revision: when a quality number looks wrong, check whether
  something is being compared by substring before changing a threshold.

Row 18 → `DONE`. The backlog is closed.

## 2026-09-29 · A compose stack, and the windows into a running system

Not a backlog row — the backlog is closed — but the dashboard built at row 17 had nothing serving it, and
the decision log was only readable with `sqlite3` on the command line.

`docker compose up --build` now brings up four things: the API (8000), the decision log in a browser
(8080), Prometheus (9090) and Grafana with the repository's dashboard already provisioned (3000). Two
one-off jobs — `train` and `gate` — sit behind a `tools` profile so `up` never starts a training run or a
full evaluation by surprise.

Four decisions worth stating:

- **The viewer is read-only** (`sqlite_web -r`). This is the audit record NFR-05 is about; a viewer that can
  edit it is not an audit record.
- **The key is read at run time, never built in.** `env_file: [.env]`, and `.dockerignore` excludes `.env`
  from the build context. A test asserts no service carries `LLM_API_KEY` in its environment.
- **The kill switch lives in the shared volume**, so `docker compose exec api touch
  /app/storage/KILL_SWITCH` works from the host. A switch an operator cannot reach without entering the
  container is not an emergency control (FR-16).
- **`tests/test_ops_stack.py` (8 tests) checks the stack against the application**: that Prometheus scrapes
  a path the API actually serves, that the viewer points at the same volume the API writes to, that every
  mounted host file exists, and that the image copies everything the package metadata needs. A drifted
  stack fails quietly — empty panels, a target permanently down — and nobody notices until a demonstration.

**Honest limits.** `docker compose config` validates the file and the tests check it against the code, but
**the image has never been built and the stack has never been started**: the Docker daemon is not running
in this environment. Two build-breaking problems were found and fixed by inspection rather than by running
it — `pyproject` declares `readme = "README.md"` and the Dockerfile did not copy it, which would have
failed minutes into the build at the *second* `uv sync`; and the profile guard stops `up` from launching a
training job. Anything else will surface on the first real `docker compose up`, and given this session's
record — every component that ran for real revealed something the tests did not — that first run should be
treated as a test, not a formality.

`uv run pytest -q` → **504 passed**. `uv run ruff check .` → clean.


---

## Documentation pass — `Implementation.md`, and the FR-04 fix that was not fixed

Two things happened in this session, and the second is the one worth reading.

**The lifecycle diagram.** The third archify diagram — ten states across a `main` / `withheld` /
`terminal` lane split, thirteen transitions — failed validation four times on layout constraints: a
sublabel needing 135px in a 132px state, and three transition labels overlapping the states on either
side. The fix was not to widen anything. Adjacent states in a lane leave almost no room for a label, and
the plain main-path transitions (`received → understood`, `understood → routed`) carry no information the
step order does not already carry. Dropping those two labels and shortening the rest passed all four
gates. `docs/diagrams/lifecycle-ticket-outcomes.html`.

**The FR-04 log fix was still broken after being fixed.** See D-58. The short version: the reordering of
`state.attach(log)` was correct, the test was green, and the log was still being attached to `None` on
every request, because `attach` read `self._pipeline` (the attribute, which is `None` until the graph is
built) instead of `self.pipeline` (the property that builds it). Caught by running five tickets through a
live server and counting rows: five rows for five tickets, with the `generation` and `block` rows missing
entirely. The regression test now forces the lazy path with a monkeypatch; mutating the fix back fails it.

That is the third session running in which the defect that mattered was found by *running the thing*, not
by the suite — and the second in which a fix I had already written and tested turned out not to be a fix.

**`docs/Implementation.md`** (≈1730 lines) is the technical account the repository did not have: runtime
architecture, the data model, all seven graph nodes, the RAG subsystem end to end, the five guardrails
including both quote-matcher defects, the provider client, the decision log, then seventeen use cases —
every API endpoint and every failure mode — each with a mermaid diagram and a verbatim request/response
captured from a running server. Then the mapping from each of the twenty-two measured problems of the
human-only process to the mechanism that addresses it, the measured results with every shortfall stated
(FCR 52.5% against a ≥60% target; five NFR-06 fairness breaches), and a glossary of every abbreviation.

`docs/Implementation.html` is generated from it by `scripts/render_implementation_html.py`. Checked before
committing: 11 of 11 mermaid diagrams parse under the pinned mermaid 10.9.1, 34 of 34 in-page anchors
resolve, 3 of 3 diagram links exist. Not checked: how the page looks — the Chrome extension was not
connected, so the rendering is unverified in exactly the way the compose stack's first `up` is.

`uv run pytest -q` → **505 passed**. `uv run ruff check .` → clean.

---

## Review row R1 · The test status, established (A12, NFR-09)

The review row was written from `.pytest_cache/v/cache/lastfailed`, dated 28 Sep 20:55 — **before** the
last three commits. It listed five failures. There are none: `uv run pytest -q` → **506 passed**
(505 before this row's own test), `uv run pytest --collect-only -q` → 506 collected, `uv run ruff check .`
clean. The five named tests pass individually. The cache was stale, not the suite.

That is worth saying plainly because the row's premise was wrong and the correct response to a wrong
premise is to say so, not to go looking for a failure to justify it.

**The real defect in the row was the number in the README**: step 5 said "469 tests" while the suite held
505, and `docs/PROGRESS.md` had said 504. A count in a setup document is wrong from the next commit
onwards, and an assessor who runs the suite and counts something different has been handed a reason to
distrust the rest of the file. Step 5 now says the suite runs with no network and no API key, and says
why no number is quoted. The same edit removed "it is worth the seven tests" from the ops section, which
had already drifted from eight.

**Tests added.** `tests/test_docs_consistency.py` — a new file for the checks that stop README,
`.env.example` and `docs/decisions.md` contradicting each other between sessions (R9 will extend it).
`test_T_R1_1_the_readme_does_not_promise_a_test_count` fails on any `\d{2,}\s+(\w+\s+)?tests?` in the
README; it failed on `469 tests` before the fix.

**Review.** Done inline rather than with the `reviewer` subagent: the change is two sentences of README
prose and one regex, with no requirement or spec for a reviewer to check it against. Recorded here so the
departure from `/next-feature` step 6 is visible rather than silent.

`uv run pytest -q` → **506 passed**. `uv run ruff check .` → clean.

---

## Review row R2 · The classifier's columns reach the row (FR-13, FR-05, FR-08, A8)

**The row's evidence was exactly right, and the consequence is worse than it reads.** `intent`,
`intent_confidence`, `intent_alternatives`, `urgency` and `urgency_confidence` were empty on every row of
every database this project has written: 162 of 162 in `storage/gate-openai-2.db`, 5 of 5 in
`storage/decisions.db`. `Outcome.to_entry()` never passed them.

`/queue` sorts on the log's `urgency`, so an always-null column meant `order_escalation_queue` fell back to
`medium` for everything and **FR-05's urgency-first queue was oldest-first** on anything that came through
the real pipeline. `test_T_FR05_3` passed the whole time because it writes `urgency` into the log by hand —
a test that constructs the state it is testing cannot fail when the code stops producing it. That is the
second time in three sessions a green test was covering a real defect, and both times for the same reason.

**Files changed.** `src/ticketing_agent/classify.py` (new `row_fields()`; `log_fields()` builds on it and
gained the `intent_confidence` it never set), `pipeline.py` (`Outcome` gained six fields; `to_entry`,
`_outcome`, `_failed_outcome` and the intermediate rows carry them, via `_classification_fields` and
`_prediction_fields`), `logging_store.py` (schema 1 → 2: `urgency_reason` column plus an additive,
idempotent `_migrate`), `api.py` (`TicketOut` gained `urgency`/`urgency_confidence`; `QueueItem` gained
`detail`). D-60, D-61, D-62.

**Tests added (12).** `test_T_R2_1`, `_1b`, `_3`, `_4`, `_5`, `_6`, `_7`, `_8`, `_9` in
`tests/test_pipeline.py`; `test_T_R2_2` in `tests/test_fr04_api.py`; `test_T_R2_10`, `_11` in
`tests/test_fr13_decision_log.py`. Spec acceptance lists extended: FR-13 §6 items 20–27, FR-08 §6 items
17–18, so these trace to a requirement rather than to a backlog row.

**The independent review found one high and three mediums. All four are fixed, not recorded.**

1. *high* — FR-05 asks for the urgency, its confidence **and the reason**, and the reason reached no row:
   it travelled only in `log_fields()`'s `detail`, which `row_fields` drops, and the graph writes no
   `classification` row. `/queue` filled `urgency_reason` from the row's `detail` — the *escalation*
   detail. The reviewer's point is the one that matters: before this row that was merely unhelpful, because
   the urgency beside it was null and the row was visibly unfilled; with a real urgency next to it an agent
   reads `urgency: high · urgency_reason: "must_escalate_intent: intent security_incident"` and gets a
   confident wrong answer to "why is this urgent?". **A fix that makes a field look authoritative has to
   make it correct at the same time.** New column, new schema version, migration; `/queue` serves the
   escalation detail under `detail`. T-R2-5.
2. *medium* — `by_stage = {r["stage"]: r for r in rows}` keeps the **last** row per stage, and the FR-12
   block row and the terminal row both carry `stage="validation"`. My own T-R2-4 was therefore testing
   `to_entry()` twice and the block row not at all. Keyed on `decision` now. The reviewer verified this
   with a runtime mutation rather than by reading, which is why it was caught.
3. *medium* — `prediction_confidence` and `intent_confidence` can disagree. Kept, because they answer
   different questions, and documented in D-61 and pinned by T-R2-8 — which is what was actually missing.
4. *medium* — nothing asserted the property over a whole run. T-R2-6 runs the engineered corpus and
   asserts every terminal row carries an urgency, naming the three paths allowed not to.

Three lows also fixed: `_classification_fields` duck-types `row_fields` (a classifier without it turned a
**successfully drafted** ticket into a `pipeline_error` escalation); the merge direction is pinned by
T-R2-7; the intermediate rows now carry `prediction_value`/`prediction_confidence`, because
`governance_record` emitted a populated `alternatives` list beside a null prediction — "alternatives to
nothing" in the projection an assessor reads.

**Recorded, not fixed.** The reviewer noted that `api.py` mutates the shared pipeline's `_log` per request
while FastAPI runs sync endpoints on a threadpool, so two concurrent POSTs can route one ticket's
intermediate rows through another's `DecisionLog` handle. Same path, so the content is right, but a closed
handle raises `DecisionLogUnavailable`, which is deliberately uncaught. Pre-existing and not worsened here;
it belongs to row R10, which reviews the API.

`docs/Implementation.md` §UC-11 now carries a note saying its captured `/queue` response predates this row
and that its `urgency: null` is the defect, not honest reporting — the document said the opposite.

`uv run pytest -q` → **516 passed**. `uv run ruff check .` → clean.

---

## Review row R3 · The text that was sent, in the run output and in the log (FR-14, FR-13, NFR-03)

`outcomes.jsonl` carried the decision, the citations, the latency and the segments — and **not one word of
what went to the customer**. That is not a gap in reporting. NFR-03's hallucination rate and citation
accuracy are measured, by the Evaluation Framework's own definition, by human review of at least 50
responses by two assessors with an agreement rate. Without the text those numbers could not be produced at
all: not "were not produced yet", *could not be*. A demonstration could not show what the system sends,
and a complaint could not be reconstructed.

**Files changed.** `evaluation/harness.py` (new `_outcome_line` and `_handover_fields`),
`src/ticketing_agent/logging_store.py` (schema 2 → 3: `reply_text`; `redact` made public),
`src/ticketing_agent/pipeline.py` (`Outcome` gained `customer_goal`, `already_tried`,
`suggested_first_check`; `to_entry` sets `reply_text`; `_terminal_reason` is fail-closed on a missing
guardrail report), `scripts/review_sample.py` (new). D-63, D-64, D-65.

**Tests added (10).** `test_T_R3_1`, `_2`, `_3`, `_3b`, `_3c`, `_4`, `_4b`, `_5` in
`tests/test_fr14_harness.py`; `_6` in `tests/test_fr13_decision_log.py`; `_7` in
`tests/test_pipeline.py`. Plus `T-R2-12` (the migration race). Spec lists extended: FR-14 §6 items 28–35,
FR-13 §6 items 28–30 and the §2 column table, which had documented neither `reply_text` nor R2's
`urgency_reason`.

### The review found two highs. Both were real, and one was mine.

**1. The run output was writing raw customer text to disk, and the test that forbids exactly that could
not see it.** On the template path `customer_goal` is literally the first sentence of the ticket body, and
`already_tried` is extracted from it by PR-02. `_safe` substitutes only when `secrets_in` fires — national
ids, card numbers, credentials — so an email address went straight through. The reviewer reproduced it:
`"customer_goal": "Please write back to dana.okonkwo@acme-health.example or call 07700900123."`

`test_T_FR14_18_no_customer_text_reaches_the_report_or_the_log` asserted on `outcomes.jsonl` and passed
throughout, because it runs a `FakePipeline` that produces no handover and no reply at all. **A test can
forbid something for a year without ever having been in a position to detect it.** That is the same shape
as R2's queue test, two rows running.

The handover is customer-derived *by requirement* (FR-01 asks what the customer is trying to achieve), so
the fix is not to strip it. The run output now applies the decision log's own scrubbed-column redaction to
all four handover fields and names every redaction on the line in `handover_redactions`, rather than
altering text silently. `logging_store.redact` is public for this: two artefacts holding customer-derived
text under two different policies is how one of them becomes the leak. T-FR14-18 now asserts FR-14 §18's
actual property — `metrics.json`, `metrics.md` and the log's `detail`, which it had never checked — and
T-R3-5 covers the run output with the real pipeline and a ticket that really contains an email address.

This is *stricter* than the log's own `summary` column, which is unscrubbed by the author's standing
decision. **Two items are now written into R14 rather than being quietly settled by me**: whether the log's
`summary` should be brought up to the same policy, and that the policy does not cover phone numbers —
`guardrails.phones_in` exists, deliberately tolerant after D-51, and is used by neither artefact.

**2. My own T-R3-4 asserted nothing, and could not have failed anyway.** `all(... for r in rows if
r["decision"] == "escalate")` ran over a fixture that produced six answers and zero escalations, so `all()`
over an empty sequence was `True`. And it was unfalsifiable regardless: `Outcome.draft` is already None on
every escalation, so dropping the conditional in `to_entry` would still have written NULL. The single case
D-63 rests on — a draft written, then withheld — was untested. T-R3-4b builds it with a judge that refuses
every draft, and asserts the FR-12 block row is written and `reply_text` is NULL on *every* row.

### Four mediums, all fixed

- **The migration had a race that stopped the run.** `_migrate` read `PRAGMA table_info` then issued
  `ALTER TABLE` with no tolerance for "already added". `api.py` opens a log per request, so a harness run
  starting while one dashboard poll was in flight would hit it — and the constructor turns any sqlite error
  into `DecisionLogUnavailable`, which under D-27 aborts the whole run. Losing a race to add a column
  someone else has already added is not a reason to stop. T-R2-12; T-R3-6 checks a migration that really
  failed still does stop it.
- **`_terminal_reason` read a missing guardrail report as a pass** while `_after_check` has always read it
  as blocked. Unreachable today, fixed anyway: since this row, `answered` is the *only* gate on persisting
  the outbound text to two artefacts, so the cost of it becoming reachable changed. D-65, T-R3-7.
- **FR-13's spec documented neither new column.** The log's schema changed twice in two rows with its own
  spec unchanged. §2's table and §7's privacy discussion now carry both.
- **D-64's fix was one file short.** `tests/test_fr04_api.py` also built a `ProviderClient` without its own
  cache path, so the suite was reading from and **writing into** `storage/llm_cache.sqlite` — the cache
  gate runs replay from. Six rows with `model: test-model`, both timestamps from today's runs. Fixed in
  that helper too; the six rows removed after backing the file up to
  `storage/llm_cache.sqlite.bak-before-test-row-cleanup`. The cache key includes the model name, so they
  could never have been served to a real run — clutter, not a correctness risk.

Five lows fixed: a one-line JSON array now exits 2 naming the mistake instead of raising `AttributeError`
(the likely slip is pointing `--input` at a `metrics.json`); the sort key is `(ticket_id, source_index)`,
because duplicate ticket ids are legal (D-12) and a stable sort left those tied on file order — the exact
dependency the sort exists to remove; sampling draws shuffled *indices* rather than using `Random.sample`,
whose consumption of the Mersenne Twister stream is not a documented cross-version guarantee; T-R3-3 now
samples 5 of 12 rather than 3 of 6, where an unseeded sampler passed about one run in twenty; and
`suggested_first_check` is asserted.

Recorded, not fixed: the log's `reply_text` is capped at 4000 characters and `outcomes.jsonl` is not, so an
unusually long reply differs between them. Noted in D-63 — the run output is the artefact for review, the
log is the artefact for audit, and the log says when it cut something.

`uv run pytest -q` → **529 passed**. `uv run ruff check .` → clean.

---

## Review row R4 · The report describes the run, not the build (FR-14, A10)

The last `metrics.md` said, next to a per-class table at 100% and 42 sent replies: *"Intent precision and
recall: no classifier yet (row 8)"*, *"Private data in outbound replies: nothing is sent yet (row 11)"*,
*"while the answering path is unbuilt every ticket escalates"*. Every caveat in the report was a constant
naming a build row, and all of them had been built weeks earlier. A report that describes a different
system than the one that ran is worse than one with gaps in it: an assessor cannot tell which half to
believe, and A10 is checked against this file.

**Files changed.** `evaluation/harness.py` and `tests/test_fr14_harness.py` only — the report is the sole
thing that moved. D-66, D-67; FR-14 §4 and §6 item 25 amended, items 36–46 added; D-37's wording amended
where it mandated the phrase this row removes.

**Tests added (13).** `test_T_R4_1` … `_13`. 542 passing.

### What the review found, and what I had got wrong

The reviewer returned **five highs**, no severe. All five are fixed. Three of them were the same mistake
in different places: R4 replaced a false claim with another false claim.

1. **`_calibration` manufactured an NFR-03 pass out of a classifier outage.** It calibrated
   `prediction_confidence`, which FR-02 §3.2 floors to 0.0 when there is no usable classification — and
   `_outcome` sets it even when `state.classification is None`. Since `0.0 is not None`, a run where the
   classifier raised on every ticket produced six rows at stated 0.0% / observed 0.0%, gap 0.0, *"within
   NFR-03's 5-point limit"* — printed two lines under the section that correctly said no intent could be
   scored. D-61 already said which number to calibrate; I used the other one. Now `intent_confidence`,
   and a prediction is required. T-R4-7.
2. **One report, two verdicts on the same tickets.** Calibration keyed correctness off `Outcome.intent`
   while the per-class table used `prediction_value`. On the classifier path `pipeline.py` deliberately
   tolerates (one without `row_fields`), that gave 100% per-class precision beside a **95-point**
   calibration gap. Both now score `prediction_value`; where the stated confidence never reaches the
   outcome, calibration declares itself not computable rather than scoring something else. T-R4-8.
3. **I changed behaviour a requirement defines without changing the requirement.** FR-14 §4 mandated
   "by construction" *in those words*, §6 item 25 repeated it, and D-37 recorded it. I removed the phrase
   from the output and left all three untouched — a direct CLAUDE.md violation ("Never change behaviour a
   requirement defines without saying so first"), and the one that would have made this row unreviewable.
   Spec and decision now carry the amendment with its reason.
4. **T-R4-3, R4's own acceptance test, could not fail on either headline assertion.** Its fixture has one
   intent, so min, max and macro mean are the same number: mutating `_per_class_floor` to `max` or to a
   mean left it green, while its docstring claimed to be testing exactly that. The private-data assertion
   compared `"0" == "0"`. `two_intent_pipeline` / `two_intent_file` build two classes at different
   precisions, and T-R4-13 drives a real `private_data` path. T-R4-6.
5. **The precision cell asserted a clean NFR-03 pass and overstated its denominator.** It claimed "over 80
   labelled tickets" while computing over tickets that produced a prediction, and carried no caveat at all
   — although this project's own evidence (R8: most validation bodies duplicate a training body; three
   intents below 85% out of fold) says the 100% is leakage. Denominator corrected; the cell and the gaps
   list now carry the in-sample caution.

**Seven mediums, all fixed.** *"every response was served from the cache"* was printed on runs with zero
cache hits (now distinguishes a replay from never reaching the provider). *"A result rather than a
construction"* was asserted with no knowledge of the kill switch, a provider outage or a rule-only file —
the same over-reading this row exists to stop, pointed the other way; it now claims a construction only
when one is identifiable, and `test_T_FR14_25` had been pinning that false claim on a `FakePipeline` that
escalates by construction. `isinstance(pipeline, StubPipeline)` made `"full"` a claim about any injected
object, so every harness test's output asserted it had run the real graph; the field is positive now and
carries the class name. The worst-gap verdict counted bands the same report flags "(low confidence)",
contradicting `classify.py`'s own rule. The gaps list stopped naming classification when labels existed
but predictions did not. "Private data in outbound text" read a guardrail *detection* count — text that
was **blocked**, not sent — so FR-12 doing its job read as a target miss. And NFR-06's one row in the
summary table was a cross-reference while every dimension of the real run was above the limit; it now
carries the worst dimension and names it (`28.5 points (region)`).

Six lows fixed too, including a garbled sentence that reached the report ("is the stub's not a tuning
result"), a missing header line (a stub run announced itself only in two table cells and a bullet a
hundred lines down), and `_technical`/`_governance` being computed twice — identical today, and a future
divergence is precisely the defect this row fixes.

### The judgement call, stated plainly

**I modified an existing test.** `test_T_FR14_25` asserted the literal strings `"by construction"` and
`"nothing is sent yet"` — the exact output this row was commissioned to remove. I re-pointed it and added
assertions it did not have. The reviewer was asked to rule on this directly and found it **stronger, not
weaker**: both removed properties are re-asserted, the escalation declaration in two places rather than
one, the private-data zero as a value plus its meaning rather than a markdown substring, plus a new sweep
for build-row references anywhere in the output. It also found the string the test now pins is false for
its own fixture — fixed under medium 2 above.

Verified by running it, not only by the suite: a stub run, a rule-only run and a full cached gate run
(80 tickets, 42 answered) each produce **zero** occurrences of "row 8", "row 11", "row 14", "no classifier
yet", "nothing is sent yet", "unbuilt", "not computable yet" or "by construction".

`uv run pytest -q` → **542 passed**. `uv run ruff check .` → clean.

---

## Review row R5 · A replay is not a measurement, and NFR-01 is missed (FR-14, NFR-01)

`gate-openai-2`'s report printed **p95 95.7 ms** against NFR-01's `< 3 s`, off **0 model calls and 162
cache hits**. The live run of the same 80 tickets had measured p95 **6.7 s**. The report was showing a
comfortable pass over a miss of more than a factor of two, and nothing in it said the figure was a replay.

**Files changed.** `src/ticketing_agent/provider.py`, `evaluation/harness.py`,
`tests/test_fr14_harness.py`, `docs/specs/FR-14.md` §2 and items 47–55, D-68, and a new
`storage/CACHE_README.md`.

**Tests added (8).** `test_T_R5_1`, `_1b`, `_1c`, `_2`, `_3`, `_4`, `_5`, `_6`. **550 passing.**

### The measurement this row exists to produce

Taken with `--no-cache` on 2026-10-01 — 80 validation tickets, 319 s wall, 155 provider requests, no
replayed response:

| figure | value | target |
|---|---|---|
| p95, all tickets | 6,532 ms | — |
| **p95, automated path (45 answered)** | **5,754 ms** | NFR-01: **< 3 s** |
| median, all tickets | 4,068 ms | — |

**NFR-01 is missed by 1.9× on the figure the requirement actually names.** A 25-ticket live run measured
7,744 ms. It is now an entry in the report's gaps list with its cause: two provider round trips per
answered ticket (PR-01 to draft, PR-03 to judge) against a hosted model. NFR-01 itself asks for the
measured figure and the cause when it cannot be met, and the PRD revision can now carry it.

### What the review found — four highs, and one of them had already fired

1. **`--no-cache` wrote to the cache, and the first timing run replaced 150 of 365 rows.** `put` is
   `INSERT OR REPLACE`, and the cache key carries nothing that distinguishes a September recording from
   today's. The consequence is not academic: replaying the same 80 tickets answers **42** against the
   September recordings and **43–45** against the new ones, because the model's text decides
   `no_cited_article`, `invalid_citation` and `ungrounded_draft`. NFR-08 held only as far back as the last
   timing run, and no artefact recorded that the cache had moved. My code comment had called the
   write-through a feature — "warms the cache for the next ordinary run" — which is how a hazard gets
   written down as a convenience. `--no-cache` now writes nothing; verified by checking the cache row count
   and newest timestamp either side of a live 25-ticket run: **identical**.
   The September recordings are preserved at `storage/llm_cache.2026-09-28-gate.sqlite` with
   `storage/CACHE_README.md` explaining both files and how to replay either. **Which run the gate is
   signed off on is R13's decision, not mine**, and it now has both runs to choose between.
2. **The p95 cell said "(replay)" on runs where nothing was replayed.** The suffix was driven by
   `representative`, so a rule-only run and a kill-switch run — zero model calls, zero cache hits — read
   `0.0 ms (replay)`. That contradicts FR-14 §6 item 44, which R4 had just added, in the one cell an
   assessor reads; item 44's own test asserted only on the confidence column. There are five latency
   states now (`measured`, `partly_replayed`, `replay`, `provider_not_reached`, `stub`) and the suffix
   comes from the state.
3. **The latency was not the automated path's latency**, while a field named `representative` asserted it
   was. NFR-01 names the automated path; the harness averaged answered and escalated tickets together.
   I had guessed this would flatter the figure and said so — **I was wrong about the direction**:
   escalations are the *slower* group here (p95 6,779 ms against 5,754 ms) because they still make a PR-02
   handover call, and most make PR-01 and PR-03 first before being blocked. But the reviewer's point stands
   regardless: a synthetic run with one slow answered ticket among nineteen fast rule escalations reports a
   p95 that excludes the answered ticket outright. Both figures are reported and `nfr01_figure` names the
   right one.
4. **The strict rule and the partly-replayed branch had no test.** Mutating `representative` to drop its
   `not hits` clause left the whole suite green, and partly-replayed is the *normal* state once anything is
   cached. T-R5-6 asserts all five states directly.

### Four mediums, all fixed

One boolean was carrying two claims, so a 1%-replayed run was labelled a replay in the headline while its
own confidence cell said "partly replayed: 1 of 101" — the mirror image of the defect this row exists to
fix. `technical.latency_ms` reached `metrics.json` with no caveat two keys from the block saying the
figures were a replay. `run` did not record that reads were off, so the one configuration that cannot
reproduce itself left no trace in its own artefact and nothing stopped it being handed in as the A5
evidence. And R5's own third "Do" item — record the NFR-01 miss — was not done: the miss existed only in
the backlog file, and a live run's gaps list never mentioned NFR-01 at all.

Four lows fixed: `_latency` was computed twice, two lines below the comment forbidding exactly that;
`and not stub` was a dead clause that read as a guard; a `--limit` run described n=1 as a measurement
(the note now carries a sample-size clause); and the spec's §2 argument list was a flag short of the code.

`uv run pytest -q` → **550 passed**. `uv run ruff check .` → clean.

---

## Review row R6 · The answers the labels disagree with, named (FR-14, FR-02, NFR-03)

In `gate-openai-2`, 14 of 42 auto-answers went to tickets labelled `expected_route: escalate`, and 13 of
those were labelled `answerable_from_docs: false` — the field the Dataset Guide says exists to measure
"whether your system correctly recognises questions it cannot ground". The harness reported nothing on any
of it.

**Files changed.** `evaluation/harness.py`, `tests/test_fr14_harness.py`, `docs/specs/FR-14.md` §4 and
items 56–58. D-69, D-70.

**Tests added (5).** `test_T_R6_1` … `_5`. **574 passing.**

Measured on the validation set before R7's rules landed: **15 of 43** answered tickets labelled escalate,
the same 15 labelled not-answerable, 1 (VAL-0080) citing no expected article — answered from
`DOC-DEPLOY-003` where the label expects `DOC-DEPLOY-001`, exactly as the row described. The matrix also
showed **20** tickets the labels expected answered and the system escalated: the larger disagreement, and
one the row did not ask about.

### The review found two highs

1. **Answered tickets were matched by ticket id string.** Ids are not unique — `ingest` keeps a duplicate
   and flags it with `duplicate_ticket_id` (D-12) — so on a file with repeated ids an *escalated* ticket
   would be named in the report as an answer that was never sent, and every denominator inflated with it.
   A disagreement row that names a ticket wrongly is worse than no row, since the whole point is that the
   ids let a human settle it. Matched by `outcome.decision` now.
2. **Numerator and denominator came from different populations.** `answered_citing_no_expected_article`
   counts only answered tickets that *have* an `expected_doc_ids`, but divided by all answered — printing
   2.3% where the figure is 4.2%, in a cell that sits beside NFR-03's citation-accuracy row. Every row now
   carries its own denominator and says how many tickets could be scored for it, which is what the harness
   already did for retrieval hit rate.

### Six mediums, all fixed

My own `test_T_R6_3` answered nothing and then asserted the disagreement count was zero — an assertion
over an empty list, the third time this pattern has appeared in this backlog. It answers every ticket now
and compares against the expected set computed from the file's own labels. `_against_note` **asserted**
two things it never computed ("a fact about the labels", "it is the larger number here"); both are
computed now, and the second was simply false whenever the other direction was smaller. The markdown
rendering of the matrix had no test at all: transposing two cells left `metrics.md` contradicting
`metrics.json` with the suite green. `outside_the_matrix` — the drop channel — had no test, and neither
did the "labels present but this field absent" case. And `must_not_auto_respond`, the one label that marks
a requirement breach rather than a difference of opinion, was mentioned only in prose; it now has its own
row and the note's first sentence.

**A trap worth naming, because it caught me twice today.** My first fix for the untested matrix *looked*
right and still passed under mutation — the fixture gave two of the four cells the same value, so
transposing them changed nothing. The fixture now makes all four distinct (2, 1, 3, 4). This is the same
shape as R4's single-class precision fixture and R3's `all()` over an empty sequence.

**And an error of my own.** While mutation-testing I ran `git checkout evaluation/harness.py` to revert
the mutation — on a file holding all of this row's uncommitted work, which it duly destroyed. Recovered by
re-applying the patch scripts from the scratchpad; verified by the full suite. Mutation checks since then
copy the file aside and restore from the copy.

`uv run pytest -q` → **574 passed**. `uv run ruff check .` → clean.

---

## Review row R7 · A disputed charge, and a compliance-grade data question (FR-03, FR-09)

Two tickets were auto-answered that the requirements say must reach a person: VAL-0072 ("charges on our
invoice for a service I do not believe we use" — a dispute, which FR-03 escalates, containing none of the
trigger words) and VAL-0037 ("backups replicated outside our primary region? A compliance review has
raised this" — which the PRD's open question on data residency escalates).

**Files changed.** `src/ticketing_agent/route.py`, `src/ticketing_agent/handover.py`,
`evaluation/harness.py`, `scripts/dispute_rule_sweep.py` (new), `tests/test_fr02_routing.py`,
`tests/test_fr14_harness.py`, `tests/test_engineered_fixtures.py`, and the FR-02, FR-03, FR-09 and FR-12
specs. D-71, D-72.

**Tests added (80 in the R7 family).** One body per phrase for both tables, plus the scoping, the
precedence, the sentences, the sweep, and the two deliberate carve-outs. **636 passing.**

### The review found two highs, and the first was mine to own

1. **I under-implemented the decision.** The row says "FR-03 also escalates **a billing ticket** that
   disputes or disowns a charge". I matched every ticket, and four of my phrases named no charge at all —
   `did not use`, `didn't use`, `never enabled`, `not ours`. The result: "SSO was never enabled on our org,
   how do I turn it on?" escalated as a money dispute, and the handover note told the tier-two engineer
   that the customer was disputing their bill. Four such false positives were demonstrated through the real
   router. Every phrase names a charge now, which gives the rule its context without scoping it to an
   intent — so a *misclassified* dispute is still caught, which is D-42's reasoning for the money rule.
   `T-R7-1d` pins it.
2. **22 of the 27 dispute phrases could be deleted with the suite green.** Four of my seven test bodies
   carried two triggers each, so the shorter phrase shadowed the other. That is the **fourth** time this
   backlog has hit the same shape — a single-class precision fixture, an `all()` over an empty sequence,
   two matrix cells holding the same value, and now this. There is one body per phrase now (34 + 27), each
   asserted to carry no sibling trigger, plus `T-R7-1c`/`T-R7-2c` holding the bodies against the tables and
   `T-FR12-5` holding both tables against their specs — which the money and date tables already had and
   mine did not.

### Four mediums

`T-R7-2`'s `assert "compliance" in detail` could not fail: `detail` always begins
`compliance_data_question: …`, so emptying the whole table left it passing. It asserts the segments now.
Three words had to come out of the compliance table — `audit`, `gdpr`, `definite answer` — each matching a
product noun or ordinary impatience rather than a process; see D-72, including that two of them were in
the author's own list and why removing them follows from the same decision's carve-out. Real false
negatives were missing (`billed twice`, `charged me twice`, `duplicate invoice`, `still being billed`,
`never signed up`, `don't believe we used`) and are in. And `evaluation/harness.py`'s
`_NOT_A_TUNING_RESULT` was a second, hand-maintained copy of the reason list that R7's two new reasons
never reached — so a rule-only run under-counted and could print the wrong conclusion about itself, in the
file whose own review row was about figures contradicting each other. It is derived from `PRECEDENCE` now,
pinned by `T-R7-6`.

### What I did not do

A reviewer proposed adding `written confirmation` / `in writing` to the compliance table. It is a good
suggestion on the merits — it is the archetype of compliance-grade and would catch 6 tickets the labels
agree should escalate. I measured it: **17 tickets in the supplied data say it and 11 are labelled
answerable**, so it buys 6 agreements for 11 disagreements, and R7's decision did not list it. I left it
out, pinned its absence with `T-R7-2f` so adding it must be a decision rather than a drift, and put the
measurement in D-72 for R13. Inventing a rule that costs label agreement is not mine to do.

### For R13

* The dispute rule escalates 6 validation tickets labelled `auto_respond`. **All six are duplicates of
  VAL-0072 with identical text and the opposite label** (D-70), so this is the data contradicting itself,
  not the rule being wrong.
* The residency rule escalates VAL-0054 and VAL-0076, which ask where *this account's* data is held — what
  the rule is for, and what the labels call answerable. VAL-0054 matches on its **canned subject line**,
  and two identical bodies under different subjects route differently as a result.
* VAL-0037, the ticket this row was commissioned for, has a development twin (DEV-0106) with the same
  question labelled `auto_respond`. The rule cannot satisfy both.

`uv run pytest -q` → **636 passed**. `uv run ruff check .` → clean.

---

## Review row R8 · No figure a gate run produces is evidence about generalisation (FR-08, NFR-03)

The gate reported **100% per-class precision and recall**. 62 of the 80 validation bodies are identical to
a development ticket the classifier was trained on, and the cross-validated development figures put 3 of
22 intents **below** NFR-03's 85%. The report said nothing about which of those it was measuring.

**Files changed.** `evaluation/harness.py`, `src/ticketing_agent/classify.py` (`wording_clusters` made
public), `tests/test_fr14_harness.py`, `docs/specs/FR-14.md` §4 and items 59–67, `docs/specs/FR-08.md`
§2. D-73.

**Tests added (11).** `test_T_R8_1` … `_9`. **647 passing.**

### What running it changed about the conclusion

The split does **not** show the classifier is worse than reported: the 18 unseen-body tickets also score
100%. What it shows is that an exact-body comparison badly overstates "unseen". Under D-39's own 0.85
clustering — the threshold the classifier's cross-validation already groups its folds by — **14 of the 18
are paraphrases of a training body, leaving 4**: VAL-0003, VAL-0004, VAL-0046, VAL-0073.

So the honest finding is not "the classifier is weaker than the gate says" but "**the gate cannot speak to
this at all**", and a 100% figure over four tickets supports nothing in either direction. The report now
says that in those words and points at `classifier_calibration.md`, whose grouped cross-validation over 96
wording clusters is the figure NFR-03 turns on — 88.6% overall, 3 of 22 classes below 85%.

### The review found two highs, and both were about tests of mine that could not fail

1. **The row's actual deliverable — the per-group per-class figures — was untested.** Two single-line
   mutations left all 643 tests green: dropping the group filter, and not computing the per-group table at
   all. My fixture's four tickets were all labelled `billing_query`, so the seen and unseen blocks were
   numerically identical. With the filter gone both rows would print the whole-run in-sample figure under
   a heading saying "unseen" — the exact overstatement this row exists to remove, invisible to CI and to a
   reader. That is the **fifth** appearance of this shape in this backlog. `T-R8-1b` uses a classifier
   that is right on every seen body and wrong on every unseen one, so the blocks read 100% and 0% while
   the whole-run figure reads 50%.
2. **`assert "classifier_calibration.md" in markdown` could not fail**: the string is already emitted
   twice by pre-existing report text, so pointing `CALIBRATION_REPORT` at a nonexistent file left all
   seven R8 tests green. It is asserted inside the R8 section now.

### Five mediums

The markdown read backwards — it stated the novel *count* and then listed the 14 **paraphrase** ids, which
reads as though those were the novel ones, and never named the 4 ids a human actually acts on. The
clustering's "one id per input in input order" contract, which the harness zips against its own list, was
pinned by nothing: a sorted return preserves the length so `zip(strict=True)` cannot catch it, and the
report would have named the wrong tickets with plausible counts. The report declared a headline while the
per-class section above it still printed the whole-run figure with no pointer to the split. The clustering
was uncapped O(n²) difflib (~20 s at 4,000 bodies) inside the block whose failure costs a *completed* run
its report. And the printed `0.85` was a literal beside a call that used the function's default, so
changing the default would have left the report making a false statement about its own measurement.

### And a justification of mine that was not evidence

I had written that the normalised-body key was needed because DEV-0106 and VAL-0037 differ "only in case
and whitespace". They do not — VAL-0037 inserts two articles and drops a sentence — so normalising does
not merge them, and on the real corpus a raw-string comparison gives the same 62/18 split. The
normalisation is kept because it is the right key for a file nobody has seen, and the docstring now says
that instead of citing a pair that does not support it.

`uv run pytest -q` → **647 passed**. `uv run ruff check .` → clean.

---

## Review row R9 · One default provider, and three claims that were not true (NFR-07, NFR-09, A1)

The README said **"Groq is the provider that works"** while `.env.example` shipped OpenAI values and D-55
recorded the decision to pay for OpenAI. An assessor following the README literally would point the system
at the provider this project measured and rejected — Groq's free tier escalated 21 of 80 tickets without
attempting them.

**Files changed.** `README.md`, `.env.example`, `docs/PRD.md`, `docs/decisions.md` (D-54, D-55 amended;
D-74/75/76 added), `src/ticketing_agent/config.py`, `src/ticketing_agent/guardrails.py`,
`evaluation/harness.py`, `tests/test_docs_consistency.py`, `tests/test_fr14_harness.py`.

**Tests added (9).** `test_T_R9_1`, `_1b`, `_1c`, `_2`, `_2b`, `_2c`, `_3`, `_4`, `_5`, `_6`, `_7`, `_8`.
**659 passing.**

### I wrote a false claim into the README while fixing one

My first Pace section said "two model calls for an answered ticket and a third for an escalated one",
which implies 195 calls for the 80-ticket run. The run made **155**. A ticket escalated by rule never
reaches the drafter and costs **one** call — which is precisely the NFR-07 property the section exists to
explain, and I had written over it. Corrected against the live run, and `T-R9-3` now holds every number
the README quotes against a recorded run.

### The review found three highs, and the first is the serious one

1. **`JUDGE_MODEL_NAME` was dead configuration — and R9 had just made the claim machine-readable.**
   `.env.example` has carried it since D-55 with the comment "a **different** model on purpose: FR-12's
   grounding check is not independent" otherwise, and **nothing read it**. `GroundingJudge.check` passed
   no `model=`, so PR-03 ran on `MODEL_NAME`: the drafting model marking its own homework. The cache
   proves it — 374 recorded responses, **not one** `gpt-4.1-mini`. Putting the provider in the report
   turned a stale comment into a published claim of independence the system did not have. Wired it rather
   than deleting the claim; verified by running it, which wrote the first two `gpt-4.1-mini` responses in
   this project's history. D-74, with the two consequences for R13: every cached grounding response is now
   orphaned, and the $0.03 figure predates the independent judge.
2. **`.env.example` had no `LLM_API_KEY=` line.** Step 3 says to set it and the variable was not in the
   file to set. And with no key every ticket escalated `provider_unavailable` with **exit 0**, so
   `metrics.md` reported 80 of 80 escalated as though it were a result. FR-15's behaviour is right for a
   provider that fails and wrong for one never configured. `require_api_key()` refuses now, beside the
   `require_model()` check that was already there — the same treatment the classifier has had all along.
   D-75.
3. **The PRD still said "Zero spend: free tiers only".** D-55's own words: "the PRD revision has to record
   it rather than let it drift, because the assessment gate checks the claim." The drift was in the tree
   with the source-of-truth document on the wrong side, and `T-R9-1` reads the README, `.env.example` and
   D-55 and deliberately not the PRD. The PRD now has a revision log, and NFR-07 carries the amendment
   with the original struck through rather than erased. D-76.

### Four mediums, four lows

`.env.example` claimed "roughly 250 calls at about 2k tokens each — a few tens of cents" against the
README's 155 calls and $0.03; the repo's own responses average ~800 tokens a call, so the README was right
and a reader budgeting from `.env.example` would plan for ten times the spend. D-54 still documented
`_RateLimiter` and two settings that do not exist, and D-55 claimed "the metrics report now carries an
estimated cost" — it does not; both are struck through with the correction rather than rewritten. The call
table is now marked as a **floor**, because a repair attempt and retries both add provider requests. The
in-code default base URL was still `openrouter.ai`, so commenting out `LLM_BASE_URL` silently pointed an
OpenAI key at the provider that refused every request. `T-R9-2c` asserted its own fixture's default rather
than any behaviour. `T-R9-1b`'s cost assertion could not fail on one deletion. And the synthetic key in
`T-R9-2b` used an `sk-` prefix, which is exactly what a secret scanner looks for.

### Not done, and why

R9's "Do" list asks for Groq's `PROVIDER_TOKENS_PER_MINUTE` / `PROVIDER_REQUESTS_PER_MINUTE` settings to
be documented. **They do not exist** — the author had the pacing removed, and D-54 records that what
survived is a 429 no longer opening the circuit breaker. Documenting a setting that is not there is worse
than documenting none; `T-R9-1c` pins their absence from the README.

**`CLAUDE.md` still lists "Runtime model is a free tier only"**, which is now inconsistent with NFR-07 as
amended. That file is the author's, so the inconsistency is flagged in the PRD's revision log rather than
edited away.

`uv run pytest -q` → **659 passed**. `uv run ruff check .` → clean.

---

## Review row R12 · Housekeeping, which turned out not to be housekeeping

The row asks two things: decide whether `.archify/` is a deliverable, and keep `.DS_Store` out. Both were
already settled — `.archify/` is git-ignored with the reason beside it, the three published diagrams are
tracked in `docs/diagrams/`, `.DS_Store` is ignored, and nothing junky is tracked. That took one command
to confirm.

**Checking the rest of `.gitignore` found two promises that were true on one machine and false for every
reader.**

1. **D-68 pointed at a file inside `storage/`.** It said the September provider recordings are "preserved
   at `storage/llm_cache.2026-09-28-gate.sqlite` with `storage/CACHE_README.md` explaining both files".
   `storage/` is git-ignored: neither the caches nor the explanation survives a clone, so a sentence
   describing a local convenience was written as though it were a deliverable — and I wrote it. The
   explanation is now `docs/provider_cache.md`, tracked, and its first line says its subject is not. It
   also says what a fresh checkout actually gets: no cache, no decision log, no index, every provider call
   live, and **the published figures not reproducible from the repository alone**.
2. **No evaluation report is committed at all**, and that had made a test unfalsifiable. `T-R9-3` checked
   the README's figures against `evaluation/results/gate-openai/metrics.json` and `pytest.skip`-ed when it
   was missing — and `evaluation/results/` holds only a `.gitkeep`, so on a clean checkout, which is the
   only state NFR-09 cares about, it skipped every time. It reads D-68 now, which is tracked. CLAUDE.md
   allows "dated reports you choose to keep", so `.gitignore` gained a `!evaluation/results/kept-*/`
   exception for R13 to use.

**Tests added (1).** `test_T_R12_1_nothing_the_documents_promise_is_itself_git_ignored` — asserts the
cache explanation is tracked, says its subject is not, and that the ignore patterns the row asks about are
present. D-77 records the general rule it enforces: **a document may only point at something a reader
has**; everything else is reproducible from a command, or stated as unavailable.

`uv run pytest -q` → **660 passed**. `uv run ruff check .` → clean.

---

## Review row R10 · The review rows 16 and 17 never got (FR-04, FR-05, NFR-05)

Row 18 recorded that the reviewer session for the API and the dashboard hit a rate limit. Every other row
had a fresh-session review, and between them they found one severe and thirteen high findings — so this
code had been read by nobody but its author. R10 is that review, and it found **one severe and seven
high**.

**Files changed.** `src/ticketing_agent/api.py`, `src/ticketing_agent/pipeline.py`, `docker-compose.yml`,
`.dockerignore`, `tests/test_fr04_api.py`, `tests/test_ops_stack.py`. D-78, D-79.

**Tests added (11).** `test_T_R10_1`, `_1b`, `_2`, `_3`, `_4`, `_5`, `_6`, `_7`, `_8`, `_9`, plus
`test_the_env_name_reader_handles_both_compose_forms`. **671 passing.**

### The severe one: two concurrent tickets, one dropped entirely

`api.submit` opened a `DecisionLog` per request and attached it to the **shared** pipeline. `_record`
reads that log at write time — after a provider round trip, so seconds later — and `DecisionLog` opened
sqlite without `check_same_thread=False` while `provider.py` passes it. FastAPI runs sync endpoints on a
threadpool.

Reproduced against a real server: one ticket answered, **the other 500'd with not one row in the
database** — after being drafted and judged, two paid calls. `DecisionLogUnavailable` is deliberately
re-raised (D-27), `submit` had no handler, and the terminal row was never written. No sent answer, no
logged escalation, no audit row, no queue entry. **Two browser tabs on `/docs` would have done it.**

That breaks three non-negotiables simultaneously. And **my first fix had the same shape as the bug**: a
bare module-level `ContextVar` is visible to every instance, so a log attached to one pipeline could be
written to by another. The suite caught it in seconds — a later test's fresh pipeline picked up an earlier
test's closed handle. It is keyed on the pipeline now, and `T-R10-1b` pins it without needing a race,
because `TestClient` does not reproduce the threading reliably.

### Seven highs

Every exit from `submit` now writes a row first: a malformed body raised a 400 with **no row**, a
pipeline that could not be built 500'd with no row, and anything raised mid-flight lost the ticket.
`/health` returned `ok: true` with no classifier — the exact state `docker compose up` leaves before
`docker compose run --rm train` — and read the lazy retriever property, so the first health check tried to
build the Chroma index against a five-second HEALTHCHECK timeout.

Three more were tests asserting the wrong thing:

* **`/queue`'s `urgency_reason` could be reverted to `detail` with one word and the suite stayed green** —
  D-62's whole point, and `docs/specs/FR-08.md` claimed a test for it that only checked the row.
* **`[:limit]` could move to before the ordering** and nothing noticed, because every queue fixture had
  2–4 rows against a default limit of 50, so the two orderings were identical. On a real log that hides
  every urgent ticket beyond row N. **Sixth** time this backlog has hit the same fixture trap.
* **`test_the_kill_switch_is_reachable_from_the_host` demanded a named volume**, which the host cannot
  reach — so the test contradicted its own docstring, and making the README true made it fail. Changing
  it to a bind mount then exposed that the sqlite viewer mounted the named volume and would have read an
  empty database.

And two guards that could not fire: the dashboard check compared two checked-in files rather than the live
exporter (so deleting a metric block left a panel permanently empty with the suite green), and the
no-key check read `"LLM_API_KEY" not in service["environment"]`, which is `True` for compose's list form —
`["LLM_API_KEY=sk-live-…"]` walked straight past the guard that exists to stop exactly that.

### Recorded as stated limitations, not fixed

**The API is unauthenticated, and `/queue` returns customer-derived text** — handover summaries, and
`urgency_reason`, which carries CloudServe's historic ticket ids. `/tickets` returns the full outbound
reply. FR-04 §7 records this and the PRD puts an agent UI and auth out of scope, so it stays, stated.

**The compose stack exposes more than the API does**, and this had not been recorded anywhere: the sqlite
viewer publishes the whole decision log — including `reply_text`, the exact text sent to customers (D-63)
— on **0.0.0.0:8080 with no password**, and Grafana runs with anonymous admin and the login form
disabled. Read-only, but readable by anyone who can reach the host. The compose comment says "anything
exposed beyond localhost needs a password" while `ports:` publishes on all interfaces. **This belongs in
front of the author at R13**: it is a deployment decision, not a code defect.

Also recorded and not fixed: `/queue` has no run filter or resolution state, so a `docker compose run
--rm gate` injects 80 validation tickets into the agents' live queue and a handled escalation never leaves
it (FR-05 asks for ordering, not a status column); `pipeline.py` writes no `classification` stage row, so
FR-08 §5 is unmet and dashboard panel 8 can only ever show three of the five stages it names; a duplicate
`ticket_id` is accepted twice because each request gets a fresh `seen_ids`; there is no request-size limit
on an unauthenticated endpoint that spends provider credit per call; and `prometheus-client` is a declared
dependency imported nowhere — the same dead-configuration shape as D-74's `JUDGE_MODEL_NAME`.

`uv run pytest -q` → **671 passed**. `uv run ruff check .` → clean. `docker compose config` → valid.

## Review row R11 · A documented path nobody had ever run (NFR-09, A1)

Row 18 said it plainly: *"the image has never been built and the stack has never been started"*, and
predicted that *"anything else will surface on the first real `docker compose up`"*. The row offered two
honest outcomes — build it once, or delete it from the README. I built it, and ran a ticket through the
endpoint the README documents, end to end, on a clean volume.

**Seven defects. Every one of them broke the documented path completely, and not one was visible to 671
passing tests, because the suite does not build an image.** Five were mine, four of those shipped by R10
hours earlier.

**Files changed.** `Dockerfile`, `docker-compose.yml`, `src/ticketing_agent/api.py`, `README.md`,
`tests/test_ops_stack.py`, `tests/test_fr04_api.py`, `tests/test_docs_consistency.py`. D-80.

**Tests added (7).** `test_T_R11_1` (image file modes, derived from the `COPY` lines),
`test_T_R11_5` (every named volume path created *and* owned in the image), `test_T_R11_3` (the first
ticket does not deadlock), `test_T_R11_4` (concurrent first requests build the index once),
`test_T_R10_3b` (`/health` reports a missing classifier without loading anything), `test_T_R11_1` and
`test_T_R11_2` in the docs-consistency file (the README's volumes and its cold-start claim).
**678 passing.**

### Not mine: the image was unreadable, and the one artefact `train` makes was unwritable

`COPY` preserves the host's file modes. The author's `data/` is `-rw-------`, so inside the image those
files were `-rw------- root root` while the container runs as uid 10001. **All four data files and nine of
the ten prompts were denied.** `/search` could not read the corpus, every model call would have raised
`PromptError`, and `docker compose run --rm train` died with `Permission denied:
development_tickets.json` — which is how I found it. `RUN chmod -R a+rX` over the copied directories,
before `USER`.

`scripts/train_classifier.py` writes `evaluation/reports/classifier_calibration.md` — the out-of-fold
measurement R8 identifies as the only figures that bear on NFR-03. That directory was neither created nor
chowned in the image, and compose had no mount for it, so the report could not be written and would have
been discarded with the container if it could. Both fixed. Details in **D-80**.

### Mine: four defects I shipped in R10, found by running the thing

* **`threading.Lock` where a reentrant one was needed.** `pipeline` takes the lock, then reads
  `self.retriever`, which takes it again. The very first ticket deadlocks — for ever, no error, no log
  line. `/health` kept answering 200 the whole time, which is exactly how it presents: a container Docker
  calls healthy that cannot answer a ticket. A unit test now holds it.
* **`/health` built the pipeline as its readiness probe.** R10's finding was that `/health` must not touch
  the lazy retriever, because the HEALTHCHECK gets five seconds. My fix obeyed the letter and inverted the
  spirit: it loaded the classifier, built the index and downloaded a 79MB model. It now `stat`s three
  configured paths and reports `pipeline: not built` honestly.
* **The sqlite viewer still mounted the *named* `storage` volume** after R10 moved `storage` to a bind
  mount for FR-16. The audit window would have shown an empty database, with no error.
* **The `model-cache` volume I added in this row broke the embedder outright.** Docker creates a named
  volume mounted over a path absent from the image as **root-owned**, so uid 10001 got
  `RetrievalError: ... Permission denied: '/home/support/.cache/chroma'` and every ticket 503'd. Fixed by
  creating and chowning the directory in the image, which is what Docker seeds the volume from. This is
  the second time in two rows that a fix of mine had the same shape as the bug it fixed.

The cache volume exists because `docker compose run --rm train` fetched all 79MB and `--rm` threw it away:
every training run and every gate run would have re-downloaded it. Nothing on the host needs to read it,
so a named volume is right.

### Verified, by running it

Rebuilt image, `docker volume rm` first so the model cache was cold, and the whole sequence passed:

```
PASS  image built
PASS  docker compose run --rm train trained inside the container
PASS  classifier.joblib and classifier_calibration.md are on the host (bind mounts work)
PASS  /health answered 200: {"ok":true,"problems":[],"pipeline":"not built","documents_indexed":0,...}
PASS  POST /tickets returned a decision: auto_respond, intent=rollback_request, urgency=high
PASS  an answered ticket carries the reply, an escalated one does not
PASS  the row is in storage/decisions.db on the host
PASS  touch storage/KILL_SWITCH is seen inside the container (FR-16, no exec needed)
PASS  /metrics/prometheus exported 8 metric types at the path Prometheus is configured to scrape
```

Three earlier runs of that same script are what produced the findings above: run 1 died in `train` on the
file modes, run 2 503'd every ticket on the cache volume, run 3 hung on the lock with `/health` green.

### What the README now says, and what it deliberately does not

It said `storage/` was a named volume and told the operator to `docker compose exec api touch
/app/storage/KILL_SWITCH`. Both were false after R10. It now documents the bind mount and
`touch storage/KILL_SWITCH`, says the first run is slow and why, and says that `/health` passes while the
container is still warming up. It does **not** say the Docker path is supported or maintained: it is
verified **as of this row**, by the steps above, and re-verifying it means running them again. A static
guard cannot replace a build, which is why the row asked for one.

Two things recorded rather than fixed. **`:8080` and `:3000` have no authentication** and are published on
all interfaces — the viewer serves the whole decision log including `reply_text`, the exact text sent to
customers, and Grafana runs as an anonymous admin. The README now warns in a blockquote; the deployment
decision is the author's, and it is queued for **R13**. And `tests/test_ops_stack.py` still cannot build an
image in CI, which has no Docker; `T-R11-1` and `T-R11-5` parse the Dockerfile instead and derive what they
check from its own `COPY` and `volumes:` lines, so a new directory cannot silently miss the guard.

**My own test-writing, seventh instance of the same fault.** `T-R11-5`'s first three versions could not
fail: it split the Dockerfile on the bare word `USER`, which also appears in a comment, so the "before
`USER`" half contained everything. Before that, a version collapsed `mkdir -p` and `chown -R` from one
`RUN` into a single string and so could not tell "created" from "owned" — the exact distinction the
defect turned on. Asserting over too coarse a unit, so that two different states look identical, is now
the only mistake I have made repeatedly in this backlog.

`uv run pytest -q` → **678 passed**. `uv run ruff check .` → clean. `docker compose config` → valid.

## Review row R13 · CHECKPOINT: a fresh gate run, and the figure that will not hold still (FR-14, A9, A10)

**Status: `HUMAN`.** The row asks for the analysis and names what the author decides: which run the
gate is signed off on, whether the label or the system is right on each disagreement, and the
two-assessor review. This entry is the analysis. It does not decide any of those.

**What was run.** Three full passes over the 80 validation tickets, 2026-10-02, on this commit:

| run | input | responses | answered | escalated | blocked | kept as |
|---|---|---|---|---|---|---|
| 1 | `data/validation_tickets.json`, `--no-cache` | 138 live, 0 replayed | **42** | 38 | 3 | `evaluation/results/kept-gate-2026-10-02/` |
| 2 | a renamed copy under a path the repo has never seen | 42 live, 97 replayed | **39** | 41 | 5 | `…-renamed/` |
| 3 | that same renamed copy again | 0 live, 139 replayed | **39** | 41 | 5 | `…-replay/` |

Comparison report: `evaluation/reports/gate-2026-10-02-checkpoint.md`, regenerable with
`scripts/gate_checkpoint_report.py` (new, paths as arguments). Review sheet:
`evaluation/reports/review_sample_2026-10-02.csv`. D-81, D-82.

**Reconciliation, which is the non-negotiable.** All three runs: 80 tickets in, 80 terminal rows,
80 decisions logged, no missing, extra, duplicated or index-gapped rows, log reconciles `true`. 0
private-data detections, 0 redactions, 0 citations that do not resolve. Not one ticket was dropped
in 240 ticket-passes.

**Path independence.** Run 2 was pointed at
`…/queue-dump-2026-10-02-nobody-has-seen-this.json` and processed all 80 without a change to
anything. Note for a reader: runs 2 and 3 record that absolute temporary path in their own
`metrics.json`, so those two `input` fields point at a file that no longer exists — which is what
an unseen-file run looks like, not a defect.

### The finding: the routing is reproducible from the cache and not from the provider

`CLAUDE.md`: *"Deterministic: temperature 0, cached model responses, same input → same routing."*
Run 3 replayed run 2 entirely and reproduced it **exactly** — 0 of 80 decisions differed and all 80
replies were byte-identical. Run 1 against run 2, same commit and same configuration, hours apart:
**7 of 80 tickets routed differently**, and every one of the seven moved on a reason that reads the
model's exact words (`no_cited_article`, `ungrounded_draft`).

So the headline answered count has a run-to-run spread of **42 vs 39 on identical input** — 3.8
points of first-contact resolution. A live run is a sample. Full argument and the per-ticket table
in **D-81**; it is not a defect in the system or the cache, and no amount of re-running produces a
single stable live number.

**Against the September run D-57 signed off on**, both answered 42, and **26 tickets are routed
differently inside that identical total**. It decomposes: 8 are the R7 rules doing exactly what R7
said (6 `money_decision_required`, 2 `compliance_data_question`), and 13 are the grounding judge now
passing drafts it failed in September — D-74 made the judge model part of the cache key, which
orphaned every cached grounding response, so the September figure was replayed from a judge that is
no longer the configured one. The match at 42 is coincidence.

### The numbers the row asks for, from run 1 (the only one with no replayed response)

* **Volume and outcomes.** 42 answered, 38 escalated, 3 blocked by guardrails (all `grounding`).
  FCR proxy **52.5%** against a ≥60% target; escalation **47.5%** against ≤30%. Both missed, both
  on the right side of the baseline (42% and 58%).
* **Real latency.** Median 4,019 ms, p95 6,641 ms over all tickets; the NFR-01 figure — the
  automated path — is **p95 5,561 ms against a 3,000 ms target. NFR-01 is missed**, and the report
  says so and names the cause: two provider round trips per answered ticket, PR-01 to draft and
  PR-03 to judge, against a hosted model.
* **Retrieval.** Hit rate 96.2% over the 53 tickets with an expected article. No false-positive
  counterpart, as the report states.
* **R6, answered against the labels.** 0 answered where the label says `must_not_auto_respond`.
  **13** answered where the label says `escalate` (VAL-0004, 0014, 0016, 0022, 0024, 0025, 0034,
  0038, 0051, 0055, 0060, 0070, 0071), 12 of those same 13 also labelled not answerable from docs,
  and 1 answered citing no expected article (VAL-0080). The matrix also shows **19 tickets the
  labels expected answered that this run escalated** — the larger number, and the same question
  applies to them. R6 measured 15 and 20 on the run it had, before R7's rules landed; the
  September report predates the section and does not carry those figures at all.
* **R8, classification by wording.** 100% accuracy over all 80, and that figure is near-duplicate
  lookup: 77.5% of the run's tickets use wording from the training file. Of the 18 with an unseen
  body, 14 are paraphrases of a training body at D-39's 0.85 clustering, leaving **4 genuinely
  novel** (VAL-0003, 0004, 0046, 0073) — a sample that supports nothing either way. The figures
  that bear on NFR-03 remain the out-of-fold ones in
  `evaluation/reports/classifier_calibration.md`. Calibration worst gap **3.0 points**, inside
  NFR-03's 5, over a single populated band.
* **NFR-06 fairness.** Above the 5-point limit on tier (**35.0**, driven by n=8 enterprise),
  region (**31.4**, n=7 latin_america) and length (**16.0**, n=9 long_or_complex); within it on
  fluency (0.1) and channel (4.5). The three that fail are the three with a segment under 10.
* **Spend.** 180 live provider requests today — 138 in run 1, 42 in run 2, 0 in run 3. The report
  records request counts and the provider and model names, and **no token counts, so no cost
  figure can be derived from it**. D-55 amended NFR-07 to "a paid provider within a stated
  budget"; the budget is therefore checkable only as a call count, which the author may want to
  note in the declaration.

### Two things found by running it, and fixed here

* **`--help` described the opposite of what `--no-cache` does** — *"Responses are still written"*,
  false since R5. The cache is what decides which routing a later replay produces, and `--help` is
  where an operator looks before a timing run. Corrected; `T-R13-1` now asserts the help text and
  `ProviderClient` **against each other**, and I confirmed it fails on the old string. D-82.
* **The harness aborted with exit 134 after writing a complete and correct report.** Run 1 printed
  its summary, then `libc++abi: terminating due to uncaught exception … recursive_mutex lock
  failed` during interpreter shutdown. The report, the log and the outcomes file were all complete
  and correct. Not reproduced: runs 2 and 3 and two smaller probes (`--stub-pipeline`, and the full
  pipeline on two tickets) all exited 0. **Recorded, not fixed** — it is a native-library teardown
  in the embedder, it is intermittent, and I will not guess at a fix for something I cannot
  reproduce. It matters because `docker compose run --rm gate` and any `&&` chain read the exit
  code, and would call that passing run a failure. If the author wants it closed, the row is "the
  harness must exit 0 when the report is written", and the fix is in process teardown, not in the
  run.

### My recommendation, which is not a decision

1. **Sign off on run 1**, the live `--no-cache` run, and quote every figure from it **with the ±3
   ticket spread stated beside the headline**. It is the only one of the three that measures the
   automated path, it is the only one whose latency is real, and D-81 makes the spread a published
   property rather than a surprise.
2. **Treat the 13 + 19 disagreements as a data question first.** Four bodies inside
   `validation_tickets.json` carry more than one label, and 37 of the 62 validation tickets with an
   identical-body development twin disagree with that twin. VAL-0024/0075, VAL-0033/0060/0061 and
   VAL-0012/0034 are label contradictions inside the file the gate is scored against; on those the
   system cannot be right, because no answer agrees with both copies. The per-ticket lists are in
   the checkpoint report.
3. **Do not change a threshold or a rule on the strength of these figures.** Seven tickets move
   between two runs of the same code; any tuning inside that band is fitting noise, and tuning
   against individual validation tickets is forbidden anyway.

### Still queued for the author, carried forward

* **Which run the gate is signed off on** (D-57, D-81): run 1 live 42, run 2/3 replayed 39,
  September replayed 42 — and D-74 orphaned every cached grounding response that the September
  figure was replayed from.
* **The label contradictions** above, and R7's specific cases: the dispute rule escalates 6
  tickets labelled `auto_respond` that are all duplicates of VAL-0072 with the opposite label
  (D-70); the residency rule escalates VAL-0054 and VAL-0076, VAL-0054 on its canned subject line;
  VAL-0037 has a development twin labelled the other way (D-72). The rule cannot satisfy both.
* **NFR-01 is missed**: automated-path p95 5,561 ms against <3 s, cause stated.
* **NFR-06 is missed on three of five dimensions**, each driven by a segment under 10 tickets.
* **The unauthenticated `:8080` and `:3000` windows** (R11): the viewer serves the whole decision
  log including the text sent to customers, Grafana runs as an anonymous admin, both on all
  interfaces. A deployment decision, not a code defect.
* **`CLAUDE.md` line 21 still reads "Runtime model is a free tier only"**, which NFR-07 as amended
  by D-55 contradicts, and which every run above contradicts. Flagged in the PRD revision log
  rather than edited, because that file is the author's.
* **No cost figure is derivable from any report** (spend, above).
* **The exit-134 abort**, if it is to be closed rather than recorded.
* **R14's author documents** remain untouched: risk register, incident procedure, declaration,
  kill-switch authorisation, log retention.

`uv run pytest -q` → **679 passed**. `uv run ruff check .` → clean. One test added: `T-R13-1`.
