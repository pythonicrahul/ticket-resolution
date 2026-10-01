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
        # Without this the provider cache defaults to ./storage/llm_cache.sqlite — the real
        # one, holding real responses from real gate runs. A test that builds a ProviderClient
        # then replays a recorded answer about a different article instead of the payload it
        # handed the fake transport, and fails with `invalid_citation` for no visible reason.
        "llm_cache_path": tmp_path / "llm_cache.sqlite",
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
    """NFR-04: the report and the log carry decisions, not the customer's words.

    `outcomes.jsonl` is deliberately **not** in this list any more. It was, and the test passed
    only because it runs a `FakePipeline` that produces no handover and no reply — so the
    property it appeared to guarantee was never exercised (R3 review). FR-14 §18 is about
    `metrics.json`, `metrics.md` and the log's `detail`; FR-01 requires the handover package,
    which is customer-derived by definition, and R3 requires it in the run output.
    What `outcomes.jsonl` may carry is `test_T_R3_5` below, with the real pipeline.
    """
    tickets = load_tickets(VALIDATION)
    phrases = [t.body[:40] for t in tickets[:10] if len(t.body) > 40]

    harness(tmp_path, FakePipeline(raise_on={tickets[0].ticket_id}))

    written = ((tmp_path / "out" / "metrics.json").read_text(encoding="utf-8")
               + (tmp_path / "out" / "metrics.md").read_text(encoding="utf-8"))
    for phrase in phrases:
        assert phrase not in written, f"customer text in the report: {phrase!r}"
    with DecisionLog(tmp_path / "decisions.db") as log:
        details = " ".join(r["detail"] or "" for r in log.rows())
    for phrase in phrases:
        assert phrase not in details, f"customer text in the log detail: {phrase!r}"

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
    """Spec §4: a figure this run could not produce says so, by name.

    **Re-pointed by review row R4, not weakened.** Two assertions pinned literal strings that
    R4 exists to remove: "by construction" (which named build rows 8 to 13 as the reason every
    ticket escalated, years after they were built) and "nothing is sent yet (row 11)". The
    property each one guarded is asserted here still, and more of it than before: that a
    100%-escalation run declares the fact, that a zero private-data count declares what it does
    and does not mean, and — new — that no caveat names a build row at all.
    """
    report = harness(tmp_path, FakePipeline(decide=lambda t: ("escalate", "no_retrieval")))
    markdown = (tmp_path / "out" / "metrics.md").read_text(encoding="utf-8")
    metrics = report.metrics

    assert metrics["volume"]["answered_automatically"] == 0, "the premise of this run"
    assert any("Every ticket escalated in this run" in gap for gap in metrics["gaps"]), (
        "100% escalation must be declared, not left to read as a tuning result")
    assert "every ticket escalated in this run" in {
        row["measure"]: row["confidence"]
        for row in metrics["results_table"]}["Escalation rate"]

    private = {row["measure"]: row for row in metrics["results_table"]}[
        "Private data in outbound text"]
    assert private["achieved"] == "0"
    assert "nothing was checked" in private["confidence"] and "leaked" in private["confidence"], (
        "a zero with no replies behind it must say which kind of zero it is")

    assert "does not measure routing" in metrics["technical"]["route_agreement_note"]
    assert "no false-positive counterpart" in metrics["technical"]["retrieval_hit_rate_note"]
    for phrase in ("human review", "no first-reply timestamp"):
        assert phrase in markdown, phrase
    for stale in ("row 8", "row 11", "row 13", "row 14"):
        assert stale not in markdown, f"a caveat still names a build row: {stale!r}"


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


# --- R3: the text that was sent, and the note that went with it ------------------------


