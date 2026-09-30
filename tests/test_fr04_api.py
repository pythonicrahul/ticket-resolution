"""FR-04 and FR-05 through the API (docs/specs/FR-04.md, FR-08.md §FR-05).

Offline: the app is built with an injected pipeline and a hashing-embedder retriever, so the
routes, the queue ordering and the error handling are exercised with no network and no key.

The API is the one surface aimed at **agents** rather than customers, so most of what is asserted
here is about what it must not expose: the agents' own past answers are not searchable, a failure
does not leak an exception, and the queue carries the urgency with its reason.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ticketing_agent.api import build_app
from ticketing_agent.config import Settings
from ticketing_agent.pipeline import Outcome
from ticketing_agent.retrieve import Retriever

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "data" / "documentation.json"
GROUND_TRUTH = ROOT / "data" / "ground_truth_responses.json"


def recorded_embedder():
    """The real model's vectors, recorded once (row 5's fixture), so FR-04's criterion — a claim
    about *meaning* — can be tested offline. A hashing embedder cannot make that claim."""
    from ticketing_agent.retrieve import RecordedEmbedder

    payload = json.loads((ROOT / "tests" / "fixtures" / "recorded_embeddings.json")
                         .read_text("utf-8"))
    return RecordedEmbedder({k: v for k, v in payload["vectors"].items()}, payload["embedder"])


def settings(tmp_path, **overrides):
    values = {"model_name": "test-model", "llm_api_key": "test-key-not-a-real-one",
              "docs_path": DOCS, "chroma_path": tmp_path / "chroma",
              "decision_log_path": tmp_path / "decisions.db",
              "kill_switch_file": tmp_path / "absent",
              "confidence_threshold": 0.85, "relevance_threshold": 0.0}
    values.update(overrides)
    return Settings(**values)


class FakePipeline:
    """One outcome per ticket, so the API's own behaviour is what is under test."""

    def __init__(self, decision="escalate", reason="must_escalate_intent"):
        self.decision, self.reason = decision, reason
        self.seen: list[str] = []

    def process(self, ticket):
        self.seen.append(ticket.ticket_id)
        return Outcome(
            ticket=ticket, decision=self.decision, reason=self.reason,
            explanation="This is a security report, which always goes to a person.",
            all_reasons=(self.reason,) if self.reason else (),
            stage="routing", prediction_value="security_incident", prediction_confidence=0.9,
            threshold_applied=0.85, summary="A former employee still has access.",
            uncertainty="This is a security report, which always goes to a person.",
            draft=None if self.decision == "escalate" else "Here is the answer.",
            requirement_ids=("FR-14",))


def _index(embedder):
    import tempfile

    one = Retriever(Settings(model_name="t", docs_path=DOCS,
                             chroma_path=Path(tempfile.mkdtemp()) / "chroma",
                             relevance_threshold=0.0), embedder=embedder)
    one.build_index(DOCS)
    return one


@pytest.fixture(scope="module")
def meaning_retriever():
    """Real recorded vectors: the only fixture that can support a claim about meaning."""
    return _index(recorded_embedder())


@pytest.fixture(scope="module")
def retriever():
    """Deterministic token hashing: enough for ranking, bounds, errors and what is indexed."""
    from ticketing_agent.retrieve import HashingEmbedder

    return _index(HashingEmbedder())


def client(tmp_path, retriever, pipeline=None, **overrides):
    app = build_app(settings(tmp_path, **overrides), retriever=retriever,
                    pipeline=pipeline or FakePipeline())
    return TestClient(app)


# --- FR-04: search ---------------------------------------------------------------------


def test_T_FR04_1_the_prds_own_criterion(tmp_path, meaning_retriever):
    """"my deployment keeps dying" returns DOC-DEPLOY-001 in the top 3 — and it shares no content
    word with the article, so this is a claim about meaning and needs the recorded vectors."""
    response = client(tmp_path, meaning_retriever).get(
        "/search", params={"q": "my deployment keeps dying"})

    assert response.status_code == 200
    top = [hit["doc_id"] for hit in response.json()["results"][:3]]
    assert "DOC-DEPLOY-001" in top, top
    assert "dying" not in " ".join(h["text"] for h in response.json()["results"]).lower(), (
        "it matched by meaning, not by the word")


def test_T_FR04_2_results_are_ranked_and_carry_their_ids(tmp_path, retriever):
    body = client(tmp_path, retriever).get("/search", params={"q": "invoice breakdown"}).json()

    assert body["query"] == "invoice breakdown"
    assert body["count"] == len(body["results"])
    ranks = [hit["rank"] for hit in body["results"]]
    assert ranks == sorted(ranks)
    for hit in body["results"]:
        assert hit["doc_id"] and hit["chunk_id"].startswith(hit["doc_id"])
        assert 0.0 <= hit["score"] <= 1.0


