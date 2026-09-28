"""Build Specification §06: the demonstration, as a script so it is the same every time.

    uv run python scripts/demo_walkthrough.py

Five things, in the order the assessment asks for them:

1. **one ticket per channel** — email, chat, forum and docs_comment through the whole graph;
2. **a guardrail block** — an engineered draft that leaks an address, refused;
3. **an escalation with its handover** — what a tier-two engineer actually receives;
4. **the kill switch** — on, one ticket, off again;
5. **where the evidence is** — the decision log rows for everything above.

It uses the response cache, so a second run costs nothing. Nothing here is a test: the tests are
`uv run pytest`. This is what a person watches.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ticketing_agent.classify import IntentClassifier, TrainedClassifier
from ticketing_agent.config import Settings, load_settings
from ticketing_agent.generate import Drafter
from ticketing_agent.guardrails import GroundingJudge, Guardrails
from ticketing_agent.handover import HandoverWriter
from ticketing_agent.ingest import load_tickets
from ticketing_agent.logging_store import DecisionLog
from ticketing_agent.pipeline import SupportPipeline
from ticketing_agent.provider import ProviderClient
from ticketing_agent.retrieve import Retriever
from ticketing_agent.route import Router

ROOT = Path(__file__).resolve().parents[1]
RULE = "─" * 78


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=None, help="defaults to TRAINING_TICKETS_PATH")
    parser.add_argument("--log", default=str(ROOT / "storage" / "demo.db"))
    args = parser.parse_args()

    settings = load_settings()
    tickets = load_tickets(Path(args.input) if args.input
                           else settings.require_path("training_tickets_path"))
    pipeline, client = _build(settings)

    with DecisionLog(Path(args.log), run_id="demo") as log:
        pipeline.attach_log(log)
        _one_per_channel(pipeline, tickets)
        _a_guardrail_block(settings, client)
        _an_escalation(pipeline, tickets)
        _the_kill_switch(settings, pipeline, tickets)
        _the_evidence(log)
    return 0


def _build(settings: Settings) -> tuple[SupportPipeline, ProviderClient]:
    retriever = Retriever(settings)
    retriever.build_index(settings.require_path("docs_path"))
    client = ProviderClient(settings)
    return SupportPipeline(
        retriever=retriever,
        classifier=IntentClassifier(TrainedClassifier.load(settings.classifier_path)),
        router=Router(settings), drafter=Drafter(client),
        guardrails=Guardrails(judge=GroundingJudge(client)),
        handover_writer=HandoverWriter(client), settings=settings), client


def _one_per_channel(pipeline: SupportPipeline, tickets: list) -> None:
    print(f"\n{RULE}\n1. ONE TICKET PER CHANNEL\n{RULE}")
    seen: set[str] = set()
    for ticket in tickets:
        if ticket.channel in seen or ticket.is_malformed:
            continue
        seen.add(ticket.channel)
        outcome = pipeline.process(ticket)
        print(f"\n[{ticket.channel}] {ticket.ticket_id}: {ticket.subject[:60] or '(no subject)'}")
        print(f"  decision : {outcome.decision}"
              + (f" ({outcome.reason})" if outcome.reason else ""))
        print(f"  intent   : {outcome.prediction_value} at {outcome.prediction_confidence:.2f}"
              f" against threshold {outcome.threshold_applied}")
        print(f"  sources  : {[p.chunk_id for p in outcome.passages][:3]}")
        if outcome.draft:
            print("  reply    : " + outcome.draft.splitlines()[0][:100])
        else:
            print(f"  handover : {(outcome.summary or '')[:100]}")
        if len(seen) == 4:
            break


def _a_guardrail_block(settings: Settings, client: ProviderClient) -> None:
    print(f"\n{RULE}\n2. A GUARDRAIL BLOCK (the reply is refused, never redacted)\n{RULE}")
    fixture = next(
        d for d in json.loads((ROOT / "tests" / "fixtures" / "draft_replies.json")
                              .read_text(encoding="utf-8"))
        if d["synthetic"]["category"] == "private_data_in_draft")
    from ticketing_agent.retrieve import Passage

    passages = tuple(Passage(chunk_id=r["chunk_id"], doc_id=r["doc_id"], title=r.get("title", ""),
                             heading=r.get("heading", ""), text=r["text"], score=0.5, rank=n + 1)
                     for n, r in enumerate(fixture["retrieved"]))
    report = Guardrails(judge=GroundingJudge(client)).check_draft(
        ticket=None, reply=fixture["text"],
        sentences=((fixture["text"], tuple(fixture["citations"])),), retrieved=passages,
        citations=tuple(fixture["citations"]), confidence=0.95, threshold_applied=0.85)

    print(f"  draft    : {fixture['text'][:90]}…")
    print(f"  passed   : {report.passed}   reason: {report.reason}")
    for result in report.results:
        print(f"    {'pass' if result.passed else 'FAIL'}  {result.name:22s} {result.detail[:70]}")
    print("  note     : the detail names the pattern, never the value (NFR-04).")


def _an_escalation(pipeline: SupportPipeline, tickets: list) -> None:
    print(f"\n{RULE}\n3. AN ESCALATION, AND WHAT THE ENGINEER RECEIVES\n{RULE}")
    for ticket in tickets:
        outcome = pipeline.process(ticket)
        if outcome.decision != "escalate" or not outcome.summary:
            continue
        print(f"  ticket      : {ticket.ticket_id} [{ticket.channel}] {ticket.subject[:50]}")
        print(f"  reason      : {outcome.reason}")
        print(f"  explanation : {outcome.explanation}")
        print(f"  summary     : {outcome.summary}")
        print(f"  uncertainty : {outcome.uncertainty}")
        print(f"  articles    : {[p.doc_id for p in outcome.passages][:3]}")
        return
    print("  (no escalation in this sample)")


def _the_kill_switch(settings: Settings, pipeline: SupportPipeline, tickets: list) -> None:
    print(f"\n{RULE}\n4. THE KILL SWITCH\n{RULE}")
    answerable = next(t for t in tickets if not t.is_malformed)
    switch = Path(settings.kill_switch_file)
    before = pipeline.process(answerable)
    switch.parent.mkdir(parents=True, exist_ok=True)
    switch.write_text("", encoding="utf-8")
    try:
        during = pipeline.process(answerable)
    finally:
        switch.unlink(missing_ok=True)
    after = pipeline.process(answerable)

    print(f"  switch off : {before.decision} ({before.reason or 'answered'})")
    print(f"  switch on  : {during.decision} ({during.reason})  <- touch {switch}")
    print(f"  switch off : {after.decision} ({after.reason or 'answered'})")
    print("  note       : no redeploy, and it takes effect on the next ticket (FR-16).")


def _the_evidence(log: DecisionLog) -> None:
    print(f"\n{RULE}\n5. THE EVIDENCE: every decision above, logged before it was acted on\n{RULE}")
    rows = log.rows()
    print(f"  {len(rows)} rows written to {log.path}")
    for row in rows[-8:]:
        print(f"    {row['ticket_id']:10s} {row['stage']:11s} {row['decision']:12s} "
              f"{str(row['reason'])[:28]:28s} {row['prompt_version'] or '-'!s:11s}")
    print("\n  Each row carries the requirement ids it served, the threshold applied, the "
          "sources used\n  and the guardrail results — the Governance Framework's minimum "
          "record (FR-13).")


if __name__ == "__main__":
    raise SystemExit(main())
