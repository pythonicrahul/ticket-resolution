# ticketing-agent

A support system for CloudServe Solutions (Forward Deployed AI Engineering capstone). It answers tickets from CloudServe's own documentation when it can defend the answer, with citations, and escalates to a person, with a summary and the relevant articles attached, when it cannot. Every decision is logged.

> Status: built through the backlog in `docs/BACKLOG.md`. Requirements are in `docs/PRD.md`, one spec per requirement in `docs/specs/`, and every non-obvious choice in `docs/decisions.md`.
>
> **Start here for how it works:** [`docs/Implementation.md`](docs/Implementation.md) — the full technical account, with a diagram and a captured request/response for every API use case. Rendered as a single page in [`docs/Implementation.html`](docs/Implementation.html).

## Setup (from a clean checkout)

1. Install uv: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or see https://docs.astral.sh/uv/).
2. Install dependencies: `uv sync`
3. Configure: `cp .env.example .env`, then set `LLM_API_KEY`. **The default provider is OpenAI**:

   ```
   LLM_BASE_URL=https://api.openai.com/v1
   MODEL_NAME=gpt-4o-mini          # drafting (PR-01) and the handover note (PR-02)
   JUDGE_MODEL_NAME=gpt-4.1-mini   # the grounding check (PR-03), deliberately a different model
   ```

   **Why a paid provider, when the Build Specification says free tiers only:** the free tiers
   throttled so heavily that development and testing became very challenging. OpenRouter's free
   endpoints refused 20 consecutive requests from a shared pool (D-46), and Groq's free tier
   escalated 21 of 80 tickets without attempting them — still 8 of 65 after the client learned to
   pace itself (D-54). Gate runs were unrepeatable and the numbers measured throttling rather than
   quality. This is raised rather than done quietly: D-55 amends NFR-07, and a full 80-ticket run
   costs about **$0.03**.

   `.env.example` also sets the paths the project needs — `DOCS_PATH`,
   `TRAINING_TICKETS_PATH`, `CHROMA_PATH`, `DECISION_LOG_PATH`, `CLASSIFIER_PATH`. Copying it is
   enough; if you edit `.env` by hand, keep `TRAINING_TICKETS_PATH`, because step 4 needs it and
   so does the report's "classification by wording" section (R8).

   **The free route still works** if you prefer it. `.env.example` carries the values commented
   out, with the model ids for each: `api.groq.com` (per-account limits, the better of the two)
   and `openrouter.ai` (a shared pool, which refused every request when it was measured). A throttled run **escalates rather than fails**
   (FR-15, A11): every ticket still ends as a logged decision, with reason `provider_unavailable`
   for the ones the provider would not take.
4. **Train the classifier**: `uv run python scripts/train_classifier.py`. It fits on
   `data/development_tickets.json` and writes `storage/classifier.joblib` (about a minute). The
   harness never trains during a run, so without this it refuses to start — a missing model is a
   setup error, not a silent fallback.
5. Check: `uv run pytest -v` — the full suite runs with no network and no API key. (No count is quoted here on purpose: it goes stale the next time a test is added.)

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

**Pace.** How many model calls a ticket costs depends on how far it gets, which is NFR-07's point:

| ticket | calls | which |
|---|---|---|
| answered | 2 | draft (PR-01), grounding judge (PR-03) |
| escalated by rule, before drafting | **1** | handover note (PR-02) only |
| drafted, then refused by the drafter | 2 | draft, handover |
| drafted, then blocked by the grounding check | 3 | draft, judge, handover |

Those are floors. `complete_structured` is allowed **one repair attempt** when a reply does not
parse, and the transport retries a timeout or a 5xx up to `LLM_MAX_RETRIES` — and `model_calls`
counts provider *requests*, so a ticket can cost three or four. A figure above
`2 × answered + escalations` is the retries, not broken accounting.

A measured 80-ticket run on OpenAI (`api.openai.com`, `gpt-4o-mini` + `gpt-4.1-mini`) made
**155 calls** — 1.94 per ticket, not 3 — because 14 tickets escalated
by rule and never reached the drafter. It took **319 s** and about **$0.03** — measured on 2026-10-01 with `gpt-4o-mini` for both
roles, before `JUDGE_MODEL_NAME` was wired (D-74), so a run with an independent judge costs
somewhat more. No committed report carries a token or cost figure; the arithmetic is in D-68. Its latency was
median **4.1 s** and p95 **6.5 s** per ticket, which **misses NFR-01's 3-second target**; the figure
and its cause are in every report's gaps list (D-68). On a free tier the same run takes 20–30
minutes if it finishes at all, because the per-minute limit binds rather than the model.

Replies are cached by prompt and content, so a second run over the same tickets makes no provider
call and is free. A cached run is **not a timing measurement** and the report marks it as a replay;
use `--no-cache` for timing, which neither reads nor writes the cache (D-68).

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
target permanently down — so the checks are worth having.

## Repository map

`src/ticketing_agent/` components · `evaluation/` harness and results · `prompts/` versioned prompt library · `docs/` requirements, specs, decisions · `tests/` tests and synthetic fixtures · `data/` pack datasets.

## Attribution

See `ATTRIBUTION.md`.
