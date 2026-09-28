# Evaluation run (FR-14)

- Generated: 2026-09-28T17:33:42Z  
- Input: `data/validation_tickets.json` — 80 tickets in the file  
- Scored against labels: **80 of 80**  
- Thresholds in use: relevance **0.25**, confidence **0.85**, top_k 5  
- Wall time: 8.15 s  

## Volume

| figure | value |
|---|---|
| Tickets processed | 80 |
| Answered automatically | 42 |
| Escalated | 38 |
| Blocked by guardrails | 16 |

## Business outcomes

| figure | value |
|---|---|
| First contact resolution (proxy) | 52.5% |
| Escalation rate | 47.5% |
| Processing time, mean | 91.8 ms |
| Processing time, median | 91.0 ms |

*The data has no first-reply timestamp, so no customer-visible wait can be computed. What is reported is this system's own processing time per ticket.*

## Technical

| figure | value |
|---|---|
| Retrieval hit rate | 96.2% (n=53) |
| Route agreement with labels | 57.5% |
| Latency median | 91.0 ms |
| Latency p95 | 95.7 ms |
| Citations that do not resolve | 0 |

*Retrieval hit rate: Share of labelled-answerable tickets whose expected article was retrieved, at the relevance threshold in use. Tickets with no expected article are excluded, so this has no false-positive counterpart: it cannot fall when retrieval returns too much.*

*Route agreement: Agreement with the labelled expected_route. While the answering path is unbuilt every ticket escalates, so this figure is simply the share of tickets labelled escalate — it does not measure routing.*

### Classification, per class

| intent | precision | recall | support |
|---|---|---|---|
| account_access | 100.0% | 100.0% | 4 |
| api_key_issue | 100.0% | 100.0% | 6 |
| api_usage_question | 100.0% | 100.0% | 4 |
| authentication_failure | 100.0% | 100.0% | 3 |
| billing_query | 100.0% | 100.0% | 10 |
| compliance_request | 100.0% | 100.0% | 1 |
| configuration_help | 100.0% | 100.0% | 3 |
| data_export | 100.0% | 100.0% | 1 |
| data_residency | 100.0% | 100.0% | 6 |
| database_issue | 100.0% | 100.0% | 1 |
| deployment_failure | 100.0% | 100.0% | 5 |
| feature_request | 100.0% | 100.0% | 3 |
| integration_help | 100.0% | 100.0% | 3 |
| onboarding | 100.0% | 100.0% | 4 |
| performance_degradation | 100.0% | 100.0% | 3 |
| quota_or_overage | 100.0% | 100.0% | 2 |
| rate_limit | 100.0% | 100.0% | 1 |
| rollback_request | 100.0% | 100.0% | 6 |
| security_incident | 100.0% | 100.0% | 4 |
| sso_configuration | 100.0% | 100.0% | 1 |
| unclear_request | 100.0% | 100.0% | 6 |
| webhook_issue | 100.0% | 100.0% | 3 |

Overall accuracy: 100.0%

## Governance

| figure | value |
|---|---|
| Decisions logged | 80 |
| Log reconciles with tickets processed | yes |
| Guardrail activations by type | {'grounding': 16} |
| Private data detections | 0 |
| Redactions in the log | 0 |
| Model calls / cache hits | 0 / 162 |

## Why tickets ended where they did

| reason | tickets |
|---|---|
| none | 42 |
| ungrounded_draft | 16 |
| must_escalate_intent | 14 |
| no_cited_article | 4 |
| invalid_citation | 3 |
| no_answer_drafted | 1 |

Tickets arriving on an unrecognised channel: **0** (D-13: these escalate by rule).


## Segments (NFR-06, Governance fairness audit)

### By tier

| segment | tickets | answered | escalated | retrieval hit rate | median latency | note |
|---|---|---|---|---|---|---|
| business | 30 | 53.3% | 46.7% | 100.0% (n=22) | 91.1 ms |  |
| enterprise | 8 | 62.5% | 37.5% | 100.0% (n=6) | 90.9 ms | low confidence (n<10) |
| standard | 42 | 50.0% | 50.0% | 92.0% (n=25) | 91.1 ms |  |