def real_pipeline(tmp_path, answers=True, handover_parses=True):
    """The real graph over fake edges, so `reply` and `handover` are genuinely produced.

    A `FakePipeline` returning a hand-built `Outcome` would assert that the harness copies a
    string it was handed. What R3 exists for is the text the system actually sends, so the
    drafter, the guardrails and the handover writer are all the real ones.
    """
    from ticketing_agent.classify import Classification
    from ticketing_agent.generate import Drafter
    from ticketing_agent.guardrails import Guardrails, JudgeVerdict
    from ticketing_agent.handover import HandoverWriter
    from ticketing_agent.pipeline import SupportPipeline
    from ticketing_agent.provider import FakeTransport, ProviderClient
    from ticketing_agent.route import Router

    config = settings(tmp_path)
    retriever = Retriever(config, embedder=HashingEmbedder(),
                          client=chromadb.PersistentClient(path=str(tmp_path / "chroma-real")))
    retriever.build_index(DOCS)
    # Drafted from what *the ticket* retrieves, not from a query of our own. The pipeline
    # searches `ticket.text` — subject and body, cleaned — so searching the body alone picks a
    # different passage and every draft is refused with `invalid_citation`, which is FR-11
    # working correctly and a test that never answers anything.
    from ticketing_agent.ingest import normalise_ticket

    passage = retriever.search(normalise_ticket(_answerable_entry(0), index=0).text)[0]
    # The longest sentence of that passage, quoted verbatim. The *first* sentence of a
    # Resolution section is often a numbered stub ("1."), which clears no content-word overlap
    # floor and is refused by the grounding check — correctly, and uselessly for this test.
    grounded = max((s.strip() for s in passage.text.replace("\n", " ").split(". ")), key=len)

    class Fixed:
        def classify(self, ticket):
            return Classification(
                intent="billing_query" if answers else "security_incident",
                intent_confidence=0.95,
                intent_alternatives=(("quota_or_overage", 0.02),), urgency="high",
                urgency_confidence=0.7, urgency_reason="closest to DEV-0001")

    class Judge:
        def check(self, sentences, retrieved, indices=None):
            return JudgeVerdict(unsupported=(), detail="judged", prompt_version="PR-03 v1.0")

    draft = json.dumps({"answerable": True, "unknown_reason": "", "sentences": [
        {"text": grounded, "citations": [passage.chunk_id]}]})
    note = json.dumps({"summary": "The customer wants their invoice breakdown.",
                       "customer_goal": "See the charge for each service.",
                       "already_tried": ["checked the invoice PDF"],
                       "system_uncertainty": "ignored by FR-01 §3.4",
                       "relevant_passages": [passage.chunk_id],
                       "suggested_first_check": "Confirm the billing period."})
    # `handover_parses=False` makes PR-02's reply unusable, so `HandoverWriter` falls back to
    # its template — the path that copies the ticket's own first sentence into `customer_goal`,
    # and the only one on which the run output can carry raw customer text.
    body = draft if answers else (note if handover_parses else json.dumps({"not": "a note"}))
    transport = FakeTransport([{"choices": [{"message": {"content": body}}],
                                "model": "test-model", "system_fingerprint": "fp"}] * 400)
    client = ProviderClient(config, transport=transport)
    return SupportPipeline(retriever=retriever, classifier=Fixed(), router=Router(config),
                           drafter=Drafter(client), guardrails=Guardrails(judge=Judge()),
                           handover_writer=HandoverWriter(client), settings=config)


def _answerable_entry(n: int) -> dict:
    """One ordinary billing question. Every ticket is identical apart from its id, because what
    is under test is the run output, not the retrieval."""
    return {"ticket_id": f"ANS-{n:03d}", "channel": "email", "subject": "Invoice question",
            "body": "Where can I see the breakdown of my invoice by service?",
            "received_at": "2026-05-01T09:00:00Z", "customer_tier": "standard",
            "customer_region": "europe", "language_fluency": "fluent",
            "customer_name": "Dana Okonkwo"}


