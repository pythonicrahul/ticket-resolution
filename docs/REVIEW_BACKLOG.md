# Review backlog (post-build review, 2026-09-30)

Findings from a review of the finished build against the Build Specification's twelve acceptance
criteria, the PRD and the recorded runs (`evaluation/results/gate-openai*`, `storage/gate-openai-2.db`).
The tests were **not** run during the review (no package index was reachable), so every row starts by
running the suite.

## How to work this file

Same rules as `docs/BACKLOG.md` and `/next-feature`: take the first `TODO` row whose dependencies are
`DONE`, write failing tests first, implement, run `uv run pytest -q && uv run ruff check .` (up to 6
attempts), review in a fresh session with the `reviewer` agent, update this table and
`docs/PROGRESS.md`, and commit with a message starting with the row's first requirement ID. Rows of kind
`author` are for the author, not Claude Code: skip them. Status values: `TODO` · `DONE` · `BLOCKED` ·
`HUMAN`.

Prompt for Claude Code: *"Work through docs/REVIEW_BACKLOG.md one row at a time, following the rules at
the top of that file and in .claude/commands/next-feature.md. Stop at any HUMAN, BLOCKED or author row."*

| # | Item | Requirements | Depends on | Kind | Status |
|---|---|---|---|---|---|
| R1 | Establish the current test status and fix the README test count | A12, NFR-09 | — | build | DONE |
| R2 | Put classification, urgency and alternatives on every decision-log row | FR-13, FR-05, FR-08, A8 | R1 | build | TODO |
| R3 | Persist the sent reply and the handover note in the run output | FR-14, FR-13, NFR-03 | R1 | build | TODO |
| R4 | Make the metrics report describe the system that actually ran | FR-14, A10 | R1 | build | TODO |
| R5 | Report real latency; mark cache replays as replays | FR-14, NFR-01 | R4 | build | TODO |
| R6 | Report "answered but labelled escalate / unanswerable" | FR-14, FR-02, NFR-03 | R4 | build | TODO |
| R7 | Widen the money rule to disputed and unrecognised charges; escalate compliance-grade data-residency questions | FR-03, FR-09 | R1 | build | TODO |
| R8 | Report classification on unseen wording, not only on the validation file | FR-08, NFR-03 | R4 | build | TODO |
| R9 | Provider: OpenAI is the default, and every document says so and why | NFR-07, NFR-09, A1 | R1 | build | TODO |
| R10 | Independent review of rows 16 and 17 (API, dashboard) | FR-04, FR-05, NFR-05 | R2 | build | TODO |
| R11 | Build and start the Docker stack once, or remove it from the README | NFR-09, A1 | R9 | build | TODO |
| R12 | Housekeeping: `.archify/` | — | — | build | TODO |
| R13 | CHECKPOINT: fresh gate run after R2–R9, and a hand review of the auto-answers that disagree with the labels | FR-14, A9, A10 | R2–R9 | checkpoint | TODO |
| R14 | Author documents: risk register, incident procedure, declaration, kill-switch authorisation, log retention | Governance §2, §5, §6 | — | author | TODO |

---

## R1 · Establish the current test status and fix the README test count

**Evidence.** `.pytest_cache/v/cache/lastfailed` (28 Sep 20:55, before the last three commits) lists five
failing tests:
`test_T_FR08_11_the_reported_figures_are_out_of_fold`,
`test_T_FR08_4_a_broken_embedder_or_model_still_falls_back`,
`test_T_FR11_4_a_sentence_with_no_citation_makes_the_draft_unusable`,
`test_T_FR06_4_the_three_lines_follow_the_drafted_text_unchanged`,
`test_T_FR06_12_an_unusable_name_falls_back_rather_than_going_out[Dana\nOkonkwo]`.
The README says 469 tests, PROGRESS says 504, and the pytest node cache holds 525.

**Do.** Run `uv run pytest -q` and `uv run ruff check .`. Fix any failure at its cause (never by weakening
a test). Replace the number in README step 5 with wording that cannot go stale, e.g. "the full suite runs
with no network and no API key".

**Done when.** The suite is green, ruff is clean, and the PROGRESS entry records the real count.

## R2 · Put classification, urgency and alternatives on every decision-log row

