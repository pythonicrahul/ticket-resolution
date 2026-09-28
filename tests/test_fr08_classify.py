"""FR-08 and FR-05 acceptance tests (docs/specs/FR-08.md).

Offline, with an injected deterministic embedder. These test **behaviour**: every output has the
fields the requirements name, nothing raises, the fallback is safe, the calibration arithmetic is
right, and the queue orders as FR-05 says. Whether the classifier is any *good* is measured by
`scripts/train_classifier.py` with the real model — a hashing embedder cannot say (same division
as FR-10).
"""
import json
from pathlib import Path

import pytest

from ticketing_agent.classify import (
    FALLBACK_INTENT,
    URGENCY_ORDER,
    Classification,
    ClassifierError,
    IntentClassifier,
    calibration_table,
    order_escalation_queue,
    train,
)
from ticketing_agent.ingest import normalise_ticket
from ticketing_agent.retrieve import HashingEmbedder

ROOT = Path(__file__).resolve().parents[1]
DEV = ROOT / "data" / "development_tickets.json"
FIXTURES = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="module")
def trained():
    """One model for the whole module: training 500 hashed embeddings is fast but not free."""
    return train(DEV, embedder=HashingEmbedder())


@pytest.fixture(scope="module")
def classifier(trained):
    return IntentClassifier(trained, embedder=HashingEmbedder())


@pytest.fixture(scope="module")
def dev_tickets():
    entries = json.loads(DEV.read_text(encoding="utf-8"))
    return [normalise_ticket(e, index=i) for i, e in enumerate(entries)]


def test_T_FR08_1_every_ticket_gets_an_intent_a_confidence_and_alternatives(classifier,
                                                                           dev_tickets, trained):
    for ticket in dev_tickets:
        result = classifier.classify(ticket)
        assert isinstance(result, Classification)
        assert result.intent in trained.intents, result.intent
        assert 0.0 <= result.intent_confidence <= 1.0
        assert result.intent_alternatives, "the alternatives considered are part of the output"


def test_T_FR08_2_alternatives_are_the_rest_of_the_ranking(classifier, dev_tickets):
    for ticket in dev_tickets[:50]:
        result = classifier.classify(ticket)
        names = [name for name, _ in result.intent_alternatives]
        scores = [score for _, score in result.intent_alternatives]

        assert result.intent not in names, "the chosen intent is not its own alternative"
        assert len(names) == len(set(names))
        assert len(names) <= 3
        assert scores == sorted(scores, reverse=True)
        assert all(0.0 <= s <= 1.0 for s in scores)
        assert all(s <= result.intent_confidence + 1e-9 for s in scores), (
            "an alternative cannot be more probable than the choice")


@pytest.mark.parametrize("entry", [
    {"ticket_id": "SYN-EMPTY", "channel": "chat", "subject": "", "body": ""},
    {"ticket_id": "SYN-SPACE", "channel": "chat", "subject": "   ", "body": "\n\t "},
    {"ticket_id": "SYN-ODD", "channel": "sms", "subject": "", "body": "x"},
    "not a ticket object at all",
])
def test_T_FR08_3_an_unusable_ticket_falls_back_rather_than_raising(classifier, entry):
    """FR-08: fall back to unclear_request rather than raising an error."""
    ticket = normalise_ticket(entry, index=0)
    result = classifier.classify(ticket)

    # Every case must produce a usable Classification, whatever the input was.
    assert isinstance(result, Classification)
    assert result.urgency in URGENCY_ORDER
    assert 0.0 <= result.intent_confidence <= 1.0
    assert result.urgency_reason, "there is always a reason, including for the fallback"

    if not ticket.text.strip():
        assert result.intent == FALLBACK_INTENT
        assert result.intent_confidence == 0.0
        assert result.fallback is True


def test_T_FR08_4_a_model_that_breaks_mid_classification_falls_back(trained, dev_tickets):
    """FR-08: fall back rather than raise — but only for a failure inside classification.

    A *mismatched* embedder is a different thing and must fail loudly at construction (T-FR08-9):
    with the guard off it produced a per-ticket error that the fallback turned into a run of
    `unclear_request` looking like a calibration result (review finding 7). There is deliberately
    no flag to disable the check, so this test breaks the model instead.
    """
    class BrokenModel:
        intent_model = trained.intent_model
        urgency_model = trained.urgency_model
        intents = trained.intents
        urgencies = trained.urgencies
        fingerprint = trained.fingerprint
        embedder_signature = trained.embedder_signature
        training_ids = trained.training_ids
        training_vectors = trained.training_vectors
        training_urgencies = trained.training_urgencies
        intent_confidence_calibrator = None
        urgency_confidence_calibrator = None

    broken = BrokenModel()
    broken.intent_model = _Exploding()
    classifier = IntentClassifier(broken, embedder=HashingEmbedder())
    result = classifier.classify(dev_tickets[0])

    assert result.fallback is True
    assert result.intent == FALLBACK_INTENT
    assert result.intent_confidence == 0.0
    assert "fell back" in result.urgency_reason