Variation across segments: **12.5 points**, **above** NFR-06's 5-point limit (3 segments).

Small segments included in that figure, treat with care: enterprise.

### By fluency

| segment | tickets | answered | escalated | retrieval hit rate | median latency | note |
|---|---|---|---|---|---|---|
| fluent | 61 | 50.8% | 49.2% | 95.5% (n=44) | 91.0 ms |  |
| non_fluent | 19 | 57.9% | 42.1% | 100.0% (n=9) | 91.1 ms |  |

Variation across segments: **7.1 points**, **above** NFR-06's 5-point limit (2 segments).

### By region

| segment | tickets | answered | escalated | retrieval hit rate | median latency | note |
|---|---|---|---|---|---|---|
| asia_pacific | 21 | 57.1% | 42.9% | 100.0% (n=17) | 92.2 ms |  |
| europe | 25 | 52.0% | 48.0% | 94.7% (n=19) | 91.0 ms |  |
| latin_america | 7 | 28.6% | 71.4% | 100.0% (n=3) | 90.6 ms | low confidence (n<10) |
| north_america | 27 | 55.6% | 44.4% | 92.9% (n=14) | 90.8 ms |  |

Variation across segments: **28.5 points**, **above** NFR-06's 5-point limit (4 segments).

Small segments included in that figure, treat with care: latin_america.

### By channel

| segment | tickets | answered | escalated | retrieval hit rate | median latency | note |
|---|---|---|---|---|---|---|
| chat | 22 | 40.9% | 59.1% | 84.6% (n=13) | 90.9 ms |  |
| docs_comment | 16 | 50.0% | 50.0% | 100.0% (n=10) | 91.0 ms |  |
| email | 31 | 61.3% | 38.7% | 100.0% (n=22) | 91.9 ms |  |
| forum | 11 | 54.5% | 45.5% | 100.0% (n=8) | 91.1 ms |  |

Variation across segments: **20.4 points**, **above** NFR-06's 5-point limit (4 segments).

### By length

| segment | tickets | answered | escalated | retrieval hit rate | median latency | note |
|---|---|---|---|---|---|---|
| long_or_complex | 9 | 77.8% | 22.2% | 100.0% (n=7) | 91.8 ms | low confidence (n<10) |
| short | 71 | 49.3% | 50.7% | 95.7% (n=46) | 91.0 ms |  |

Variation across segments: **28.5 points**, **above** NFR-06's 5-point limit (2 segments).

Small segments included in that figure, treat with care: long_or_complex.

## Results table (Evaluation Framework)

| measure | baseline | target | achieved | confidence in the figure |
|---|---|---|---|---|
| First contact resolution (proxy) | 42% | ≥60% | 52.5% | proxy: automated handling, not confirmed resolution; n=80 |
| Escalation rate | 58% | ≤30% | 47.5% | n=80; 100% is by construction until row 14, not a tuning result |
| Processing time p95 | — | <3 s | 95.7 ms | no model call in the stub pipeline, so this will rise at row 11 |
| Intent precision (per class) | — | ≥85% | not computable yet (row 8) | labels available for 80 tickets |
| Retrieval hit rate | — | — | see technical.retrieval_hit_rate_pct | — |
| Hallucination rate | — | ≤5% (human review) | needs human review of ≥50 responses (two assessors) | — |
| Citation accuracy | — | ≥95% (human review) | needs human review; the harness counts unresolvable citations only | — |
| Private data in outbound text | — | 0 | no reply is sent yet (row 11) | — |
| Cross-segment variation | — | <5 points | see segments.*.variation_points | — |

## What this run does not measure

- Intent precision and recall: no classifier yet (row 8).
- Hallucination rate and citation accuracy: need human review of at least 50 responses by two assessors (Evaluation Framework tier two); the harness reports unresolvable citations as a floor only.
- Private data in outbound replies: nothing is sent yet (row 11), so zero here means 'nothing was generated', not 'nothing leaked'.
- Customer satisfaction: no live customers; the Evaluation Framework's rubric proxy needs a human sample.
- Confidence calibration: needs the classifier's confidences (row 8).
- Response time: the data has no first-reply timestamp, so only processing time is real.