def test_T_FR04_4_the_agents_own_answers_are_never_searchable(tmp_path, retriever):
    """The requirement singles this out, and it is also the file that would make the evaluation
    meaningless if the system could read from it."""
    answers = json.loads(GROUND_TRUTH.read_text("utf-8"))
    one = client(tmp_path, retriever)
    indexed = {chunk.chunk_id for chunk in retriever.chunks}

    for row in answers[:15]:
        text = row.get("response") or row.get("reply") or ""
        phrase = " ".join(text.split()[:8])
        if len(phrase) < 20:
            continue
        for hit in one.get("/search", params={"q": phrase}).json()["results"]:
            assert hit["chunk_id"] in indexed, "a hit came from outside the documentation index"
            assert hit["text"] not in text, "an expert answer was served as documentation"


@pytest.mark.parametrize(("k", "expected"), [(0, 422), (21, 422), (1, 200), (20, 200)])
def test_T_FR04_5_k_is_bounded(tmp_path, retriever, k, expected):
    response = client(tmp_path, retriever).get("/search", params={"q": "invoice", "k": k})
    assert response.status_code == expected
    if expected == 200:
        assert len(response.json()["results"]) <= k


def test_T_FR04_5b_the_default_is_five(tmp_path, retriever):
    body = client(tmp_path, retriever).get("/search", params={"q": "invoice"}).json()
    assert len(body["results"]) <= 5


@pytest.mark.parametrize("query", ["", "   "])
def test_T_FR04_6_an_empty_query_is_refused(tmp_path, retriever, query):
    """"Nothing matched" and "you asked nothing" are different answers."""
    response = client(tmp_path, retriever).get("/search", params={"q": query})
    assert response.status_code in (400, 422)


def test_T_FR04_7_a_retrieval_failure_is_a_503_that_leaks_nothing(tmp_path):
    from ticketing_agent.retrieve import RetrievalError

    class Broken:
        chunks = ()

        def search(self, *args, **kwargs):
            raise RetrievalError("chroma said: /secret/path/to/index is corrupt")

    response = client(tmp_path, Broken()).get("/search", params={"q": "invoice"})
    assert response.status_code == 503
    assert "/secret/path" not in response.text


def test_T_FR04_8_search_takes_no_decision(tmp_path, retriever):
    from ticketing_agent.logging_store import DecisionLog

    one = client(tmp_path, retriever)
    one.get("/search", params={"q": "invoice breakdown"})
    with DecisionLog(settings(tmp_path).decision_log_path) as log:
        assert log.rows() == [], "a search decides nothing about a ticket, so it logs nothing"


# --- FR-05: submit a ticket, and the escalation queue ----------------------------------


def test_submitting_a_ticket_returns_its_outcome_and_logs_it(tmp_path, retriever):
    from ticketing_agent.logging_store import DecisionLog

    pipeline = FakePipeline()
    response = client(tmp_path, retriever, pipeline).post("/tickets", json={
        "ticket_id": "API-1", "channel": "email", "subject": "Former employee has access",
        "body": "They left three weeks ago and their key still works.",
        "received_at": "2026-05-01T09:00:00Z"})

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "escalate"
    assert body["reason"] == "must_escalate_intent"
    assert body["explanation"] and body["summary"]
    assert pipeline.seen == ["API-1"]

    with DecisionLog(settings(tmp_path).decision_log_path) as log:
        rows = log.rows()
    assert len(rows) == 1 and rows[0]["ticket_id"] == "API-1", (
        "every decision is logged, the API included (FR-13)")


def test_a_block_inside_the_pipeline_is_recorded_for_an_api_ticket_too(tmp_path, retriever):
    """FR-12 §5: "the block is recorded". The API used to process the ticket and *then* attach
    the log, so every row the graph wrote as it went was silently lost — a guardrail block
    through the harness was recorded and the same block through HTTP was not."""
    class BlockingPipeline(FakePipeline):
        def __init__(self):
            super().__init__()
            self.log = None

        def attach_log(self, log):
            self.log = log

        def process(self, ticket):
            from ticketing_agent.logging_store import DecisionEntry

            assert self.log is not None, "the log must be attached before the work starts"
            self.log.perform(DecisionEntry(
                ticket_id=ticket.ticket_id, stage="validation", decision="block",
                reason="private_data_in_draft", requirement_ids=["FR-12"],
                guardrail_results=[["private_data", False]]), lambda: None)
            return super().process(ticket)

    from ticketing_agent.logging_store import DecisionLog

    pipeline = BlockingPipeline()
    response = client(tmp_path, retriever, pipeline).post("/tickets", json={
        "ticket_id": "API-B", "channel": "email", "subject": "Question",
        "body": "Please help with my invoice.", "received_at": "2026-05-01T09:00:00Z"})

    assert response.status_code == 200
    with DecisionLog(settings(tmp_path).decision_log_path) as log:
        decisions = [r["decision"] for r in log.rows()]
    assert "block" in decisions, "the block was not recorded"
    assert decisions.index("block") < decisions.index("escalate"), "and it came first"


