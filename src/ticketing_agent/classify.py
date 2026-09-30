"""FR-08, FR-05: intent and urgency with calibrated confidence and the alternatives considered.

Spec: docs/specs/FR-08.md. Embeddings plus calibrated logistic regression, not a model call
(D-03): routing has to be deterministic (NFR-08), a language model's self-reported confidence is
not calibrated while a fitted classifier's can be, and this costs nothing (NFR-07) and keeps
working during a provider outage (A11).

Three things are deliberate:

* **Calibration is cross-validated, never the training fit.** NFR-03 wants stated confidence
  within 5 points of observed accuracy, and a model scored on its own training data would look
  near-perfect and prove nothing.
* **Nothing raises.** FR-08 says the fallback is `unclear_request`, which FR-09 escalates by rule,
  so an unclassifiable ticket is safe by construction rather than by luck.
* **The reason FR-05 asks for is evidence, not a fabricated rationale.** A logistic regression over
  embeddings has no readable features, so the classifier reports the nearest labelled training
  tickets instead of inventing a sentence about keywords.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ticketing_agent.ingest import Ticket, evaluation_labels, load_tickets

_log = logging.getLogger(__name__)

#: Bumped when a training rule changes, so a stale model is never served (D-33's lesson).
TRAINER_VERSION = 1

#: FR-08's fallback, which FR-09 escalates by rule.
FALLBACK_INTENT = "unclear_request"
#: FR-05's urgency ordering, most urgent first: the escalation queue's primary sort key.
URGENCY_ORDER = ("high", "medium", "low")
FALLBACK_URGENCY = "medium"

#: How many alternatives the Build Specification's "the alternatives it considered" needs.
MAX_ALTERNATIVES = 3
#: Nearest labelled tickets shown as the reason (FR-05 §3.4).
EVIDENCE_NEIGHBOURS = 2
#: A calibration band with fewer than this cannot demonstrate a 5-point claim (§3.3 rule 9).
MIN_BAND_FOR_CONFIDENCE = 25


class ClassifierError(Exception):
    """FR-08 §4: the model cannot be used at all. Raised at construction, never per ticket."""


@dataclass(frozen=True)
class Classification:
    """FR-08, FR-05: one ticket's intent and urgency, with everything the log and an agent need."""

    intent: str
    intent_confidence: float
    intent_alternatives: tuple[tuple[str, float], ...]
    urgency: str
    urgency_confidence: float
    urgency_reason: str
    evidence: tuple[tuple[str, str, float], ...] = ()
    fallback: bool = False
    model_fingerprint: str = ""

    def row_fields(self) -> dict[str, Any]:
        """FR-13, FR-05, FR-08: what the classifier decided, and nothing a caller owns.

        Deliberately narrower than `log_fields`: no `detail` and no `prediction_*`, because
        routing, drafting and the guardrails each own those on their own rows and a merge that
        overwrote them produced rows that contradicted themselves (R2).
        """
        return {
            "intent": self.intent,
            "intent_confidence": self.intent_confidence,
            "intent_alternatives": self.intent_alternatives,
            "urgency": self.urgency,
            "urgency_confidence": self.urgency_confidence,
            # FR-05 asks for the urgency, its confidence *and the reason*. It used to travel
            # only inside `detail`, which `row_fields` drops — so it reached no row at all.
            "urgency_reason": self.urgency_reason,
        }

    def log_fields(self) -> dict[str, Any]:
        """FR-13: the classification half of a decision-log row, ready to splat into an entry."""
        return {
            **self.row_fields(),
            "prediction_value": self.intent,
            "prediction_confidence": self.intent_confidence,
            "detail": f"model {self.model_fingerprint}; {self.urgency_reason}",
        }


@dataclass(frozen=True)
class CalibrationBand:
    """One row of the Evaluation Framework §3 calibration table."""

    lower: float
    upper: float
    count: int
    mean_confidence: float | None
    observed_accuracy: float | None
    gap_points: float | None
    low_confidence: bool = False


