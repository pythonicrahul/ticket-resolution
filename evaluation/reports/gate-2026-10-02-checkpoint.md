# Gate checkpoint, 2026-10-02 (review row R13)

Three runs of the same 80 validation tickets on the same code, and the September run
the gate was signed off on in D-57. Every figure is read from those runs' own
`metrics.json` and `outcomes.jsonl`, kept under
`evaluation/results/kept-gate-2026-10-02*/`; regenerate with the command in the
docstring of `scripts/gate_checkpoint_report.py`.

## The four runs

| run | responses | answered | escalated | blocked | provider calls | cache hits | automated-path p95 | wall |
|---|---|---|---|---|---|---|---|---|
| **live, `--no-cache`** | all live | 42 | 38 | 3 | 138 | 0 | 5561 ms | 319 s |
| renamed copy, cache on | partly replayed | 39 | 41 | 5 | 42 | 97 | 2185 ms | 74 s |
| replay of the renamed run | all replayed | 39 | 41 | 5 | 0 | 139 | 95 ms | 8 s |
| September (D-57), replayed | all replayed | 42 | 38 | — | — | — | not measured | — |

Every run: 80 tickets in, 80 terminal rows, log reconciles, 0 private-data detections, 0 redactions, 0 unresolvable citations.

## Determinism, with every response replayed

The same input file twice, the second served entirely from the cache the first one recorded. This is the condition the non-negotiable in `CLAUDE.md` names: *cached model responses, same input → same routing*.

**0 of 80 tickets routed differently.**

Not one. Routing and every reply are identical.

Reply text identical on **80 of 80**.

## The same code and configuration, hours apart

Run 1 reached the provider for all 138 responses. Run 2 replayed 97 of 139 and fetched 42. Nothing else differs: same code, same thresholds, same models, temperature 0.

**7 of 80 tickets routed differently.**

| ticket | live | renamed |
|---|---|---|
| VAL-0022 | auto_respond / — | escalate / no_cited_article |
| VAL-0033 | auto_respond / — | escalate / ungrounded_draft |
| VAL-0038 | auto_respond / — | escalate / ungrounded_draft |
| VAL-0039 | escalate / no_cited_article | auto_respond / — |
| VAL-0071 | auto_respond / — | escalate / no_cited_article |
| VAL-0073 | auto_respond / — | escalate / ungrounded_draft |
| VAL-0078 | escalate / ungrounded_draft | auto_respond / — |

## Against the September run the gate was signed off on

Both runs answered 42 and 42. **26 tickets** are routed differently inside that near-identical total.

| moved to escalate because | tickets |
|---|---|
| `money_decision_required` | 6 — VAL-0013, VAL-0040, VAL-0041, VAL-0044, VAL-0053, VAL-0072 |
| `compliance_data_question` | 2 — VAL-0037, VAL-0054 |
| `no_cited_article` | 2 — VAL-0039, VAL-0050 |
| `ungrounded_draft` | 2 — VAL-0066, VAL-0078 |
| `malformed_draft` | 1 — VAL-0023 |
| moved to **auto_respond** | 13 — VAL-0010, VAL-0024, VAL-0033, VAL-0042, VAL-0045, VAL-0047, VAL-0060, VAL-0061, VAL-0069, VAL-0073, VAL-0074, VAL-0075, VAL-0077 |

They were escalated in September for: `ungrounded_draft` ×12, `invalid_citation` ×1.

## The labels contradict themselves

Measured on the supplied files, identical body text after whitespace and case normalisation.

* **4 bodies inside `validation_tickets.json`** carry more than one label:

  * VAL-0012 → auto_respond/answerable=True, VAL-0034 → escalate/answerable=False
  * VAL-0013 → auto_respond/answerable=True, VAL-0040 → auto_respond/answerable=True, VAL-0041 → auto_respond/answerable=True, VAL-0044 → auto_respond/answerable=True, VAL-0053 → auto_respond/answerable=True, VAL-0065 → auto_respond/answerable=True, VAL-0072 → escalate/answerable=False
  * VAL-0024 → escalate/answerable=False, VAL-0075 → auto_respond/answerable=True
  * VAL-0033 → auto_respond/answerable=True, VAL-0060 → escalate/answerable=False, VAL-0061 → auto_respond/answerable=True

