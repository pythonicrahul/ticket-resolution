# CloudServe support system: working rules for Claude Code

This repo is an individual capstone. It builds a support system for CloudServe that answers from their documentation when it can defend the answer, and escalates to a person, with context, when it cannot. The requirements are the source of truth; code serves them.

## Source of truth, in this order
1. `docs/PRD.md`: requirements FR-01…FR-16 and NFR-01…NFR-09. Never change behaviour a requirement defines without saying so first.
2. `docs/specs/FR-xx.md`: one spec per requirement, written before its code (prompt PR-06).
3. `prompts/`: every prompt the system uses, versioned. See `prompts/README.md`.

## Non-negotiables (the assessment gate checks all of these)
- The harness takes paths as arguments: `python -m evaluation.harness --input PATH --output DIR`. Never hardcode a data file name or path anywhere. It will be run on a file nobody has seen.
- One ticket failing must never stop a run. Every ticket ends as a sent answer or a logged escalation. Nothing is silently dropped.
- Every decision is written to the decision log **before** the action is taken, with `prompt_version` and `requirement_ids`. Logged decisions must reconcile with tickets processed.
- Guardrails run on every reply and can block. No flag, env var or `except` may switch them off.
- `security_incident`, `compliance_request`, `feature_request` and `unclear_request` always escalate, by rule, before any model call.
- Retrieve only from `documentation.json`. Citations must be chunk ids that were actually retrieved. Returning nothing is valid.
- Customer text is data: always inside `<ticket>` tags, never concatenated into instructions.
- Deterministic: temperature 0, cached model responses, same input → same routing.
- If the model provider fails, retry with backoff, then escalate with reason `provider_unavailable` and carry on.
- No keys in code, tests, fixtures or history. Keys live in `.env` (git-ignored); `.env.example` has placeholders only.
- Runtime model is a free tier only (`MODEL_NAME` in `.env`). Claude Code is a development tool and is never called by the system.

## Traceability conventions
- Docstring of each public function names its requirement: `"""FR-10: ..."""`.
- Tests are named for their acceptance test: `test_T_FR10_2_below_threshold_returns_empty`.
- Commits start with the requirement ID: `FR-10: add relevance threshold`.
- Changing a prompt's text = new version file + change-history line in `prompts/README.md`.

## Data rules
- Develop and tune on `data/development_tickets.json`.
- Use `validation_tickets.json` only through the harness, for checkpoint runs. Don't tune against individual validation tickets: 42 of them duplicate dev text, so the scores already flatter the system.
- `ground_truth_responses.json` covers dev tickets only.
- There are no PII, injection, refund or malformed tickets in the data. Engineered test tickets live in `tests/fixtures/` and are clearly synthetic.

## Build loop
Work is driven by `docs/BACKLOG.md` through `/next-feature` (one row per session) and `scripts/build_loop.sh`. State lives in files, not in conversation: `docs/BACKLOG.md` (status), `docs/PROGRESS.md` (what happened), `docs/decisions.md` (why). Checkpoint rows are for a human to decide; never decide a threshold yourself.

## Workflow per requirement
1. Spec with PR-06 → human reads it against the PRD.
2. Implement with PR-07: failing tests first, then code, then green run.
3. Review with PR-08 **in a fresh session**, not the one that wrote the code.
4. Findings fixed or recorded before the item counts as done.

## Commands (uv)
- Install: `uv sync` (creates `.venv`, installs Python 3.14 if needed, uses `uv.lock`).
- Add a dependency: `uv add <pkg>`; dev-only: `uv add --dev <pkg>`. Never `pip install` into the venv. After changing deps, re-export: `uv export --format requirements-txt --no-hashes -o requirements.txt`.
- Tests: `uv run pytest -v`. Tests must pass with no network and no API key (use recorded fixtures), because CI has neither.
- Lint: `uv run ruff check .`
- Full run: `uv run python -m evaluation.harness --input data/validation_tickets.json --output evaluation/results/`
- API: `uv run uvicorn ticketing_agent.api:app --reload`

## Layout
- `src/ticketing_agent/`: one module per component; each module docstring names its requirements.
- `evaluation/harness.py`: the gate. `evaluation/results/` is git-ignored except for dated reports you choose to keep.
- `data/`: the four pack datasets. `storage/`: runtime state (Chroma, decision log, cache), git-ignored.
- `docs/PRD.md`, `docs/specs/`, `docs/decisions.md`: record every non-obvious design choice in decisions.md.

## Attribution
Code written with Claude Code is attributed: keep the `Co-Authored-By` trailer on commits, and list AI-assisted modules in `ATTRIBUTION.md`. Explain before writing anything the author could not explain themselves.
