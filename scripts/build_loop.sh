#!/usr/bin/env bash
# Runs /next-feature repeatedly, one backlog row per fresh Claude Code session, until done or a human is needed.
# Usage: scripts/build_loop.sh [max_iterations]
set -euo pipefail
cd "$(dirname "$0")/.."
MAX="${1:-20}"
mkdir -p logs
PROMPT="$(sed '1,/^---$/{/^---$/!d}; 1,/^---$/d' .claude/commands/next-feature.md)"

for i in $(seq 1 "$MAX"); do
  if grep -qE '\| (BLOCKED|HUMAN) \|' docs/BACKLOG.md; then
    echo "Stopped: a row needs a human (see docs/BACKLOG.md and docs/PROGRESS.md)."; exit 0
  fi
  if ! grep -q '| TODO |' docs/BACKLOG.md; then
    echo "Backlog complete."; exit 0
  fi
  echo "=== iteration $i  $(date '+%F %T') ==="
  claude -p "$PROMPT" --permission-mode acceptEdits --max-turns 120 2>&1 | tee -a "logs/loop_$(date +%F).log"
  if ! uv run pytest -q >/dev/null 2>&1 && ! grep -qE '\| (BLOCKED|HUMAN) \|' docs/BACKLOG.md; then
    echo "Tests are red but no row was marked BLOCKED. Stopping for a human."; exit 1
  fi
done
echo "Reached $MAX iterations."
