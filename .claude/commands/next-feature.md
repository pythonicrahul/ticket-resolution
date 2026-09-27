---
description: Build the next backlog item end to end (spec → tests → code → review → commit), then stop.
---
You are running one iteration of the build loop for this repository. Do exactly one backlog item, then stop.

## 1. Orient
Read `CLAUDE.md`, `docs/BACKLOG.md`, the last three entries of `docs/PROGRESS.md`, and `docs/PRD.md` rows for the item's requirements.
If any row is `BLOCKED` or `HUMAN`, print `LOOP STOP: waiting for human on row <n>` and stop without changing anything.
Pick the first `TODO` row whose dependencies are all `DONE`. If none, print `LOOP STOP: nothing to do` and stop.

## 2. If the row's kind is `checkpoint`
Do the analysis the row asks for (run the relevant script or harness command), write the findings and your recommendation as a PROGRESS entry, set the row to `HUMAN`, commit, print `LOOP STOP: checkpoint <n> needs review`, and stop. Do not make the decision yourself.

## 3. Specify
For each requirement in the row without `docs/specs/<FR-ID>.md`, write it following `prompts/development/PR-06_spec_from_requirement_v1.0.md`. Put genuine ambiguities under "Open questions" and pick the most conservative option (the one that escalates rather than answers).

## 4. Tests first
Write the acceptance tests `T-<FR-ID>-n` from the spec in `tests/`. Tests must run with no network and no API key: use the fake provider and fixtures. Run `uv run pytest -q` and confirm the new tests fail for the right reason.

## 5. Implement and iterate
Implement following `prompts/development/PR-07_implement_requirement_v1.0.md`. Then repeat up to **6 times**:
`uv run pytest -q && uv run ruff check .`
Read the failure, fix the cause, run again.
- Never delete, skip, weaken or `xfail` an existing test to make it pass. If you believe a test is wrong, stop and treat the item as blocked.
- Never add a flag or exception handler that disables a guardrail or skips logging.
If still red after 6 attempts: append a PROGRESS entry with the failing test names, the error, what you tried and your best hypothesis; set the row to `BLOCKED`; do not commit; print `LOOP STOP: blocked on row <n>` and stop.

## 6. Independent review
Delegate to the `reviewer` subagent with the item's requirement IDs. Fix every finding marked severe or high; re-run step 5's command until green. Record findings you chose not to fix, with the reason, in the PROGRESS entry.

## 7. Record and commit
- Set the row to `DONE` in `docs/BACKLOG.md`.
- Append to `docs/PROGRESS.md`: date, row, requirements, files changed, tests added (names), test count and result, design decisions made (also add them to `docs/decisions.md` if non-obvious), open questions.
- `git add -A && git commit` with a message starting `<FR-ID>:` (first requirement of the row) and a one-line summary.
Print `LOOP OK: row <n> done` and stop.
