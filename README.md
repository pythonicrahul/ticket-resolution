# ticketing-agent

A support system for CloudServe Solutions (Forward Deployed AI Engineering capstone). It answers a
ticket from CloudServe's own documentation when it can defend the answer — with citations — and
escalates to a person, with a summary and the relevant articles attached, when it cannot. Every
decision is written to a log **before** the action is taken.

**It is not an agent.** It is a fixed LangGraph pipeline: classify → retrieve → route → draft →
five guardrails → send or escalate. The model never chooses the next step, which is what makes the
routing auditable and the same input reach the same decision.

| | |
|---|---|
| How it works, in detail | [`docs/Implementation.md`](docs/Implementation.md) · [rendered](docs/Implementation.html) |
| Requirements | [`docs/PRD.md`](docs/PRD.md) — FR-01…FR-16, NFR-01…NFR-09, one spec each in [`docs/specs/`](docs/specs) |
| Why anything is the way it is | [`docs/decisions.md`](docs/decisions.md) — every non-obvious choice, numbered |
| What was built, in order | [`docs/PROGRESS.md`](docs/PROGRESS.md), [`docs/BACKLOG.md`](docs/BACKLOG.md), [`docs/REVIEW_BACKLOG.md`](docs/REVIEW_BACKLOG.md) |
| Demo and video | [`demo/README.md`](demo/README.md) · [`demo/VIDEO_SCRIPT.md`](demo/VIDEO_SCRIPT.md) · [slides](demo/CloudServe_Capstone_Presentation.pptx) |

---

## Run it

> **You need your own OpenAI API key.** None ships with this repo; `.env.example` has
> `LLM_API_KEY=` empty. Get one at <https://platform.openai.com/api-keys> — a full 80-ticket
> evaluation run costs about $0.03.
>
> Without a key the test suite, `--stub-pipeline` and `/search` all still work, and a run still
> completes: every ticket escalates `provider_unavailable` and is logged (FR-15). That is correct
> behaviour, not a result. A free provider is in step 3 of Setup.

Four commands from a clean clone, run from the repository root. Nothing else is needed.

```bash
# 1. install uv (skip if you have it), then open a NEW terminal so it is on your PATH
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. install Python 3.14 and every dependency into .venv
uv sync

# 3. configure
cp .env.example .env
#    now open .env and put your key on the LLM_API_KEY line, so it reads:
#      LLM_API_KEY=<paste your OpenAI key here, no quotes>
#    nothing else in the file needs changing

# 4. train the ticket classifier (about a minute, needed once)
uv run python scripts/train_classifier.py
```

Check it worked. The suite needs no network and no API key, so it passes before you add one:

```bash
uv run pytest -q          # every test green
uv run ruff check .       # "All checks passed!"
```

### Then choose one

**A. Serve it, and submit a ticket.**

```bash
uv run uvicorn ticketing_agent.api:app
```

Open <http://127.0.0.1:8000/docs> to try every endpoint from the browser, or in a second terminal:

```bash
curl -s -X POST http://127.0.0.1:8000/tickets \
  -H 'Content-Type: application/json' -d @demo/payloads/01_answered_email_deploy.json
```

You should get `"decision": "auto_respond"`, a `reply` that names the article it used, and
`citations` holding the chunk ids it actually retrieved. **The first request is slow** — it builds
the search index and downloads the embedding model once; after that a ticket takes about four
seconds, nearly all of it waiting on the model.

Fourteen ready-made tickets are in `demo/payloads/`, one per case — answered, escalated, blocked by
a guardrail, prompt injection, a credential in the ticket body, malformed, and the kill switch.

**B. Run the full unattended evaluation (the gate).**

```bash
uv run python -m evaluation.harness \
  --input data/validation_tickets.json \
  --output evaluation/results/my-run
```

About five minutes for 80 tickets. It writes `metrics.md`, `metrics.json` and `outcomes.jsonl` into
the output directory, and the report must contain `Log reconciles with tickets processed: yes`.
**Point `--input` at any ticket file with the documented schema** — no data file name is hardcoded
anywhere, which is deliberate and tested.

