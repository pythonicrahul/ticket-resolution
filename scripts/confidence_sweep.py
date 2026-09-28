"""FR-02, row 9: sweep the confidence threshold T and write the report a human decides from.

    uv run python scripts/confidence_sweep.py --input data/development_tickets.json

Writes `evaluation/reports/confidence_sweep.md`. For each candidate T it routes every development
ticket through the real classifier, the real index and the real `Router`, and reports what that T
would cost and buy:

* **answered / escalated** — the escalation rate the PRD trades against wrong answers;
* **wrong answers sent** — answered tickets whose label says `escalate`, plus answered tickets
  whose predicted intent is wrong. The second is the honest proxy for "would the reply have been
  wrong": the drafter is not built yet, and a reply grounded in the articles retrieved for the
  wrong intent is the failure Marcus described;
* **two bases, both reported.** The saved classifier was fitted on these 500 tickets, so asking it
  to classify them again measures a lookup, not a classifier — the first run of this script read
  0 wrong intents at every T, which is the row-8 leak again (D-39). The headline table therefore
  uses the model's **grouped out-of-fold** predictions and cross-fitted confidences, and the
  in-sample figures are kept beside them so the size of the leak stays visible;
* **needless escalations** — escalated tickets the labels say were answerable;
* **the confidence-decided slice** — how many tickets T actually decides. Most escalations come
  from rules that T cannot move (FR-09, FR-03, FR-10), so a table of totals overstates T's power;
* **answer rate by fluency** — the fairness column, because the threshold is also a fairness
  decision (D-35 found the retrieval threshold widened that gap).

It chooses nothing. T is the author's call at checkpoint row 10.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import replace
from pathlib import Path

from ticketing_agent.classify import Classification, IntentClassifier, TrainedClassifier
from ticketing_agent.config import Settings, load_settings
from ticketing_agent.ingest import evaluation_labels, load_tickets
from ticketing_agent.retrieve import Retriever
from ticketing_agent.route import PRECEDENCE, Router

ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = (0.0, 0.50, 0.60, 0.70, 0.75, 0.80, 0.85, 0.90, 0.925, 0.95, 0.97, 0.99)
DEFAULT_REPORT = ROOT / "evaluation" / "reports" / "confidence_sweep.md"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=None,
                        help="tickets to sweep; defaults to TRAINING_TICKETS_PATH from .env "
                             "(development only: never validation, never the hidden set)")
    parser.add_argument("--docs", default=None, help="defaults to DOCS_PATH from .env")
    parser.add_argument("--chroma", default=str(ROOT / "storage" / "chroma-sweep"))
    parser.add_argument("--classifier", default=None, help="defaults to CLASSIFIER_PATH")
    parser.add_argument("--output", default=str(DEFAULT_REPORT))
    args = parser.parse_args()

    configured = load_settings()
    docs_path = Path(args.docs) if args.docs else configured.require_path("docs_path")
    input_path = (Path(args.input) if args.input
                  else configured.require_path("training_tickets_path"))
    classifier_path = Path(args.classifier) if args.classifier else configured.classifier_path
    # A committed report names no home directory and no setting the reader cannot see.
    args.docs, args.classifier = _relative(docs_path), _relative(classifier_path)
    training = configured.training_tickets_path
    if training is not None and input_path.resolve() != training.resolve():
        print(f"REFUSING: --input {_relative(input_path)} is not TRAINING_TICKETS_PATH "
              f"({_relative(training)}). Tuning against validation or hidden tickets is "
              "forbidden (CLAUDE.md); the harness is the only sanctioned way to run them, and "
              "42 validation tickets duplicate development text, so a T chosen here would "
              "flatter itself. There is deliberately no flag to override this.")
        return 1
    if configured.kill_switch_file.exists():
        print(f"REFUSING: the kill switch at {configured.kill_switch_file} is on, so every "
              "ticket would escalate for that reason and the sweep would measure nothing.")
        return 1

    # The sweep's own settings: the configured relevance threshold (that decision is made, D-38),
    # a kill switch that is deliberately absent, and T supplied per row below.
    base = Settings(
        docs_path=docs_path, chroma_path=args.chroma,
        retrieval_top_k=configured.retrieval_top_k,
        relevance_threshold=configured.relevance_threshold,
        kill_switch_file=ROOT / "storage" / "no-kill-switch-during-the-sweep",
        model_name="not-used-by-routing")

    tickets = load_tickets(input_path)
    retriever = Retriever(base)
    stats = retriever.build_index(docs_path)
    print(f"index: {stats.chunks} chunks from {stats.documents} documents "
          f"(embedder {stats.embedder})")
    model = TrainedClassifier.load(classifier_path)
    classifier = IntentClassifier(model)
    # (ticket_id -> out-of-fold predicted intent, cross-fitted calibrated confidence). Grouped so
    # that no ticket body was in both the training and the test fold (D-39).
    out_of_fold = {tid: (intent, confidence)
                   for tid, intent, confidence in model.report.out_of_fold_predictions}
    print(f"classifier: {classifier_path.name}, fingerprint {model.fingerprint}; "
          f"{len(out_of_fold)} out-of-fold predictions "
          f"({model.report.intent_accuracy_pct}% grouped accuracy, "
          f"{model.report.intent_accuracy_naive_pct}% row-wise)")

    # Classify and retrieve once per ticket; T is applied afterwards, so every row of the table
    # is measured from the same predictions (and the sweep costs one embedding pass, not twelve).
    prepared = []
    missing_out_of_fold = 0
    for number, ticket in enumerate(tickets, start=1):
        in_sample = classifier.classify(ticket)
        held_out = out_of_fold.get(ticket.ticket_id)
        if held_out is None:
            missing_out_of_fold += 1
        prepared.append({
            "ticket": ticket,
            "in_sample": in_sample,
            "out_of_fold": _swap(in_sample, held_out),
            "passages": retriever.search(ticket.text, threshold=base.relevance_threshold),
            "labels": evaluation_labels(ticket),
        })
        if number % 100 == 0:
            print(f"  {number}/{len(tickets)} tickets")
    if missing_out_of_fold:
        print(f"  note: {missing_out_of_fold} ticket(s) had no out-of-fold prediction (they were "
              "not in the training file) and fall back to the in-sample one")

    rows = [_measure(t, prepared, base, "out_of_fold") for t in THRESHOLDS]
    leaked = [_measure(t, prepared, base, "in_sample") for t in THRESHOLDS]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(_render(rows, leaked, prepared, stats, args, base, input_path,
                              missing_out_of_fold, model),
                      encoding="utf-8")
    print(f"\nwrote {_relative(output)}")
    return 0


def _swap(classification: Classification,
          held_out: tuple[str, float] | None) -> Classification:
    """The same classification with the out-of-fold intent and confidence substituted in.

    Routing reads only those two fields, so this is enough to route the ticket as the model would
    have routed one it had not been trained on.
    """
    if held_out is None:
        return classification
    intent, confidence = held_out
    return replace(classification, intent=intent, intent_confidence=confidence)


def _measure(threshold: float, prepared: list[dict], base: Settings, basis: str) -> dict:
    router = Router(Settings(**{**base.__dict__, "confidence_threshold": threshold}))
    answered = wrong_route = wrong_intent = needless = 0
    leaks: list[tuple[str, str, str, float]] = []
    reasons: dict[str, int] = defaultdict(int)
    by_fluency: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    decided_by_t = 0

    for item in prepared:
        labels = item["labels"]
        classification = item[basis]
        decision = router.decide(item["ticket"], classification, item["passages"])
        fluency = item["ticket"].language_fluency
        by_fluency[fluency][1] += 1
        if decision.decision == "auto_respond":
            answered += 1
            by_fluency[fluency][0] += 1
            if labels.get("expected_route") == "escalate":
                wrong_route += 1
            if labels.get("must_not_auto_respond"):
                # FR-09 fires on the *predicted* intent, so a misclassification can miss it.
                leaks.append((item["ticket"].ticket_id, str(labels.get("intent")),
                              classification.intent, round(classification.intent_confidence, 4)))
            if labels.get("intent") != classification.intent:
                wrong_intent += 1
        else:
            reasons[decision.reason] += 1
            if labels.get("expected_route") == "auto_respond":
                needless += 1
            # The slice T actually decides: escalated, and low confidence was the only reason.
            if decision.all_reasons == ("low_confidence",):
                decided_by_t += 1

    total = len(prepared)
    return {
        "basis": basis,
        "threshold": threshold,
        "answered": answered,
        "answered_pct": _pct(answered, total),
        "escalated_pct": _pct(total - answered, total),
        "wrong_route": wrong_route,
        "wrong_intent": wrong_intent,
        "wrong_intent_pct": _pct(wrong_intent, answered),
        "must_escalate_answered": len(leaks),
        "leaks": leaks,
        "needless": needless,
        "needless_pct": _pct(needless, total - answered),
        "decided_by_t": decided_by_t,
        "reasons": dict(reasons),
        "fluency": {k: tuple(v) for k, v in sorted(by_fluency.items())},
    }


def _gap(row: dict, fluencies: list[str]) -> float:
    """The widest fluency difference in one row, which NFR-06 caps at 5 points."""
    shares = [_pct(*row["fluency"].get(f, (0, 0))) for f in fluencies]
    return round(max(shares) - min(shares), 1) if shares else 0.0


def _calibration_caveat(model) -> str:
    """FR-02's third PRD criterion is about calibration, and it is not met in every band."""
    bands = [b for b in model.report.intent_calibration
             if b.gap_points is not None and not b.low_confidence]
    if not bands:
        return ("The classifier's report carries no usable calibration band, so nothing here can "
                "say how far the stated confidence is from the observed accuracy.")
    worst = max(bands, key=lambda b: b.gap_points)
    second = sorted(b.gap_points for b in bands)[-2] if len(bands) > 1 else worst.gap_points
    return (
        "PRD FR-02's third acceptance criterion is *calibration within 5 points per confidence "
        "band*, and it is measured in `evaluation/reports/classifier_calibration.md` (T-FR08-18), "
        f"not here. The worst band is **{worst.lower:.2f}–{worst.upper:.2f}: {worst.count} "
        f"predictions stating {worst.mean_confidence:.1f}% and observed to be right "
        f"{worst.observed_accuracy:.1f}% of the time — {worst.gap_points} points out**, against a "
        f"limit of 5. Every other band is within {second} points of observed accuracy.\n\n"
        "This matters for the choice below: the must-escalate tickets that a misclassification "
        "lets through state confidences in the high 0.7s and low 0.8s, which is exactly where the "
        "stated number is least trustworthy. A T set inside a badly calibrated band does not buy "
        "the escalation rate it looks like on paper.")


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _render(rows: list[dict], leaked: list[dict], prepared: list[dict], stats, args,
            base: Settings, input_path: Path, missing_out_of_fold: int, model) -> str:
    total = len(prepared)
    labelled_escalate = sum(1 for p in prepared
                            if p["labels"].get("expected_route") == "escalate")
    fluencies = sorted({p["ticket"].language_fluency for p in prepared})
    floor = rows[0]  # threshold 0.0: every escalation here comes from a rule T cannot move

    lines = [
        "# Confidence threshold sweep (FR-02)",
        "",
        f"- Tickets: {total} from `{_relative(input_path)}` — development only  ",
        (f"- Labelled `escalate`: {labelled_escalate} "
         f"({_pct(labelled_escalate, total)}%); labelled `auto_respond`: "
         f"{total - labelled_escalate}  "),
        (f"- Relevance threshold held at {base.relevance_threshold} (D-38); top_k "
         f"{base.retrieval_top_k}; corpus `{args.docs}` — {stats.chunks} chunks  "),
        (f"- Classifier: `{args.classifier}`, fingerprint `{model.fingerprint}`  "),
        (f"- Basis: the model's **grouped out-of-fold** predictions and cross-fitted "
         f"confidences — {model.report.intent_accuracy_pct}% intent accuracy, against "
         f"{model.report.intent_accuracy_naive_pct}% row-wise (D-39)"
         + (f"; {missing_out_of_fold} ticket(s) fell back to in-sample  "
            if missing_out_of_fold else "  ")),
        "",
        "Generated by `scripts/confidence_sweep.py`. **This report chooses nothing**: T is the",
        "author's decision at checkpoint row 10.",
        "",
        "## What each T would do",
        "",
        ("| T | answered | escalated | answered against the label | answered on a wrong intent "
         "| must-escalate answered | needless escalations | tickets T alone decided |"),
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['threshold']:.3f} | {row['answered']} ({row['answered_pct']}%) "
            f"| {row['escalated_pct']}% | {row['wrong_route']} | {row['wrong_intent']} "
            f"({row['wrong_intent_pct']}% of answered) | {row['must_escalate_answered']} "
            f"| {row['needless']} ({row['needless_pct']}% of escalations) "
            f"| {row['decided_by_t']} |")

    lines += [
        "",
        (f"**T is not the escalation lever it looks like.** At T = 0, where the threshold never "
         f"fires, {floor['escalated_pct']}% of tickets still escalate: every other rule "
         "(FR-09's four intents, FR-03's money and date triggers, FR-07's defects, FR-10 "
         "returning nothing) is untouched by T. The right-hand column is the honest measure of "
         "what T decides — the tickets whose *only* reason to escalate is confidence."),
        "",
        ("**`must-escalate answered` is the finding of this sweep, and it is not 0.** FR-09's "
         "rule is exact — it never lets a *predicted* `security_incident`, `compliance_request`, "
         "`feature_request` or `unclear_request` through, and T-FR09-1 to T-FR09-8 hold that in "
         "place. But it fires on the predicted intent, and the classifier is 88.6% accurate on "
         "wording it has not seen, so a must-escalate ticket misread as something answerable is "
         "not caught by the rule. On in-sample predictions this column reads 0 at every T, which "
         "is why the leaked table below is kept: the PRD's \"zero auto-responses\" criterion is "
         "met by the rule and by a harness run over data the model was fitted on, and **T is the "
         "only thing standing behind it on unseen tickets**. That is the trade-off this "
         "checkpoint is really about."),
        "",
        "## The same sweep on in-sample predictions, which is what a leak looks like",
        "",
        ("The saved model was fitted on these 500 tickets. Asking it to classify them again is a "
         "lookup: below is the identical sweep run that way. It reports **0 wrong intents at "
         "every T** and a flat answer rate, which is how this script read before the basis was "
         "fixed — the row-8 mistake (D-39) in a new place. The difference between the two tables "
         "is the size of the leak, not a modelling choice."),
        "",
        ("| T | answered (out of fold) | answered (in sample) | wrong intent (out of fold) "
         "| wrong intent (in sample) |"),
        "|---|---|---|---|---|",
    ]
    for honest, leak in zip(rows, leaked, strict=True):
        lines.append(
            f"| {honest['threshold']:.3f} | {honest['answered']} ({honest['answered_pct']}%) "
            f"| {leak['answered']} ({leak['answered_pct']}%) | {honest['wrong_intent']} "
            f"| {leak['wrong_intent']} |")

    lines += [
        "",
        "## The confidence this table thresholds is not uniformly trustworthy",
        "",
        _calibration_caveat(model),
        "",
        "## The must-escalate tickets a misclassification would let through",
        "",
        ("Every ticket labelled `must_not_auto_respond` that the out-of-fold model would have "
         "answered at T = 0, with the confidence it stated. Read the confidence column as the T "
         "that would have caught it: a T above the value shown escalates the ticket for low "
         "confidence instead, which is the second line of defence FR-09's spec §7 names."),
        "",
        "| ticket | labelled intent | predicted intent | stated confidence |",
        "|---|---|---|---|",
    ]
    for ticket_id, labelled, predicted, confidence in sorted(rows[0]["leaks"],
                                                            key=lambda row: -row[3]):
        lines.append(f"| {ticket_id} | {labelled} | {predicted} | {confidence:.4f} |")
    if not rows[0]["leaks"]:
        lines.append("| — | — | — | none |")

    lines += [
        "",
        "## Why each ticket escalated",
        "",
        "| T | " + " | ".join(PRECEDENCE) + " |",
        "|---|" + "---|" * len(PRECEDENCE),
    ]
    for row in rows:
        counts = " | ".join(str(row["reasons"].get(reason, 0)) for reason in PRECEDENCE)
        lines.append(f"| {row['threshold']:.3f} | {counts} |")

    lines += [
        "",
        ("Only the `low_confidence` column moves with T; the rest are the fixed cost of the "
         "rules. `kill_switch` is 0 by construction — the sweep refuses to run with the switch "
         "on, because every row would read the same."),
        "",
        "## Answer rate by language fluency (NFR-06)",
        "",
        "| T | " + " | ".join(fluencies) + " | gap (points) |",
        "|---|" + "---|" * (len(fluencies) + 1),
    ]
    for row in rows:
        shares = [_pct(*row["fluency"].get(f, (0, 0))) for f in fluencies]
        flag = " ⚠" if _gap(row, fluencies) >= 5.0 else ""
        lines.append(f"| {row['threshold']:.3f} | "
                     + " | ".join(f"{s}%" for s in shares)
                     + f" | {_gap(row, fluencies)}{flag} |")

    lines += [
        "",
        ("**NFR-06 allows under 5 percentage points of difference across fluency.** The rows "
         "above that exceed it: "
         + (", ".join(f"T = {row['threshold']:.3f} ({_gap(row, fluencies)} points)"
                      for row in rows if _gap(row, fluencies) >= 5.0) or "none")
         + ". A T chosen off the wrong-answer columns alone can therefore breach a "
           "non-functional requirement, which is why the column is here and not in an appendix. "
           "Note the two lowest rows read 0.0 only because almost nothing is answered at all."),
        "",
        ("The fairness question is whether raising T takes answers away from non-fluent writers "
         "faster than from fluent ones. Read the gap column with the population sizes: "
         + ", ".join(f"{f} {sum(1 for p in prepared if p['ticket'].language_fluency == f)}"
                     for f in fluencies)
         + "."),
        "",
        "## What this report cannot tell you",
        "",
        ("- **`answered on a wrong intent` is a proxy, not a measured wrong answer.** The drafter "
         "arrives at row 11 and the guardrails at row 12; until then nobody knows whether a "
         "wrong-intent ticket would have produced a wrong reply or an honest \"I could not find "
         "this\". The column is the upper bound on how often T lets a misclassification through."),
        ("- **The labels are the pack's, and 42 validation tickets duplicate development text.** "
         "These figures are development-set figures; the gate run at row 15 is what tests them."),
        ("- **One T serves 22 intents.** A class the classifier gets right 60% of the time and "
         "one it never misses share the same floor. Per-intent thresholds are a small change and "
         "deliberately not made before the author has seen this curve."),
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