def test_the_log_reaches_a_lazily_built_pipeline_on_the_very_first_ticket(tmp_path, retriever):
    """The previous test injects a pipeline, so `_State._pipeline` is already set and the bug
    hides. A real deployment builds the graph on the first request: the log must reach *that*
    graph, on that request, or FR-12 §5's "the block is recorded" is lost for the process."""
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog

    built: list[str] = []

    class Lazy(FakePipeline):
        def __init__(self):
            super().__init__()
            self.log = None
            built.append("built")

        def attach_log(self, log):
            self.log = log

        def process(self, ticket):
            assert self.log is not None, "the graph was built after the log was attached"
            self.log.perform(DecisionEntry(
                ticket_id=ticket.ticket_id, stage="generation", decision="continue",
                requirement_ids=["FR-11"], prompt_version="PR-01 v1.0", model_calls=1),
                lambda: None)
            return super().process(ticket)

    import ticketing_agent.api as module

    app = module.build_app(settings(tmp_path), retriever=retriever, pipeline=None)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(module, "_build_pipeline", lambda *a, **k: Lazy())
        response = TestClient(app).post("/tickets", json={
            "ticket_id": "LAZY-1", "channel": "email", "subject": "Question",
            "body": "Where do I find the usage breakdown?",
            "received_at": "2026-05-01T09:00:00Z"})

    assert response.status_code == 200
    assert built == ["built"], "the graph was built exactly once, by this request"
    with DecisionLog(settings(tmp_path).decision_log_path) as log:
        stages = [r["stage"] for r in log.rows()]
    assert "generation" in stages, "the row the graph wrote as it went was lost"


def test_a_ticket_that_cannot_be_read_is_a_400_not_a_crash(tmp_path, retriever):
    response = client(tmp_path, retriever).post("/tickets", json={"channel": "email"})
    assert response.status_code in (400, 422)


def test_T_FR05_the_queue_is_ordered_by_urgency_then_age(tmp_path, retriever):
    """FR-05's criterion: sorted by urgency, then received_at, with the reason shown."""
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog

    rows = [("Q-1", "low", "2026-05-01T09:00:00Z"), ("Q-2", "high", "2026-05-03T09:00:00Z"),
            ("Q-3", "high", "2026-05-02T09:00:00Z"), ("Q-4", "medium", "2026-05-01T09:00:00Z")]
    with DecisionLog(settings(tmp_path).decision_log_path, run_id="api") as log:
        for ticket_id, urgency, received in rows:
            log.record(DecisionEntry(
                ticket_id=ticket_id, stage="routing", decision="escalate",
                reason="must_escalate_intent", explanation="It goes to a person.",
                requirement_ids=["FR-05"], urgency=urgency, urgency_confidence=0.7,
                summary=f"{ticket_id} needs a person.", received_at=received,
                channel="email", detail="closest to DEV-0001"))

    body = client(tmp_path, retriever).get("/queue").json()
    assert [item["ticket_id"] for item in body["items"]] == ["Q-3", "Q-2", "Q-4", "Q-1"]
    first = body["items"][0]
    assert first["urgency"] == "high"
    assert first["urgency_confidence"] == 0.7
    assert first["summary"], "an agent picking this up sees why it is here"


def test_the_queue_holds_only_escalations(tmp_path, retriever):
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog

    with DecisionLog(settings(tmp_path).decision_log_path, run_id="api") as log:
        log.record(DecisionEntry(ticket_id="A-1", stage="routing", decision="auto_respond",
                                 explanation="Answered.", threshold_applied=0.85,
                                 requirement_ids=["FR-02"], urgency="high"))
        log.record(DecisionEntry(ticket_id="E-1", stage="routing", decision="escalate",
                                 reason="low_confidence", explanation="A person takes it.",
                                 requirement_ids=["FR-02"], urgency="low"))

    items = client(tmp_path, retriever).get("/queue").json()["items"]
    assert [i["ticket_id"] for i in items] == ["E-1"], "an answered ticket is not in the queue"


