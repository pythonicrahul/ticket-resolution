# Engineered test tickets

Everything in this directory is **synthetic**. None of it came from CloudServe's data. The supplied
datasets contain no PII, no injection attempts, no refund or dispute tickets and no malformed records
(checked: 0 of 500 development tickets mention `refund|credit|dispute|chargeback|money back|reimburse`),
so the requirements that must hold for those cases — FR-03, FR-07, FR-12 — have nothing to be tested
against without these files. That is what backlog row 2 (B-17) built.

Conventions, so nothing here can be mistaken for real data:

- Every ticket id starts with `SYN-` and every draft id with `SYN-DRAFT-`. Real ids are `DEV-*` and `VAL-*`. The one exception is inside `malformed_tickets.json`, where entries that carry no `ticket_id` (or are not objects at all) get a generated `GEN-…` id from ingest — which is the behaviour that file exists to test.
- Contact details use reserved ranges only: `example.com` addresses, `+1 555 01xx` numbers, the
  national-id-shaped `000-00-0000` (an invalid prefix for a real one).
- Credentials are obvious placeholders (`SYNTHETIC-EXAMPLE-NOT-A-REAL-KEY-*`). No PEM armour is used
  anywhere, deliberately, so no secret scanner ever sees key-shaped material in this repository.
- The one card number is `4111 1111 1111 1111`, the long-standing public test value. It is Luhn-valid
  because the detector must be tested against something Luhn-valid.
- `customer_name` is always `Synthetic Tester`.

## Files

| File | Entries | Serves | What it holds |
|---|---|---|---|
| `malformed_tickets.json` | 13 | FR-07 | Malformed and empty input: empty body, body only, odd characters, unsupported channel, 20 000-character body, missing subject, wrong types, duplicate id, and three entries that are not objects at all. **Exempt from the schema contract below** — being non-conforming is its purpose. |
| `pii_tickets.json` | 6 | FR-12, NFR-04 | Card number, pasted credentials, credential plus national-id-shaped reference, third-party contact details (`pii_echo_risk`: answerable, but no reply may echo them), plus two lookalikes. |
| `injection_tickets.json` | 10 | FR-12 | Instruction override, tag breaking and prompt extraction, developer-mode demand, social engineering, prompt extraction, a bypass request in a non-fluent register, a line-start role label, plus three lookalikes (`override`, `act as`, and an inline `system:` log paste). |
| `money_commitment_tickets.json` | 17 | FR-03 | Refund, credit note, dispute/chargeback, SLA credit, compensation/goodwill, waive/write off, money back/reimburse, reverse the charge, two ETA demands, a mixed explanatory-plus-refund ticket, one honestly-labelled `known_over_escalation` case, and five explanatory billing tickets that **must still be answered** (invoice lines, plans, proration, usage limits, retention period — the four kinds PRD FR-03 permits, plus a factual date question). |
| `draft_replies.json` | 11 | FR-12, FR-11, FR-03, FR-06 | Engineered **draft replies** with their retrieved passages, for the post-draft checks: two that must pass (a grounded cited reply and an honest "I don't know"), two leaking private data, two ungrounded, three carrying a forbidden commitment, one leaking prompt text, one empty. |

## Schema

Each entry in the three conforming files is a ticket in the pack schema (`ticket_id`, `channel`,
`subject`, `body`, `received_at`, `customer_*`, `language_fluency`, `labels`, `history`) plus one extra
block that the runtime never reads:

```json
"synthetic": {
  "category": "injection",
  "why": "why this ticket exists, in one or two sentences",
  "expected_reason": "instruction_injection_detected",
  "expected_all_reasons": ["instruction_injection_detected", "money_commitment_requested"],
  "expected_guardrail": "instruction_integrity",
  "requirement_ids": ["FR-12", "FR-03"],
  "secrets": [], "contact_details": [], "markers": ["ignore all previous", "you are now"],
  "money_triggers": ["refund"], "date_triggers": []
}
```

