# Evaluation run (FR-14)

- Generated: 2026-10-02T11:41:12Z  
- Input: `data/validation_tickets.json` — 80 tickets in the file  
- Scored against labels: **80 of 80**  
- Pipeline: **full** (`SupportPipeline`), **--no-cache**: recorded responses neither read nor written  
- Thresholds in use: relevance **0.25**, confidence **0.85**, top_k 5  
- Provider: **api.openai.com**, model `gpt-4o-mini`, grounding judge `gpt-4.1-mini`  
- Wall time: 318.56 s  

## Volume

| figure | value |
|---|---|
| Tickets processed | 80 |
| Answered automatically | 42 |
| Escalated | 38 |
| Blocked by guardrails | 3 |

## Business outcomes

| figure | value |
|---|---|
| First contact resolution (proxy) | 52.5% |
| Escalation rate | 47.5% |
| Processing time, mean | 3965.9 ms |
| Processing time, median | 4019.5 ms |

*The data has no first-reply timestamp, so no customer-visible wait can be computed. What is reported is this system's own processing time per ticket.*

## Technical

| figure | value |
|---|---|
| Retrieval hit rate | 96.2% (n=53) |
| Route agreement with labels | 60.0% |
| Latency median | 4019.5 ms |
| Latency p95 | 6640.7 ms |
| Citations that do not resolve | 0 |

*Retrieval hit rate: Share of labelled-answerable tickets whose expected article was retrieved, at the relevance threshold in use. Tickets with no expected article are excluded, so this has no false-positive counterpart: it cannot fall when retrieval returns too much.*

*Route agreement: Agreement with the labelled expected_route. This run escalated 47.5% of tickets against 40.0% labelled escalate. The closer those two are, the more of this figure is the base rate rather than agreement, and it says nothing about whether the label or the system is right on a disagreement.*

### Classification, per class

*Over every scored ticket in this run, which on the supplied data is mostly wording the classifier was trained on. The split by wording is below, and the figure that bears on NFR-03 is in* `evaluation/reports/classifier_calibration.md`*.*

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

### Confidence calibration (NFR-03, Evaluation Framework §3)

| band | n | stated | observed | gap |
|---|---|---|---|---|
| 0.0–0.2 | 0 | — | — | — |
| 0.2–0.4 | 0 | — | — | — |
| 0.4–0.6 | 0 | — | — | — |
| 0.6–0.8 | 0 | — | — | — |
| 0.8–1.0 | 80 | 97.0% | 100.0% | 3.0 pts |

Worst gap: **3.0 points**, within NFR-03's 5-point limit.

*80 of 80 labelled tickets carried both a stated confidence and a labelled intent. NFR-03 asks for stated confidence within 5 points of observed accuracy. A band with fewer than 25 predictions cannot support that claim and is excluded from the verdict, as in the classifier's own report.*

### Classification by wording (R8)

Training wording read from `/Users/rahuljain/Code/ticketing-agent/data/development_tickets.json` (215 distinct bodies). **77.5%** of this run's tickets use wording the classifier was trained on.

| group | tickets | scored | overall accuracy | lowest per-class precision |
|---|---|---|---|---|
| **unseen body (the headline)** | 18 | 18 | 100.0% | 100.0% |
| seen in training | 62 | 62 | 100.0% | 100.0% |

Of the 18 tickets with an unseen body, **14** are paraphrases of a training body at D-39's 0.85 clustering.

**4 ticket(s) use genuinely novel wording**: VAL-0003, VAL-0004, VAL-0046, VAL-0073.

Paraphrases: VAL-0006, VAL-0014, VAL-0025, VAL-0026, VAL-0038, VAL-0048, VAL-0049, VAL-0051, VAL-0052, VAL-0055, VAL-0058, VAL-0062, VAL-0066, VAL-0068.

*The headline classification figure is the **unseen_wording** one: a score measured on wording the classifier was trained on is near-duplicate lookup, not generalisation. **But an exact-body comparison overstates it**: of the 18 tickets whose body is not in the training file, 14 fall in a wording cluster that contains a training body under D-39's 0.85 clustering — the clusters are transitive, so this counts a paraphrase of a paraphrase, which errs toward calling wording seen. That leaves **4** with genuinely novel wording, and a figure over 4 ticket(s) supports nothing either way. The cross-validated figures in `evaluation/reports/classifier_calibration.md` group their folds by wording cluster over the whole development set and are the ones that bear on NFR-03.*