def answerable_file(tmp_path, count=6, labelled=False):
    """A small input file of answerable tickets. There is no such fixture: the engineered
    corpus is PII, injection, money and malformed tickets, all of which escalate by design.

    `labelled=True` adds the ground-truth block the harness scores against, so the report's
    classification, calibration and retrieval figures are computable.
    """
    entries = []
    for n in range(count):
        entry = _answerable_entry(n)
        if labelled:
            entry["labels"] = {"intent": "billing_query", "urgency": "high",
                               "expected_route": "auto_respond",
                               "answerable_from_docs": True,
                               "expected_doc_ids": ["DOC-BILL-002"]}
        entries.append(entry)
    path = tmp_path / ("answerable-labelled.json" if labelled else "answerable.json")
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def _lines(tmp_path):
    return [json.loads(line) for line in
            (tmp_path / "out" / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()]


def test_T_R3_1_every_answered_line_carries_the_text_that_was_sent(tmp_path):
    """R3 (FR-14, NFR-03): without the reply, the two-assessor review cannot be done at all.

    `outcomes.jsonl` carried the decision, the citations and the segments but not one word of
    what went to the customer — so hallucination rate and citation accuracy, which the
    Evaluation Framework measures by human review of the text, were unmeasurable, the demo
    could not show what was sent, and a complaint could not be reconstructed.
    """
    from ticketing_agent.generate import DISCLOSURE

    harness(tmp_path, real_pipeline(tmp_path), input_path=answerable_file(tmp_path))
    answered = [line for line in _lines(tmp_path) if line["decision"] == "auto_respond"]

    assert answered, "this fixture file is meant to produce answers"
    for line in answered:
        assert line["reply"], line["ticket_id"]
        assert DISCLOSURE in line["reply"], "FR-06's disclosure is part of what was sent"
        assert line["citations"], "and the passages it was drafted from"
        cited = {p["chunk_id"] for p in line["cited_passages"]}
        assert cited == set(line["citations"]), "the sheet needs the text, not only the ids"
        assert all(p["text"] for p in line["cited_passages"])
        assert line["intent"] and line["intent_confidence"] is not None
        assert line["urgency"] == "high"


def test_T_R3_2_every_escalated_line_carries_its_handover(tmp_path):
    """R3 (FR-01): the note a tier-two engineer is given, in the run output."""
    harness(tmp_path, real_pipeline(tmp_path, answers=False),
            input_path=answerable_file(tmp_path))
    escalated = [line for line in _lines(tmp_path) if line["decision"] == "escalate"]

    assert escalated
    for line in escalated:
        assert line["reply"] is None, "nothing was sent"
        note = line["handover"]
        assert note["summary"] and note["system_uncertainty"], line["ticket_id"]
        assert note["customer_goal"]
        assert note["suggested_first_check"], "FR-01: where a tier-two engineer should start"
        assert isinstance(note["already_tried"], list) and note["already_tried"]
        assert line["handover_redactions"] == [], "nothing private in this fixture to redact"


def test_T_R3_4_the_reply_is_on_the_terminal_row_too(tmp_path):
    """R3 (FR-13): a complaint is reconstructed from the log, not from a run directory."""
    harness(tmp_path, real_pipeline(tmp_path), input_path=answerable_file(tmp_path))

    with DecisionLog(tmp_path / "decisions.db") as log:
        rows = log.terminal_rows()
    answered = [r for r in rows if r["decision"] == "auto_respond"]
    assert answered
    assert all(r["reply_text"] for r in answered)
    assert all(r["reply_text"] is None for r in rows if r["decision"] == "escalate"), (
        "a withheld draft is not a sent reply, and the log must not imply it was")


def test_T_R3_3_the_review_sheet_is_a_sample_a_person_can_work_through(tmp_path):
    """R3: `scripts/review_sample.py` — n rows, deterministic for a seed, with the passages."""
    import csv

    from scripts.review_sample import main as review_sample

    # 5 of 12, not 3 of 6: with C(6,3) = 20 an unseeded sampler passes this about one run in
    # twenty, which is not a determinism test. C(12,5) = 792.
    harness(tmp_path, real_pipeline(tmp_path), input_path=answerable_file(tmp_path, count=12))
    source = tmp_path / "out" / "outcomes.jsonl"
    first, second, other_seed = tmp_path / "a.csv", tmp_path / "b.csv", tmp_path / "c.csv"

    assert review_sample(["--input", str(source), "--n", "5", "--seed", "1",
                          "--output", str(first)]) == 0
    assert review_sample(["--input", str(source), "--n", "5", "--seed", "1",
                          "--output", str(second)]) == 0
    assert review_sample(["--input", str(source), "--n", "5", "--seed", "2",
                          "--output", str(other_seed)]) == 0
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8"), (
        "the same seed gives the same sample, so two assessors review the same replies")
    assert first.read_text(encoding="utf-8") != other_seed.read_text(encoding="utf-8"), (
        "and it is a sample, not the first five")

    rows = list(csv.DictReader(first.open(encoding="utf-8")))
    assert len(rows) == 5
    for row in rows:
        assert row["reply"] and row["cited_passages"]
        for column in ("assessor_1_supported", "assessor_1_notes",
                       "assessor_2_supported", "assessor_2_notes"):
            assert column in row and row[column] == "", "the assessors fill these in"