class _Exploding:
    """A model whose prediction fails, which is what the fallback exists for."""

    def predict_proba(self, *_args, **_kwargs):
        raise RuntimeError("the model is gone")


def test_T_FR08_4b_there_is_no_way_to_disable_the_embedder_check():
    """CLAUDE.md: no flag may switch off a check that keeps the system honest."""
    import inspect

    from ticketing_agent import classify as module

    params = inspect.signature(IntentClassifier.__init__).parameters
    assert not {"verify", "strict", "check", "skip_verify"} & set(params)
    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "verify=False" not in source


def test_T_FR08_5_classification_is_deterministic(classifier, dev_tickets):
    for ticket in dev_tickets[:25]:
        first, second = classifier.classify(ticket), classifier.classify(ticket)
        assert first == second


def test_T_FR08_6_ground_truth_is_never_read(classifier, dev_tickets):
    """A classifier that peeked at labels would score perfectly and mean nothing."""
    for ticket in dev_tickets[:30]:
        stripped = normalise_ticket(
            {k: v for k, v in ticket.raw.items() if k not in {"labels", "history"}},
            index=ticket.source_index)
        assert classifier.classify(ticket) == classifier.classify(stripped)


def test_T_FR08_7_training_uses_the_path_it_is_given(tmp_path):
    small = tmp_path / "three-intents.json"
    entries = json.loads(DEV.read_text(encoding="utf-8"))
    keep = {"billing_query", "deployment_failure", "onboarding"}
    subset = [e for e in entries if e["labels"]["intent"] in keep]
    small.write_text(json.dumps(subset), encoding="utf-8")

    model = train(small, embedder=HashingEmbedder())
    assert set(model.intents) == keep
    assert model.training_tickets == len(subset)
    assert model.fingerprint != train(DEV, embedder=HashingEmbedder()).fingerprint


def test_T_FR08_8_a_saved_model_reloads_and_classifies_identically(trained, dev_tickets, tmp_path):
    path = tmp_path / "classifier.joblib"
    trained.save(path)
    from ticketing_agent.classify import TrainedClassifier

    reloaded = TrainedClassifier.load(path)
    assert reloaded.fingerprint == trained.fingerprint
    assert reloaded.intents == trained.intents

    original = IntentClassifier(trained, embedder=HashingEmbedder())
    copy = IntentClassifier(reloaded, embedder=HashingEmbedder())
    for ticket in dev_tickets[:20]:
        assert original.classify(ticket) == copy.classify(ticket)


def test_T_FR08_9_a_model_trained_with_another_embedder_is_rejected(trained):
    """D-33's lesson: serving predictions from a mismatched model is worse than having none."""
    with pytest.raises(ClassifierError, match="embedder"):
        IntentClassifier(trained, embedder=HashingEmbedder(dim=64))


def test_T_FR08_10_the_calibration_arithmetic_is_right():
    # Two bands populated: 0.0-0.2 with one wrong prediction, 0.8-1.0 with three of four right.
    bands = calibration_table(
        probabilities=[0.10, 0.85, 0.90, 0.95, 0.82],
        correct=[False, True, True, True, False],
        bands=5,
    )
    assert len(bands) == 5
    low, high = bands[0], bands[4]

    assert (low.lower, low.upper) == (0.0, 0.2)
    assert low.count == 1
    assert low.mean_confidence == 10.0
    assert low.observed_accuracy == 0.0
    assert low.gap_points == 10.0

    assert high.count == 4
    assert high.mean_confidence == 88.0, "mean of 85, 90, 95, 82"
    assert high.observed_accuracy == 75.0, "three of four right"
    assert high.gap_points == 13.0
    assert high.low_confidence is True, "four predictions cannot demonstrate a 5-point claim"

    empty = bands[2]
    assert empty.count == 0 and empty.mean_confidence is None
    assert empty.observed_accuracy is None and empty.gap_points is None

    assert calibration_table([], [], bands=5)[0].count == 0


