# ticketing-agent

A support system for CloudServe Solutions (Forward Deployed AI Engineering capstone). It answers tickets from CloudServe's own documentation when it can defend the answer, with citations, and escalates to a person, with a summary and the relevant articles attached, when it cannot. Every decision is logged.

> Status: project skeleton. Components are specified in `docs/PRD.md` and built one requirement at a time.

## Setup (from a clean checkout)

1. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or see https://docs.astral.sh/uv/).
2. Install dependencies: `uv sync`
3. Configure: `cp .env.example .env`, then set `LLM_API_KEY` (a free OpenRouter or Groq key).
4. Check: `uv run pytest -v`

No uv? `python3.14 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pip install -e .`

## Run

| What | Command |
|---|---|
| Full unattended evaluation (the gate) | `uv run python -m evaluation.harness --input data/validation_tickets.json --output evaluation/results/` |
| API | `uv run uvicorn ticketing_agent.api:app` |
| Tests | `uv run pytest -v` |

The harness accepts any ticket file with the documented schema: point `--input` at it.

## Repository map

`src/ticketing_agent/` components · `evaluation/` harness and results · `prompts/` versioned prompt library · `docs/` requirements, specs, decisions · `tests/` tests and synthetic fixtures · `data/` pack datasets.

## Attribution

See `ATTRIBUTION.md`.
