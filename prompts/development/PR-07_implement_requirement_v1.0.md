---
id: PR-07
version: 1.0
category: Specification (implementation, development only)
serves: All FRs (one run per requirement)
model: Claude Code (development tool only; never called by the running system)
---

# PR-07 · Implementation prompt for Claude Code: tests first, then code, for one requirement.

**Inputs:** docs/specs/{FR-ID}.md, CLAUDE.md, the target module in src/.

**Output:** Failing tests, then code that makes them pass, a green test run, and one commit prefixed '{FR-ID}:'.

**How we know it worked:** The test output is shown in the session; CI passes on push.

**Known weaknesses:** Claude Code can widen scope or touch other requirements; the prompt tells it to stop and explain. You must understand the code: the pack says you will be asked about it.

## Prompt text

```text
Implement {FR-ID} as specified in docs/specs/{FR-ID}.md, following CLAUDE.md.

1. Write the acceptance tests T-{FR-ID}-n in tests/ first. Run them and confirm they fail.
2. Implement in src/{module}.py until they pass.
3. Tag every decision-log write from this code with requirement_ids ["{FR-ID}"].
4. Do not change the behaviour of other requirements. If you need to, stop and explain why first.
5. Run python -m pytest tests/ -v and show the result.
6. Commit with a message that starts "{FR-ID}:".
```

## Change history

- v1.0: first version.
