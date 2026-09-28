"""FR-14 acceptance tests T-FR14-1 … T-FR14-18 (docs/specs/FR-14.md).

Offline: a fake pipeline, the hashing embedder, a temporary decision log and output directory.
No network, no API key, no model download.
"""
import json
from pathlib import Path

import chromadb
import pytest

from evaluation.harness import HarnessError, run
from ticketing_agent.config import Settings
from ticketing_agent.ingest import Ticket, load_tickets
from ticketing_agent.logging_store import DecisionLog
from ticketing_agent.pipeline import Outcome, StubPipeline
from ticketing_agent.retrieve import HashingEmbedder, Retriever

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "data" / "documentation.json"
VALIDATION = ROOT / "data" / "validation_tickets.json"
FIXTURES = ROOT / "tests" / "fixtures"


def settings(tmp_path, **overrides):
    values = {
        "docs_path": DOCS,
        "chroma_path": tmp_path / "chroma",
        "decision_log_path": tmp_path / "decisions.db",
        "relevance_threshold": 0.0,
        "confidence_threshold": 0.8,
        "retrieval_top_k": 3,
        "model_name": "test-model",
    }
    values.update(overrides)
    return Settings(**values)


class FakePipeline:
    """Answers or escalates on command, so the harness's own behaviour is what is under test."""

    def __init__(self, decide=None, raise_on=None):
        self.decide = decide or (lambda ticket: ("auto_respond", None))
        self.raise_on = raise_on or set()
        self.seen: list[str] = []

    def process(self, ticket: Ticket) -> Outcome:
        self.seen.append(ticket.ticket_id)
        if ticket.ticket_id in self.raise_on or self.raise_on == "all":
            raise RuntimeError(f"pipeline exploded on {ticket.ticket_id}")
        decision, reason = self.decide(ticket)
        return Outcome(
            ticket=ticket,
            decision=decision,
            reason=reason,
            explanation="Answered from the documentation." if decision == "auto_respond"
            else "Sent to a person.",
            all_reasons=(reason,) if reason else (),
            threshold_applied=0.8,
            prediction_value=(ticket.raw.get("labels") or {}).get("intent"),
            prediction_confidence=0.9,
            requirement_ids=("FR-14",),
        )


def stub_pipeline(tmp_path, **overrides):
    """The real row-6 pipeline on an injected embedder, so no model is needed."""
    config = settings(tmp_path, **overrides)
    retriever = Retriever(config, embedder=HashingEmbedder(),
                          client=chromadb.PersistentClient(path=str(tmp_path / "chroma-stub")))
    retriever.build_index(DOCS)
    return StubPipeline(retriever=retriever, threshold=config.relevance_threshold)


def harness(tmp_path, pipeline, input_path=VALIDATION, **overrides):
    return run(input_path=Path(input_path), output_dir=tmp_path / "out",
               settings=settings(tmp_path, **overrides), pipeline=pipeline)


def test_T_FR14_1_paths_are_arguments_and_the_report_appears(tmp_path):
    """Build Spec §04: it will be pointed at a file nobody has seen."""
    renamed = tmp_path / "a-file-nobody-has-seen.json"
    renamed.write_text(VALIDATION.read_text(encoding="utf-8"), encoding="utf-8")

    report = harness(tmp_path, FakePipeline(), input_path=renamed)

    out = tmp_path / "out"
    assert report.exit_code == 0
    for name in ("metrics.json", "metrics.md", "outcomes.jsonl"):
        assert (out / name).exists(), name
    assert report.metrics["run"]["input"].endswith("a-file-nobody-has-seen.json")


def test_T_FR14_2_tickets_out_equals_tickets_in(tmp_path):
    tickets = load_tickets(VALIDATION)
    report = harness(tmp_path, FakePipeline())

    assert report.metrics["volume"]["tickets_processed"] == len(tickets)
    outcomes = [json.loads(line) for line in
                (tmp_path / "out" / "outcomes.jsonl").read_text().splitlines()]
    assert len(outcomes) == len(tickets)
    assert {o["ticket_id"] for o in outcomes} == {t.ticket_id for t in tickets}

    with DecisionLog(tmp_path / "decisions.db") as log:
        assert len(log.terminal_rows()) == len(tickets)
    assert report.metrics["governance"]["reconciles"] is True


