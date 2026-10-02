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
              # D-64: without this the provider cache defaults to the real
              # ./storage/llm_cache.sqlite, so a test both replays from and writes into the
              # cache a gate run replays from.
              "llm_cache_path": tmp_path / "llm_cache.sqlite",
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


def test_the_grafana_dashboard_names_panels_that_the_scrape_provides(tmp_path, retriever):
    """A dashboard referring to a metric nobody exports is a dashboard of empty panels.

    **Reads the live endpoint, not `ops/metrics_reference.txt`** (R10 review). It used to
    compare two checked-in files, so deleting a metric block from `api.py` left the suite green
    and a panel permanently empty — while `scripts/write_metrics_reference.py`, the dashboard's
    own `description` and the provisioning file all claim the two "cannot drift". The reference
    is regenerated by hand; the exporter is the thing that has to be right.
    """
    import re

    dashboard = json.loads((ROOT / "ops" / "grafana_dashboard.json").read_text("utf-8"))
    scraped = client(tmp_path, retriever).get("/metrics/prometheus").text
    exported = set(re.findall(r"^# TYPE (\S+) ", scraped, re.MULTILINE))
    assert exported, "the endpoint exported no metrics"

    # And the checked-in reference still matches what the endpoint emits, so the file the
    # dashboard was written against is not quietly stale.
    reference = set(re.findall(r"^# TYPE (\S+) ",
                               (ROOT / "ops" / "metrics_reference.txt").read_text("utf-8"),
                               re.MULTILINE))
    assert reference == exported, (
        f"ops/metrics_reference.txt is stale — run scripts/write_metrics_reference.py. "
        f"only in file: {sorted(reference - exported)}; only in exporter: "
        f"{sorted(exported - reference)}")

    used = set()
    for panel in dashboard["panels"]:
        assert panel.get("title"), "every panel says what it shows"
        for target in panel.get("targets", []):
            used |= set(re.findall(r"ticketing_[a-z_]+", target["expr"]))

    assert used, "the dashboard queries nothing"
    assert used <= exported, f"panels query metrics nobody exports: {sorted(used - exported)}"


# --- R2: the queue orders on urgency the pipeline actually produced --------------------


def _real_pipeline(tmp_path, retriever, urgencies):
    """The real graph over fake edges, with a classifier that varies urgency by ticket id.

    `test_T_FR05_the_queue_is_ordered_by_urgency_then_age` writes `urgency` into the log by
    hand, so it passed throughout the period in which the pipeline never wrote that column.
    This builds the graph that a request actually runs, so the column has to be filled by the
    code under test.
    """
    import json as _json

    from ticketing_agent.classify import Classification
    from ticketing_agent.generate import Drafter
    from ticketing_agent.guardrails import Guardrails, JudgeVerdict
    from ticketing_agent.handover import HandoverWriter
    from ticketing_agent.pipeline import SupportPipeline
    from ticketing_agent.provider import FakeTransport, ProviderClient
    from ticketing_agent.route import Router

    class VaryingClassifier:
        def classify(self, ticket):
            return Classification(
                intent="security_incident", intent_confidence=0.93,
                intent_alternatives=(("compliance_request", 0.04),),
                urgency=urgencies[ticket.ticket_id], urgency_confidence=0.7,
                urgency_reason="closest to DEV-0001")

    class Judge:
        def check(self, sentences, retrieved, indices=None):
            return JudgeVerdict(unsupported=(), detail="judged", prompt_version="PR-03 v1.0")

    note = _json.dumps({"summary": "A former employee still has access.",
                        "customer_goal": "Revoke the access.", "already_tried": [],
                        "system_uncertainty": "ignored by FR-01 §3.4",
                        "relevant_passages": [], "suggested_first_check": None})
    config = settings(tmp_path)
    transport = FakeTransport([{"choices": [{"message": {"content": note}}],
                               "model": "test-model", "system_fingerprint": "fp"}] * 10)
    client_obj = ProviderClient(config, transport=transport)
    return SupportPipeline(retriever=retriever, classifier=VaryingClassifier(),
                           router=Router(config), drafter=Drafter(client_obj),
                           guardrails=Guardrails(judge=Judge()),
                           handover_writer=HandoverWriter(client_obj), settings=config)