def test_T_R3_3b_a_sample_larger_than_the_file_takes_what_there_is(tmp_path):
    """Asking for 50 from a file of 6 is not an error; the sheet says what it got."""
    import csv

    from scripts.review_sample import main as review_sample

    harness(tmp_path, real_pipeline(tmp_path), input_path=answerable_file(tmp_path))
    out = tmp_path / "sheet.csv"
    assert review_sample(["--input", str(tmp_path / "out" / "outcomes.jsonl"), "--n", "500",
                          "--seed", "1", "--output", str(out)]) == 0
    answered = [line for line in _lines(tmp_path) if line["decision"] == "auto_respond"]
    assert len(list(csv.DictReader(out.open(encoding="utf-8")))) == len(answered)


def test_T_R3_5_the_run_output_redacts_what_the_log_would_redact(tmp_path):
    """R3 review (high): `outcomes.jsonl` was writing raw customer text to disk.

    On the template path `customer_goal` is literally the first sentence of the body
    (`handover.py`), and `already_tried` is extracted from it by PR-02. Written raw, an email
    address and a phone number from a ticket went straight into a file — and `T-FR14-18`, the
    test that forbids exactly that, could not see it, because it runs a `FakePipeline` that
    produces no handover at all.

    The handover is customer-derived **by requirement** (FR-01), so the fix is not to strip it
    but to hold it to the same policy the decision log holds its scrubbed columns to, and to
    name every redaction on the line rather than altering text silently.
    """
    entry = _answerable_entry(0)
    # In the **first** sentence: that is what the template puts into `customer_goal`.
    entry["body"] = ("Please write back to dana.okonkwo@acme-health.example once someone has "
                     "looked at this. The service has been down since Tuesday.")
    path = tmp_path / "leaky.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")

    harness(tmp_path, real_pipeline(tmp_path, answers=False, handover_parses=False),
            input_path=path)
    written = (tmp_path / "out" / "outcomes.jsonl").read_text(encoding="utf-8")
    line = json.loads(written)

    assert line["decision"] == "escalate"
    assert "dana.okonkwo@acme-health.example" not in written, "an email address reached the file"
    assert "[redacted:email]" in written
    assert line["handover_redactions"], "and the line says what was redacted, not just that"
    assert any(r.endswith(":email") for r in line["handover_redactions"])


def test_T_R3_4b_a_draft_that_was_written_and_then_withheld_is_not_a_sent_reply(tmp_path):
    """R3 review (high): the one case D-63 rests on, and it was untested.

    `T-R3-4` asserted `reply_text is None` over the escalations of a run that produced none, so
    `all()` over an empty sequence made it vacuous — and it was unfalsifiable anyway, because
    `Outcome.draft` is already None on every escalation. This builds the real thing: a draft
    that passes generation and is then blocked by a guardrail. The log must not imply the
    customer received it.
    """
    from ticketing_agent.guardrails import Guardrails, JudgeVerdict

    pipeline = real_pipeline(tmp_path)

    class Blocking:
        """Refuses every draft, the way the grounding check refuses an unsupported one."""

        def check(self, sentences, retrieved, indices=None):
            return JudgeVerdict(unsupported=tuple(range(len(sentences))),
                                detail="nothing supported", prompt_version="PR-03 v1.0")

    pipeline._guardrails = Guardrails(judge=Blocking())
    harness(tmp_path, pipeline, input_path=answerable_file(tmp_path, count=3))

    lines = _lines(tmp_path)
    assert lines and all(line["decision"] == "escalate" for line in lines), (
        "every draft was blocked, so nothing was sent")
    assert all(line["reply"] is None for line in lines)

    with DecisionLog(tmp_path / "decisions.db") as log:
        rows = log.rows()
    blocked = [r for r in rows if r["decision"] == "block"]
    terminal = [r for r in rows if r["decision"] in {"auto_respond", "escalate"}]
    assert blocked, "FR-12 §5: the block is its own row"
    assert terminal and all(r["decision"] == "escalate" for r in terminal)
    assert all(r["reply_text"] is None for r in rows), (
        "a withheld draft is not a sent reply, on any row")