@dataclass(frozen=True)
class TrainingReport:
    """What the fit measured, out of fold. Consumed by the metrics report and row 10.

    `intent_accuracy_pct` is the **grouped** figure: folds are split so that no ticket body
    appears in both training and test. That is the honest generalisation estimate on this data,
    and it is much lower than the row-wise one (D-39). `*_naive_pct` keeps the row-wise figure
    beside it so the size of the leak is visible rather than hidden.
    """

    cross_validated: bool
    folds: int
    grouped: bool
    grouped_folds: int
    #: Distinct wording clusters (near-duplicates merged) — the grouping the headline figure uses.
    distinct_texts: int
    #: Distinct exact bodies, for contrast: the gap between the two is the paraphrase problem.
    distinct_bodies: int
    calibration_basis: str
    intent_accuracy_pct: float
    intent_accuracy_naive_pct: float
    urgency_accuracy_naive_pct: float
    urgency_accuracy_pct: float
    intent_per_class: dict[str, dict[str, Any]]
    urgency_per_class: dict[str, dict[str, Any]]
    intent_calibration: tuple[CalibrationBand, ...]
    urgency_calibration: tuple[CalibrationBand, ...]
    out_of_fold_predictions: tuple[tuple[str, str, float], ...]
    worst_calibration_gap_points: float | None


@dataclass
class TrainedClassifier:
    """A fitted, calibrated pair of models plus the report that says how good they are."""

    intent_model: Any
    urgency_model: Any
    intents: tuple[str, ...]
    urgencies: tuple[str, ...]
    embedder_signature: str
    fingerprint: str
    training_tickets: int
    report: TrainingReport
    #: Training embeddings and labels, kept for the nearest-neighbour evidence FR-05 asks for.
    training_ids: tuple[str, ...] = ()
    training_vectors: Any = None
    training_urgencies: tuple[str, ...] = ()
    #: Maps the model's top probability onto the observed accuracy at that level, fitted on the
    #: grouped out-of-fold predictions. NFR-03 is about this number, not the full distribution.
    intent_confidence_calibrator: Any = None
    urgency_confidence_calibrator: Any = None
    trainer_version: int = TRAINER_VERSION

    def save(self, path: str | Path) -> Path:
        """Persist the model. Training is an artefact step, never part of an evaluation run."""
        import joblib

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, path)
        return path

    @staticmethod
    def load(path: str | Path) -> TrainedClassifier:
        import joblib

        path = Path(path)
        if not path.exists():
            raise ClassifierError(
                f"no trained classifier at {path}. Build one with "
                "`uv run python scripts/train_classifier.py` — the harness never trains during a "
                "run, so a missing model is a setup error rather than a silent fallback.")
        try:
            model = joblib.load(path)
        except Exception as exc:  # noqa: BLE001 - a corrupt artefact is a setup error
            raise ClassifierError(f"the classifier at {path} could not be loaded: {exc}") from None
        if not isinstance(model, TrainedClassifier):
            raise ClassifierError(f"{path} does not hold a trained classifier")
        if model.trainer_version != TRAINER_VERSION:
            raise ClassifierError(
                f"the classifier at {path} was trained by version {model.trainer_version}, "
                f"this is version {TRAINER_VERSION}: retrain it")
        return model


