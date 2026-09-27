---
id: PR-08
version: 1.0
category: Review
serves: All FRs; NFR-04, NFR-05
model: Claude Code (development tool only; never called by the running system). Run in a new session, not the one that wrote the code.
---

# PR-08 · Review prompt for Claude Code: checks a finished component against its requirement, in a fresh session.

**Inputs:** docs/PRD.md, docs/specs/{FR-ID}.md, and the diff or files for {FR-ID}.

**Output:** A list of findings, most severe first, each with file and line. No fixes.

**How we know it worked:** Findings are fixed or recorded as accepted before the component counts as done (sprint plan definition of done).

**Known weaknesses:** An AI reviewer misses things too. Use it to add to your own reading, not to replace it.

## Prompt text

```text
Review the implementation of {FR-ID} against docs/PRD.md and docs/specs/{FR-ID}.md. Do not change any files. List findings only, most severe first, each with a file and line number.

Check for:
1. Any acceptance criterion with no test.
2. Any path where a ticket could be dropped, or a decision taken without being logged.
3. Any place where customer text reaches a model prompt without being delimited as data.
4. Any hardcoded file path, data file name, key or model name.
5. Any guardrail that a flag, environment variable or exception handler can switch off.
6. Any behaviour that differs between two runs on the same input.
```

## Change history

- v1.0: first version.
