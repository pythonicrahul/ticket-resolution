"""NFR-05: write `ops/metrics_reference.txt` from the endpoint itself.

    uv run python scripts/write_metrics_reference.py

The dashboard is checked against this file by a test, and this file is generated from the live
`/metrics/prometheus` response — so a panel can never query a metric the exporter does not
export. Writing the reference by hand would have been one more thing to drift.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from ticketing_agent.api import build_app
from ticketing_agent.config import Settings
from ticketing_agent.logging_store import DecisionEntry, DecisionLog

ROOT = Path(__file__).resolve().parents[1]
HEADER = (
    "# The metrics /metrics/prometheus exports (NFR-05). Generated from the endpoint itself by\n"
    "# scripts/write_metrics_reference.py, so the dashboard and the exporter cannot disagree.\n"
    "# Sample values below come from one synthetic row and mean nothing.\n\n"
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    settings = Settings(model_name="not-used", docs_path=ROOT / "data" / "documentation.json",
                        chroma_path=tmp / "chroma", decision_log_path=tmp / "decisions.db",
                        kill_switch_file=tmp / "absent", confidence_threshold=0.85,
                        relevance_threshold=0.25)
    # One row of each shape, so every metric appears with at least one label value.
    with DecisionLog(settings.decision_log_path, run_id="sample") as log:
        log.record(DecisionEntry(ticket_id="S-1", stage="routing", decision="escalate",
                                 reason="no_retrieval", explanation="A person takes it.",
                                 requirement_ids=["FR-10"], model_calls=1,
                                 prompt_version="PR-01 v1.0",
                                 guardrail_results=[["grounding", False]]))
        log.record(DecisionEntry(ticket_id="S-2", stage="routing", decision="auto_respond",
                                 explanation="Answered.", threshold_applied=0.85,
                                 requirement_ids=["FR-02"]))

    client = TestClient(build_app(settings, retriever=_EmptyIndex(), pipeline=None))
    out = ROOT / "ops" / "metrics_reference.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(HEADER + client.get("/metrics/prometheus").text, encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}")
    return 0


class _EmptyIndex:
    """No documents needed: the scrape reads the decision log, not the corpus."""

    chunks: tuple = ()


if __name__ == "__main__":
    raise SystemExit(main())