**Evidence.** In `storage/gate-openai-2.db`, `storage/gate-2026-09-28.db` and `storage/decisions.db`, the
columns `intent`, `intent_confidence`, `intent_alternatives` and `urgency` are empty on **every** row.
`Outcome.to_entry()` in `src/ticketing_agent/pipeline.py` never passes them; only `handover.py` reads
`classification.intent_alternatives`. The consequences:
- `/queue` (`api.py`) orders by the log's `urgency`, which is always `None`, so the FR-05 queue silently
  becomes oldest-first on real traffic. `test_T_FR05_the_queue_is_ordered_by_urgency_then_age` passes only
  because it writes `urgency` into the log directly.
- The Governance Framework record's `alternatives` is always `[]`, so the Build Spec's "records the
  alternatives it considered, not only the option it chose" is unmet.

**Do.** Carry `intent`, `intent_confidence`, `intent_alternatives`, `urgency` and `urgency_confidence` from
`state.classification` onto `Outcome` and into `to_entry()`, and onto the intermediate rows that already
know them. Return urgency from `POST` ticket responses alongside intent and confidence.

**Tests.**
- `T-R2-1` End to end: a ticket through the real graph (fake provider) produces a terminal row whose
  intent, urgency and non-empty alternatives match the classifier's output.
- `T-R2-2` Two escalated tickets of different urgency, **submitted through the API** (not inserted into the
  log), come back from `/queue` urgency-first.
- `T-R2-3` `governance_record()` of a real pipeline row has a non-empty `alternatives` list.

## R3 · Persist the sent reply and the handover note in the run output

**Evidence.** `outcomes.jsonl` lines carry decision, citations and segments but not the text that was
sent. The decision log has no reply column either. Without the text, the Evaluation Framework's human
review (≥50 responses, two assessors, for hallucination rate and citation accuracy) cannot be done, the
demo cannot show what was sent, and a complaint cannot be reconstructed.

**Do.** Add to each `outcomes.jsonl` line: `reply` (the exact outbound text including disclosure, or
`null`), `handover` (summary, customer goal, already tried, system uncertainty) for escalations,
`intent`, `intent_confidence`, `urgency`. Add a `reply_text` column to the decision log on the terminal
`auto_respond` row, subject to the existing redaction rules. Add
`scripts/review_sample.py --input <outcomes.jsonl> --n 50 --seed 1 --output <csv>` that writes a
two-assessor review sheet (ticket, reply, cited passages, columns for each assessor's
supported/unsupported verdict per sentence).

**Tests.** `T-R3-1` every auto-answered line has a non-empty `reply` that ends with the disclosure;
`T-R3-2` every escalated line has a handover; `T-R3-3` the review sheet has n rows, is deterministic for a
seed, and carries the passages each reply cited.

## R4 · Make the metrics report describe the system that actually ran

**Evidence.** The latest `metrics.md` / `metrics.json` still say, next to real per-class figures and 42
sent replies: "Intent precision and recall: no classifier yet (row 8)", "Private data in outbound
replies: nothing is sent yet (row 11)", "Confidence calibration: needs the classifier's confidences
(row 8)", "no model call in the stub pipeline, so this will rise at row 11", "100% is by construction
until row 14", and a route-agreement note saying "While the answering path is unbuilt every ticket
escalates … it does not measure routing". These were written for the stub pipeline. This report is what
acceptance criterion A10 checks.

**Do.** Derive every note and every entry in `gaps` from what the run actually measured, e.g. print
"not computable" only when the figure really is absent, and use different wording for `--stub-pipeline`
runs. Remove every "row N" reference from generated output. Fill the results table from the computed
figures: intent precision from the per-class table, private-data detections from the guardrail counts,
calibration from the classifier's bands.

**Tests.** `T-R4-1` a full-pipeline run's report contains none of: "row 8", "row 11", "row 14", "stub",
"unbuilt", "not computable yet" for figures that were computed; `T-R4-2` a `--stub-pipeline` run's report
says it is a stub run; `T-R4-3` the results table's intent-precision cell equals the per-class minimum or
macro figure it claims to be.

## R5 · Report real latency; mark cache replays as replays