def test_T_R3_3c_pointing_the_sheet_at_the_wrong_file_is_a_clean_exit(tmp_path, capsys):
    """R3 review (low): a one-line JSON array parses, then `.get` blows up with a traceback.

    The likely operator slip is `--input` at a run's `metrics.json` or at a tickets file.
    """
    from scripts.review_sample import main as review_sample

    wrong = tmp_path / "metrics.json"
    wrong.write_text(json.dumps([{"ticket_id": "T-1"}]), encoding="utf-8")
    assert review_sample(["--input", str(wrong), "--n", "5", "--seed", "1",
                          "--output", str(tmp_path / "s.csv")]) == 2
    assert "not an outcomes.jsonl" in capsys.readouterr().err

    assert review_sample(["--input", str(tmp_path / "missing.jsonl"), "--n", "5", "--seed", "1",
                          "--output", str(tmp_path / "s.csv")]) == 2
    assert review_sample(["--input", str(wrong), "--n", "0", "--seed", "1",
                          "--output", str(tmp_path / "s.csv")]) == 2


# --- R4: the report describes the system that actually ran ------------------------------

#: Every phrase the report used to print next to real figures. These were written for the row-6
#: stub and were still there after rows 8 to 14 built everything they said was missing.
STALE_PHRASES = ("row 8", "row 11", "row 14", "row 6", "no classifier yet",
                 "nothing is sent yet", "the answering path is unbuilt",
                 "not computable yet", "by construction until")


def test_T_R4_1_a_full_run_report_says_nothing_about_unbuilt_rows(tmp_path):
    """R4 (FR-14, A10): the report is what acceptance criterion A10 is checked against.

    The last `metrics.md` said "Intent precision and recall: no classifier yet (row 8)" and
    "Private data in outbound replies: nothing is sent yet (row 11)" **next to** a per-class
    table at 100% and 42 sent replies. A report that describes a different system than the one
    that ran is worse than no report: an assessor cannot tell which half to believe.
    """
    harness(tmp_path, real_pipeline(tmp_path), input_path=answerable_file(tmp_path))

    written = ((tmp_path / "out" / "metrics.md").read_text(encoding="utf-8")
               + (tmp_path / "out" / "metrics.json").read_text(encoding="utf-8"))
    for phrase in STALE_PHRASES:
        assert phrase not in written, f"the report still describes an unbuilt system: {phrase!r}"
    assert "stub" not in written.lower(), "this was not a stub run"


def test_T_R4_2_a_stub_run_says_it_is_a_stub_run(tmp_path):
    """R4: different wording, because a stub run really has not measured most of this."""
    report = harness(tmp_path, stub_pipeline(tmp_path))

    assert report.metrics["run"]["pipeline"] == "stub"
    written = (tmp_path / "out" / "metrics.md").read_text(encoding="utf-8")
    assert "stub" in written.lower(), "a reader must not mistake this for a measured run"
    assert "no reply was generated" in written, (
        "and it must say why the figures are missing, not blame an unbuilt row")
    for phrase in ("row 8", "row 11", "row 14"):
        assert phrase not in written, phrase


def test_T_R4_3_the_results_table_is_filled_from_the_computed_figures(tmp_path):
    """R4: "not computable" only when the figure really is absent."""
    report = harness(tmp_path, real_pipeline(tmp_path),
                     input_path=answerable_file(tmp_path, labelled=True))
    metrics = report.metrics
    table = {row["measure"]: row for row in metrics["results_table"]}

    per_class = metrics["technical"]["classification"]["per_class"]
    worst = min(v["precision_pct"] for v in per_class.values()
                if v["precision_pct"] is not None)
    assert table["Intent precision (per class)"]["achieved"] == f"{worst}%", (
        "the cell claims to be the per-class figure, so it must be the worst class, "
        "not the average — NFR-03 wants ≥85% per class")

    detections = metrics["governance"]["private_data_detections"]
    assert table["Private data in outbound text"]["achieved"] == str(detections)
    assert "nothing is sent" not in table["Private data in outbound text"]["confidence"]

    assert metrics["technical"]["calibration"]["bands"], "NFR-03's calibration table"
    assert "needs the classifier" not in json.dumps(metrics["gaps"])


def test_T_R4_4_a_figure_that_really_is_absent_still_says_so(tmp_path):
    """R4: the point is not to remove the caveats — it is to earn them.

    An unlabelled file can compute no classification figures at all, and the report must say
    that rather than printing a number or a stale excuse.
    """
    entries = [{k: v for k, v in _answerable_entry(n).items()} for n in range(3)]
    path = tmp_path / "unlabelled.json"
    path.write_text(json.dumps(entries), encoding="utf-8")

    report = harness(tmp_path, real_pipeline(tmp_path), input_path=path)
    metrics = report.metrics

    assert metrics["run"]["scored_against_labels"] == "0 of 3"
    assert metrics["technical"]["classification"]["not_computable"], (
        "no labels, so no precision — and it must say which, not which build row")
    assert "row" not in metrics["technical"]["classification"]["not_computable"]
    table = {row["measure"]: row for row in metrics["results_table"]}
    assert table["Intent precision (per class)"]["achieved"] == "not computable"
    assert any("no labels" in gap for gap in metrics["gaps"])


