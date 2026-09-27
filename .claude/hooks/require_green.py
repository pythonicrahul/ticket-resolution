#!/usr/bin/env python3
"""Stop hook: don't let Claude end a turn with red tests unless a row is genuinely BLOCKED.

Exit code 2 blocks the stop and feeds stderr back to Claude. `stop_hook_active` is set when Claude is
already continuing because of this hook, so we only push back once per stop (no infinite loops).

BLOCKED is the one escape hatch: step 5 of /next-feature says a blocked row is recorded and left
uncommitted, which means red tests. HUMAN does not qualify: a checkpoint row asks a human to choose a
threshold, it does not change code, so the suite must still be green. Narrower than it was, but still a
gate that file content can open, so it is dev tooling only: the system's own guardrails (FR-12) live in
src/ and no flag or file can switch them off.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

event = json.load(sys.stdin)
if event.get("stop_hook_active"):
    sys.exit(0)

backlog_file = REPO / "docs" / "BACKLOG.md"
backlog = backlog_file.read_text(encoding="utf-8") if backlog_file.exists() else ""
if "| BLOCKED |" in backlog:
    sys.exit(0)

result = subprocess.run(
    ["uv", "run", "pytest", "-q", "-x"], capture_output=True, text=True, check=False, cwd=REPO
)
if result.returncode != 0:
    tail = "\n".join(result.stdout.splitlines()[-25:])
    print("Tests are failing. Fix them, or follow step 5 of /next-feature and mark the row BLOCKED.\n" + tail,
          file=sys.stderr)
    sys.exit(2)
sys.exit(0)