class IntentClassifier:
    """FR-08, FR-05: classify one ticket. Never raises; falls back instead."""

    def __init__(self, model: TrainedClassifier, embedder: Any | None = None) -> None:
        self._model = model
        self._embedder = embedder if embedder is not None else _default_embedder()
        # No flag disables this. With the check off, a mismatched embedder produced a per-ticket
        # dimension error that the fallback turned into a run of `unclear_request` — exactly the
        # state spec §4 forbids, because it looks like a calibration result.
        signature = _embedder_signature(self._embedder)
        if signature != model.embedder_signature:
            raise ClassifierError(
                "this classifier was trained with a different embedder "
                f"({model.embedder_signature!r}, now {signature!r}). Predictions from a "
                "mismatched model are meaningless, so it is not served — retrain it.")

    def classify(self, ticket: Ticket) -> Classification:
        """FR-08: an intent, a confidence and the alternatives — or the fallback, never an error."""
        text = (getattr(ticket, "text", "") or "").strip()
        if not text:
            return self._fallback("the ticket has no text to classify")
        try:
            vector = self._embedder([text])[0]
            intent, intent_confidence, alternatives = self._predict(
                self._model.intent_model, self._model.intents, vector,
                getattr(self._model, "intent_confidence_calibrator", None))
            urgency, urgency_confidence, _ = self._predict(
                self._model.urgency_model, self._model.urgencies, vector,
                getattr(self._model, "urgency_confidence_calibrator", None))
            evidence = self._nearest(vector, exclude=ticket.ticket_id)
        except Exception as exc:  # noqa: BLE001 - FR-08: fall back rather than raising
            _log.warning("classification fell back for %s: %s", ticket.ticket_id, exc)
            return self._fallback(f"classification failed ({type(exc).__name__})")

        return Classification(
            intent=intent,
            intent_confidence=intent_confidence,
            intent_alternatives=alternatives,
            urgency=urgency,
            urgency_confidence=urgency_confidence,
            urgency_reason=_reason(evidence),
            evidence=evidence,
            fallback=False,
            model_fingerprint=self._model.fingerprint,
        )

    # --- internals ------------------------------------------------------------------

    def _predict(self, model: Any, classes: tuple[str, ...], vector: list[float],
                 calibrator: Any = None) -> tuple[str, float, tuple[tuple[str, float], ...]]:
        probabilities = model.predict_proba([vector])[0]
        ranked = sorted(zip(classes, probabilities, strict=True),
                        key=lambda pair: (-float(pair[1]), pair[0]))
        best = ranked[0][0]
        raw = float(ranked[0][1])
        confidence = round(_apply_calibrator(calibrator, raw), 4)
        alternatives = tuple((name, round(float(score), 4))
                             for name, score in ranked[1:1 + MAX_ALTERNATIVES])
        return best, confidence, alternatives

    def _nearest(self, vector: list[float],
                 exclude: str = "") -> tuple[tuple[str, str, float], ...]:
        """FR-05 §3.4: the nearest labelled training tickets, as readable evidence.

        A ticket from the training file excludes itself: "this ticket resembles itself, whose
        label is X" is not a reason a support manager can use, and it would surface that ticket's
        ground-truth urgency in the decision log.
        """
        if self._model.training_vectors is None or not len(self._model.training_ids):
            return ()
        import numpy as np

        matrix = np.asarray(self._model.training_vectors, dtype=float)
        query = np.asarray(vector, dtype=float)
        norms = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(query) or 1.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            similarities = np.where(norms > 0, matrix @ query / np.where(norms > 0, norms, 1), 0.0)
        order = [i for i in np.argsort(-similarities, kind="stable")
                 if self._model.training_ids[int(i)] != exclude][:EVIDENCE_NEIGHBOURS]
        return tuple(
            (self._model.training_ids[int(i)], self._model.training_urgencies[int(i)],
             round(float(max(0.0, min(1.0, similarities[int(i)]))), 4))
            for i in order
        )

    def _fallback(self, why: str) -> Classification:
        return Classification(
            intent=FALLBACK_INTENT,
            intent_confidence=0.0,
            intent_alternatives=(),
            urgency=FALLBACK_URGENCY,
            urgency_confidence=0.0,
            urgency_reason=f"fell back: {why}",
            evidence=(),
            fallback=True,
            model_fingerprint=getattr(self._model, "fingerprint", ""),
        )


