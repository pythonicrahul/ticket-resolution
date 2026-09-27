# Build backlog

The build loop works top to bottom: it takes the first `TODO` row whose dependencies are all `DONE`.
Status values: `TODO` · `DONE` · `BLOCKED` (tried and failed; human needed) · `HUMAN` (checkpoint reached; human must review, then set to DONE).
Only the loop changes TODO → DONE/BLOCKED/HUMAN. Only a human changes BLOCKED/HUMAN → TODO or DONE.

| # | Item | Requirements | Depends on | Kind | Status |
|---|---|---|---|---|---|
| 1 | B-02 Ingest and normalise four channels | FR-07 | — | build | TODO |
| 2 | B-17 Synthetic test tickets (PII, injection, refund/dispute, malformed, empty) in tests/fixtures/ | FR-03, FR-07, FR-12 | 1 | build | TODO |
| 3 | B-10 Decision log (SQLite, Governance schema, reconciliation helper) | FR-13 | 1 | build | TODO |
| 4 | Provider client: timeout, retries, backoff, circuit breaker, response cache, fake provider for tests | FR-15 | — | build | TODO |
| 5 | B-03/B-04 Chunking, Chroma index, retrieval with relevance threshold; threshold sweep script writing evaluation/reports/retrieval_sweep.md | FR-10 | 1 | build | TODO |
| 6 | B-05 Harness: --input/--output, per-ticket isolation, metrics.json + metrics.md with segment tables, stub pipeline | FR-14 | 1, 3 | build | TODO |
| 7 | CHECKPOINT: retrieval threshold choice. Summarise the sweep and recommend a value | FR-10 | 5, 6 | checkpoint | TODO |
| 8 | B-06 Intent + urgency classifier with calibration table (cross-validated on dev) | FR-08, FR-05 | 1 | build | TODO |
| 9 | B-07 Routing: must-escalate rules, billing/money rule, kill switch, confidence threshold sweep | FR-09, FR-03, FR-16, FR-02 | 3, 8 | build | TODO |
| 10 | CHECKPOINT: confidence threshold choice. Summarise the sweep and the trade-off, recommend a value | FR-02 | 9 | checkpoint | TODO |
| 11 | B-08 Answer drafting with citations (PR-01) and disclosure line | FR-11, FR-06 | 4, 5 | build | TODO |
| 12 | B-09 Guardrails: PII, integrity, commitments, grounding (PR-03 + exact-quote check) | FR-12 | 2, 11 | build | TODO |
| 13 | Escalation handover package (PR-02 + template fallback) | FR-01 | 4, 9 | build | TODO |
| 14 | Pipeline wiring (LangGraph): every ticket ends answered or escalated; wire into harness | FR-01..FR-16 | 6, 12, 13 | build | TODO |
| 15 | CHECKPOINT: THE GATE (B-11). Full unattended run on data/validation_tickets.json and on a renamed copy; report counts, reconciliation, runtime | FR-14 | 14 | checkpoint | TODO |
| 16 | API: submit ticket, /search, escalation queue ordered by urgency, /metrics | FR-04, FR-05 | 14 | build | TODO |
| 17 | B-12 Prometheus metrics + Grafana dashboard JSON | NFR-05 | 16 | build | TODO |
| 18 | CHECKPOINT: build complete. Summarise what exists, known gaps, and what the PRD revision (Stage 5) should record | — | 17 | checkpoint | TODO |