def test_T_FR14_3_one_failing_ticket_does_not_stop_the_run(tmp_path):
    tickets = load_tickets(VALIDATION)
    victim = tickets[5].ticket_id
    pipeline = FakePipeline(raise_on={victim})

    report = harness(tmp_path, pipeline)

    assert report.exit_code == 0, "the run must complete"
    assert len(pipeline.seen) == len(tickets), "every ticket was attempted"
    assert report.metrics["volume"]["tickets_processed"] == len(tickets)
    assert report.metrics["reasons"].get("pipeline_error") == 1
    with DecisionLog(tmp_path / "decisions.db") as log:
        rows = log.rows(ticket_id=victim)
    assert len(rows) == 1
    assert rows[0]["decision"] == "escalate" and rows[0]["reason"] == "pipeline_error"
    assert "RuntimeError" in rows[0]["detail"]


def test_T_FR14_4_a_pipeline_that_always_fails_still_completes(tmp_path):
    """FR-15's criterion and A11: with everything broken, every ticket escalates and is logged."""
    tickets = load_tickets(VALIDATION)
    report = harness(tmp_path, FakePipeline(raise_on="all"))

    assert report.exit_code == 0
    assert report.metrics["volume"]["escalated"] == len(tickets)
    assert report.metrics["volume"]["answered_automatically"] == 0
    assert report.metrics["governance"]["decisions_logged"] == len(tickets)
    assert report.metrics["governance"]["reconciles"] is True


def test_T_FR14_5_a_reconciliation_mismatch_fails_the_run(tmp_path):
    report = harness(tmp_path, FakePipeline())
    assert report.exit_code == 0

    # A second run into the same log with a stray row: the log and the tickets now disagree.
    with DecisionLog(tmp_path / "decisions.db", run_id="stray-run") as log:
        log.start_run(VALIDATION, 80)
        from ticketing_agent.logging_store import DecisionEntry

        log.record(DecisionEntry(
            ticket_id="VAL-NOT-IN-THE-FILE", source_index=999, stage="routing",
            decision="escalate", reason="invented", explanation="not a real ticket",
            requirement_ids=("FR-14",)))
        reconciliation = log.reconcile(load_tickets(VALIDATION))
    assert not reconciliation.ok
    assert "VAL-NOT-IN-THE-FILE" in reconciliation.extra


def test_T_FR14_6_the_report_carries_every_build_spec_group(tmp_path):
    report = harness(tmp_path, FakePipeline())
    metrics = report.metrics

    assert set(metrics["volume"]) == {"tickets_processed", "answered_automatically",
                                      "escalated", "blocked_by_guardrails"}
    assert {"first_contact_resolution_proxy_pct", "escalation_rate_pct",
            "processing_time_ms"} <= set(metrics["business"])
    assert metrics["business"]["processing_time_ms"].keys() >= {"mean", "median"}
    assert {"classification", "retrieval_hit_rate_pct", "latency_ms"} <= set(metrics["technical"])
    assert {"decisions_logged", "guardrail_activations_by_type",
            "private_data_detections"} <= set(metrics["governance"])

    markdown = (tmp_path / "out" / "metrics.md").read_text(encoding="utf-8")
    for heading in ("## Volume", "## Business outcomes", "## Technical", "## Governance",
                    "## Segments", "## Results table", "## What this run does not measure"):
        assert heading in markdown, heading


def test_T_FR14_7_segments_cover_every_required_dimension(tmp_path):
    report = harness(tmp_path, FakePipeline())
    segments = report.metrics["segments"]

    assert set(segments) == {"tier", "fluency", "region", "channel", "length"}
    for key, block in segments.items():
        assert block["rows"], key
        for name, row in block["rows"].items():
            assert row["tickets"] > 0, (key, name)
            assert "low_confidence" in row
        assert "variation_points" in block

    # The validation set has 8 enterprise tickets, which must be flagged rather than reported flat.
    enterprise = segments["tier"]["rows"].get("enterprise")
    assert enterprise and enterprise["tickets"] == 8
    assert enterprise["low_confidence"] is True
    markdown = (tmp_path / "out" / "metrics.md").read_text(encoding="utf-8")
    assert "low confidence (n<10)" in markdown