def train(tickets_path: str | Path, *, embedder: Any | None = None,
          folds: int = 5) -> TrainedClassifier:
    """FR-08 §3.1: fit both classifiers on a labelled ticket file and cross-validate them.

    The file is the **development** set. Tuning against validation is forbidden (CLAUDE.md), and
    nothing here reads the file the harness is pointed at.
    """
    import numpy as np
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    path = Path(tickets_path)
    tickets = load_tickets(path)
    embedder = embedder if embedder is not None else _default_embedder()

    rows: list[tuple[str, str, str, str, str]] = []
    for ticket in tickets:
        # `evaluation_labels` is the only sanctioned way to read ground truth (FR-07), and since
        # D-25 made `raw` a read-only view an `isinstance(..., dict)` check on it finds nothing.
        labels = evaluation_labels(ticket)
        intent, urgency = labels.get("intent"), labels.get("urgency")
        if not intent or not urgency or not ticket.text.strip():
            continue
        rows.append((ticket.ticket_id, ticket.text, str(intent), str(urgency), ticket.body))
    if len(rows) < folds * 2:
        raise ClassifierError(
            f"{path} has {len(rows)} usable labelled tickets, too few to cross-validate {folds} "
            "folds. Training needs the labelled development set.")

    ids = [r[0] for r in rows]
    vectors = np.asarray(embedder([r[1] for r in rows]), dtype=float)
    intent_labels = [r[2] for r in rows]
    urgency_labels = [r[3] for r in rows]

    intents = tuple(sorted(set(intent_labels)))
    urgencies = tuple(sorted(set(urgency_labels)))
    usable_folds = _usable_folds(intent_labels, urgency_labels, folds)

    def inner_cv() -> Any:
        """The calibration CV, built fresh each time because sklearn consumes the splitter."""
        return StratifiedKFold(n_splits=usable_folds, shuffle=True, random_state=0)

    def estimator(balanced: bool, cv: Any = None) -> Any:
        """One definition used for both the shipped fit and the out-of-fold measurement.

        They must be the same estimator or the report describes a model nobody runs — which it
        did: the production urgency model was class-weighted and the measured one was not.
        Sigmoid rather than isotonic per class: 22 intents over 500 tickets is 13-29 examples
        each, and isotonic on 13 points fits noise (FR-08 §3.1 rule 2).
        """
        return CalibratedClassifierCV(
            LogisticRegression(max_iter=2000, random_state=0,
                               class_weight="balanced" if balanced else None),
            method="sigmoid", cv=cv if cv is not None else inner_cv())

    def fit(labels: list[str], balanced: bool = False) -> Any:
        return estimator(balanced).fit(vectors, labels)

    # Urgency is weighted because it collapses onto the majority class otherwise: measured 0%
    # recall on `low` across 128 tickets, i.e. the class was never predicted at all. Intent is
    # left unweighted — its 22 classes are already near-balanced at 13 to 29 examples each.
    intent_model, urgency_model = fit(intent_labels), fit(urgency_labels, balanced=True)

    # Out-of-fold predictions are the only honest basis for the reported figures — but on this
    # data row-wise folds are not enough. The 500 development tickets hold only ~216 distinct
    # bodies, and every repeated body carries one intent, so a body appearing in both the training
    # and test fold makes the score a near-duplicate lookup rather than a classification (D-39).
    # Grouping the folds by body text removes that leak; the row-wise figure is kept beside it so
    # the size of the leak is visible.
    # Grouping by exact body was not enough. Measured on this data: 168 of 215 distinct bodies
    # have another *distinct* body with the same intent at >0.85 character similarity ("a restore
    # we started last Friday…" against "a restore we started yesterday morning…"), so exact-body
    # folds still test on a paraphrase of something they trained on. Near-duplicates are clustered
    # so the reported figure is about wording the model has genuinely not seen (D-39).
    bodies = [(body or text).strip().lower() for _, text, _, _, body in rows]
    groups = _near_duplicate_groups(bodies)
    exact_groups = [hashlib.sha256(b.encode("utf-8")).hexdigest()[:16] for b in bodies]
    grouped_splitter, grouped, grouped_folds = _group_splitter(intent_labels, groups, folds)

    def out_of_fold(labels: list[str], splitter: Any, group_list: list[str] | None,
                    balanced: bool = False) -> Any:
        # Same estimator as the shipped fit, inner CV included: a different inner CV shifted the
        # top-probability distribution by 7.5 points, so the calibrator would have been fitted on
        # a distribution the shipped model does not produce.
        return cross_val_predict(estimator(balanced), vectors, labels, cv=splitter,
                                 method="predict_proba", groups=group_list)

    row_wise = StratifiedKFold(n_splits=usable_folds, shuffle=True, random_state=0)
    naive_intent = out_of_fold(intent_labels, row_wise, None)
    naive_urgency = out_of_fold(urgency_labels, row_wise, None, balanced=True)
    if grouped:
        intent_probs = out_of_fold(intent_labels, grouped_splitter, groups)
        urgency_probs = out_of_fold(urgency_labels, grouped_splitter, groups, balanced=True)
    else:
        intent_probs, urgency_probs = naive_intent, naive_urgency

    intent_calibrator = _fit_confidence_calibrator(intent_probs, intents, intent_labels)
    urgency_calibrator = _fit_confidence_calibrator(urgency_probs, urgencies, urgency_labels)

    report = _report(intents, urgencies, intent_labels, urgency_labels,
                     intent_probs, urgency_probs, ids, usable_folds,
                     grouped_folds=grouped_folds, distinct_bodies=len(set(exact_groups)),
                     naive_intent_probs=naive_intent, naive_urgency_probs=naive_urgency,
                     grouped=grouped, distinct_texts=len(set(groups)),
                     intent_calibrator=intent_calibrator,
                     urgency_calibrator=urgency_calibrator, groups=groups)

    signature = _embedder_signature(embedder)
    fingerprint = _fingerprint(path, signature, intents, urgencies, len(rows))
    return TrainedClassifier(
        intent_model=intent_model,
        urgency_model=urgency_model,
        intents=intents,
        urgencies=urgencies,
        embedder_signature=signature,
        fingerprint=fingerprint,
        training_tickets=len(rows),
        report=report,
        training_ids=tuple(ids),
        training_vectors=vectors,
        training_urgencies=tuple(urgency_labels),
        intent_confidence_calibrator=intent_calibrator,
        urgency_confidence_calibrator=urgency_calibrator,
    )


