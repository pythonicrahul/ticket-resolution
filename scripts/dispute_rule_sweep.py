"""FR-03 §3.1b, FR-09 §3.7 (review row R7): what the two new escalation rules cost, measured.

    uv run python scripts/dispute_rule_sweep.py --input data/development_tickets.json
    uv run python scripts/dispute_rule_sweep.py --input data/validation_tickets.json --ids

R7 asked for this **reported, not asserted**. Two reasons it is a script rather than a test:

* The answer is a judgement for the author. Both rules escalate tickets the supplied labels call
  answerable, and whether that is the rule being wrong or the label being wrong is not something
  a test can decide.
* On the validation set the labels contradict themselves on exactly these tickets (D-70): seven
  byte-identical bodies carry `expected_route: escalate` once and `auto_respond` six times. A
  test asserting agreement with those labels would be asserting a contradiction.

Paths are arguments and no file name is hardcoded (CLAUDE.md). The rule tables are imported from
`route.py`, never copied here, so this measures the rules that run rather than a second copy of
them — the mistake D-18 records.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ticketing_agent.ingest import TicketFileError, evaluation_labels, load_tickets
from ticketing_agent.route import (
    ACCOUNT_SPECIFIC_TRIGGERS,
    COMPLIANCE_TRIGGERS,
    DATA_RESIDENCY_INTENT,
    DISPUTE_TRIGGERS,
    MONEY_TRIGGERS,
    matches_triggers,
)


def sweep(path: Path) -> dict[str, Any]:
    """Which tickets each new rule would escalate, and what the labels expected of them.

    The data-residency rule is scoped to the **predicted** intent at runtime; this sweep has no
    classifier, so it uses the labelled intent as a stand-in and says so. That makes the figure
    an estimate of the rule's reach, not of a run's outcome.
    """
    tickets = load_tickets(path)
    labelled = sum(1 for t in tickets if evaluation_labels(t))
    out: dict[str, Any] = {"input": str(path), "tickets": len(tickets),
                           "labelled": labelled, "rules": {}}

    for name, matcher in (
        ("money_decision_required", lambda t, _l: matches_triggers(t.text, DISPUTE_TRIGGERS)),
        ("compliance_data_question", lambda t, labels: (
            (matches_triggers(t.text, ACCOUNT_SPECIFIC_TRIGGERS)
             + matches_triggers(t.text, COMPLIANCE_TRIGGERS))
            if labels.get("intent") == DATA_RESIDENCY_INTENT else [])),
    ):
        rows = []
        for ticket in tickets:
            labels = evaluation_labels(ticket)
            matched = matcher(ticket, labels)
            if matched:
                rows.append({
                    "ticket_id": ticket.ticket_id,
                    "expected_route": labels.get("expected_route"),
                    "answerable_from_docs": labels.get("answerable_from_docs"),
                    "triggers": matched[:3],
                    # Only meaningful for the dispute rule: does the existing FR-03 money rule
                    # already escalate this ticket, so the new rule changes nothing for it?
                    # Money triggers say nothing about a residency ticket, so the field is
                    # omitted there rather than printed as a misleading zero.
                    "already_caught_by_the_money_rule": (
                        bool(matches_triggers(ticket.text, MONEY_TRIGGERS))
                        if name == "money_decision_required" else None),
                })
        newly = [r for r in rows if r["expected_route"] == "auto_respond"]
        agreed = [r for r in rows if r["expected_route"] == "escalate"]
        unlabelled = [r for r in rows if r["expected_route"] is None]
        out["rules"][name] = {
            "matched": len(rows),
            "already_caught_by_the_money_rule": (
                sum(1 for r in rows if r["already_caught_by_the_money_rule"])
                if name == "money_decision_required" else None),
            "labels_absent": len(unlabelled),
            "labels_agree_escalate": len(agreed),
            "labels_say_answer": len(newly),
            "tickets": rows,
        }
    return out


def report(result: dict[str, Any], show_ids: bool) -> None:
    print(f"\n{result['input']} — {result['tickets']} tickets")
    print("(the data-residency rule is scoped to the predicted intent at runtime; this sweep "
          "uses the labelled intent as a stand-in, so it measures the rule's reach)")
    if all(b["matched"] == 0 for b in result["rules"].values()) and result["labelled"] == 0:
        print("NOTE: this file carries no labels, so the residency rule had no intent to scope "
              "with and could not match anything here. Its reach on an unlabelled file is not "
              "measurable by this script.")
    for name, block in result["rules"].items():
        print(f"\n  {name}")
        print(f"    matched                        {block['matched']}")
        print(f"    labels already expected escalate  {block['labels_agree_escalate']}")
        print(f"    labels expected an answer         {block['labels_say_answer']}")
        if block["already_caught_by_the_money_rule"] is not None:
            print(f"    already escalating on the money rule  "
                  f"{block['already_caught_by_the_money_rule']}")
        if block["labels_absent"]:
            print(f"    no label to compare against        {block['labels_absent']}")
        if show_ids:
            for row in block["tickets"]:
                mark = "NEW " if row["expected_route"] == "auto_respond" else "    "
                print(f"      {mark}{row['ticket_id']}  label={row['expected_route']}  "
                      f"answerable={row['answerable_from_docs']}  via {row['triggers']}")


def main(argv: Sequence[str] | None = None) -> int:
    """Returns the process exit code. Reports; never asserts and never tunes."""
    parser = argparse.ArgumentParser(
        prog="python scripts/dispute_rule_sweep.py",
        description="Report what FR-03 §3.1b and FR-09 §3.7 cost on any ticket file (R7).")
    parser.add_argument("--input", required=True, help="the ticket file to sweep (any path)")
    parser.add_argument("--ids", action="store_true", help="list every matched ticket")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="write the whole result as JSON to stdout instead")
    args = parser.parse_args(argv)

    try:
        result = sweep(Path(args.input))
    except (TicketFileError, OSError) as exc:
        print(f"cannot read {args.input}: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        report(result, show_ids=args.ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