### Answered against the labels (R6)

| disagreement | count | of those that could be scored | tickets |
|---|---|---|---|
| **Answered, label says must_not_auto_respond** | 0 | 0.0% of 42 | — |
| Answered, label says escalate | 13 | 31.0% of 42 | VAL-0004, VAL-0014, VAL-0016, VAL-0022, VAL-0024, VAL-0025, VAL-0034, VAL-0038, VAL-0051, VAL-0055, VAL-0060, VAL-0070, VAL-0071 |
| Answered, label says not answerable from docs | 12 | 28.6% of 42 | VAL-0004, VAL-0014, VAL-0016, VAL-0022, VAL-0024, VAL-0025, VAL-0034, VAL-0051, VAL-0055, VAL-0060, VAL-0070, VAL-0071 |
| Answered, cited no expected article | 1 | 3.3% of 30 | VAL-0080 |

Decision against the labelled `expected_route`:

| expected ↓ / actual → | auto_respond | escalate |
|---|---|---|
| auto_respond | 29 | 19 |
| escalate | 13 | 19 |

*The labels are the pack's, not this system's. A disagreement is a question for a human -- the label may be wrong, or the answer may be -- and the ids are listed so it can be answered rather than argued about. `must_not_auto_respond` means something narrower in the pack data than in this corpus (D-20). Every ticket in the second row is also in the first: 12 of 13. The matrix also shows **19** ticket(s) the labels expected to be answered and this run escalated, and it is the larger number here. The same question applies to them.*

## Governance

| figure | value |
|---|---|
| Decisions logged | 80 |
| Log reconciles with tickets processed | yes |
| Guardrail activations by type | {'grounding': 3} |
| Private data detections | 0 |
| Redactions in the log | 0 |
| Model calls / cache hits | 138 / 0 |

## Why tickets ended where they did

| reason | tickets |
|---|---|
| none | 42 |
| must_escalate_intent | 14 |
| money_decision_required | 7 |
| no_cited_article | 6 |
| compliance_data_question | 4 |
| ungrounded_draft | 3 |
| invalid_citation | 2 |
| malformed_draft | 1 |
| no_answer_drafted | 1 |

Tickets arriving on an unrecognised channel: **0** (D-13: these escalate by rule).


## Segments (NFR-06, Governance fairness audit)

### By tier

| segment | tickets | answered | escalated | retrieval hit rate | median latency | note |
|---|---|---|---|---|---|---|
| business | 30 | 60.0% | 40.0% | 100.0% (n=22) | 4033.8 ms |  |
| enterprise | 8 | 25.0% | 75.0% | 100.0% (n=6) | 5518.0 ms | low confidence (n<10) |
| standard | 42 | 52.4% | 47.6% | 92.0% (n=25) | 3774.6 ms |  |

Variation across segments: **35.0 points**, **above** NFR-06's 5-point limit (3 segments).

Small segments included in that figure, treat with care: enterprise.

### By fluency

| segment | tickets | answered | escalated | retrieval hit rate | median latency | note |
|---|---|---|---|---|---|---|
| fluent | 61 | 52.5% | 47.5% | 95.5% (n=44) | 3871.2 ms |  |
| non_fluent | 19 | 52.6% | 47.4% | 100.0% (n=9) | 4851.0 ms |  |

Variation across segments: **0.1 points**, within NFR-06's 5-point limit (2 segments).

### By region

| segment | tickets | answered | escalated | retrieval hit rate | median latency | note |
|---|---|---|---|---|---|---|
| asia_pacific | 21 | 52.4% | 47.6% | 100.0% (n=17) | 4336.2 ms |  |
| europe | 25 | 60.0% | 40.0% | 94.7% (n=19) | 4029.7 ms |  |
| latin_america | 7 | 28.6% | 71.4% | 100.0% (n=3) | 2171.3 ms | low confidence (n<10) |
| north_america | 27 | 51.9% | 48.1% | 92.9% (n=14) | 3889.2 ms |  |

