# Demo runbook

Everything needed to run the live demonstration for the submission video, the same way every time.
The full narration (slides and demo) is in [`VIDEO_SCRIPT.md`](VIDEO_SCRIPT.md).

## What the assessment asks the demo to show

At least **seven minutes** of the system running on real tickets, including:

| Required | Where in this demo |
|---|---|
| a success (an answered ticket) | steps 2–5 |
| an escalation | steps 7–11 |
| a guardrail blocking something | steps 6, 12, 13 |
| part of the unattended run over the evaluation set | Terminal 3, started at the beginning, finished at the end |

## The day before: rehearse once, end to end

```bash
cd ~/Code/ticketing-agent
uv sync
cp -n .env.example .env                      # then put your OpenAI key in LLM_API_KEY
uv run python scripts/train_classifier.py    # once; writes storage/classifier.joblib
brew install jq                              # demo.sh uses it to show only the useful fields
```

Then do a full dress rehearsal (the steps below) and note anything that differs from the
"What you should see" table. The model's wording changes a little between runs; the
**decision** and **reason** columns should not, except step 6 (see the table).

## Recording setup

- Three terminal tabs, font size **18 or larger** (code that cannot be read on playback counts as not shown).
- A browser tab with `docs/diagrams/architecture.html` open (for the "how it flows" moment).
- Close notifications. Hide your `.env` file: never `cat` it on screen.

**Terminal 1: the API, on a fresh decision log just for the video**

```bash
DECISION_LOG_PATH=./storage/demo.db uv run uvicorn ticketing_agent.api:app
```

Warm it up once **before you press record** (the first request builds the search index and loads the classifier):

```bash
curl -s "http://127.0.0.1:8000/search?q=warm+up" > /dev/null
```

**Terminal 3: the unattended evaluation run** (start it at the beginning of the demo, it takes ~5 minutes):

```bash
uv run python -m evaluation.harness \
  --input data/validation_tickets.json \
  --output evaluation/results/video-run \
  --decision-log storage/video-run.db \
  --run-id video-run
```

**Terminal 2: the demo**

```bash
demo/demo.sh          # one step per Enter key
demo/demo.sh 9        # restart from step 9 if you fluff a take
```

## What you should see

| Step | Payload | Expected decision | Expected reason | What to point at |
|---|---|---|---|---|
| 0 | `GET /health` | — | — | thresholds 0.85 / 0.25, kill switch off |
| 1 | `GET /search` "my deployment keeps dying" | — | — | DOC-DEPLOY-001 at rank 1 with no shared keywords |
| 2 | `01_answered_email_deploy.json` | auto_respond | — | citation, "drafted automatically", route to a person |
| 3 | `02_answered_chat_permissions.json` | auto_respond | — | a different channel, same pipeline |
| 4 | `03_answered_forum_api_key.json` | auto_respond | — | |
| 5 | `04_answered_docs_comment_non_fluent.json` | auto_respond | — | broken English, same article as step 4 |
| 6 | `05_grounding_block_webhook.json` | escalate | ungrounded_draft | high confidence, still blocked. **Model-dependent**: it escalated in both recorded runs, but if it answers, say so honestly |
| 7 | `06_security_incident.json` | escalate | must_escalate_intent | no reply drafted at all |
| 8 | `07_feature_request.json` | escalate | must_escalate_intent | |
| 9 | `08_refund_request.json` | escalate | money_commitment_requested | matched trigger: refund |
| 10 | `09_disputed_charge.json` | escalate | money_decision_required | this exact text was auto-answered before review row R7 |
| 11 | `10_compliance_data_residency.json` | escalate | compliance_data_question | "our primary region", "compliance review" |
| 12 | `11_prompt_injection.json` | escalate | instruction_injection_detected | markers: ignore all previous, reveal your, system prompt |
| 13 | `12_credential_in_ticket.json` | escalate | private_data_in_ticket | pattern name logged, the password never sent to the model |
| 14 | `13_malformed_ticket.json` | HTTP 400 | — | every defect named |
| 15 | kill switch + `14_kill_switch_check.json` | escalate | kill_switch | same ticket as step 2, now escalated; switch off again |
| 16 | `GET /queue` | — | — | urgency first, every item has a summary |
| 17 | `GET /metrics` | — | — | counts come from the decision log |

Steps 9–13 are decided by rules on the ticket text, so they behave identically on every run.
Steps 7, 8 and 11 depend on the classifier, which was trained on this wording, so they are stable too.
Steps 2–6 call the model (about $0.001 each).

## Showing the evidence (optional, strong for the governance section)

The decision log, one row per decision, in the order they were taken:

```bash
sqlite3 -header -column storage/demo.db \
  "select ticket_id, stage, decision, reason, intent, round(intent_confidence,2) as conf, urgency
   from decisions order by row_id;"
```

When Terminal 3 finishes, open the report it wrote:

```bash
open evaluation/results/video-run/metrics.md      # or: less evaluation/results/video-run/metrics.md
```

Point at: tickets processed (80), "Log reconciles with tickets processed: yes", the reasons table,
and the latency block (a live run, not a replay).

## If something goes wrong on camera

| Symptom | Fix |
|---|---|
| `The API is not answering` | Terminal 1 is not running, or still starting. Wait for `Uvicorn running on…`. |
| Every ticket escalates with `provider_unavailable` | The key is missing or wrong in `.env`, or the network dropped. Check the key, restart Terminal 1. |
| The API refuses to start: classifier missing | Run `uv run python scripts/train_classifier.py` once. |
| Step 6 answers instead of escalating | Expected sometimes. Say: "this guardrail judges the model's own draft, so it varies; the next two are rules and never vary." |
| Every ticket escalates `kill_switch` | A leftover switch file: `rm storage/KILL_SWITCH`. |
| The queue shows old tickets | Restart Terminal 1 with a new `DECISION_LOG_PATH`, e.g. `./storage/demo2.db`. |