**Evidence.** `evaluation/reports/gate-openai-2026-09-28.md` (run `gate-openai-2`) reports latency median
91 ms and p95 95.7 ms with **0 model calls and 162 cache hits**: a replay. The live run `gate-openai`
measured median **4.4 s** and p95 **6.7 s**, which **misses NFR-01 (< 3 s)**.

**Do.** When a run made fewer model calls than it answered tickets, head the report with a prominent
"cache replay: latency is not representative" notice and put the latency figures in the results table as
"replay" rather than as achieved values. Add `--no-cache` to the harness for timing runs. Record the NFR-01
miss in the PROGRESS entry so the PRD revision can carry it.

**Tests.** `T-R5-1` a run served fully from cache is labelled a replay in both metrics files; `T-R5-2`
`--no-cache` makes zero cache reads.

## R6 · Report "answered but labelled escalate / unanswerable"

**Evidence.** In `gate-openai-2`, **14 of 42 auto-answers** went to tickets labelled
`expected_route = escalate`, 13 of them labelled `answerable_from_docs = false`: VAL-0004, 0014, 0016,
0022, 0025, 0034, 0037, 0038, 0051, 0055, 0066, 0070, 0071, 0072. VAL-0080 was answered from
DOC-DEPLOY-003 where the label expects DOC-DEPLOY-001. No must-escalate ticket was answered. The Dataset
Guide says `answerable_from_docs` exists to measure "whether your system correctly recognises questions it
cannot ground"; the harness reports nothing on it.

**Do.** Add to the Technical group and the results table:
- answered tickets labelled `expected_route = escalate` (count, %, ticket ids);
- answered tickets labelled `answerable_from_docs = false`;
- answered tickets whose citations miss every `expected_doc_ids` (count, ids);
- the confusion matrix of decision against `expected_route`.
Report each only when the labels are present ("scored against labels: N of M").

**Tests.** `T-R6-1` a synthetic three-ticket file with known labels and decisions yields the exact counts
and ids; `T-R6-2` an unlabelled file omits the figures instead of reporting zero.

## R7 · Widen the money rule; escalate compliance-grade data-residency questions

