#!/usr/bin/env bash
# Video demo runner: one step per keypress, the same every time.
#
#   Terminal 1:  DECISION_LOG_PATH=./storage/demo.db uv run uvicorn ticketing_agent.api:app
#   Terminal 2:  demo/demo.sh            # all steps, pausing between them
#                demo/demo.sh 9          # start at step 9
#                AUTO=1 demo/demo.sh     # no pauses (rehearsal)
#
# Needs: curl and jq (brew install jq). Talks to BASE (default http://127.0.0.1:8000).
set -uo pipefail
cd "$(dirname "$0")/.."

BASE="${BASE:-http://127.0.0.1:8000}"
START="${1:-0}"
P=demo/payloads
KILL_SWITCH="${KILL_SWITCH_FILE:-./storage/KILL_SWITCH}"

bold() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
note() { printf '\033[2m%s\033[0m\n' "$*"; }
pause() { [[ "${AUTO:-0}" == "1" ]] && return; read -r -p $'\n\033[2m↵ next\033[0m ' _; }

# The fields worth reading on camera, in the order a support manager would read them.
SHOW='{ticket_id, decision, reason, intent, confidence, urgency, threshold_applied, explanation,
       citations, reply, summary, uncertainty}'

submit() {  # submit <payload file>
  note "POST /tickets  ← $1"
  local out code
  out=$(curl -sS -w '\n%{http_code}' -X POST "$BASE/tickets" \
        -H 'Content-Type: application/json' -d @"$1")
  code=$(tail -n1 <<<"$out"); out=$(sed '$d' <<<"$out")
  note "HTTP $code"
  if [[ "$code" == "200" ]]; then jq "$SHOW" <<<"$out"; else jq . <<<"$out" 2>/dev/null || echo "$out"; fi
}

step() {  # step <n> <title> ; returns 1 if this step is before START
  (( $1 < START )) && return 1
  bold "── Step $1 · $2"
  return 0
}

curl -sf "$BASE/health" >/dev/null || { echo "The API is not answering at $BASE. Start it first (see demo/README.md)."; exit 1; }

if step 0 "Is it up, and what is it enforcing?"; then
  curl -sS "$BASE/health" | jq .
  note "Look for: pipeline ready, documents_indexed 90, kill_switch false, thresholds 0.85 / 0.25."
  note "(Before the first request the pipeline is built lazily; the warm-up in demo/README.md fixes that.)"
  pause
fi

if step 1 "Agent search: customer words, not article words (FR-04)"; then
  note 'GET /search?q=my deployment keeps dying'
  curl -sS -G "$BASE/search" --data-urlencode "q=my deployment keeps dying" --data-urlencode "k=3" \
    | jq '{query, count, results: [.results[] | {rank, score, doc_id, title, heading}]}'
  note "The article is called 'Container deployments failing during the health check phase'."
  note "No shared keywords, still rank 1: that is semantic search (embeddings)."
  pause
fi

if step 2 "Answered: email, deployment rolls back (FR-11, FR-06)"; then
  submit "$P/01_answered_email_deploy.json"
  note "Read the reply: cited article, 'drafted automatically', and how to reach a person."
  pause
fi

if step 3 "Answered: chat, permissions question"; then submit "$P/02_answered_chat_permissions.json"; pause; fi
if step 4 "Answered: forum, API key returning 401"; then submit "$P/03_answered_forum_api_key.json"; pause; fi

if step 5 "Answered: docs comment, non-fluent English"; then
  submit "$P/04_answered_docs_comment_non_fluent.json"
  note "Same question as step 4 in broken English: same article, same answer."
  pause
fi

if step 6 "Guardrail: a draft the documentation could not support (FR-12)"; then
  submit "$P/05_grounding_block_webhook.json"
  note "Expected: escalate / ungrounded_draft. Confidence was high; grounding still said no."
  note "This one depends on what the model writes. If it answers instead, say so and move on:"
  note "steps 11 and 12 show guardrails that are rule-based and fire every time."
  pause
fi

if step 7 "Always a person: security incident (FR-09)"; then
  submit "$P/06_security_incident.json"
  note "No reply was drafted at all. The rule fires before any answer is written."
  pause
fi

if step 8 "Always a person: feature request (FR-09)"; then submit "$P/07_feature_request.json"; pause; fi

if step 9 "Money: a refund request (FR-03)"; then
  submit "$P/08_refund_request.json"
  note "Matched on the text ('refund'), not on the predicted intent: a misclassified refund still escalates."
  pause
fi

if step 10 "Money: a disputed charge, added after the review (FR-03, R7)"; then
  submit "$P/09_disputed_charge.json"
  note "Before the review fix this exact wording was auto-answered. Now: money_decision_required."
  pause
fi

if step 11 "Compliance-grade data question (FR-09, R7)"; then submit "$P/10_compliance_data_residency.json"; pause; fi

if step 12 "Guardrail: prompt injection (FR-12)"; then
  submit "$P/11_prompt_injection.json"
  note "The ticket text never reached a drafting prompt."
  pause
fi

if step 13 "Guardrail: a password in the ticket (NFR-04)"; then
  submit "$P/12_credential_in_ticket.json"
  note "Caught before any prompt is built: the password was never sent to the model provider."
  pause
fi

if step 14 "Malformed input: rejected with every defect named (FR-07)"; then submit "$P/13_malformed_ticket.json"; pause; fi

if step 15 "Kill switch: stop all automatic replies, no restart (FR-16)"; then
  mkdir -p "$(dirname "$KILL_SWITCH")"; touch "$KILL_SWITCH"
  note "touch $KILL_SWITCH"
  curl -sS "$BASE/health" | jq '{kill_switch}'
  submit "$P/14_kill_switch_check.json"
  note "Same ticket that was answered in step 2. Now: escalate / kill_switch."
  rm -f "$KILL_SWITCH"; note "rm $KILL_SWITCH  (switch off again)"
  curl -sS "$BASE/health" | jq '{kill_switch}'
  pause
fi

if step 16 "The escalation queue a tier-two engineer reads (FR-05, FR-01)"; then
  curl -sS "$BASE/queue?limit=20" | jq '.items[] | {ticket_id, urgency, reason, summary, detail}'
  note "Urgency first, then oldest. Every item carries a summary: no raw forwarded tickets."
  pause
fi

if step 17 "Metrics, computed from the decision log (FR-14, NFR-05)"; then
  curl -sS "$BASE/metrics" | jq .
fi

bold "Demo complete. Every decision above is in the decision log the API was started with."