def _fit_confidence_calibrator(probabilities: Any, classes: tuple[str, ...],
                               truth: list[str]) -> Any:
    """Fit top-probability → observed accuracy on the grouped out-of-fold predictions.

    NFR-03 asks that *stated confidence* be within 5 points of observed accuracy. That is a claim
    about the number the system reports, not about the whole probability distribution, and
    per-class sigmoid calibration over 22 classes does not deliver it: measured 72% stated against
    100% observed before this was added. Isotonic regression here is safe because it is
    one-dimensional over ~500 points, not per class over 13.
    """
    if probabilities is None or len(truth) < 50:
        return None
    from sklearn.linear_model import LogisticRegression

    tops = [[float(row.max())] for row in probabilities]
    correct = [1 if classes[int(row.argmax())] == label else 0
               for row, label in zip(probabilities, truth, strict=True)]
    if len(set(correct)) < 2:
        return None  # all right or all wrong: nothing to map
    try:
        # Logistic rather than isotonic. Isotonic's flat regions emit *exactly* 1.0, which on this
        # data meant 488 of 500 tickets stating certainty from a 94%-accurate classifier and left
        # FR-02 with four distinct values to threshold on. A one-dimensional logistic fit is
        # smooth, monotone and bounded away from 0 and 1.
        return LogisticRegression(max_iter=1000, random_state=0).fit(tops, correct)
    except Exception as exc:  # noqa: BLE001 - an unfittable calibrator is not a training failure
        _log.warning("could not fit a confidence calibrator: %s", exc)
        return None


