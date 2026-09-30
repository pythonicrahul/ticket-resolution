# ticketing-agent

A support system for CloudServe Solutions (Forward Deployed AI Engineering capstone). It answers tickets from CloudServe's own documentation when it can defend the answer, with citations, and escalates to a person, with a summary and the relevant articles attached, when it cannot. Every decision is logged.

> Status: built through the backlog in `docs/BACKLOG.md`. Requirements are in `docs/PRD.md`, one spec per requirement in `docs/specs/`, and every non-obvious choice in `docs/decisions.md`.
>
> **Start here for how it works:** [`docs/Implementation.md`](docs/Implementation.md) — the full technical account, with a diagram and a captured request/response for every API use case. Rendered as a single page in [`docs/Implementation.html`](docs/Implementation.html).

## Setup (from a clean checkout)

1. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or see https://docs.astral.sh/uv/).
2. Install dependencies: `uv sync`
3. Configure: `cp .env.example .env`, then set `LLM_API_KEY`. **Groq is the provider that works**:
   its free tier gives per-account limits, while OpenRouter's free endpoints share one pool that
   rejected every request when this was measured (D-47). `.env.example` carries the model ids and
   the reason for each.
4. **Train the classifier**: `uv run python scripts/train_classifier.py`. It fits on
   `data/development_tickets.json` and writes `storage/classifier.joblib` (about a minute). The
   harness never trains during a run, so without this it refuses to start — a missing model is a
   setup error, not a silent fallback.
5. Check: `uv run pytest -v` — 469 tests, no network and no API key needed.

No uv? `python3.14 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pip install -e .`

## Run

| What | Command |
|---|---|
| Full unattended evaluation (the gate) | `uv run python -m evaluation.harness --input data/validation_tickets.json --output evaluation/results/` |
| The same, with no model calls | add `--stub-pipeline` (ingest and retrieval only; for exercising the machinery) |
| API | `uv run uvicorn ticketing_agent.api:app` |
| Tests | `uv run pytest -v` |

The harness accepts any ticket file with the documented schema: point `--input` at it. No data file
name is hardcoded anywhere.

**Pace.** The free tier allows 8000 tokens a minute, and an answered ticket costs up to three model
calls (draft, grounding judge, handover), so a full 80-ticket run takes 20-30 minutes. Replies are
cached by prompt and content, so a second run over the same tickets is nearly free.

**Stopping it.** `touch storage/KILL_SWITCH` stops every automatic reply from the next ticket
onwards; every ticket then escalates with that reason recorded. Delete the file to resume (FR-16).

## Run it with Docker

`docker compose up --build` brings up the system and three windows into it:

| | | |
|---|---|---|
| the support system | <http://localhost:8000/docs> | submit a ticket, search, the escalation queue |
| the decision log | <http://localhost:8080> | every decision, browsable, **read-only** |
| Prometheus | <http://localhost:9090> | what it scraped |
| Grafana | <http://localhost:3000> | the dashboard in `ops/`, already provisioned |

Two one-off jobs sit behind a profile, so `up` never starts a training run or an evaluation by
surprise:

```
docker compose run --rm train    # fit the classifier into the shared volume (needed once)
docker compose run --rm gate     # a full unattended run; the report lands in ./evaluation/results
```

Your `.env` is read at run time and excluded from the build context, so no key is ever baked into
an image. `storage/` is a named volume shared by the services that need it, which is why the log
the API writes is the log the viewer shows — and why `touch`ing the kill switch works from the
host:

```
docker compose exec api touch /app/storage/KILL_SWITCH   # every ticket now escalates (FR-16)
docker compose exec api rm    /app/storage/KILL_SWITCH   # and back
```

`tests/test_ops_stack.py` checks the stack against the application: that Prometheus scrapes a path
the API serves, that the viewer is read-only and points at the real log, that every mounted file
exists, and that no service carries a key. A stack that has drifted fails quietly — empty panels, a
target permanently down — so it is worth the seven tests.

## Repository map

`src/ticketing_agent/` components · `evaluation/` harness and results · `prompts/` versioned prompt library · `docs/` requirements, specs, decisions · `tests/` tests and synthetic fixtures · `data/` pack datasets.

## Attribution

See `ATTRIBUTION.md`.