def test_T_FR08_11_the_reported_figures_are_out_of_fold_and_leak_free(trained, dev_tickets):
    """Two separate claims: the score is out of fold, and the folds do not share a ticket body.

    On this data the second matters more than the first. The 500 development tickets hold ~215
    distinct bodies, and every repeated body carries one intent, so row-wise folds put the same
    wording in training and test and the score becomes a near-duplicate lookup (D-39).
    """
    report = trained.report
    assert report.cross_validated is True
    assert report.grouped is True, "the reported figure must come from body-grouped folds"
    assert report.folds >= 2
    assert len(report.out_of_fold_predictions) == trained.training_tickets

    # The leak is real and large, so the two figures must differ: if they ever match, the
    # grouping has stopped working.
    assert report.intent_accuracy_naive_pct - report.intent_accuracy_pct > 5.0, (
        f"grouped {report.intent_accuracy_pct}% vs row-wise "
        f"{report.intent_accuracy_naive_pct}%: the body grouping is not taking effect")
    assert report.distinct_texts < trained.training_tickets, (
        "this dataset repeats ticket bodies; if it stopped, revisit D-39")

    # And the reported figure is not the training fit, which would be near-perfect.
    import numpy as np

    from ticketing_agent.retrieve import HashingEmbedder as _Embedder

    vectors = np.asarray(_Embedder()([t.text for t in dev_tickets if t.text.strip()]), dtype=float)
    in_fold = trained.intent_model.score(
        vectors, [t.raw["labels"]["intent"] for t in dev_tickets if t.text.strip()])
    assert report.intent_accuracy_pct < 100.0 * in_fold, (
        "the reported accuracy is not below the training fit, so it is the training fit")


def test_T_FR08_14_no_ticket_body_appears_in_two_folds(trained):
    """The grouping is the protection, so assert it directly rather than trusting the number."""
    import hashlib

    from sklearn.model_selection import StratifiedGroupKFold

    entries = json.loads(DEV.read_text(encoding="utf-8"))
    usable = [e for e in entries if e["labels"].get("intent") and (e["body"] or e["subject"])]
    groups = [hashlib.sha256((e["body"] or "").strip().lower().encode()).hexdigest()
              for e in usable]
    labels = [e["labels"]["intent"] for e in usable]

    splitter = StratifiedGroupKFold(n_splits=trained.report.folds, shuffle=True, random_state=0)
    for train_idx, test_idx in splitter.split(usable, labels, groups):
        shared = {groups[i] for i in train_idx} & {groups[i] for i in test_idx}
        assert not shared, f"{len(shared)} ticket bodies appear in both folds"


def test_T_FR08_12_per_class_precision_and_recall_are_reported(trained):
    per_class = trained.report.intent_per_class
    assert set(per_class) == set(trained.intents)
    for name, row in per_class.items():
        assert row["support"] > 0, name
        for key in ("precision_pct", "recall_pct"):
            assert row[key] is None or 0.0 <= row[key] <= 100.0
    assert sum(row["support"] for row in per_class.values()) == trained.training_tickets


def test_T_FR05_1_every_ticket_gets_an_urgency_and_a_confidence(classifier, dev_tickets):
    for ticket in dev_tickets[:100]:
        result = classifier.classify(ticket)
        assert result.urgency in URGENCY_ORDER
        assert 0.0 <= result.urgency_confidence <= 1.0


def test_T_FR05_2_the_urgency_reason_is_checkable_evidence(classifier, dev_tickets):
    """FR-05 asks for the reason; nearest labelled tickets are true and readable (§3.4)."""
    known = {t.ticket_id for t in dev_tickets}
    result = classifier.classify(dev_tickets[7])

    assert result.evidence, "the reason must rest on something"
    for ticket_id, label, similarity in result.evidence:
        assert ticket_id in known, f"{ticket_id} is not a real training ticket"
        assert label in URGENCY_ORDER
        assert 0.0 <= similarity <= 1.0
        assert ticket_id in result.urgency_reason
    assert "closest to" in result.urgency_reason


def test_T_FR05_3_the_queue_is_ordered_by_urgency_then_age():
    items = [
        {"ticket_id": "A", "urgency": "medium", "received_at": "2026-05-01T09:00:00Z"},
        {"ticket_id": "B", "urgency": "high", "received_at": "2026-05-03T09:00:00Z"},
        {"ticket_id": "C", "urgency": "high", "received_at": "2026-05-01T09:00:00Z"},
        {"ticket_id": "D", "urgency": "low", "received_at": "2026-04-01T09:00:00Z"},
        {"ticket_id": "E", "urgency": "high", "received_at": None},
    ]
    assert [i["ticket_id"] for i in order_escalation_queue(items)] == ["C", "B", "E", "A", "D"]