def _cross_fitted_confidences(probabilities: Any, classes: tuple[str, ...], truth: list[str],
                              groups: list[str] | None) -> list[float]:
    """Calibrated confidences where no prediction was scored by a calibrator that saw it."""
    if probabilities is None:
        return []
    tops = [float(row.max()) for row in probabilities]
    if groups is None or len(truth) < 100:
        return [max(0.0, min(1.0, value)) for value in tops]

    # Two halves, split by body so a wording never sits on both sides.
    ordered = sorted(set(groups))
    first_half = {group for index, group in enumerate(ordered) if index % 2 == 0}
    side = [0 if group in first_half else 1 for group in groups]

    out = list(tops)
    for held_out in (0, 1):
        fit_rows = [i for i, s in enumerate(side) if s != held_out]
        score_rows = [i for i, s in enumerate(side) if s == held_out]
        if len(fit_rows) < 50 or not score_rows:
            continue
        calibrator = _fit_confidence_calibrator(
            probabilities[fit_rows], classes, [truth[i] for i in fit_rows])
        for i in score_rows:
            out[i] = _apply_calibrator(calibrator, tops[i])
    return [max(0.0, min(1.0, value)) for value in out]


def _apply_calibrator(calibrator: Any, raw: float) -> float:
    if calibrator is None:
        return max(0.0, min(1.0, raw))
    try:
        return max(0.0, min(1.0, float(calibrator.predict_proba([[raw]])[0][1])))
    except Exception as exc:  # noqa: BLE001 - never let calibration break a classification
        # Silently reverting to an uncalibrated number would make the calibration report stop
        # describing the system with nothing to show for it, so say so.
        _log.warning("confidence calibration failed, reporting the raw probability: %s", exc)
        return max(0.0, min(1.0, raw))


def calibration_table(probabilities: list[float], correct: list[bool],
                      bands: int = 5) -> tuple[CalibrationBand, ...]:
    """Evaluation Framework §3: stated confidence against observed accuracy, per band.

    Percentages, because that is how NFR-03's "within 5 points" is stated. A band with too few
    predictions to support that claim is flagged rather than quietly reported.
    """
    out: list[CalibrationBand] = []
    for index in range(bands):
        lower, upper = index / bands, (index + 1) / bands
        # The top band is closed so a confidence of exactly 1.0 is counted somewhere.
        members = [(p, c) for p, c in zip(probabilities, correct, strict=True)
                   if (lower <= p < upper) or (index == bands - 1 and p == 1.0)]
        if not members:
            out.append(CalibrationBand(round(lower, 4), round(upper, 4), 0, None, None, None))
            continue
        stated = round(100.0 * sum(p for p, _ in members) / len(members), 1)
        observed = round(100.0 * sum(1 for _, c in members if c) / len(members), 1)
        out.append(CalibrationBand(
            lower=round(lower, 4),
            upper=round(upper, 4),
            count=len(members),
            mean_confidence=stated,
            observed_accuracy=observed,
            gap_points=round(abs(stated - observed), 1),
            low_confidence=len(members) < MIN_BAND_FOR_CONFIDENCE,
        ))
    return tuple(out)


