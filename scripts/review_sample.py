"""NFR-03, FR-14, review row R3: the two-assessor review sheet.

    uv run python scripts/review_sample.py --input evaluation/results/<run>/outcomes.jsonl \
        --n 50 --seed 1 --output evaluation/reports/review_sample.csv

The Evaluation Framework's tier two asks for hallucination rate and citation accuracy to be
measured by **human review of at least 50 responses by two assessors, with an agreement rate**.
The harness cannot produce those numbers; it counts unresolvable citations, which is a floor and
says so. This script produces the sheet the two people actually fill in.

Three properties the tests hold in place:

* **Deterministic for a seed.** Both assessors must review the *same* replies or the agreement
  rate means nothing, and they run this on different machines. `random.Random(seed)` over the
  lines sorted by ticket id, never over a dict or a directory listing.
* **The passages travel with the reply.** An assessor cannot judge "supported" against a list of
  chunk ids, so the cited passages' text is in the sheet.
* **Asking for more than exists is not an error.** A smoke run has six answered tickets; the
  sheet then has six rows and the summary line says so, rather than the script failing at the
  end of a run that took five minutes.

Paths are arguments. No file name is hardcoded (CLAUDE.md).
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: What the assessors fill in. Written empty, one pair per assessor, so the sheet can be opened
#: in a spreadsheet and handed to two people without further preparation.
ASSESSOR_COLUMNS = ("assessor_1_supported", "assessor_1_notes",
                    "assessor_2_supported", "assessor_2_notes")

COLUMNS = ("ticket_id", "intent", "intent_confidence", "urgency", "citations",
           "reply", "cited_passages", *ASSESSOR_COLUMNS)


def load_answered(path: Path) -> list[dict[str, Any]]:
    """The auto-answered lines of an `outcomes.jsonl`, sorted by ticket id.

    Sorted before sampling, not after: the file's order is the input file's order, and sampling
    from it would make the sheet depend on how the tickets happened to be arranged.
    """
    lines: list[dict[str, Any]] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{number} is not JSON: {exc}") from exc
        if not isinstance(entry, dict):
            # A one-line JSON array parses fine and then blows up on `.get`. The likely slip is
            # pointing --input at a run's `*.json` or at a tickets file instead of outcomes.jsonl.
            raise TypeError(
                f"{path}:{number} is not a JSON object: this is not an outcomes.jsonl")
        if entry.get("decision") == "auto_respond" and entry.get("reply"):
            lines.append(entry)
    # `(ticket_id, source_index)`, not `ticket_id` alone: duplicate ticket ids are legal in an
    # input file (D-12), and a stable sort would leave those tied on the file's own order —
    # the exact dependency this sort exists to remove.
    return sorted(lines, key=lambda e: (str(e.get("ticket_id", "")),
                                        _as_int(e.get("source_index"))))


def _as_int(value: Any) -> int:
    """A missing or unusable `source_index` sorts first rather than raising."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def sample(entries: Sequence[dict[str, Any]], n: int, seed: int) -> list[dict[str, Any]]:
    """`n` of them, or all of them when there are fewer, in a fixed order for a given seed.

    Indices are drawn, not objects, and the draw is `Random(seed).shuffle` over `range(len)`
    rather than `Random.sample`: the Mersenne Twister *stream* is a documented cross-version
    guarantee, while how `sample` consumes it is an implementation detail. The two assessors
    run this on different machines and must get the same replies.
    """
    order = list(range(len(entries)))
    random.Random(seed).shuffle(order)
    chosen = [entries[i] for i in sorted(order[:n])]
    return chosen


def _passages(entry: dict[str, Any]) -> str:
    """The cited passages as text an assessor can read in one cell."""
    return "\n\n".join(
        f"[{p.get('chunk_id')}] {p.get('title')} — {p.get('heading')}\n{p.get('text')}"
        for p in entry.get("cited_passages") or ())


def write_sheet(entries: Sequence[dict[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for entry in entries:
            writer.writerow({
                "ticket_id": entry.get("ticket_id", ""),
                "intent": entry.get("intent") or "",
                "intent_confidence": entry.get("intent_confidence") or "",
                "urgency": entry.get("urgency") or "",
                "citations": ", ".join(entry.get("citations") or ()),
                "reply": entry.get("reply") or "",
                "cited_passages": _passages(entry),
                **{column: "" for column in ASSESSOR_COLUMNS},
            })


def main(argv: Sequence[str] | None = None) -> int:
    """Returns the process exit code. Never raises on a bad path or a bad file."""
    parser = argparse.ArgumentParser(
        prog="python scripts/review_sample.py",
        description="Write the two-assessor review sheet from a run's outcomes.jsonl (NFR-03).")
    parser.add_argument("--input", required=True, help="an outcomes.jsonl from a harness run")
    parser.add_argument("--n", type=int, default=50, help="how many replies to sample")
    parser.add_argument("--seed", type=int, default=1, help="fixed, so both assessors match")
    parser.add_argument("--output", required=True, help="the CSV to write")
    args = parser.parse_args(argv)

    if args.n < 1:
        print("--n must be at least 1", file=sys.stderr)
        return 2
    source = Path(args.input)
    try:
        answered = load_answered(source)
    except (OSError, TypeError, ValueError) as exc:
        print(f"cannot read {source}: {exc}", file=sys.stderr)
        return 2

    chosen = sample(answered, args.n, args.seed)
    try:
        write_sheet(chosen, Path(args.output))
    except OSError as exc:
        print(f"cannot write {args.output}: {exc}", file=sys.stderr)
        return 2

    if not answered:
        print(f"this run answered nothing, so there is nothing to review; "
              f"an empty sheet is in {args.output}", file=sys.stderr)
        return 0
    shortfall = ("" if len(chosen) >= args.n else
                 f" — fewer than the {args.n} asked for, which is all the run answered")
    print(f"{len(chosen)} replies for review from {len(answered)} answered{shortfall}; "
          f"sheet in {args.output}")
    if len(chosen) < 50:
        print("note: the Evaluation Framework asks for at least 50 responses by two assessors.",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