def test_T_FR14_8_a_file_without_labels_still_produces_a_full_report(tmp_path):
    stripped = tmp_path / "no-labels.json"
    entries = json.loads(VALIDATION.read_text(encoding="utf-8"))
    for entry in entries:
        entry.pop("labels", None)
        entry.pop("history", None)
    stripped.write_text(json.dumps(entries), encoding="utf-8")

    report = harness(tmp_path, FakePipeline(), input_path=stripped)

    assert report.exit_code == 0
    assert report.metrics["run"]["scored_against_labels"] == f"0 of {len(entries)}"
    assert report.metrics["volume"]["tickets_processed"] == len(entries)
    assert report.metrics["technical"]["retrieval_hit_rate_pct"] is None
    assert "not_computable" in report.metrics["technical"]["classification"]

    with_labels = harness(tmp_path / "second", FakePipeline())
    assert with_labels.metrics["run"]["scored_against_labels"] == "80 of 80"
    assert "per_class" in with_labels.metrics["technical"]["classification"]


def test_T_FR14_9_latency_is_reported_both_ways(tmp_path):
    metrics = harness(tmp_path, FakePipeline()).metrics
    assert metrics["technical"]["latency_ms"]["median"] is not None
    assert metrics["technical"]["latency_ms"]["p95"] is not None
    assert metrics["business"]["processing_time_ms"]["mean"] is not None
    assert metrics["business"]["processing_time_ms"]["median"] is not None
    assert "no first-reply timestamp" in metrics["business"]["response_time_note"]


def test_T_FR14_10_precision_and_recall_are_per_class(tmp_path):
    metrics = harness(tmp_path, FakePipeline()).metrics
    classification = metrics["technical"]["classification"]

    assert "per_class" in classification
    assert len(classification["per_class"]) > 5, "the data has 22 intents"
    for name, row in classification["per_class"].items():
        assert set(row) == {"precision_pct", "recall_pct", "support"}, name
    # The fake pipeline predicts the label, so accuracy is 100% — which proves the wiring, and
    # the real figure arrives with the classifier at row 8.
    assert classification["overall_accuracy_pct"] == 100.0


def test_T_FR14_11_the_thresholds_in_use_are_reported(tmp_path):
    report = harness(tmp_path, FakePipeline(), relevance_threshold=0.35,
                     confidence_threshold=0.72)
    assert report.metrics["run"]["thresholds"]["relevance_threshold"] == 0.35
    assert report.metrics["run"]["thresholds"]["confidence_threshold"] == 0.72
    markdown = (tmp_path / "out" / "metrics.md").read_text(encoding="utf-8")
    assert "relevance **0.35**" in markdown and "confidence **0.72**" in markdown