def test_T_FR05_4_the_queue_order_is_stable_and_deterministic():
    items = [{"ticket_id": name, "urgency": "medium", "received_at": "2026-05-01T09:00:00Z"}
             for name in ("first", "second", "third")]
    once = [i["ticket_id"] for i in order_escalation_queue(items)]
    assert once == ["first", "second", "third"], "equal keys keep their order"
    assert once == [i["ticket_id"] for i in order_escalation_queue(items)]


def test_T_FR08_13_a_classification_fits_the_decision_log(classifier, dev_tickets):
    from ticketing_agent.logging_store import DecisionEntry

    ticket = dev_tickets[0]
    result = classifier.classify(ticket)
    entry = DecisionEntry(
        ticket_id=ticket.ticket_id, source_index=ticket.source_index, stage="classification",
        decision="continue", requirement_ids=("FR-08", "FR-05"),
        **result.log_fields(),
    )
    assert entry.prediction_value == result.intent
    assert entry.prediction_confidence == result.intent_confidence
    assert entry.intent == result.intent
    assert entry.urgency == result.urgency
    assert tuple(entry.intent_alternatives) == result.intent_alternatives
    assert entry.urgency_confidence == result.urgency_confidence
    assert result.model_fingerprint in (entry.detail or "")


def test_T_FR08_15_the_measured_model_is_the_shipped_model(trained):
    """The report described a different estimator from the one that shipped, once.

    The urgency model is class-weighted to stop it collapsing onto the majority class; the
    out-of-fold estimator that produced the reported figures was not, so the report measured a
    model nobody would ever run. Both now come from one factory, and this pins it.
    """
    intent_base = trained.intent_model.estimator
    urgency_base = trained.urgency_model.estimator

    assert urgency_base.class_weight == "balanced", (
        "urgency is weighted: unweighted it never predicts `low` at all")
    assert intent_base.class_weight is None, "intent's 22 classes are already near-balanced"
    for base in (intent_base, urgency_base):
        assert base.random_state == 0, "determinism (NFR-08) depends on this"
        assert base.max_iter >= 2000
    assert trained.intent_model.method == "sigmoid"


def test_T_FR08_16_the_stated_confidence_is_calibrated_not_raw(trained, dev_tickets):
    """NFR-03 is a claim about the number the system states, so that number is what is fitted.

    Per-class sigmoid calibration over 22 classes left confidence 30 points below observed
    accuracy; a one-dimensional fit from top-probability to correctness closes it.
    """
    assert trained.intent_confidence_calibrator is not None
    assert trained.urgency_confidence_calibrator is not None

    classifier = IntentClassifier(trained, embedder=HashingEmbedder())
    raw_and_stated = []
    for ticket in dev_tickets[:60]:
        result = classifier.classify(ticket)
        raw = float(trained.intent_model.predict_proba(
            [HashingEmbedder()([ticket.text])[0]])[0].max())
        raw_and_stated.append((raw, result.intent_confidence))

    assert any(abs(raw - stated) > 0.01 for raw, stated in raw_and_stated), (
        "the stated confidence is the raw probability, so nothing was calibrated")
    assert all(0.0 <= stated <= 1.0 for _, stated in raw_and_stated)

    # The reported table is cross-fitted: no prediction is scored by a calibrator that saw it, so
    # a perfect 0.0-point gap would mean the in-sample mistake had crept back in.
    judgeable = [b for b in trained.report.intent_calibration
                 if b.gap_points is not None and not b.low_confidence]
    assert judgeable, "at least one band must carry enough predictions to judge"
    assert any(b.gap_points > 0.0 for b in judgeable), (
        "every gap is exactly zero, which means the calibrator was scored on its own fit")


def test_T_FR08_17_the_stated_confidence_is_not_degenerate(trained, dev_tickets):
    """The Build Spec wants a confidence that "reflects the actual probability of being correct".

    The first calibrator was isotonic on 0/1 targets, whose flat regions emit *exactly* 1.0: it
    stated certainty for 488 of 500 tickets and took four distinct values in total, leaving FR-02
    nothing to threshold on. A confidence that cannot vary is not a confidence.
    """
    classifier = IntentClassifier(trained, embedder=HashingEmbedder())
    stated = [classifier.classify(t).intent_confidence for t in dev_tickets]

    assert len(set(stated)) >= 20, (
        f"only {len(set(stated))} distinct confidence values across {len(stated)} tickets: "
        "the calibrator has saturated")
    saturated = sum(1 for value in stated if value >= 0.999)
    assert saturated / len(stated) < 0.25, (
        f"{saturated} of {len(stated)} tickets state near-certainty from a classifier whose own "
        "accuracy is well below 100%")
    assert max(stated) - min(stated) > 0.05, "the confidence barely varies, so it cannot separate"