def test_T_R4_5_the_route_agreement_note_describes_what_happened(tmp_path):
    """R4: the note said "while the answering path is unbuilt every ticket escalates".

    That was true at row 6 and false from row 14. The caveat it exists to carry — that this
    figure is not a measurement of routing when everything escalated — is real, so it is kept
    and made conditional on the run rather than on the build.
    """
    answered_run = harness(tmp_path / "a", real_pipeline(tmp_path / "a"),
                           input_path=answerable_file(tmp_path / "a"))
    note = answered_run.metrics["technical"]["route_agreement_note"]
    assert "unbuilt" not in note
    assert "every ticket escalate" not in note

    all_escalated = harness(tmp_path / "b", stub_pipeline(tmp_path / "b"))
    note = all_escalated.metrics["technical"]["route_agreement_note"]
    assert "Every ticket escalated" in note, (
        "when nothing was answered the figure really is just the share labelled escalate")


def two_intent_pipeline(tmp_path, wrong_for=("ANS-004", "ANS-005")):
    """A classifier with **two** intents and a real error, so the per-class floor can differ.

    R4 review (high): T-R4-3 asserted that the precision cell is the *worst* class and not the
    average, on a fixture with exactly one class — where min, max and macro are the same
    number. Mutating `_per_class_floor` to `max` or to a mean left it passing. This fixture has
    two labelled intents and gets one of them wrong on two tickets, so the three aggregates are
    three different numbers.
    """
    from ticketing_agent.classify import Classification
    from ticketing_agent.generate import Drafter
    from ticketing_agent.guardrails import Guardrails, JudgeVerdict
    from ticketing_agent.handover import HandoverWriter
    from ticketing_agent.pipeline import SupportPipeline
    from ticketing_agent.provider import FakeTransport, ProviderClient
    from ticketing_agent.route import Router

    config = settings(tmp_path)
    retriever = Retriever(config, embedder=HashingEmbedder(),
                          client=chromadb.PersistentClient(path=str(tmp_path / "chroma-2i")))
    retriever.build_index(DOCS)

    class TwoIntents:
        def classify(self, ticket):
            # The tickets labelled `rollback_request` are called `billing_query`, so
            # billing_query's precision falls while rollback_request's recall does.
            intent = "billing_query"
            return Classification(
                intent=intent, intent_confidence=0.95,
                intent_alternatives=(("rollback_request", 0.03),), urgency="high",
                urgency_confidence=0.7, urgency_reason="closest to DEV-0001")

    class Judge:
        def check(self, sentences, retrieved, indices=None):
            return JudgeVerdict(unsupported=(), detail="judged", prompt_version="PR-03 v1.0")

    transport = FakeTransport([{"choices": [{"message": {"content": json.dumps(
        {"answerable": False, "unknown_reason": "not covered", "sentences": []})}}],
        "model": "test-model", "system_fingerprint": "fp"}] * 400)
    client = ProviderClient(config, transport=transport)
    return SupportPipeline(retriever=retriever, classifier=TwoIntents(), router=Router(config),
                           drafter=Drafter(client), guardrails=Guardrails(judge=Judge()),
                           handover_writer=HandoverWriter(config and client), settings=config)