**C. Walk through every case, narrated.**

```bash
demo/demo.sh          # in a second terminal, with the API running
```

One case per Enter key. What each should produce is in [`demo/README.md`](demo/README.md).

If anything does not match, go to [Troubleshooting](#troubleshooting).

---

## What you need

| | |
|---|---|
| OS | macOS or Linux (Windows through WSL) |
| Tools | `git`, `curl`, and [uv](https://docs.astral.sh/uv/), which installs Python 3.14 for you |
| An API key | **your own OpenAI key**, from <https://platform.openai.com/api-keys> — about $0.03 for a full evaluation run. See step 3 for why it is paid, and for the free alternative |
| Network | the first run downloads Python, the packages and the ~80 MB embedding model; after that, search and classification run locally |
| Disk | about 2 GB |

No uv? `python3.14 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
&& pip install -e .`, then use `python` wherever this file says `uv run python`.

## Setup, explained

1. **Install uv**: `curl -LsSf https://astral.sh/uv/install.sh | sh` (or see
   <https://docs.astral.sh/uv/>). Open a new terminal afterwards so `uv` is on your `PATH`.
2. **Install dependencies**: `uv sync`. Creates `.venv` and installs Python 3.14 if you don't have
   it, from `uv.lock`, so you get the versions this was built and measured against.
3. Configure: `cp .env.example .env`, then **put your own OpenAI key on the `LLM_API_KEY` line**
   (<https://platform.openai.com/api-keys>). It is the only value you have to supply; the file
   ships with every other setting filled in. **The default provider is OpenAI**:

   ```
   LLM_API_KEY=                    # <- your key goes here, and nowhere else
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
   and `openrouter.ai` (a shared pool, which refused every request when it was measured). A
   throttled run **escalates rather than fails** (FR-15, A11): every ticket still ends as a logged
   decision, with reason `provider_unavailable` for the ones the provider would not take.
4. **Train the classifier**: `uv run python scripts/train_classifier.py`. It fits on
   `data/development_tickets.json` and writes `storage/classifier.joblib` (about a minute), plus
   the out-of-fold report `evaluation/reports/classifier_calibration.md`. The harness never trains
   during a run, so without this it refuses to start — a missing model is a setup error, not a
   silent fallback.
5. **Check**: `uv run pytest -v`. The full suite runs with no network and no API key, from recorded
   fixtures. (No count is quoted here on purpose: it goes stale the next time a test is added.)

## The API

| Endpoint | Purpose |
|---|---|
| `GET /health` | thresholds in force, kill-switch state, and whether anything it is configured to load is missing |
| `GET /search?q=…&k=…` | ranked documentation passages for an agent (FR-04) |
| `POST /tickets` | one ticket through the full pipeline: `auto_respond` with a cited reply, or `escalate` with a handover summary |
| `GET /queue` | escalated tickets, ordered by urgency then age (FR-05) |
| `GET /metrics`, `GET /metrics/prometheus` | operational figures, computed from the decision log |

```bash
# what the service is enforcing (thresholds, kill switch, what is missing)
curl -s http://127.0.0.1:8000/health

# search the documentation the way an agent would
curl -s -G http://127.0.0.1:8000/search --data-urlencode "q=my deployment keeps dying" --data-urlencode "k=3"

# submit a ticket
curl -s -X POST http://127.0.0.1:8000/tickets \
  -H 'Content-Type: application/json' -d @demo/payloads/01_answered_email_deploy.json

# what is waiting for a person, most urgent first
curl -s http://127.0.0.1:8000/queue

# counts from the decision log; Prometheus format at /metrics/prometheus
curl -s http://127.0.0.1:8000/metrics
```

`/health` answers immediately and deliberately builds nothing, so it reports `pipeline: not built`
until the first ticket arrives — that is a healthy state, not a fault. To keep a separate decision
log (for a demo, say), start the API with
`DECISION_LOG_PATH=./storage/demo.db uv run uvicorn ticketing_agent.api:app`; a variable set on the
command line wins over `.env`.

**The API is unauthenticated** and `/queue` returns customer-derived text. That is recorded in
FR-04 §7, and an agent UI and auth are out of scope in the PRD.

## The unattended evaluation run

```bash
uv run python -m evaluation.harness --input data/validation_tickets.json --output evaluation/results/my-run
```

It processes every ticket even if some fail, writes every decision to the log before acting, checks
that the log reconciles with the tickets processed, and writes:

| File | What it holds |
|---|---|
| `metrics.md` | the human report: volume, business outcomes, technical and governance figures, segment tables, and an explicit list of what the run does **not** measure |
| `metrics.json` | the same figures, machine-readable |
| `outcomes.jsonl` | one line per ticket: the decision, the reason, the exact reply sent or the handover note |

Useful options: `--limit N` (first N tickets), `--run-id NAME`, `--decision-log PATH`,
`--stub-pipeline` (ingest and retrieval only, no model calls — for exercising the machinery), and
`--no-cache` for a timing run (see Pace).

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

## Results you can read without running anything

Three gate runs are committed, so the figures below can be checked against their own artefacts:

| | |
|---|---|
| The reference run — 80 validation tickets, every response live | [`evaluation/results/kept-gate-2026-10-02/metrics.md`](evaluation/results/kept-gate-2026-10-02/metrics.md) |
| The same file under a path the repo had never seen, and a replay of it | `…-renamed/`, `…-replay/` |
| What the three say when compared | [`evaluation/reports/gate-2026-10-02-checkpoint.md`](evaluation/reports/gate-2026-10-02-checkpoint.md) |
| The two-assessor review sheet, ready to fill in | [`evaluation/reports/review_sample_2026-10-02.csv`](evaluation/reports/review_sample_2026-10-02.csv) |
| Out-of-fold classifier measurement | [`evaluation/reports/classifier_calibration.md`](evaluation/reports/classifier_calibration.md) |

From the reference run: **42 of 80 answered, 38 escalated, 3 blocked** by a guardrail; 80 decisions
logged against 80 tickets with no row missing, extra or duplicated; 0 private-data detections and 0
citations that do not resolve.

And what it does not reach, stated plainly because a report that only carries its good numbers is
not evidence:

| | target | measured |
|---|---|---|
| First contact resolution (proxy) | ≥60% | **52.5%** |
| Escalation rate | ≤30% | **47.5%** |
| Automated-path latency, p95 | <3 s | **5.6 s** — two provider round trips per answered ticket |
| Cross-segment variation | <5 points | **35 points** on tier, driven by a segment of 8 |
| Hallucination rate, citation accuracy | human review of ≥50 replies | **not done** — the sheet above is ready, the run answered 42 |

**One number to read carefully.** Two live runs of this same commit, hours apart, routed **7 of 80
tickets differently** — each on a reason that reads the model's exact words. A fully replayed run
reproduces its predecessor exactly: 0 of 80 decisions differ and all 80 replies are byte-identical.
So the routing is reproducible from the cache and not from the provider, and any single live figure
carries a ±3 ticket spread. That is measured, not estimated (D-81).

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `uv: command not found` | Open a new terminal after installing uv, or add `~/.local/bin` to `PATH`. |
| The harness or API refuses to start: classifier missing | Run setup step 4 once. |
| It refuses to start: `LLM_API_KEY` or `MODEL_NAME` missing | Setup step 3: copy `.env.example` to `.env` and set the key. |
| `/health` says `pipeline: not built` | Expected before the first ticket. It builds nothing on purpose, so the healthcheck cannot time out. |
| Every ticket escalates with `provider_unavailable` | The key is wrong, the network is down, or the provider is throttling. The run still completes; fix the key and run again. |
| Every ticket escalates with `kill_switch` | A switch file was left behind: `rm storage/KILL_SWITCH`. |
| The first request is slow | It builds the documentation index and downloads the embedding model once. |
| A second run is suspiciously fast | It was served from the response cache. That is expected; use `--no-cache` for timings. |

## Run it with Docker

> Optional. The uv path above is the supported one. This stack was built and run end to end on
> 2026-10-02 (review row R11) — a ticket through the documented endpoint, the decision log read
> back from the host, the kill switch, and the Prometheus scrape — and it is verified *as of that
> row*, not continuously: CI has no Docker, so the tests parse the compose file rather than build
> the image.

`docker compose up --build` brings up the system and three windows into it:

| | | |
|---|---|---|
| the support system | <http://localhost:8000/docs> | submit a ticket, search, the escalation queue |
| the decision log | <http://localhost:8080> | every decision, browsable, **read-only** |
| Prometheus | <http://localhost:9090> | what it scraped |
| Grafana | <http://localhost:3000> | the dashboard in `ops/`, already provisioned |

**The first run is slow, and only the first.** The embedding model (all-MiniLM-L6-v2, 79 MB) is
downloaded on first use and the Chroma index is built on the first request, so a cold `train` or a
cold first ticket takes minutes rather than seconds. Both land in volumes, so it happens once per
machine and not once per run — it was once per run until review row R11 gave the model cache a
volume (D-80). `/health` deliberately does **not** trigger either, so Docker's healthcheck passes
while the container is still warming up; it reports `pipeline: not built` until the first ticket.

Two one-off jobs sit behind a profile, so `up` never starts a training run or an evaluation by
surprise:

```
docker compose run --rm train    # fit the classifier into the shared volume (needed once)
docker compose run --rm gate     # a full unattended run; the report lands in ./evaluation/results
```

Your `.env` is read at run time and excluded from the build context, so no key is ever baked into
an image. `storage/` is a **bind mount** on this repository's own `storage/` directory, shared by
the services that need it — which is why the log the API writes is the log the viewer shows, and
why FR-16's kill switch works from the host with no `exec` at all:

```
touch storage/KILL_SWITCH   # every ticket now escalates, from the next one onwards (FR-16)
rm    storage/KILL_SWITCH   # and back
```

It was a *named* volume until review row R11. A named volume lives inside Docker's own storage
area — inside the VM on macOS — so the host cannot reach it, and an emergency control an operator
cannot operate is not one. The evaluation inputs are **not** in the image: the agents' own answer
file must not ship to a serving container (FR-04 §3.1), so `gate` mounts `./data` read-only, which
also means a file nobody has seen can be dropped in and named with `--input`.

> **The three windows have no authentication.** `:8080` serves the entire decision log — including
> the exact text sent to customers — and `:3000` runs Grafana as an anonymous admin, both published
> on all interfaces. That is fine on a laptop and is not fine anywhere else; putting this stack on a
> reachable host needs a password or a bound interface first. The API itself is unauthenticated too
> (FR-04 §7, and an agent UI is out of scope in the PRD).

`tests/test_ops_stack.py` checks the stack against the application: that Prometheus scrapes a path
the API serves, that the viewer is read-only and points at the real log, that every mounted file
exists, and that no service carries a key. A stack that has drifted fails quietly — empty panels, a
target permanently down — so the checks are worth having.

## Repository map

| Path | What is there |
|---|---|
| `src/ticketing_agent/` | the components: ingest, classify, retrieve, route, generate, guardrails, handover, provider, decision log, pipeline, API |
| `evaluation/` | the unattended harness, the committed runs under `results/kept-*`, and the reports |
| `prompts/` | the versioned prompt library and its register |
| `docs/` | requirements, specs, decisions, implementation notes, diagrams, backlogs and progress log |
| `demo/` | demo payloads, the demo runner, the demo runbook and the video script |
| `tests/` | the test suite and synthetic fixtures |
| `data/` | the pack's datasets |
| `scripts/` | training, sweeps, the review-sample sheet, the gate comparison and other one-off tools |
| `storage/` | runtime state: Chroma index, decision log, response cache, classifier (git-ignored) |

## Attribution

Written with Claude Code; see [`ATTRIBUTION.md`](ATTRIBUTION.md) for which modules, and
`Co-Authored-By` trailers on every commit.