`expected_reason`, `expected_all_reasons` and the four evidence lists are **derived from the ticket's own
text** by the generator, never hand-written, so a fixture cannot claim a behaviour its words do not
produce. Two of them originally did, and the contract tests caught it.

`expected_reason` is the primary reason under the precedence in `docs/specs/FR-12.md` §3.4 (D-16), and
`expected_all_reasons` lists every rule that matches. A ticket that is both an injection attempt and a
refund request therefore has one deterministic primary reason and loses neither fact.

`labels.expected_route` and `labels.must_not_auto_respond` are set to match, so routing and guardrail
tests can assert against them directly. **A synthetic convention:** in this corpus
`must_not_auto_respond` is exactly `expected_route == "escalate"`. In the supplied data it is not — 87
of the 189 escalate tickets carry the flag and 102 do not, because there it marks the four intent
classes of FR-09 rather than every escalation. Anything mixing this corpus with pack data (a fairness
table, an FR-09 count) has to account for that.

## Lookalikes are not padding

Each category carries negative cases: `pii_lookalike`, `injection_lookalike`, `explanatory_billing`.
They exist so an over-broad rule fails its own fixtures rather than quietly wrecking first-contact
resolution. Two of them already changed a spec: `SYN-INJ-LOOKALIKE-001` ("how do I **override** the
default retry interval") and `SYN-INJ-LOOKALIKE-002` ("can a webhook **act as** a health check") are
why the FR-12 injection markers are phrases rather than bare words. A third, `SYN-INJ-LOOKALIKE-003`
("our logs show 'restart requested by **system:** worker-3'"), is why `system:` and `assistant:` now match
only at the start of a line (D-24): as a bare substring they would escalate a pasted log.

## Intents are chosen so the rule under test is the only thing that can fire

PII, injection and money fixtures carry intents that normally auto-respond (`billing_query`,
`deployment_failure`, `account_access`, `api_usage_question`, `webhook_issue`, …), never one of the four
always-escalate intents of FR-09. If they were labelled `security_incident`, they would escalate by
intent and prove nothing about the guardrail.

`tests/test_engineered_fixtures.py` holds the contract tests. The pattern tables it mirrors from
`docs/specs/FR-03.md` and `docs/specs/FR-12.md` are temporary: row 12 implements them in
`src/ticketing_agent/guardrails.py`, and the tests must then import from there and drop the mirror.

## Drafts

`draft_replies.json` holds what row 12 needs and no ticket can provide: candidate replies. Each entry is

```json
{
  "draft_id": "SYN-DRAFT-COMMIT-001",
  "for_ticket": "SYN-MONEY-001",
  "text": "the candidate reply",
  "citations": ["DOC-BILL-001#1"],
  "retrieved": [{"chunk_id": "DOC-BILL-001#1", "doc_id": "DOC-BILL-001", "text": "..."}],
  "synthetic": {
    "category": "commitment_in_draft",
    "why": "...",
    "expected_passed": false,
    "expected_failures": ["grounding", "commitments"],
    "expected_reason": "ungrounded_draft",
    "requirement_ids": ["FR-12", "FR-03"]
  }
}
```

`expected_failures` lists every check the draft should fail, **in the FR-12 §3.2 check order**, and
`expected_reason` is the reason the first of them produces. Several drafts fail more than one check
honestly — a reply claiming a refund was issued is both ungrounded and a forbidden commitment — and
saying so is better than pretending each fixture isolates one rule.

The passages are carried with the draft so the grounding check can run with no Chroma index and no
model call.

**Open question for row 5:** `chunk_id` is written as `DOC-BILL-001#1` (doc id, `#`, chunk ordinal).
Row 5 (chunking and retrieval, FR-10) has not fixed a chunk-id format yet. If it chooses another, these
fixtures must be updated with it, or row 5 adopts this one.