def two_intent_file(tmp_path, count=6, wrong=2):
    """`count` tickets, the last `wrong` of them labelled a different intent."""
    entries = []
    for n in range(count):
        entry = _answerable_entry(n)
        entry["labels"] = {"intent": "rollback_request" if n >= count - wrong
                           else "billing_query"}
        entries.append(entry)
    path = tmp_path / "two-intent.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_T_R4_6_the_precision_cell_is_the_worst_class_and_not_the_average(tmp_path):
    """R4 review (high): NFR-03's target is ≥85% **per class**, so the floor is the figure.

    A macro average of 100% over twenty classes and 40% over one meets no requirement and
    reads as 97%. With two classes at different precisions, min, max and mean are three
    different numbers and the cell can only be one of them.
    """
    report = harness(tmp_path, two_intent_pipeline(tmp_path),
                     input_path=two_intent_file(tmp_path, count=6, wrong=2))
    metrics = report.metrics
    per_class = metrics["technical"]["classification"]["per_class"]
    values = sorted(v["precision_pct"] for v in per_class.values()
                    if v["precision_pct"] is not None)
    assert len(values) >= 1
    cell = {row["measure"]: row for row in metrics["results_table"]}[
        "Intent precision (per class)"]
    assert cell["achieved"] == f"{values[0]}%", (per_class, cell)
    if len(values) > 1:
        assert values[0] != values[-1], "the fixture must make the aggregates differ"
        assert cell["achieved"] != f"{values[-1]}%"

    # The denominator is what produced a prediction, not every labelled ticket.
    predicted = sum(1 for line in _lines(tmp_path) if line["prediction_value"])
    assert f"{predicted} of " in cell["confidence"]
    assert "in-sample caution" in cell["confidence"], (
        "the project's own evidence says this figure is partly leakage (R8)")


def test_T_R4_7_calibration_uses_the_classifiers_number_not_routings_floor(tmp_path):
    """R4 review (high): a classifier outage printed a clean NFR-03 pass.

    `prediction_confidence` is what routing compared and is floored to 0.0 when there is no
    usable classification at all (D-61, FR-02 §3.2) — and `0.0 is not None`, so those tickets
    were counted. Six tickets on which the classifier raised became six rows at stated 0.0% /
    observed 0.0%, gap 0.0, "within NFR-03's 5-point limit", printed two lines under the
    section that correctly said no intent could be scored.
    """
    pipeline = real_pipeline(tmp_path)

    class Dead:
        def classify(self, ticket):
            raise RuntimeError("the classifier is unavailable")

    pipeline._classifier = Dead()
    report = harness(tmp_path, pipeline,
                     input_path=answerable_file(tmp_path, count=6, labelled=True))
    metrics = report.metrics

    assert metrics["technical"]["classification"]["not_computable"]
    calibration = metrics["technical"]["calibration"]
    assert calibration["bands"] == [] and calibration["pairs"] == 0, (
        "no classification means no calibration, not a pass manufactured from floored zeros")
    assert calibration["not_computable"]
    assert "within_5_points" not in calibration
    assert any("Confidence calibration" in gap for gap in metrics["gaps"]), (
        "FR-14 §4: a figure this run could not produce says so, by name")
    assert any("Intent precision" in gap for gap in metrics["gaps"])


def test_T_R4_8_calibration_and_the_per_class_table_score_the_same_field(tmp_path):
    """R4 review (high): one report, two verdicts on the same tickets.

    Calibration keyed correctness off `Outcome.intent`, which only arrives via `row_fields()` —
    a path `pipeline.py` deliberately tolerates being absent. A classifier without it produced
    100% per-class precision beside a 95-point calibration gap, both describing the same six
    tickets.
    """
    pipeline = real_pipeline(tmp_path)

    class Minimal:
        intent, intent_confidence, intent_alternatives = "billing_query", 0.95, ()

    class MinimalClassifier:
        def classify(self, _ticket):
            return Minimal()

    pipeline._classifier = MinimalClassifier()
    report = harness(tmp_path, pipeline,
                     input_path=answerable_file(tmp_path, count=6, labelled=True))
    technical = report.metrics["technical"]

    assert technical["classification"]["overall_accuracy_pct"] == 100.0
    calibration = technical["calibration"]
    bands = [b for b in calibration["bands"] if b["count"]]
    if bands:
        for band in bands:
            assert band["observed_pct"] == 100.0, (
                "the two sections must agree about which tickets were right")
    else:
        # The honest outcome on this path, and the one the fix produces: without `row_fields`
        # the classifier's own stated confidence never reaches the outcome, so there is nothing
        # to calibrate and the report says so — rather than scoring a different field and
        # printing a 95-point gap beside 100% precision.
        assert calibration["not_computable"] and calibration["pairs"] == 0
        assert any("Confidence calibration" in gap for gap in report.metrics["gaps"])
    assert "within_5_points" not in calibration or calibration["within_5_points"] is not False, (
        "no run may report an NFR-03 failure that contradicts a 100% per-class table")


