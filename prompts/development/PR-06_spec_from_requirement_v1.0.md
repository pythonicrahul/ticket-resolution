---
id: PR-06
version: 1.0
category: Specification
serves: All FRs (one run per requirement)
model: Claude Code (development tool only; never called by the running system)
---

# PR-06 · Specification prompt for Claude Code: turns one PRD requirement into a written spec before any code.

**Inputs:** docs/PRD.md, CLAUDE.md, and the requirement ID.

**Output:** docs/specs/{FR-ID}.md with purpose, inputs and outputs, decision rules, failure behaviour, decision-log fields, and numbered acceptance tests T-{FR-ID}-n.

**How we know it worked:** You read the spec against the PRD row before implementing. Every acceptance criterion in the PRD appears as a test.

**Known weaknesses:** Claude Code may fill ambiguity with its own choices; the prompt makes it list open questions instead. You remain accountable for the spec.

## Prompt text

```text
Read CLAUDE.md and docs/PRD.md. Write the specification for {FR-ID} in docs/specs/{FR-ID}.md. Do not write any code.

Include:
1. Purpose: one sentence, quoting the requirement.
2. Inputs and outputs, with exact field names and types.
3. The decision rules, in order.
4. Failure behaviour: what happens with missing data, an empty retrieval, a model provider error, or invalid model output.
5. Which decision-log fields this component writes.
6. Numbered acceptance tests named T-{FR-ID}-1, T-{FR-ID}-2 and so on, each one checkable by a pytest test without network access.

Where the PRD is ambiguous, list the question under "Open questions" rather than choosing silently.
```

## Change history

- v1.0: first version.