**Evidence.** VAL-0072 ("There are charges on our invoice for a service I do not believe we use. Could you
explain what these relate to?") was auto-answered: it is a disputed charge, which FR-03 says must reach a
person, but it contains none of the trigger words. VAL-0037 ("Are backups replicated outside our primary
region? A compliance review has raised this and I need a definite answer.") was auto-answered: the PRD's
open question on data residency says account-specific or compliance-grade location questions escalate.

**Decision to implement.** (a) FR-03 also escalates a billing ticket that disputes or disowns a charge:
wording such as "do not believe we use", "didn't order", "not ours", "don't recognise/recognize",
"shouldn't be charged", "charged twice", "overcharged", "incorrect charge". (b) A `data_residency` ticket
escalates when it asks about the customer's own data location or is raised for compliance, audit, legal
or regulatory purposes ("our data", "our backups", "compliance", "audit", "regulator", "definite answer");
general policy questions may still be answered. Record both as decisions in `docs/decisions.md`, and add
the patterns beside the existing FR-03 tables in `guardrails.py`/`route.py`, not in tests.

**Tests.** `T-R7-1` VAL-0072's text escalates with reason `money_decision_required`; `T-R7-2` VAL-0037's
text escalates with a data-residency reason; `T-R7-3` a plain "how is proration calculated?" still
answers; `T-R7-4` a general "which regions do you offer?" still answers; `T-R7-5` a dev-set sweep reports
how many previously answered dev tickets the new rules now escalate (reported, not asserted).

## R8 · Report classification on unseen wording

**Evidence.** The gate reports 100% per-class precision and recall, but **62 of the 80 validation tickets
have a body identical to a development ticket** the classifier was trained on. Cross-validated dev
figures (`evaluation/reports/classifier_calibration.md`) show three intents below NFR-03's 85%.

**Do.** In the metrics report, split classification figures into "body seen in training data" and "unseen
wording", with counts, and state which one the headline uses (the unseen one). The harness finds the
training file through configuration (`TRAINING_TICKETS_PATH`), never a hardcoded name; if it is absent, say
the split could not be made. Reference the cross-validated dev figures in the report text.

**Tests.** `T-R8-1` on a synthetic file with two seen and two unseen bodies, the split counts are 2/2;
`T-R8-2` with the training file missing, the report says the split was not possible and still completes.

## R9 · Provider: OpenAI is the default, and every document says so and why

**Decision (author).** The system runs on **OpenAI** (a small paid model, `gpt-4o-mini` class, a few US
cents per 80-ticket run). **Reason: the free tiers throttle so heavily that development and testing became
very challenging.** OpenRouter's free endpoints refused 20 consecutive requests from a shared pool (D-46).
Groq's free tier escalated 21 of 80 tickets without attempting them, and still 8 of 65 after the client
learned to pace itself (D-54). That made gate runs unrepeatable and turned quality measurements into
throttling measurements. The Build Specification says free tiers only and to raise it rather than pay; this
is raised here, in D-55 and in the report, with the evidence, the spend per run, and the free-tier path
kept working for anyone who prefers it.

**Evidence of inconsistency.** README step 3 says "**Groq is the provider that works**"; `.env.example`
says OpenAI on a small paid budget (D-55). An assessor substitutes "a working key" and follows the README
literally.

**Do.**
- README step 3: OpenAI is the default (base URL, model names, where the key goes), followed by one
  sentence of the reason above and the approximate cost per run. Keep Groq as a documented free
  alternative with its `PROVIDER_TOKENS_PER_MINUTE` / `PROVIDER_REQUESTS_PER_MINUTE` settings and a note
  that a throttled run escalates rather than fails (A11).
- `.env.example`: OpenAI values active, Groq and OpenRouter commented, same wording as the README.
- `docs/decisions.md` D-55: add the sentence "free tiers throttled so heavily that development and testing
  became very challenging", and link D-46 and D-54.
- The metrics report states the provider host, the model names and the model-call count, so the spend is
  visible in every run.
- Fix README "Pace" to describe both providers.

**Tests.** `T-R9-1` README, `.env.example` and D-55 name the same default provider (a test reads all
three); `T-R9-2` every run's `metrics.json` carries `provider_base_url` (host only, never the key) and the
model names.

## R10 · Independent review of rows 16 and 17

**Evidence.** PROGRESS row 18: the reviewer session for the API and dashboard hit a rate limit; every other
row had a fresh-session review, which found one severe and thirteen high findings between them.

**Do.** Run the `reviewer` agent on `api.py`, `ops/`, `tests/test_fr04_api.py`, `tests/test_ops_stack.py`.
Fix severe and high findings. Record the rest with reasons. Also record in the PROGRESS entry that the API is
unauthenticated and `/queue` exposes customer text (FR-04 §7), as a stated limitation.

## R11 · Build and start the Docker stack once, or remove it from the README

**Evidence.** PROGRESS: "the image has never been built and the stack has never been started".

**Do.** `docker compose up --build`, then `docker compose run --rm train` and one ticket through
`http://localhost:8000/docs`. Fix what breaks. If Docker is not available, move the Docker section under a
heading "Optional, not verified" so an assessor does not take it as the documented path.

## R12 · Housekeeping

`.archify/` is untracked. Commit it if it is a deliverable (architecture page for the report), otherwise add
it to `.gitignore`. Check that no `.DS_Store` gets committed (add to `.gitignore`).

## R13 · CHECKPOINT: fresh gate run and hand review

**Do.**
1. `uv run python -m evaluation.harness --input data/validation_tickets.json --output evaluation/results/gate-<date> --no-cache`
   and the same on a renamed copy of the file.
2. Summarise in PROGRESS: counts, reconciliation, real latency, the R6 figures, the R8 split, spend.
3. Produce the R3 review sheet for 50 replies.
Set the row to `HUMAN`. The author reads every auto-answer that disagrees with its label (R6 list),
decides whether the label or the system is right, and does the two-assessor review.

## R14 · Author documents (not for Claude Code)

From `docs/pack_alignment.md` §4: the risk register R-01…R-08 with likelihood, impact, a specific
mitigation and a named owner (add R-06 provider unavailable and R-09 paid-provider cost); the six-step
incident procedure; the governance declaration; the kill-switch authorisation; the decision-log retention
period; whether the handover `summary` may carry customer text.