* **62 of 80** validation tickets have a development ticket with an identical body, and **37 of those 62** carry a different label from their twin:

  * VAL-0001 (`auto_respond`) vs DEV-0002
  * VAL-0005 (`auto_respond`) vs DEV-0384
  * VAL-0009 (`escalate`) vs DEV-0253
  * VAL-0010 (`auto_respond`) vs DEV-0407
  * VAL-0011 (`auto_respond`) vs DEV-0030, DEV-0461
  * VAL-0012 (`auto_respond`) vs DEV-0040, DEV-0042, DEV-0339
  * VAL-0013 (`auto_respond`) vs DEV-0073
  * VAL-0016 (`escalate`) vs DEV-0213, DEV-0437
  * VAL-0018 (`auto_respond`) vs DEV-0384
  * VAL-0022 (`escalate`) vs DEV-0213, DEV-0437
  * VAL-0024 (`escalate`) vs DEV-0145, DEV-0250, DEV-0491
  * VAL-0029 (`auto_respond`) vs DEV-0170, DEV-0226
  * VAL-0031 (`auto_respond`) vs DEV-0030, DEV-0461
  * VAL-0034 (`escalate`) vs DEV-0009, DEV-0122, DEV-0127 …
  * VAL-0036 (`auto_respond`) vs DEV-0170, DEV-0226
  * VAL-0037 (`escalate`) vs DEV-0188, DEV-0205, DEV-0388 …
  * VAL-0039 (`auto_respond`) vs DEV-0181
  * VAL-0040 (`auto_respond`) vs DEV-0073
  * VAL-0041 (`auto_respond`) vs DEV-0073
  * VAL-0042 (`auto_respond`) vs DEV-0283, DEV-0347
  * VAL-0043 (`escalate`) vs DEV-0240, DEV-0327, DEV-0470
  * VAL-0044 (`auto_respond`) vs DEV-0073
  * VAL-0053 (`auto_respond`) vs DEV-0073
  * VAL-0054 (`auto_respond`) vs DEV-0061, DEV-0118, DEV-0123
  * VAL-0056 (`auto_respond`) vs DEV-0086, DEV-0108, DEV-0272
  * VAL-0057 (`auto_respond`) vs DEV-0280, DEV-0353, DEV-0442
  * VAL-0059 (`auto_respond`) vs DEV-0039, DEV-0147
  * VAL-0060 (`escalate`) vs DEV-0080, DEV-0117, DEV-0125 …
  * VAL-0063 (`auto_respond`) vs DEV-0266
  * VAL-0065 (`auto_respond`) vs DEV-0073
  * VAL-0069 (`auto_respond`) vs DEV-0283, DEV-0347
  * VAL-0070 (`escalate`) vs DEV-0103, DEV-0155, DEV-0190 …
  * VAL-0071 (`escalate`) vs DEV-0048, DEV-0366
  * VAL-0072 (`escalate`) vs DEV-0078, DEV-0151, DEV-0189 …
  * VAL-0075 (`auto_respond`) vs DEV-0051, DEV-0119
  * VAL-0076 (`auto_respond`) vs DEV-0110, DEV-0154, DEV-0359
  * VAL-0079 (`auto_respond`) vs DEV-0275

## The R6 list, for the hand review

From the live run. Each of these the author reads and decides whether the label or the system is right.

| disagreement | count | tickets |
|---|---|---|
| Answered, label says `must_not_auto_respond` | 0 | — |
| Answered, label says `escalate` | 13 | VAL-0004, VAL-0014, VAL-0016, VAL-0022, VAL-0024, VAL-0025, VAL-0034, VAL-0038, VAL-0051, VAL-0055, VAL-0060, VAL-0070, VAL-0071 |
| Answered, label says not answerable from docs | 12 | VAL-0004, VAL-0014, VAL-0016, VAL-0022, VAL-0024, VAL-0025, VAL-0034, VAL-0051, VAL-0055, VAL-0060, VAL-0070, VAL-0071 |
| Answered, cited no expected article | 1 | VAL-0080 |

And the other direction, which is the larger number:

| expected ↓ / actual → | auto_respond | escalate |
|---|---|---|
| auto_respond | 29 | 19 |
| escalate | 13 | 19 |

