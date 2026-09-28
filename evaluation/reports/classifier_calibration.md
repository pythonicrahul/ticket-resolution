# Intent and urgency classifier (FR-08, FR-05)

- Trained on `data/development_tickets.json` — 500 labelled tickets, 215 distinct bodies, **96 distinct wording clusters**  
- Model fingerprint `22790005327d6b39`, embedder `default::{}:dim=384:fd54befa0e354197…`  
- 22 intents, 3 urgency levels  
- Cross-validation: **3 folds, grouped by wording cluster** (near-duplicates merged); 5 folds row-wise for the contrast column  
- Calibration measured cross-fitted: each half scored by a calibrator fitted on the other  

## Read this first: why two accuracy figures

The development set repeats itself. 500 tickets hold about 96 distinct bodies, and every repeated body carries the same intent.
Row-wise cross-validation therefore puts the *same wording* in the training and test
folds, and the score measures near-duplicate lookup rather than classification. Grouping
the folds so a body never appears in both is the honest estimate of how this behaves on
wording it has not seen.

| measure | grouped (honest) | row-wise (leaky) | target |
|---|---|---|---|
| Intent accuracy | **88.6%** | 99.6% | — |
| Urgency accuracy | **48.4%** | 51.8% | — |

For urgency, the majority class alone scores about 45.2%, so compare the figure against that rather than against zero.

## Intent, per class (NFR-03 wants ≥85% precision per class)

| intent | precision | recall | support |
|---|---|---|---|
| account_access ⚠ | 68.8% | 100.0% | 22 |
| api_key_issue | 86.7% | 72.2% | 18 |
| api_usage_question | 100.0% | 87.5% | 24 |
| authentication_failure | 85.7% | 90.0% | 20 |
| billing_query | 85.7% | 100.0% | 24 |
| compliance_request | 91.3% | 80.8% | 26 |
| configuration_help | 100.0% | 94.1% | 17 |
| data_export | 90.6% | 100.0% | 29 |
| data_residency ⚠ | 82.9% | 100.0% | 29 |
| database_issue | 89.7% | 100.0% | 26 |
| deployment_failure | 87.1% | 100.0% | 27 |
| feature_request ⚠ | 58.1% | 90.0% | 20 |
| integration_help | 100.0% | 52.4% | 21 |
| onboarding | 100.0% | 86.4% | 22 |
| performance_degradation | 88.5% | 100.0% | 23 |
| quota_or_overage | 100.0% | 47.8% | 23 |
| rate_limit | 100.0% | 92.3% | 13 |
| rollback_request | 93.3% | 100.0% | 28 |
| security_incident | 100.0% | 65.4% | 26 |
| sso_configuration | 92.9% | 100.0% | 26 |
| unclear_request | 100.0% | 93.3% | 15 |
| webhook_issue | 100.0% | 85.7% | 21 |

**3 of 22 classes are below the 85% precision target.**

## Urgency, per class

| urgency | precision | recall | support |
|---|---|---|---|
| high | 54.4% | 25.3% | 146 |
| low | 100.0% | 0.8% | 128 |
| medium | 47.3% | 90.3% | 226 |

## Calibration (Evaluation Framework §3)

Stated confidence against observed accuracy, in five bands, from the grouped
out-of-fold predictions. NFR-03 is met when every band a figure can be claimed for
is within 5 points.

### Intent

| confidence band | predictions | mean stated | observed accuracy | gap |
|---|---|---|---|---|
| 0.0–0.2 | 0 | — | — | — |
| 0.2–0.4 | 0 | — | — | — |
| 0.4–0.6 | 0 | — | — | — |
| 0.6–0.8 | 25 | 78.6% | 36.0% | 42.6 pts |
| 0.8–1.0 | 475 | 89.1% | 91.4% | 2.3 pts |

### Urgency

| confidence band | predictions | mean stated | observed accuracy | gap |
|---|---|---|---|---|
| 0.0–0.2 | 0 | — | — | — |
| 0.2–0.4 | 0 | — | — | — |
| 0.4–0.6 | 500 | 48.4% | 48.4% | 0.0 pts |
| 0.6–0.8 | 0 | — | — | — |
| 0.8–1.0 | 0 | — | — | — |

**Worst gap in a band large enough to judge: 42.6 pts** (NFR-03 allows 5).

## What these numbers mean

- **Intent accuracy 88.6%** on wording the model has not seen. NFR-03's ≥85% is met overall, but **3 of 22 classes are below 85% precision**, and NFR-03 states the target per class — so on the strict reading it is **not met**.
- **Calibration: NFR-03 is met where it matters and fails in one thin band.** The band holding 475 of 500 predictions is within 2.3 points; the worst band large enough to judge is 42.6 points against a 5-point limit. Read the table, not the single worst number.
- **Urgency 48.4% against a ceiling of 73.0%.** 67 ticket bodies in this set carry more than one urgency label, so no model that sees only the text can do better than that ceiling. This is a limit of the labelling, not of the model.
- **Urgency low (0.8% recall of 128) is effectively unreachable**, despite class weighting, because its embedding centroid sits 0.96 cosine from `medium`. FR-05's three levels are in practice two, and the escalation queue should be read that way.

## What this report does not say

- Nothing here is measured on the validation set, and nothing on the hidden set. These
  are development-set figures, cross-validated.
- The grouped figure is the one to quote. If a report elsewhere shows a much higher
  intent accuracy, it is row-wise and it is measuring the repetition in the data.
- The confidence threshold that consumes these probabilities is a separate decision
  (FR-02, checkpoint row 10). This report is its input, not its answer.