def order_escalation_queue(items: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """FR-05: urgency first, then age, oldest first. A missing timestamp sorts last, not crashes."""
    rank = {name: position for position, name in enumerate(URGENCY_ORDER)}

    def key(pair: tuple[int, dict[str, Any]]) -> tuple[int, int, float, int]:
        position, item = pair
        raw = item.get("urgency")
        urgency = str(raw).strip().lower() if raw else FALLBACK_URGENCY
        # An urgency nobody recognises is treated like a missing one — as `medium` — not sent to
        # the bottom of the queue. "critical" or "P1" ranking below `low` is the wrong direction
        # for an escalation queue to guess in.
        position_rank = rank.get(urgency, rank[FALLBACK_URGENCY])
        received = _as_timestamp(item.get("received_at"))
        return (
            position_rank,
            0 if received is not None else 1,   # no timestamp: after everything dated
            received if received is not None else 0.0,
            position,                            # stable for equal keys
        )

    return tuple(item for _, item in sorted(enumerate(items), key=key))


# --- internals ------------------------------------------------------------------------


def _near_duplicate_groups(texts: list[str], threshold: float = 0.85) -> list[str]:
    """Cluster near-identical wordings so a paraphrase cannot straddle a fold boundary.

    Union-find over the distinct texts with a cheap token-overlap prefilter before the expensive
    ratio, which keeps this well under a second for the 215 distinct bodies in this dataset.
    """
    import difflib

    distinct = sorted(set(texts))
    parent = {text: text for text in distinct}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    tokens = {text: set(text.split()) for text in distinct}
    for index, left in enumerate(distinct):
        for right in distinct[index + 1:]:
            overlap = tokens[left] & tokens[right]
            smaller = min(len(tokens[left]), len(tokens[right])) or 1
            if len(overlap) / smaller < threshold * 0.8:
                continue  # cannot reach the ratio; skip the expensive comparison
            if difflib.SequenceMatcher(None, left, right).ratio() >= threshold:
                parent[find(left)] = find(right)
    return [find(text) for text in texts]


def _group_splitter(intent_labels: list[str], groups: list[str],
                    folds: int) -> tuple[Any, bool, int]:
    """Folds that keep one wording's tickets together, so none is in train and test at once.

    Falls back to ungrouped folds — and says so — when a class has too few distinct wordings to
    split, rather than reporting a grouped figure it could not actually compute. The number of
    splits it settled on is returned, because it is usually *fewer* than requested and printing the
    requested count beside the word "grouped" would misdescribe the measurement.
    """
    from sklearn.model_selection import StratifiedGroupKFold

    per_class: dict[str, set[str]] = {}
    for label, group in zip(intent_labels, groups, strict=True):
        per_class.setdefault(label, set()).add(group)
    smallest = min(len(v) for v in per_class.values()) if per_class else 0
    splits = min(folds, smallest)
    if splits < 2:
        _log.warning("a class has fewer than 2 distinct wordings: reporting row-wise folds only")
        return (None, False, 0)
    return (StratifiedGroupKFold(n_splits=splits, shuffle=True, random_state=0), True, splits)


def _as_timestamp(value: Any) -> float | None:
    """Seconds since the epoch, so ages compare as times. Unparseable sorts last, never crashes."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    from datetime import datetime

    text = str(value).strip().replace(" ", "T")
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        return datetime.fromisoformat(candidate).timestamp()
    except (ValueError, OSError, OverflowError):
        return None


def _usable_folds(intent_labels: list[str], urgency_labels: list[str], folds: int) -> int:
    """Stratified folds cannot exceed the smallest class, so shrink rather than crash."""
    smallest = min(
        min(intent_labels.count(name) for name in set(intent_labels)),
        min(urgency_labels.count(name) for name in set(urgency_labels)),
    )
    return max(2, min(folds, smallest))


def _report(intents: tuple[str, ...], urgencies: tuple[str, ...], intent_labels: list[str],
            urgency_labels: list[str], intent_probs: Any, urgency_probs: Any,
            ids: list[str], folds: int, naive_intent_probs: Any = None,
            naive_urgency_probs: Any = None, grouped: bool = True,
            distinct_texts: int = 0, intent_calibrator: Any = None,
            urgency_calibrator: Any = None,
            groups: list[str] | None = None, grouped_folds: int = 0,
            distinct_bodies: int = 0) -> TrainingReport:
    intent_predictions = [intents[int(row.argmax())] for row in intent_probs]
    urgency_predictions = [urgencies[int(row.argmax())] for row in urgency_probs]
    # The table must judge the number the system will state — the calibrated one — but scoring a
    # calibrator on the predictions it was fitted on is the same in-sample mistake the grouped
    # folds exist to avoid (it reported a 0.0-point gap). So the confidences in this table are
    # **cross-fitted**: each half is calibrated by a calibrator fitted only on the other half,
    # grouped so no ticket body crosses. The calibrator shipped in the model is then fitted on all
    # of it, which is standard and is the one place this report is optimistic by construction.
    intent_confidences = _cross_fitted_confidences(intent_probs, intents, intent_labels, groups)
    urgency_confidences = _cross_fitted_confidences(urgency_probs, urgencies, urgency_labels,
                                                   groups)

    intent_bands = calibration_table(
        intent_confidences, [p == t for p, t in zip(intent_predictions, intent_labels, strict=True)])
    urgency_bands = calibration_table(
        urgency_confidences,
        [p == t for p, t in zip(urgency_predictions, urgency_labels, strict=True)])
    gaps = [b.gap_points for b in (*intent_bands, *urgency_bands)
            if b.gap_points is not None and not b.low_confidence]

    truth_rows = intent_labels

    def accuracy(probabilities: Any, classes: tuple[str, ...], truth: list[str]) -> float:
        if probabilities is None:
            return 0.0
        predictions = [classes[int(row.argmax())] for row in probabilities]
        return round(100.0 * sum(1 for p, t in zip(predictions, truth, strict=True) if p == t)
                     / len(truth), 1)

    basis = ("cross-fitted: each half scored by a calibrator fitted on the other"
             if groups is not None and len(truth_rows) >= 100
             else "raw model probabilities: too few rows to cross-fit a calibrator")
    return TrainingReport(
        cross_validated=True,
        folds=folds,
        grouped=grouped,
        grouped_folds=grouped_folds,
        distinct_texts=distinct_texts,
        distinct_bodies=distinct_bodies,
        calibration_basis=basis,
        intent_accuracy_pct=accuracy(intent_probs, intents, intent_labels),
        urgency_accuracy_pct=accuracy(urgency_probs, urgencies, urgency_labels),
        intent_accuracy_naive_pct=accuracy(naive_intent_probs, intents, intent_labels),
        urgency_accuracy_naive_pct=accuracy(naive_urgency_probs, urgencies, urgency_labels),
        intent_per_class=_per_class(intent_labels, intent_predictions),
        urgency_per_class=_per_class(urgency_labels, urgency_predictions),
        intent_calibration=intent_bands,
        urgency_calibration=urgency_bands,
        out_of_fold_predictions=tuple(
            (ticket_id, prediction, round(confidence, 4)) for ticket_id, prediction, confidence
            in zip(ids, intent_predictions, intent_confidences, strict=True)),
        worst_calibration_gap_points=max(gaps) if gaps else None,
    )


def _per_class(truth: list[str], predicted: list[str]) -> dict[str, dict[str, Any]]:
    """NFR-03: per class, because an average hides the classes that fail."""
    out: dict[str, dict[str, Any]] = {}
    for name in sorted(set(truth) | set(predicted)):
        tp = sum(1 for t, p in zip(truth, predicted, strict=True) if t == name and p == name)
        fp = sum(1 for t, p in zip(truth, predicted, strict=True) if t != name and p == name)
        fn = sum(1 for t, p in zip(truth, predicted, strict=True) if t == name and p != name)
        out[name] = {
            "precision_pct": round(100.0 * tp / (tp + fp), 1) if tp + fp else None,
            "recall_pct": round(100.0 * tp / (tp + fn), 1) if tp + fn else None,
            "support": tp + fn,
        }
    return {name: row for name, row in out.items() if row["support"] or row["precision_pct"]}


def _reason(evidence: tuple[tuple[str, str, float], ...]) -> str:
    """FR-05 §3.4: true, checkable evidence rather than an invented rationale."""
    if not evidence:
        return "no comparable ticket in the training set"
    parts = ", ".join(f"{ticket_id} ({label}, {similarity:.2f})"
                      for ticket_id, label, similarity in evidence)
    return f"closest to {parts}"


def _fingerprint(path: Path, embedder_signature: str, intents: tuple[str, ...],
                 urgencies: tuple[str, ...], rows: int) -> str:
    material = json.dumps({
        "trainer_version": TRAINER_VERSION,
        "training_file": hashlib.sha256(path.read_bytes()).hexdigest()[:16],
        "embedder": embedder_signature,
        "intents": list(intents),
        "urgencies": list(urgencies),
        "rows": rows,
    }, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _embedder_signature(embedder: Any) -> str:
    from ticketing_agent.retrieve import _embedder_signature as signature

    return signature(embedder)


def _default_embedder() -> Any:
    from ticketing_agent.retrieve import _default_embedder as default

    return default()


