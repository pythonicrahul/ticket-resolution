"""Smoke tests: the repo's data is present and has the documented schema. No network needed."""
import json
from pathlib import Path

DATA = Path(__file__).resolve().parents[1] / "data"
TICKET_KEYS = {"ticket_id", "channel", "subject", "body", "received_at", "customer_id",
               "customer_tier", "customer_region", "language_fluency", "labels", "history"}


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def test_package_imports():
    import ticketing_agent  # noqa: F401


def test_development_tickets_schema():
    tickets = load("development_tickets.json")
    assert len(tickets) == 500
    assert all(TICKET_KEYS <= t.keys() for t in tickets)
    assert {t["channel"] for t in tickets} == {"email", "chat", "docs_comment", "forum"}


def test_documentation_corpus():
    docs = load("documentation.json")
    assert len(docs) == 29
    ids = {d["doc_id"] for d in docs}
    cited = {i for t in load("development_tickets.json") for i in t["labels"]["expected_doc_ids"]}
    assert cited <= ids
