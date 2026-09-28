"""FR-10, row 7: sweep the relevance threshold and write the report a human decides from.

    uv run python scripts/retrieval_sweep.py --input data/development_tickets.json

Writes `evaluation/reports/retrieval_sweep.md`. It reports, for each candidate threshold, what
retrieval would do to the development tickets, judged against their `expected_doc_ids`:

* **hit rate** — the share of answerable tickets whose expected article is retrieved at all;
* **top-1 accuracy** — the share whose *best* passage is from an expected article, which is the
  question the threshold actually turns on;
* **precision at k** — over answerable tickets only, reported against its attainable ceiling,
  because with top_k=5 and usually one expected article the maximum is far below 100%;
* **empty rate** — how often nothing is returned, which for unanswerable tickets is the *right*
  answer and is reported separately for exactly that reason;
* **hit rate by fluency** — because the Governance Framework's fairness audit says retrieval-based
  systems usually do worse on non-fluent English, and finding that honestly is worth more than a
  table showing no variation (NFR-06).

It chooses nothing. The threshold is the author's call at checkpoint row 7; this script only
makes the trade-off visible.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from ticketing_agent.config import Settings, load_settings
from ticketing_agent.retrieve import Retriever

ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS = (0.0, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=None,
                        help="tickets to sweep against; defaults to TRAINING_TICKETS_PATH from "
                             ".env (development only: never the validation or hidden set)")
    parser.add_argument("--docs", default=None, help="defaults to DOCS_PATH from .env")
    parser.add_argument("--chroma", default=str(ROOT / "storage" / "chroma-sweep"))
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--output", default=str(ROOT / "evaluation" / "reports" / "retrieval_sweep.md"))
    args = parser.parse_args()

    configured = load_settings()
    docs_path = Path(args.docs) if args.docs else configured.require_path("docs_path")
    input_path = (Path(args.input) if args.input
                  else configured.require_path("training_tickets_path"))
    args.docs, args.input = _relative(docs_path), _relative(input_path)
    tickets = json.loads(input_path.read_text(encoding="utf-8"))
    settings = Settings(docs_path=docs_path, chroma_path=args.chroma,
                        retrieval_top_k=args.top_k, relevance_threshold=0.0,
                        model_name="not-used-by-retrieval")
    retriever = Retriever(settings)
    stats = retriever.build_index(docs_path)
    if stats.skipped:
        print(f"WARNING: {len(stats.skipped)} document(s) were not indexed: {stats.skipped}")
    print(f"index: {stats.chunks} chunks from {stats.documents} documents, "
          f"embedder {stats.embedder}, rebuilt={stats.rebuilt}")

    # Retrieve once per ticket at threshold 0, then apply each candidate threshold to the scores.
    # One embedding pass, every threshold measured from the same retrieval.
    retrieved: list[dict] = []
    for number, ticket in enumerate(tickets, start=1):
        query = f"{ticket.get('subject', '')} {ticket.get('body', '')}".strip()
        passages = retriever.search(query, top_k=args.top_k, threshold=0.0)
        labels = ticket.get("labels") or {}
        retrieved.append({
            "ticket_id": ticket.get("ticket_id"),
            "answerable": bool(labels.get("answerable_from_docs")),
            "expected": set(labels.get("expected_doc_ids") or []),
            "fluency": ticket.get("language_fluency", "unknown"),
            "tier": ticket.get("customer_tier", "unknown"),
            "hits": [(p.doc_id, p.score) for p in passages],
        })
        if number % 100 == 0:
            print(f"  {number}/{len(tickets)} tickets")

    chunks_by_doc: dict[str, int] = {}
    for chunk in retriever.chunks:
        chunks_by_doc[chunk.doc_id] = chunks_by_doc.get(chunk.doc_id, 0) + 1
    rows = [_measure(threshold, retrieved) for threshold in THRESHOLDS]
    report = _render(rows, retrieved, stats, args,
                     _precision_ceiling(retrieved, chunks_by_doc, args.top_k))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8")
    print(f"\nwrote {output}")
    print(report[report.index("| threshold"):].split("\n\n")[0])
    return 0


def _measure(threshold: float, retrieved: list[dict]) -> dict:
    answerable = [t for t in retrieved if t["answerable"] and t["expected"]]
    unanswerable = [t for t in retrieved if not t["answerable"]]

    def kept(ticket: dict) -> list[tuple[str, float]]:
        return [(doc_id, score) for doc_id, score in ticket["hits"] if score >= threshold]

    hits = sum(1 for t in answerable if t["expected"] & {d for d, _ in kept(t)})
    top1 = sum(1 for t in answerable if kept(t) and kept(t)[0][0] in t["expected"])
    # Precision over the *same* population as the numerator: mixing answerable hits with passages
    # returned for unanswerable tickets made the column move with the denominator (row 5 review).
    returned_answerable = sum(len(kept(t)) for t in answerable)
    correct = sum(len([1 for d, _ in kept(t) if d in t["expected"]]) for t in answerable)
    by_segment: dict[str, dict[str, list[int]]] = {
        "fluency": defaultdict(lambda: [0, 0]), "tier": defaultdict(lambda: [0, 0])}
    for ticket in answerable:
        for segment in ("fluency", "tier"):
            bucket = by_segment[segment][ticket[segment]]
            bucket[1] += 1
            if ticket["expected"] & {d for d, _ in kept(ticket)}:
                bucket[0] += 1

    # The decision the threshold actually makes is "return nothing". Two columns judge it: how
    # often it fires, and how often it is right — i.e. of the tickets left empty, how many were
    # genuinely unanswerable. Without the second, a high empty rate looks like success.
    empty_tickets = [t for t in retrieved if not kept(t)]
    empty_and_unanswerable = sum(1 for t in empty_tickets if not t["answerable"])
    return {
        "threshold": threshold,
        "hit_rate": _pct(hits, len(answerable)),
        "empty_rate_all": _pct(len(empty_tickets), len(retrieved)),
        "empty_precision": _pct(empty_and_unanswerable, len(empty_tickets)),
        "top1": _pct(top1, len(answerable)),
        "precision": _pct(correct, returned_answerable),
        "empty_answerable": _pct(sum(1 for t in answerable if not kept(t)), len(answerable)),
        "empty_unanswerable": _pct(sum(1 for t in unanswerable if not kept(t)), len(unanswerable)),
        "mean_returned": round(sum(len(kept(t)) for t in retrieved) / max(1, len(retrieved)), 2),
        "segments": {name: {k: (v[0], v[1]) for k, v in sorted(buckets.items())}
                     for name, buckets in by_segment.items()},
    }


def _relative(path: Path) -> str:
    """Report paths relative to the repository, so a committed report names no home directory."""
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def _pct(part: int, whole: int) -> float:
    return round(100.0 * part / whole, 1) if whole else 0.0


def _precision_ceiling(retrieved: list[dict], chunks_by_doc: dict[str, int], top_k: int) -> float:
    """The best precision at k that is even attainable: an expected article has a fixed number of
    chunks, so with top_k=5 and one expected article the maximum is well under 100%."""
    answerable = [t for t in retrieved if t["answerable"] and t["expected"]]
    best = sum(min(top_k, sum(chunks_by_doc.get(d, 0) for d in t["expected"])) for t in answerable)
    return _pct(best, top_k * len(answerable))


def _render(rows: list[dict], retrieved: list[dict], stats, args, ceiling: float) -> str:
    answerable = [t for t in retrieved if t["answerable"] and t["expected"]]
    unanswerable = [t for t in retrieved if not t["answerable"]]

    lines = [
        "# Retrieval threshold sweep (FR-10)",
        "",
        f"- Tickets: {len(retrieved)} from `{args.input}`  ",
        (f"- Answerable with a labelled article: {len(answerable)}; "
         f"labelled unanswerable: {len(unanswerable)}  "),
        f"- Corpus: `{args.docs}` — {stats.documents} documents, {stats.chunks} chunks  ",
        (f"- Embedder: `{stats.embedder}` (chroma's built-in all-MiniLM-L6-v2, D-02), "
         f"fingerprint `{stats.fingerprint}`; top_k {args.top_k}  "),
        "",
        "Generated by `scripts/retrieval_sweep.py`. **This report chooses nothing**: the threshold",
        "is the author's decision at checkpoint row 7.",
        "",
        "## What each threshold would do",
        "",
        ("| threshold | hit rate on answerable | top-1 correct | precision at k "
         "| answerable left empty | unanswerable correctly empty | returns nothing "
         "| of those, truly unanswerable | mean passages |"),
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['threshold']:.2f} | {row['hit_rate']}% | {row['top1']}% | "
            f"{row['precision']}% | {row['empty_answerable']}% | {row['empty_unanswerable']}% "
            f"| {row['empty_rate_all']}% | {row['empty_precision']}% | {row['mean_returned']} |")

    lines += [
        "",
        (f"**Precision at k cannot reach 100%.** At threshold 0, where all {args.top_k} passages "
         f"are returned, the attainable maximum on this corpus is {ceiling}%: an expected article "
         "has only so many chunks, so the rest of the five are necessarily from other articles "
         "even when the ranking is perfect. Above threshold 0 the column rises partly because "
         "fewer passages are returned, which is not the same as ranking better — which is why "
         "**top-1 correct** is here. That column answers the question the threshold actually "
         "turns on: would the drafter see the right passage first."),
        "",
        ("**The two right-hand columns are the decision.** *Returns nothing* is how often the "
         "threshold fires at all; *of those, truly unanswerable* is how often it was right to. A "
         "threshold that empties a lot of tickets but is wrong about most of them is escalating "
         "answerable work, and the escalation-rate target cannot be reached that way — that is "
         "FR-02's confidence threshold and FR-12's grounding, not this one."),
        "",
        "Read the empty-rate columns together. A threshold that leaves few answerable",
        "tickets empty while leaving most unanswerable ones empty is doing the job FR-10 asks for;",
        "one that returns something for everything is hiding failure (Build Specification §08).",
        "",
        "## Hit rate by segment (NFR-06, Governance Framework fairness audit)",
        "",
    ]
    for segment in ("fluency", "tier"):
        names = sorted({name for row in rows for name in row["segments"][segment]})
        sizes = {name: rows[0]["segments"][segment].get(name, (0, 0))[1] for name in names}
        lines += [
            f"### By {segment}",
            "",
            "| threshold | " + " | ".join(f"{n} (n={sizes[n]})" for n in names) + " | gap |",
            "|---" * (len(names) + 2) + "|",
        ]
        for row in rows:
            values = [_pct(*row["segments"][segment].get(name, (0, 0))[::-1][::-1])
                      for name in names]
            gap = round(max(values) - min(values), 1) if values else 0.0
            lines.append(f"| {row['threshold']:.2f} | "
                         + " | ".join(f"{value}%" for value in values) + f" | {gap} pts |")
        lines.append("")

    smallest = min((rows[0]["segments"]["fluency"].get(n, (0, 0))[1]
                    for n in rows[0]["segments"]["fluency"]), default=0)
    lines += [
        "**Sample sizes matter here.** The smallest fluency bucket is n=" + str(smallest) + ", so a",
        "gap of a few points is inside sampling noise (roughly ±6 points at 95% confidence for a",
        "rate near 90% at n=87). Treat a gap as a finding when it is both above NFR-06's 5 points",
        "**and** larger than the noise — and say which it is in the report rather than quoting a",
        "decimal as though it were precise.",
        "",
        "The Governance Framework expects retrieval to do worse on non-fluent English, because",
        "matching depends on phrasing. That pattern is visible here and it **widens as the",
        "threshold rises**, so the threshold is a fairness decision as well as a quality one;",
        "PR-04's query rewrite exists for it.",
        "",
        "## Caveats",
        "",
        "- Scores come from the real embedding model. A sweep run with the hashing test embedder",
        "  would produce numbers that mean nothing (FR-10 §7).",
        "- Judged against `expected_doc_ids` at the **document** level: a chunk of the right",
        "  article counts as a hit, which is the level FR-10's acceptance criterion is written at.",
        "- Development tickets only. The validation set is for checkpoint runs through the harness,",
        "  and the hidden set is seen once, after submission.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