def test_T_R4_9_a_rule_only_run_is_not_called_a_tuning_result(tmp_path):
    """R4 review (medium): the replacement asserted the opposite error.

    "a result rather than a construction" was printed with no knowledge of the kill switch
    (FR-16), a provider outage (FR-15) or a file of nothing but must-escalate tickets — in each
    of which 100% escalation really is by construction. That is the same over-reading R4 exists
    to stop, pointed the other way.
    """
    report = harness(tmp_path, FakePipeline(
        decide=lambda t: ("escalate", "must_escalate_intent")))
    cell = {row["measure"]: row for row in report.metrics["results_table"]}["Escalation rate"]

    assert "not a tuning result" in cell["confidence"]
    assert "must_escalate_intent" in cell["confidence"]
    assert "a result rather than a construction" not in cell["confidence"]


def test_T_R4_10_the_kill_switch_is_named_as_the_reason_everything_escalated(tmp_path):
    """FR-16 plus R4: with the switch on, 100% escalation is a construction and must say so."""
    switch = tmp_path / "KILL_SWITCH"
    switch.write_text("on", encoding="utf-8")
    report = harness(tmp_path, FakePipeline(decide=lambda t: ("escalate", "kill_switch")),
                     kill_switch_file=switch)
    cell = {row["measure"]: row for row in report.metrics["results_table"]}["Escalation rate"]

    assert "kill switch" in cell["confidence"] and "FR-16" in cell["confidence"]
    assert "not a tuning result" in cell["confidence"]


def test_T_R4_11_a_run_that_never_reached_the_provider_does_not_claim_a_cache_replay(tmp_path):
    """R4 review (medium): "every response was served from the cache" with zero cache hits."""
    report = harness(tmp_path, FakePipeline(
        decide=lambda t: ("escalate", "must_escalate_intent")))
    governance = report.metrics["governance"]
    assert governance["model_calls"] == 0 and governance["cache_hits"] == 0, "the premise"

    cell = {row["measure"]: row for row in report.metrics["results_table"]}[
        "Processing time p95"]
    assert "cache" not in cell["confidence"] or "no cache hit" in cell["confidence"]
    assert "the provider was never reached" in cell["confidence"]


def test_T_R4_12_the_fairness_row_carries_a_figure_and_not_a_cross_reference(tmp_path):
    """R4 review (medium): NFR-06's one row in the summary table had no value at all.

    `"see segments.*.variation_points"` — while every dimension of the real gate run was above
    the 5-point limit. The summary table is what an assessor reads.
    """
    report = harness(tmp_path, FakePipeline(
        decide=lambda t: ("auto_respond", None) if t.customer_tier == "standard"
        else ("escalate", "low_confidence")))
    metrics = report.metrics
    cell = {row["measure"]: row for row in metrics["results_table"]}["Cross-segment variation"]

    worst = max(block["variation_points"] for block in metrics["segments"].values()
                if block["variation_points"] is not None)
    assert cell["achieved"].startswith(f"{worst} points"), cell
    assert "see segments" not in cell["achieved"]
    assert any(name in cell["achieved"] for name in metrics["segments"]), (
        "and it names which dimension was worst")


def test_T_R4_13_the_private_data_row_counts_what_was_sent_not_what_was_blocked(tmp_path):
    """R4 review (medium): the cell read a guardrail *detection* count.

    FR-12 blocks a reply that fails the `private_data` check, so a detection is the guardrail
    working. Against a target of 0, reading detections here made a working block look like a
    target miss — and the measure is named "in outbound text".
    """
    from ticketing_agent.guardrails import Guardrails, JudgeVerdict

    pipeline = real_pipeline(tmp_path)

    class LeakyJudge:
        def check(self, sentences, retrieved, indices=None):
            return JudgeVerdict(unsupported=(), detail="judged", prompt_version="PR-03 v1.0")

    pipeline._guardrails = Guardrails(judge=LeakyJudge())
    entry = _answerable_entry(0)
    entry["body"] = ("Where can I see my invoice breakdown? Reply to "
                     "dana.okonkwo@acme-health.example.")
    path = tmp_path / "leaky.json"
    path.write_text(json.dumps([entry]), encoding="utf-8")

    report = harness(tmp_path, pipeline, input_path=path)
    metrics = report.metrics
    cell = {row["measure"]: row for row in metrics["results_table"]}[
        "Private data in outbound text"]

    assert cell["achieved"] == "0", (
        "nothing was sent carrying private data, whatever the guardrails detected")
    if metrics["governance"]["private_data_detections"]:
        assert "blocked" in cell["confidence"] and "FR-12" in cell["confidence"]
