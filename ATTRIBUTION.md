# Attribution

Code, prompts and documents in this repository were written with the help of AI tools. Each entry says what, where, and what was changed by hand.

| What | Where | Tool | Human changes |
|---|---|---|---|
| Project skeleton, CLAUDE.md, prompt drafts, PRD draft | repo root, prompts/, docs/ | Claude | Reviewed and edited by the author |
| FR-07 ingest: spec, `Ticket` representation, normalisation, acceptance tests, synthetic fixtures | `docs/specs/FR-07.md`, `src/ticketing_agent/ingest.py`, `tests/test_fr07_ingest.py`, `tests/fixtures/malformed_tickets.json` | Claude Code (build loop, PR-06/PR-07), reviewed by a second Claude Code session (PR-08) | Author read the spec against the PRD, decided the blocking-defect and duplicate-id rules, and the open questions in the spec are the author's to answer |

Commits written with Claude Code keep their `Co-Authored-By` trailer.