def test_metrics_reports_what_the_run_did(tmp_path, retriever):
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog

    with DecisionLog(settings(tmp_path).decision_log_path, run_id="api") as log:
        log.record(DecisionEntry(ticket_id="M-1", stage="routing", decision="auto_respond",
                                 explanation="Answered.", threshold_applied=0.85,
                                 requirement_ids=["FR-02"]))
        log.record(DecisionEntry(ticket_id="M-2", stage="routing", decision="escalate",
                                 reason="no_retrieval", explanation="A person takes it.",
                                 requirement_ids=["FR-10"]))

    body = client(tmp_path, retriever).get("/metrics").json()
    assert body["decisions"] == 2
    assert body["answered"] == 1 and body["escalated"] == 1
    assert body["by_reason"]["no_retrieval"] == 1
    assert body["thresholds"]["confidence"] == 0.85, "a reader can see which floor was in force"


def test_the_health_check_says_what_is_wired(tmp_path, retriever):
    body = client(tmp_path, retriever).get("/health").json()
    assert body["ok"] is True
    assert body["documents_indexed"] > 0
    assert body["kill_switch"] is False


# --- NFR-05: the scrape endpoint and the dashboard -------------------------------------


def test_prometheus_exposes_the_decision_log_not_in_process_counters(tmp_path, retriever):
    """A counter resets when the process does; NFR-05 asks for 100% of decisions to be
    auditable. The scrape is a view of the log, so a restart does not lose the history."""
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog

    with DecisionLog(settings(tmp_path).decision_log_path, run_id="api") as log:
        log.record(DecisionEntry(ticket_id="P-1", stage="routing", decision="auto_respond",
                                 explanation="Answered.", threshold_applied=0.85,
                                 requirement_ids=["FR-02"], model_calls=2,
                                 prompt_version="PR-01 v1.0"))
        log.record(DecisionEntry(ticket_id="P-2", stage="routing", decision="escalate",
                                 reason="no_retrieval", explanation="A person takes it.",
                                 requirement_ids=["FR-10"]))
        log.record(DecisionEntry(ticket_id="P-3", stage="validation", decision="block",
                                 reason="private_data_in_draft", requirement_ids=["FR-12"],
                                 guardrail_results=[["private_data", False],
                                                    ["grounding", True]]))

    # A second client, as a scraper would be: nothing is carried in memory between them.
    body = client(tmp_path, retriever).get("/metrics/prometheus").text

    assert "# TYPE ticketing_decisions_total counter" in body
    assert "ticketing_decisions_total 2" in body
    assert 'ticketing_decisions_by_outcome_total{outcome="auto_respond"} 1' in body
    assert 'ticketing_escalations_by_reason_total{reason="no_retrieval"} 1' in body
    assert 'ticketing_guardrail_blocks_total{check="private_data"} 1' in body
    assert 'ticketing_guardrail_blocks_total{check="grounding"}' not in body, "it passed"
    assert "ticketing_model_calls_total 2" in body
    assert 'ticketing_threshold{kind="confidence"} 0.85' in body
    assert "ticketing_kill_switch 0" in body


def test_prometheus_is_parseable_and_escapes_its_labels(tmp_path, retriever):
    """A reason with a quote or a newline in it must not end the label value."""
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog

    with DecisionLog(settings(tmp_path).decision_log_path, run_id="api") as log:
        log.record(DecisionEntry(ticket_id="P-9", stage="routing", decision="escalate",
                                 reason='odd"reason', explanation="A person takes it.",
                                 requirement_ids=["FR-02"]))

    body = client(tmp_path, retriever).get("/metrics/prometheus").text
    assert 'reason="odd\\"reason"' in body

    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, value = line.rpartition(" ")
        assert name and float(value) == float(value), line


def test_the_kill_switch_is_visible_to_a_scraper(tmp_path, retriever):
    """A run whose escalation rate jumps to 100% should be explainable from the dashboard —
    FR-16 §7 noted that nothing alerts on the switch being on."""
    switch = tmp_path / "KILL_SWITCH"
    switch.write_text("", encoding="utf-8")
    body = client(tmp_path, retriever, kill_switch_file=switch).get("/metrics/prometheus").text
    assert "ticketing_kill_switch 1" in body


def test_the_grafana_dashboard_names_panels_that_the_scrape_provides():
    """A dashboard referring to a metric nobody exports is a dashboard of empty panels."""
    import re

    dashboard = json.loads((ROOT / "ops" / "grafana_dashboard.json").read_text("utf-8"))
    exported = set(re.findall(r"^# TYPE (\S+) ",
                              (ROOT / "ops" / "metrics_reference.txt").read_text("utf-8"), re.MULTILINE))
    assert exported, "the reference lists no metrics"

    used = set()
    for panel in dashboard["panels"]:
        assert panel.get("title"), "every panel says what it shows"
        for target in panel.get("targets", []):
            used |= set(re.findall(r"ticketing_[a-z_]+", target["expr"]))

    assert used, "the dashboard queries nothing"
    assert used <= exported, f"panels query metrics nobody exports: {sorted(used - exported)}"