def test_T_FR08_18_the_calibration_verdict_is_reported_not_assumed(trained):
    """NFR-03 wants every judgeable band within 5 points. This asserts the *measurement exists*
    and is honest about bands too small to judge — not that the target is met, because on this
    data it is not in every band, and a test that demanded otherwise would be a lie."""
    report = trained.report
    bands = report.intent_calibration

    assert len(bands) == 5
    assert sum(b.count for b in bands) == trained.training_tickets, "every prediction is in a band"
    for band in bands:
        if band.count == 0:
            assert band.gap_points is None
        else:
            assert band.gap_points is not None
            assert band.low_confidence == (band.count < 25)
    assert report.worst_calibration_gap_points is not None
    assert report.calibration_basis.startswith("cross-fitted"), (
        "the table must say what it measured; a calibrator scored on its own fit reads as perfect")


@pytest.mark.parametrize(("bad", "match"), [
    ("missing", "no trained classifier"),
    ("corrupt", "could not be loaded"),
    ("wrong_object", "does not hold"),
])
def test_T_FR08_19_a_model_that_cannot_be_used_fails_loudly(tmp_path, trained, bad, match):
    """Spec §4: a missing or unusable model must not become a silent run of `unclear_request`."""
    import joblib

    from ticketing_agent.classify import TrainedClassifier

    path = tmp_path / "classifier.joblib"
    if bad == "corrupt":
        path.write_bytes(b"not a joblib payload at all")
    elif bad == "wrong_object":
        joblib.dump({"not": "a classifier"}, path)

    with pytest.raises(ClassifierError, match=match):
        TrainedClassifier.load(path)


def test_T_FR08_20_a_stale_trainer_version_is_refused(tmp_path, trained):
    import joblib

    from ticketing_agent.classify import TrainedClassifier

    stale = TrainedClassifier(**{**trained.__dict__, "trainer_version": 0})
    path = tmp_path / "stale.joblib"
    joblib.dump(stale, path)
    with pytest.raises(ClassifierError, match="retrain"):
        TrainedClassifier.load(path)


def test_T_FR05_5_a_malformed_urgency_is_not_sent_to_the_back_of_the_queue():
    """An escalation queue must not guess downwards: "critical" below "low" is the wrong error."""
    items = [
        {"ticket_id": "low", "urgency": "low", "received_at": "2026-05-01T09:00:00Z"},
        {"ticket_id": "critical", "urgency": "critical", "received_at": "2026-05-01T09:00:00Z"},
        {"ticket_id": "missing", "urgency": None, "received_at": "2026-05-01T09:00:00Z"},
        {"ticket_id": "HIGH-shouting", "urgency": "HIGH", "received_at": "2026-05-01T09:00:00Z"},
    ]
    order = [i["ticket_id"] for i in order_escalation_queue(items)]
    assert order[0] == "HIGH-shouting", "case should not change an urgency"
    assert order.index("critical") < order.index("low"), (
        "an unrecognised urgency is treated as medium, never below low")
    assert order.index("missing") < order.index("low")


def test_T_FR05_6_ages_compare_as_times_not_as_strings():
    items = [
        {"ticket_id": "offset", "urgency": "high", "received_at": "2026-05-01T10:00:00+02:00"},
        {"ticket_id": "zulu", "urgency": "high", "received_at": "2026-05-01T09:00:00Z"},
        {"ticket_id": "epoch", "urgency": "high", "received_at": 1000000000},
        {"ticket_id": "unparseable", "urgency": "high", "received_at": "last Tuesday"},
    ]
    order = [i["ticket_id"] for i in order_escalation_queue(items)]
    # 08:00Z (the +02:00 offset) is older than 09:00Z, and the epoch value is older than both.
    assert order[:3] == ["epoch", "offset", "zulu"], order
    assert order[-1] == "unparseable", "an unreadable timestamp sorts last, it does not crash"


def test_T_FR05_7_the_reason_never_cites_the_ticket_itself(classifier, dev_tickets):
    """"This ticket resembles itself, whose label is X" is not a reason, and it would put another
    ticket's ground-truth urgency into the decision log."""
    for ticket in dev_tickets[:40]:
        result = classifier.classify(ticket)
        cited = [ticket_id for ticket_id, _, _ in result.evidence]
        assert ticket.ticket_id not in cited, f"{ticket.ticket_id} is its own evidence"
        assert ticket.ticket_id not in result.urgency_reason