Variation across segments: **31.4 points**, **above** NFR-06's 5-point limit (4 segments).

Small segments included in that figure, treat with care: latin_america.

### By channel

| segment | tickets | answered | escalated | retrieval hit rate | median latency | note |
|---|---|---|---|---|---|---|
| chat | 22 | 54.5% | 45.5% | 84.6% (n=13) | 3657.9 ms |  |
| docs_comment | 16 | 50.0% | 50.0% | 100.0% (n=10) | 3752.3 ms |  |
| email | 31 | 51.6% | 48.4% | 100.0% (n=22) | 3954.6 ms |  |
| forum | 11 | 54.5% | 45.5% | 100.0% (n=8) | 4367.7 ms |  |

Variation across segments: **4.5 points**, within NFR-06's 5-point limit (4 segments).

### By length

| segment | tickets | answered | escalated | retrieval hit rate | median latency | note |
|---|---|---|---|---|---|---|
| long_or_complex | 9 | 66.7% | 33.3% | 100.0% (n=7) | 4586.3 ms | low confidence (n<10) |
| short | 71 | 50.7% | 49.3% | 95.7% (n=46) | 3889.2 ms |  |

Variation across segments: **16.0 points**, **above** NFR-06's 5-point limit (2 segments).

Small segments included in that figure, treat with care: long_or_complex.

## Results table (Evaluation Framework)

| measure | baseline | target | achieved | confidence in the figure |
|---|---|---|---|---|
| First contact resolution (proxy) | 42% | ≥60% | 52.5% | proxy: automated handling, not confirmed resolution; n=80 |
| Escalation rate | 58% | ≤30% | 47.5% | n=80 |
| Processing time p95 | — | <3 s | 6640.7 ms | measured over 138 provider request(s), with no replayed response. |
| Intent precision (per class) | — | ≥85% | 100.0% | the lowest per-class precision over the 80 of 80 labelled tickets that produced a prediction; the macro figure is in technical.classification; worst calibration gap 3.0 points (NFR-03 asks for ≤5); in-sample caution: much of the supplied validation wording also appears in the training set, so a high figure here is not evidence of generalisation — the out-of-fold figures in evaluation/reports/classifier_calibration.md are |
| Retrieval hit rate | — | — | 96.2% | n=53 tickets with an expected article; no false-positive counterpart |
| Hallucination rate | — | ≤5% (human review) | needs human review of ≥50 responses (two assessors) | — |
| Citation accuracy | — | ≥95% (human review) | needs human review; the harness counts unresolvable citations only | — |
| Private data in outbound text | — | 0 | 0 | replies that were **sent** carrying a `private_data` detection; no draft failed that check in this run either |
| Answered against the label | — | — | 13 labelled escalate, 12 labelled not answerable from docs, 1 citing no expected article, of 42 answered and labelled | the ids are in the Technical section; a disagreement is a question for a human, not a score |
| Cross-segment variation | — | <5 points | 35.0 points (tier) | the worst of the 5 dimensions; per-dimension figures with sample sizes are in the segments section, and small segments are flagged there |

## What this run does not measure

- **NFR-01 is missed.** The automated path's p95 is 5560.8 ms against a 3,000 ms target, over 42 answered ticket(s). Two provider round trips per answered ticket (PR-01 to draft, PR-03 to judge) against a hosted model is the cause; NFR-01 was written before the provider was chosen, and the requirement itself asks for the measured figure and its cause when it cannot be met.
- Hallucination rate and citation accuracy: need human review of at least 50 responses by two assessors (Evaluation Framework tier two); the harness reports unresolvable citations as a floor only. `scripts/review_sample.py` writes the sheet from this run's `outcomes.jsonl`.
- Customer satisfaction: no live customers; the Evaluation Framework's rubric proxy needs a human sample.
- Response time: the data has no first-reply timestamp, so only processing time is real.
- Generalisation of the classification figures: much of the supplied validation wording also appears in the training set, so precision and recall measured here are partly in-sample. The out-of-fold figures in `evaluation/reports/classifier_calibration.md` are the ones that bear on NFR-03.
- Whether an automated answer was *correct*: the harness checks that citations resolve and that the guardrails passed, which is not the same as the answer being right. That is what the human review above is for.
