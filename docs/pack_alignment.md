# Pack alignment: what the source documents say that this repo did not

Read on 2026-09-27 from `~/Downloads/FDE_Capstone_Complete/Capstone_Pack`:
`03_Reference/Governance_Framework.docx`, `01_Read_First/02_Build_Specification.docx`,
`03_Reference/Evaluation_Framework.docx`. Until then `docs/PRD.md` referenced them second-hand and
`docs/specs/FR-13.md` §7 flagged the Governance field list as a reconstruction. This file records every
difference found, what was done about it, and what is left for a human. It is the input to the Stage 5
PRD revision log.

## 1 Fixed in code this session

| # | What the pack requires | Where | Was | Now |
|---|---|---|---|---|
| 1 | `decision_id` on every logged decision | Governance §1 | integer `row_id` only | `decision_id` column, `{run_id}:{random}`, indexed and unique |
| 2 | `model: {name, version}` | Governance §1 | `model_name` only | `model_version` added |
| 3 | `prediction: {value, confidence}` per stage | Governance §1 | only `intent` / `intent_confidence` | generic `prediction_value` / `prediction_confidence` |
| 4 | `threshold_applied` | Governance §1 | **absent entirely** | column added, and **required** on an `auto_respond` row |
| 5 | `sources_used: [{doc_id, score}]` | Governance §1; Build Spec "Retrieve" | `retrieved_doc_ids`, **no scores** | `sources_used` with scores; the flat id list is derived from it |
| 6 | `action_taken: auto_respond \| escalate \| block` | Governance §1 | no `block` | `block` added as a non-terminal action, so "exactly one terminal row" still holds and the metrics report can count blocks |
| 7 | `reason` as "a human readable explanation" | Governance §1; Build Spec "Route" | a machine code only | `explanation` added and **required** on every terminal row; `reason` stays the code that metrics group on |
| 8 | Stage vocabulary `classification \| routing \| generation \| validation` | Governance §1 | `classify`, `route`, `generate`, `guardrails` | renamed to the framework's four, with `ingest`, `retrieval`, `handover`, `pipeline` as named extensions |
| 9 | The record in the framework's own shape | Governance §1 | — | `governance_record()` projects a stored row into exactly that JSON, checked by T-FR13-28 |

`src/ticketing_agent/logging_store.py`, `docs/specs/FR-13.md` and
`tests/test_fr13_decision_log.py` (T-FR13-28 … T-FR13-31) carry these. 85 tests pass.

## 2 Fixed in the specs, for the rows that will implement them

- **Five guardrails, not four** (Governance §4). `commitments` is renamed `tone_and_scope`, the framework's
  own name, and a fifth check is added: `confidence_floor` — "the routing threshold was actually applied … a
  missing confidence score is not a high one", which is also FR-02's rule. `docs/specs/FR-12.md` now carries
  the framework's five-row table verbatim; `docs/specs/FR-03.md` follows the rename. Row 12 implements.
- **Metrics report contents** (Build Spec §04). Four figure groups are mandatory and must be produced by the
  code, not by hand: Volume (processed, answered, escalated, **blocked by guardrails**), Business (FCR, mean
  **and median** reply time, escalation rate), Technical (classification precision **and recall per class**,
  retrieval hit rate, latency **median and p95**), Governance (decisions logged, **guardrail activations by
  type**, **private data detections**). Recorded on backlog row 6.
- **The fairness audit needs a length band** (Governance §3). Its segments are enterprise, small business,
  fluent, non-fluent, **short tickets, long or complex tickets**. `Ticket.segments()` covers the PRD's four
  (tier, region, fluency, channel); the length band is derivable and belongs in the metrics report. Recorded
  on backlog row 6 rather than changing FR-07.
- **Calibration table** (Evaluation Framework §3): five confidence bands, stated versus observed accuracy per
  band, which is how NFR-03's "within 5 points" is demonstrated. Recorded on backlog row 8.
- **Kill-switch questions** (Governance §5): mechanism, who is authorised, how long to take effect, what
  happens to tickets in flight, how it is tested. Recorded on backlog row 9 with FR-16.
- **The test procedure is a rehearsal** (Build Spec §06): clone into an empty directory, follow the README
  literally, one ticket per channel, a guardrail block, the full run, the test command, a credential scan.
  Recorded on backlog row 15. The pack says roughly half of submissions fail at "follow the README".

## 3 Things the PRD should record at Stage 5

1. **There are twelve acceptance criteria, not eleven.** `docs/PRD.md` cites A1–A11; **A12** exists: "Tests
   run with a single documented command and pass." The repo satisfies it (`uv run pytest -v`, in the README),
   but the requirement was untraced.
2. **The hidden set size is inconsistent in the pack itself.** Build Spec §04 says "a hidden set of 120
   tickets"; §08 says "the hidden set is one hundred tickets" and the Evaluation Framework says "the hundred
   tickets in the test file". `docs/PRD.md` and `CLAUDE.md` say 120. Nothing in the build depends on it —
   `load_tickets` processes "however many there are" — but the report should not quote a number as fact.
3. **The baseline figures match**, and the PRD's targets are the pack's: FCR 42% → 60%, reply time 8–12 h →
   under 5 min, satisfaction 3.2 → 4.0, escalation 58% → ≤30%, repeat contacts halved, precision ≥85%,
   hallucination ≤5%, citation accuracy ≥95%, latency p95 < 3 s, availability 99.5%, zero private data,
   cross-group variation < 5 points. No revision needed.
4. **"Blocked by guardrails" is a reportable outcome**, not only an internal state (Build Spec §04 Volume).
   That is why `block` is now in the log's vocabulary.

## 4 Left for a human: document work the build loop cannot do

- **Risk register R-01 … R-08** (Governance §2) needs a likelihood, impact, specific mitigation and a named
  owner for each. The framework is explicit that "mitigations phrased as *we will be careful* are not
  mitigations". FR-15's PRD row already notes that "provider unavailable" (R-06) must be added.
- **Incident response**, six steps, written so someone unfamiliar could follow it at two in the morning
  (Governance §5).
- **The declaration** (Governance §6): "this system must never …", the mechanism that enforces it, the most
  likely remaining harm, and what you would not deploy without.
- **The kill-switch answers** (Governance §5) — the code is row 9, the answers are yours.
- **Retention policy for the decision log**, still open from `docs/specs/FR-13.md` §7. The framework requires
  a decision to be "reconstructable months later" but sets no retention period.
- **Whether the FR-01 handover `summary` may carry customer text** (FR-13 §7). The framework's minimum record
  includes `input_summary`, which suggests yes; NFR-04's "zero private data" is about outbound replies, not
  the internal log. Worth stating explicitly in the governance declaration either way.
