"""Stop hook: don't let Claude end a turn with red tests unless it has marked the item BLOCKED or HUMAN.

Exit code 2 blocks the stop and feeds stderr back to Claude. `stop_hook_active` is set when Claude is
already continuing because of this hook, so we only push back once per stop (no infinite loops).
"""
import json
import subprocess
import sys
from pathlib import Path

event = json.load(sys.stdin)
if event.get("stop_hook_active"):
    sys.exit(0)

backlog = Path("docs/BACKLOG.md").read_text(encoding="utf-8") if Path("docs/BACKLOG.md").exists() else ""
if "| BLOCKED |" in backlog or "| HUMAN |" in backlog:
    sys.exit(0)

result = subprocess.run(["uv", "run", "pytest", "-q", "-x"], capture_output=True, text=True)
if result.returncode != 0:
    tail = "\n".join(result.stdout.splitlines()[-25:])
    print("Tests are failing. Fix them, or follow step 5 of /next-feature and mark the row BLOCKED.\n" + tail,
          file=sys.stderr)
    sys.exit(2)
sys.exit(0)