def test_T_FR14_12_a_bad_input_file_fails_loudly_and_writes_nothing(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")

    with pytest.raises(HarnessError, match="not valid JSON"):
        harness(tmp_path, FakePipeline(), input_path=broken)
    assert not (tmp_path / "out").exists(), "no partial report"

    with pytest.raises(HarnessError, match="cannot read"):
        harness(tmp_path, FakePipeline(), input_path=tmp_path / "missing.json")


def test_T_FR14_13_the_engineered_malformed_corpus_runs_to_completion(tmp_path):
    """FR-07's acceptance criterion, end to end: malformed tickets are logged and escalated."""
    report = harness(tmp_path, stub_pipeline(tmp_path),
                     input_path=FIXTURES / "malformed_tickets.json")

    entries = json.loads((FIXTURES / "malformed_tickets.json").read_text())
    assert report.exit_code == 0
    assert report.metrics["volume"]["tickets_processed"] == len(entries)
    assert report.metrics["volume"]["escalated"] == len(entries)
    assert report.metrics["reasons"].get("malformed_ticket", 0) >= 5
    assert report.metrics["governance"]["reconciles"] is True


def test_T_FR14_14_the_json_and_the_markdown_agree(tmp_path):
    report = harness(tmp_path, FakePipeline())
    metrics = json.loads((tmp_path / "out" / "metrics.json").read_text(encoding="utf-8"))
    markdown = (tmp_path / "out" / "metrics.md").read_text(encoding="utf-8")

    assert metrics == json.loads(json.dumps(report.metrics, default=str))
    for value in (metrics["volume"]["tickets_processed"], metrics["volume"]["escalated"],
                  metrics["governance"]["decisions_logged"]):
        assert str(value) in markdown
    assert f"{metrics['business']['escalation_rate_pct']}%" in markdown


def test_T_FR14_15_a_limited_run_says_so(tmp_path):
    report = run(input_path=VALIDATION, output_dir=tmp_path / "out",
                 settings=settings(tmp_path), pipeline=FakePipeline(), limit=7)
    assert report.metrics["volume"]["tickets_processed"] == 7
    assert report.metrics["run"]["limit"] == 7
    markdown = (tmp_path / "out" / "metrics.md").read_text(encoding="utf-8")
    assert "PARTIAL RUN" in markdown, "a smoke run must not read as a full result"


def test_T_FR14_16_two_runs_of_the_same_input_agree(tmp_path):
    """NFR-08: the same input gives the same decisions."""
    first = harness(tmp_path / "a", stub_pipeline(tmp_path / "a"))
    second = harness(tmp_path / "b", stub_pipeline(tmp_path / "b"))

    def decisions(path):
        return [(o["ticket_id"], o["decision"], o["reason"], o["doc_ids"])
                for o in (json.loads(line) for line in
                          (path / "out" / "outcomes.jsonl").read_text().splitlines())]

    assert decisions(tmp_path / "a") == decisions(tmp_path / "b")
    assert first.metrics["volume"] == second.metrics["volume"]


def test_T_FR14_17_a_log_that_cannot_be_written_stops_the_run(tmp_path):
    """D-27: unlogged processing breaks FR-13 for every ticket, so the run fails rather than go on."""
    blocked = tmp_path / "unwritable"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        with pytest.raises(HarnessError, match="decision log"):
            run(input_path=VALIDATION, output_dir=tmp_path / "out",
                settings=settings(tmp_path, decision_log_path=blocked / "nested" / "log.db"),
                pipeline=FakePipeline())
        assert not (tmp_path / "out" / "outcomes.jsonl").exists()
    finally:
        blocked.chmod(0o700)


def test_T_FR14_18_no_customer_text_reaches_the_report_or_the_log(tmp_path):
    """NFR-04: the report and the log carry decisions, not the customer's words."""
    tickets = load_tickets(VALIDATION)
    phrases = [t.body[:40] for t in tickets[:10] if len(t.body) > 40]

    harness(tmp_path, FakePipeline(raise_on={tickets[0].ticket_id}))

    written = ((tmp_path / "out" / "metrics.json").read_text(encoding="utf-8")
               + (tmp_path / "out" / "metrics.md").read_text(encoding="utf-8")
               + (tmp_path / "out" / "outcomes.jsonl").read_text(encoding="utf-8"))
    for phrase in phrases:
        assert phrase not in written, f"customer text in the report: {phrase!r}"

    with DecisionLog(tmp_path / "decisions.db") as log:
        rows = json.dumps(log.rows(), default=str)
    for phrase in phrases:
        assert phrase not in rows, f"customer text in the decision log: {phrase!r}"


# --- findings from the row 6 review ---------------------------------------------------


class BadPipeline:
    """Returns something the harness cannot use, on one ticket out of many."""

    def __init__(self, victim: str, what: str):
        self.victim, self.what = victim, what
        self.seen: list[str] = []

    def process(self, ticket: Ticket):
        self.seen.append(ticket.ticket_id)
        if ticket.ticket_id != self.victim:
            return Outcome(ticket=ticket, decision="escalate", reason="no_retrieval",
                           explanation="Nothing relevant was found.", requirement_ids=("FR-14",))
        if self.what == "dict":
            return {"decision": "escalate"}
        if self.what == "none":
            return None
        if self.what == "no_reason":  # invalid for FR-13: an escalation must say why
            return Outcome(ticket=ticket, decision="escalate", explanation="but no reason")
        if self.what == "no_explanation":
            return Outcome(ticket=ticket, decision="escalate", reason="no_retrieval")
        if self.what == "bad_decision":
            return Outcome(ticket=ticket, decision="answer_it", reason="x", explanation="y")
        if self.what == "no_threshold":  # auto_respond must record the threshold it cleared
            return Outcome(ticket=ticket, decision="auto_respond", explanation="answered")
        raise AssertionError(self.what)


@pytest.mark.parametrize("what", ["dict", "none", "no_reason", "no_explanation",
                                  "bad_decision", "no_threshold"])
def test_T_FR14_19_an_unusable_outcome_costs_one_ticket_not_the_run(tmp_path, what):
    """A9: no manual intervention, no restarts, no skipped tickets — whatever one ticket does."""
    tickets = load_tickets(VALIDATION)
    victim = tickets[3].ticket_id
    pipeline = BadPipeline(victim, what)

    report = harness(tmp_path, pipeline)

    assert report.exit_code == 0, f"{what} stopped the run"
    assert len(pipeline.seen) == len(tickets), "every ticket must still be attempted"
    assert report.metrics["volume"]["tickets_processed"] == len(tickets)
    assert report.metrics["reasons"].get("pipeline_error") == 1
    with DecisionLog(tmp_path / "decisions.db") as log:
        rows = log.rows(ticket_id=victim)
        assert len(rows) == 1, "the bad ticket still has exactly one row"
        assert rows[0]["decision"] == "escalate"
        assert rows[0]["reason"] == "pipeline_error"
        assert len(log.terminal_rows()) == len(tickets)
    assert report.metrics["governance"]["reconciles"] is True


def test_T_FR14_20_a_reconciliation_mismatch_makes_the_run_exit_non_zero(tmp_path):
    """A8: the log and the tickets processed must agree, and the run must say so."""
    from ticketing_agent.logging_store import DecisionEntry

    log_path = tmp_path / "decisions.db"
    with DecisionLog(log_path, run_id="mismatch") as log:
        log.record(DecisionEntry(
            ticket_id="VAL-NOT-IN-THE-FILE", source_index=999, stage="routing",
            decision="escalate", reason="invented", explanation="a row for a ticket not in the file",
            requirement_ids=("FR-14",)))

    report = run(input_path=VALIDATION, output_dir=tmp_path / "out",
                 settings=settings(tmp_path), pipeline=FakePipeline(),
                 decision_log_path=log_path, run_id="mismatch")

    assert report.exit_code == 1, "a log that disagrees with the tickets is a failed run"
    assert report.metrics["governance"]["reconciles"] is False
    assert "VAL-NOT-IN-THE-FILE" in report.metrics["governance"]["reconciliation"]["extra"]
    assert "**NO**" in (tmp_path / "out" / "metrics.md").read_text(encoding="utf-8")


def test_T_FR14_21_the_arithmetic_is_right_on_known_inputs():
    """Build Spec §04 mandates these figures; a swapped formula would ship silently."""
    from evaluation.harness import _pct, _per_class, _percentile

    assert _pct(1, 4) == 25.0
    assert _pct(0, 10) == 0.0
    assert _pct(3, 0) is None, "nothing to measure is not zero"

    # Nearest-rank: p95 of 1..20 is the 19th value, not the 18th.
    assert _percentile([float(n) for n in range(1, 21)], 0.95) == 19.0
    assert _percentile([5.0], 0.95) == 5.0
    assert _percentile([float(n) for n in range(1, 101)], 0.95) == 95.0
    assert _percentile([1.0, 2.0], 0.5) == 1.0

    # Two classes, deliberately asymmetric so precision and recall cannot be swapped unnoticed:
    # "billing" predicted 3 times, right twice (precision 66.7%); it was the truth twice, caught
    # once... so recall 50%.
    pairs = [("billing", "billing"), ("billing", "deploy"), ("deploy", "billing"),
             ("deploy", "deploy"), ("deploy", "billing")]
    report = _per_class(pairs)
    billing = report["per_class"]["billing"]
    assert billing["precision_pct"] == 33.3, "1 of 3 predictions were billing and correct"
    assert billing["recall_pct"] == 50.0, "1 of 2 billing tickets was caught"
    assert billing["support"] == 2
    assert report["per_class"]["deploy"]["support"] == 3
    assert report["overall_accuracy_pct"] == 40.0


def test_T_FR14_22_the_cli_is_the_documented_command(tmp_path, monkeypatch):
    """CLAUDE.md's non-negotiable: python -m evaluation.harness --input PATH --output DIR."""
    from evaluation import harness as harness_module

    monkeypatch.setenv("MODEL_NAME", "test-model")
    monkeypatch.setenv("DOCS_PATH", str(DOCS))
    monkeypatch.setenv("DECISION_LOG_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "cli-chroma"))
    # Row 14 changed what the CLI builds by default: the real graph, not the stub. Both names
    # are patched so this test is about the command, not about which pipeline is wired in.
    monkeypatch.setattr(harness_module, "_build_stub_pipeline",
                        lambda *_args, **_kwargs: FakePipeline())
    monkeypatch.setattr(harness_module, "_build_pipeline",
                        lambda *_args, **_kwargs: FakePipeline())

    code = harness_module.main(["--input", str(VALIDATION), "--output", str(tmp_path / "cli-out"),
                                "--limit", "5", "--run-id", "cli-run"])

    assert code == 0
    metrics = json.loads((tmp_path / "cli-out" / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["volume"]["tickets_processed"] == 5
    assert metrics["run"]["run_id"] == "cli-run"
    assert metrics["run"]["tickets_in_file"] == 80, "the file has 80, the run took 5"

    # And the default really is the graph: --stub-pipeline is the only way to the stub, so a
    # gate run cannot quietly measure a pipeline that answers nothing (row 14).
    import inspect
    source = inspect.getsource(harness_module.run)
    assert "_build_pipeline(settings" in source
    assert "use_stub" in source

    # A bad input path exits 1 with a message, not a traceback.
    assert harness_module.main(["--input", str(tmp_path / "nope.json"),
                                "--output", str(tmp_path / "cli-out2")]) == 1


def test_T_FR14_23_an_unwritable_output_fails_before_the_run_not_after(tmp_path):
    """A10: losing a completed run to a mistyped --output is the failure this prevents."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        pipeline = FakePipeline()
        with pytest.raises(HarnessError, match="cannot write the report"):
            run(input_path=VALIDATION, output_dir=blocked / "out",
                settings=settings(tmp_path), pipeline=pipeline)
        assert pipeline.seen == [], "not a single ticket should have been processed"
    finally:
        blocked.chmod(0o700)


def test_T_FR14_24_the_variation_figure_can_say_not_measurable(tmp_path):
    """A fairness number that can only ever say "pass" is not a measurement (review finding 4)."""
    one_segment = tmp_path / "one-tier.json"
    entries = [e for e in json.loads(VALIDATION.read_text(encoding="utf-8"))
               if e["customer_tier"] == "enterprise"]
    one_segment.write_text(json.dumps(entries), encoding="utf-8")

    report = harness(tmp_path, FakePipeline(), input_path=one_segment)
    tier = report.metrics["segments"]["tier"]

    assert len(tier["rows"]) == 1
    assert tier["variation_points"] is None, "one segment cannot show variation"
    assert "not measurable" in tier["variation_basis"]
    assert tier["low_confidence_segments"] == ["enterprise"]
    markdown = (tmp_path / "out" / "metrics.md").read_text(encoding="utf-8")
    assert "**not measurable**" in markdown
    assert "treat with care" in markdown


def test_T_FR14_25_the_report_says_what_its_headline_numbers_are_not(tmp_path):
    """Spec §4: a figure the system cannot produce yet says so, by name."""
    report = harness(tmp_path, FakePipeline(decide=lambda t: ("escalate", "no_retrieval")))
    markdown = (tmp_path / "out" / "metrics.md").read_text(encoding="utf-8")

    assert any("by construction" in gap for gap in report.metrics["gaps"]), (
        "100% escalation must be declared as by construction, not read as a result")
    assert "does not measure routing" in report.metrics["technical"]["route_agreement_note"]
    assert "no false-positive counterpart" in report.metrics["technical"]["retrieval_hit_rate_note"]
    for phrase in ("human review", "nothing is sent yet", "no first-reply timestamp"):
        assert phrase in markdown, phrase


def test_T_FR14_26_no_index_rebuild_refuses_rather_than_building(tmp_path):
    """A documented flag that silently does nothing is worse than no flag on a gate run."""
    from evaluation.harness import _build_stub_pipeline

    with pytest.raises(HarnessError, match="--no-index-rebuild"):
        _build_stub_pipeline(settings(tmp_path, chroma_path=tmp_path / "empty-chroma"),
                             DOCS, allow_index_build=False)


def test_T_FR14_27_guardrail_blocks_are_counted_even_though_the_ticket_escalates(tmp_path):
    """FR-12 makes `block` non-terminal, so counting decisions would report 0 for ever."""
    def blocked(ticket):
        return ("escalate", "private_data_in_draft")

    class Blocking(FakePipeline):
        def process(self, ticket):
            outcome = super().process(ticket)
            return Outcome(**{**outcome.__dict__, "decision": "escalate",
                              "reason": "private_data_in_draft",
                              "guardrail_results": (("private_data", False), ("grounding", True))})

    report = harness(tmp_path, Blocking(decide=blocked), input_path=VALIDATION)

    assert report.metrics["volume"]["blocked_by_guardrails"] == 80
    assert report.metrics["governance"]["guardrail_activations_by_type"] == {"private_data": 80}
    assert report.metrics["governance"]["private_data_detections"] == 80