def test_T_R2_2_the_queue_orders_tickets_that_came_through_the_api(tmp_path, retriever):
    """R2 (FR-05): urgency first, on rows the API itself wrote — nothing inserted by hand.

    Both tickets are `security_incident`, so both escalate by rule (FR-09) and the only thing
    separating them in the queue is the urgency the classifier produced. The older ticket is
    the urgent one, so an oldest-first queue and an urgency-first queue disagree.
    """
    pipeline = _real_pipeline(tmp_path, retriever,
                              {"Q-LOW": "low", "Q-HIGH": "high"})
    api = client(tmp_path, retriever, pipeline=pipeline)

    def submit(ticket_id, received):
        body = {"ticket_id": ticket_id, "channel": "email",
                "subject": "Former employee still has access",
                "body": "A developer left three weeks ago and we still see calls on their key.",
                "received_at": received, "customer_tier": "business",
                "customer_region": "europe", "language_fluency": "fluent"}
        response = api.post("/tickets", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    low = submit("Q-LOW", "2026-05-01T09:00:00Z")    # older, but not urgent
    high = submit("Q-HIGH", "2026-05-03T09:00:00Z")  # newer, and urgent

    assert low["decision"] == "escalate" and high["decision"] == "escalate"
    assert high["urgency"] == "high", "the POST response reports urgency alongside intent"
    assert high["urgency_confidence"] == pytest.approx(0.7)

    items = api.get("/queue").json()["items"]
    assert [i["ticket_id"] for i in items] == ["Q-HIGH", "Q-LOW"], (
        "an always-null urgency column would give oldest-first: Q-LOW, Q-HIGH")
    assert items[0]["urgency"] == "high"
    assert items[0]["urgency_confidence"] == pytest.approx(0.7)


# --- R10: the severe one, and the audit gaps around it -----------------------------------


def test_T_R10_1_two_concurrent_tickets_both_end_in_the_log(tmp_path, retriever):
    """R10 (SEVERE): two overlapping POSTs dropped a ticket entirely — 500, and **no row**.

    `submit` opened a per-request `DecisionLog` and then mutated the **shared** pipeline via
    `state.attach(log)`. `_record` reads `self._log` at write time, after a provider round trip
    (D-68: 4.1 s median), so the window is seconds wide and any two overlapping requests hit
    it. `DecisionLog` also opened sqlite without `check_same_thread=False`, so the second
    thread's write raised, `DecisionLogUnavailable` is deliberately re-raised (D-27), `submit`
    had no handler, and the terminal row was never written.

    That breaks three non-negotiables at once: "every ticket ends as a sent answer or a logged
    escalation", FR-13's reconciliation, and NFR-05's 100%. A demo with two browser tabs on
    `/docs` would have done it.
    """
    import threading

    from ticketing_agent.logging_store import DecisionLog

    pipeline = _real_pipeline(tmp_path, retriever, {"CC-1": "high", "CC-2": "high"})
    api = client(tmp_path, retriever, pipeline=pipeline)
    results: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def submit(ticket_id: str) -> None:
        body = {"ticket_id": ticket_id, "channel": "email",
                "subject": "Former employee still has access",
                "body": "A developer left three weeks ago and we still see calls on their key.",
                "received_at": "2026-05-01T09:00:00Z", "customer_tier": "business",
                "customer_region": "europe", "language_fluency": "fluent"}
        barrier.wait(timeout=10)
        results[ticket_id] = api.post("/tickets", json=body).status_code

    threads = [threading.Thread(target=submit, args=(t,)) for t in ("CC-1", "CC-2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert results == {"CC-1": 200, "CC-2": 200}, results
    with DecisionLog(settings(tmp_path).decision_log_path) as log:
        terminal = {r["ticket_id"] for r in log.terminal_rows()}
    assert terminal == {"CC-1", "CC-2"}, (
        "every ticket ends as a sent answer or a logged escalation — nothing is dropped")


def test_T_R10_2_a_malformed_ticket_is_refused_and_still_logged(tmp_path, retriever):
    """R10 (HIGH): the 400 was correct and the missing audit row was not.

    FR-07's criterion is "malformed test tickets are logged and escalated, not crashed", and
    the module docstring claims a submitted ticket "takes the same path as a harness run —
    there is no second, looser way in". Through the harness this ticket escalates with a row;
    through the API it got a 400 and `log.rows() == []`. A 400 is the right HTTP answer to a
    malformed body; having no trace of it is not.
    """
    from ticketing_agent.logging_store import DecisionLog

    response = client(tmp_path, retriever).post(
        "/tickets", json={"channel": "carrier-pigeon", "subject": "", "body": ""})
    assert response.status_code == 400
    assert "could not be read" in response.json()["detail"]

    with DecisionLog(settings(tmp_path).decision_log_path) as log:
        rows = log.rows()
    assert len(rows) == 1, "the refusal is a decision, and FR-13 logs every decision"
    row = rows[0]
    assert row["decision"] == "escalate"
    assert row["reason"] == "malformed_ticket"
    assert row["ingest_defects"], "and records what was wrong with it"
    assert row["explanation"], "in language a support manager could read"


def test_T_R10_3_a_pipeline_that_cannot_be_built_does_not_lose_the_ticket(tmp_path, retriever,
                                                                         monkeypatch):
    """R10 (HIGH): a lazy-build failure 500'd every ticket with no row, while /health said ok.

    This is exactly the state `docker compose up` leaves before `docker compose run --rm
    train`: a container Docker marks healthy, Prometheus showing zeroes, every ticket 500ing
    and nothing logged.
    """
    import ticketing_agent.api as module
    from ticketing_agent.logging_store import DecisionLog

    def refuse(*_args, **_kwargs):
        raise RuntimeError("the classifier at storage/classifier.joblib could not be loaded")

    monkeypatch.setattr(module, "_build_pipeline", refuse)
    app = module.build_app(settings(tmp_path), retriever=retriever)
    api = TestClient(app, raise_server_exceptions=False)

    body = {"ticket_id": "NB-1", "channel": "email", "subject": "Invoice",
            "body": "Where can I see the breakdown of my invoice?",
            "received_at": "2026-05-01T09:00:00Z"}
    response = api.post("/tickets", json=body)

    assert response.status_code == 503, response.status_code
    assert "classifier.joblib" not in response.text, "the path is operational detail (NFR-04)"
    with DecisionLog(settings(tmp_path).decision_log_path) as log:
        terminal = log.terminal_rows()
    assert [r["ticket_id"] for r in terminal] == ["NB-1"], "the ticket is not lost"
    assert terminal[0]["decision"] == "escalate"

    # The graph was never built, and /health says so without trying to build one.
    health = api.get("/health").json()
    assert health["pipeline"] == "not built"


def test_T_R10_3b_health_reports_a_missing_classifier_without_loading_anything(tmp_path,
                                                                             retriever):
    """R10 (HIGH) and R11: `ok: true` with no classifier, and the fix that broke the fix.

    `docker compose up` before `docker compose run --rm train` leaves exactly this state, and
    `/health` used to call it healthy. My first fix made it honest by *building the pipeline* as
    a probe — which loads the classifier, builds the Chroma index and downloads a 79MB embedding
    model, against a HEALTHCHECK the Dockerfile gives five seconds. Running the stack showed
    `/health` never answering at all.

    So it answers by looking rather than by loading: three `stat` calls, no imports, no index.
    """
    # No injected pipeline: this has to be the state a real cold container is in.
    api = TestClient(build_app(
        settings(tmp_path, classifier_path=tmp_path / "not-trained-yet.joblib"),
        retriever=retriever))
    health = api.get("/health").json()

    assert health["ok"] is False, "a container with no classifier cannot handle a ticket"
    assert any("classifier" in problem for problem in health["problems"]), health
    assert health["pipeline"] == "not built", "and nothing was built to find that out"

    # With the file present it is healthy again, still without building anything.
    (tmp_path / "not-trained-yet.joblib").write_bytes(b"not a real model")
    health = api.get("/health").json()
    assert health["ok"] is True, health
    assert health["problems"] == []
    assert health["pipeline"] == "not built", (
        "/health must not build the graph — that is what made it hang on a cold container")


def test_T_R10_1b_a_log_attached_to_one_pipeline_is_invisible_to_another(tmp_path, retriever):
    """R10 (SEVERE), the root cause, tested without a race.

    `attach_log` set `self._log` on an object the API shares across requests, and `_record`
    reads it at write time — after a provider round trip. The fix is a `ContextVar` keyed on the
    pipeline, and **the first version of the fix was wrong in the same way**: a bare module-level
    variable is visible to every instance, so a log attached to one pipeline could be written to
    by another. The suite caught it at once, which is why this is a test and not a comment.
    """
    from ticketing_agent.logging_store import DecisionLog
    from ticketing_agent.pipeline import SupportPipeline

    one = _real_pipeline(tmp_path / "a", retriever, {})
    two = _real_pipeline(tmp_path / "b", retriever, {})
    assert isinstance(one, SupportPipeline) and one is not two

    with DecisionLog(tmp_path / "one.db", run_id="one") as log:
        one.attach_log(log)
        assert one._log is log, "the pipeline it was attached to sees it"
        assert two._log is None, (
            "and no other pipeline does — a shared variable here is the bug, not the fix")

    # A pipeline that was never attached to sees nothing, however many others were.
    assert _real_pipeline(tmp_path / "c", retriever, {})._log is None


def test_T_R10_4_the_queue_serves_the_classifiers_reason_not_the_escalation_detail(
        tmp_path, retriever):
    """R10 (HIGH): D-62's fix could be reverted with one word and the suite stayed green.

    `T-R2-5` asserts the *row* carries `urgency_reason`; nothing read it off a `/queue` item,
    and `docs/specs/FR-08.md` claims this test exists. D-62 is explicit about why it matters:
    with a real urgency beside it, an agent reads
    `urgency: high · urgency_reason: "must_escalate_intent: intent security_incident"` as a
    confident, plausible, wrong answer to "why is this urgent?".
    """
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog

    with DecisionLog(settings(tmp_path).decision_log_path, run_id="api") as log:
        log.record(DecisionEntry(
            ticket_id="Q-1", stage="routing", decision="escalate",
            reason="must_escalate_intent", explanation="It goes to a person.",
            requirement_ids=["FR-05"], urgency="high", urgency_confidence=0.7,
            urgency_reason="closest to DEV-0011 (high, 0.72)",
            detail="must_escalate_intent: intent security_incident",
            summary="Q-1 needs a person.", received_at="2026-05-01T09:00:00Z", channel="email"))

    item = client(tmp_path, retriever).get("/queue").json()["items"][0]
    assert item["urgency_reason"] == "closest to DEV-0011 (high, 0.72)", (
        "the classifier's evidence, which is what FR-05's 'and the reason' asks for")
    assert item["detail"] == "must_escalate_intent: intent security_incident", (
        "the escalation detail is a different question and is served under its own name")
    assert item["urgency_reason"] != item["detail"]


def test_T_R10_5_the_queue_orders_before_it_truncates(tmp_path, retriever):
    """R10 (HIGH): `[:limit]` after ordering versus before it, and no test passed `limit`.

    Every queue fixture had 2–4 rows, below the default 50, so the two orderings were
    identical — the fixture trap this backlog has now hit six times. On a real log (the compose
    stack shares `decisions.db` with the `gate` service: 80+ escalations in one run) truncating
    first returns the oldest N re-sorted and **hides every urgent ticket beyond row N**, which
    is the same class of defect R2 found.
    """
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog

    # Twelve low-urgency tickets arrive first, then one high-urgency one.
    with DecisionLog(settings(tmp_path).decision_log_path, run_id="api") as log:
        for n in range(12):
            log.record(DecisionEntry(
                ticket_id=f"OLD-{n:02d}", stage="routing", decision="escalate",
                reason="low_confidence", explanation="To a person.",
                requirement_ids=["FR-05"], urgency="low", urgency_confidence=0.6,
                summary="old", received_at=f"2026-05-01T09:{n:02d}:00Z", channel="email"))
        log.record(DecisionEntry(
            ticket_id="URGENT", stage="routing", decision="escalate",
            reason="must_escalate_intent", explanation="To a person.",
            requirement_ids=["FR-05"], urgency="high", urgency_confidence=0.9,
            summary="urgent", received_at="2026-05-01T23:00:00Z", channel="email"))

    body = client(tmp_path, retriever).get("/queue", params={"limit": 3}).json()
    assert body["count"] == 3
    ids = [item["ticket_id"] for item in body["items"]]
    assert ids[0] == "URGENT", (
        "the newest and most urgent ticket must survive the truncation; truncating the log "
        "before ordering it would return OLD-00, OLD-01, OLD-02")
    assert "OLD-11" not in ids


def test_T_R10_6_every_exported_metric_is_asserted_somewhere(tmp_path, retriever):
    """R10 (HIGH): two metrics were exported and asserted by no test anywhere.

    Deleting the `ticketing_rows_by_stage_total` block from `api.py` left the suite green and
    panel 8 permanently empty; so did changing `ticketing_threshold{kind="relevance"}` to a
    constant. This asserts the *values*, not just the names, for every metric the dashboard
    depends on — the dashboard test above proves the names line up, and this proves they carry
    the figures they claim to.
    """
    from ticketing_agent.logging_store import DecisionEntry, DecisionLog

    with DecisionLog(settings(tmp_path).decision_log_path, run_id="api") as log:
        log.record(DecisionEntry(
            ticket_id="M-1", stage="routing", decision="auto_respond",
            explanation="Answered.", threshold_applied=0.85, requirement_ids=["FR-02"],
            model_calls=2, prompt_version="PR-01 v1.0"))
        log.record(DecisionEntry(
            ticket_id="M-2", stage="validation", decision="block",
            reason="ungrounded_draft", explanation="Blocked.", requirement_ids=["FR-12"],
            guardrail_results=[["grounding", False], ["private_data", True]]))
        log.record(DecisionEntry(
            ticket_id="M-2", stage="routing", decision="escalate",
            reason="ungrounded_draft", explanation="To a person.",
            all_reasons=["ungrounded_draft"], requirement_ids=["FR-12"]))

    scraped = client(tmp_path, retriever).get("/metrics/prometheus").text

    def value(line_start: str) -> float:
        line = next(ln for ln in scraped.splitlines() if ln.startswith(line_start))
        return float(line.rsplit(" ", 1)[1])

    assert value("ticketing_decisions_total ") == 2, "two terminal rows"
    assert value('ticketing_decisions_by_outcome_total{outcome="auto_respond"}') == 1
    assert value('ticketing_decisions_by_outcome_total{outcome="escalate"}') == 1
    assert value('ticketing_escalations_by_reason_total{reason="ungrounded_draft"}') == 1
    assert value('ticketing_guardrail_blocks_total{check="grounding"}') == 1
    # The two the review found asserted nowhere:
    assert value('ticketing_rows_by_stage_total{stage="routing"}') == 2
    assert value('ticketing_rows_by_stage_total{stage="validation"}') == 1
    assert value('ticketing_threshold{kind="relevance"}') == (
        settings(tmp_path).relevance_threshold)
    assert value('ticketing_threshold{kind="confidence"}') == (
        settings(tmp_path).confidence_threshold)
    assert value("ticketing_model_calls_total ") == 2
    assert value("ticketing_kill_switch ") == 0


def test_T_R10_7_search_honours_its_own_bounds_and_reports_real_scores(tmp_path, retriever):
    """R10 (MEDIUM): three FR-04 output guarantees had no test, and "ranked" was vacuous.

    `assert ranks == sorted(ranks)` is satisfied by every rank being 0, and the score check was
    only `0.0 <= score <= 1.0` — so an agent could be served constant scores and ranks with the
    suite green. `min_score` (FR-04 §3 rule 3) was exercised by nothing at all.
    """
    api = client(tmp_path, retriever)
    body = api.get("/search", params={"q": "invoice breakdown", "k": 5}).json()
    assert len(body["results"]) >= 2, "this query needs to return several hits to compare them"

    scores = [hit["score"] for hit in body["results"]]
    ranks = [hit["rank"] for hit in body["results"]]
    assert ranks == list(range(1, len(ranks) + 1)), (
        f"ranks are 1..n in order, not a constant: {ranks}")
    assert scores == sorted(scores, reverse=True), f"and scores descend: {scores}"
    assert len(set(scores)) > 1, (
        "a fabricated constant score would pass every other assertion in this file")

    # FR-04 §3 rule 3: min_score actually filters. The floor sits between the weakest and the
    # strongest hit, not *on* a reported score: the response rounds to 4 dp while the retriever
    # filters on the raw value, so a floor equal to the top reported score can exclude its own
    # hit. That rounding is the reason this reads the way it does rather than a neater way.
    floor = scores[-1] + (scores[0] - scores[-1]) / 2
    filtered = api.get("/search",
                       params={"q": "invoice breakdown", "k": 5, "min_score": floor}).json()
    assert filtered["results"], "the strongest hits clear a floor below them"
    assert all(hit["score"] >= floor - 1e-4 for hit in filtered["results"]), filtered
    assert len(filtered["results"]) < len(body["results"]), (
        "min_score is passed to the retriever, not ignored")


def test_T_R11_3_the_first_ticket_does_not_deadlock_on_the_lazy_build_lock(tmp_path, retriever):
    """R11, found by running a real container: `/health` fine, POST never returning.

    R10 put one lock over both lazy builds, to stop two first requests each building the Chroma
    index against the same path. But `pipeline` takes the lock and then reads `self.retriever`,
    which takes it again — and `threading.Lock` is not reentrant, so the first ticket blocked for
    ever with no error, no traceback and no log line. The container looked healthy throughout,
    because `/health` deliberately takes no lock.

    This builds both lazily, through the real `_State`, with a timeout: a deadlock fails it
    instead of hanging the suite.
    """
    import threading

    import ticketing_agent.api as module

    built: list[str] = []

    def fake_build(settings_arg, retriever_arg):
        built.append("pipeline")
        return FakePipeline()

    state = module._State(settings(tmp_path), retriever, None)
    original = module._build_pipeline
    module._build_pipeline = fake_build
    try:
        done = threading.Event()

        def resolve():
            # The nesting that deadlocked: `pipeline` -> lock -> `retriever` -> lock.
            state.pipeline  # noqa: B018
            done.set()

        worker = threading.Thread(target=resolve, daemon=True)
        worker.start()
        assert done.wait(timeout=30), (
            "the first ticket deadlocked on the lazy-build lock: `pipeline` holds it and "
            "`retriever` wants it, so the lock has to be reentrant")
    finally:
        module._build_pipeline = original

    assert built == ["pipeline"], built
    assert state.pipeline_state == "ready"


def test_T_R11_4_concurrent_first_requests_build_the_index_once(tmp_path, retriever):
    """And the property the lock exists for, which the RLock must not give up."""
    import threading

    import ticketing_agent.api as module

    calls: list[int] = []

    def counting_build(settings_arg, retriever_arg):
        calls.append(1)
        return FakePipeline()

    state = module._State(settings(tmp_path), retriever, None)
    original = module._build_pipeline
    module._build_pipeline = counting_build
    try:
        barrier = threading.Barrier(4)

        def resolve():
            barrier.wait(timeout=10)
            state.pipeline  # noqa: B018

        threads = [threading.Thread(target=resolve, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive(), "a build deadlocked or never finished"
    finally:
        module._build_pipeline = original

    assert len(calls) == 1, f"four concurrent first requests built the pipeline {len(calls)} times"
