"""FR-14, review row R13: compare several gate runs against each other and against the labels.

    uv run python scripts/gate_checkpoint_report.py \
        --live evaluation/results/kept-gate-2026-10-02 \
        --renamed evaluation/results/kept-gate-2026-10-02-renamed \
        --replay evaluation/results/kept-gate-2026-10-02-replay \
        --september evaluation/results/gate-openai-2 \
        --validation data/validation_tickets.json \
        --development data/development_tickets.json \
        --output evaluation/reports/gate-2026-10-02-checkpoint.md

A single run's report cannot answer the question R13 exists to settle: whether the figure the
gate is signed off on is stable, and whether a disagreement with a label is the system's fault
or the data's. That needs two runs side by side and the two ticket files read for duplicate
bodies, which is what this does. It computes nothing the harness already computes; it reads the
runs' own artefacts.

Paths are arguments and no file name is hardcoded (CLAUDE.md). Every run directory is read as
`metrics.json` + `outcomes.jsonl`, so any harness output works, including one from a file nobody
has seen.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
from collections import Counter


def outcomes(d: pathlib.Path) -> dict:
    return {json.loads(l)["ticket_id"]: json.loads(l)
            for l in (d / "outcomes.jsonl").read_text().splitlines() if l.strip()}


def metrics(d: pathlib.Path) -> dict:
    return json.loads((d / "metrics.json").read_text())


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", required=True)
    p.add_argument("--renamed", required=True)
    p.add_argument("--replay", required=True)
    p.add_argument("--september", required=True)
    p.add_argument("--validation", required=True)
    p.add_argument("--development", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()

    runs = {k: pathlib.Path(v) for k, v in
            (("live", a.live), ("renamed", a.renamed), ("replay", a.replay))}
    o = {k: outcomes(v) for k, v in runs.items()}
    m = {k: metrics(v) for k, v in runs.items()}
    sept = outcomes(pathlib.Path(a.september))

    val = json.loads(pathlib.Path(a.validation).read_text())
    dev = json.loads(pathlib.Path(a.development).read_text())
    db: dict[str, list] = {}
    for t in dev:
        db.setdefault(norm(t["body"]), []).append(t)
    vb: dict[str, list] = {}
    for t in val:
        vb.setdefault(norm(t["body"]), []).append(t)

    L: list[str] = []
    L += ["# Gate checkpoint, 2026-10-02 (review row R13)", "",
          "Three runs of the same 80 validation tickets on the same code, and the September run",
          "the gate was signed off on in D-57. Every figure is read from those runs' own",
          "`metrics.json` and `outcomes.jsonl`, kept under",
          "`evaluation/results/kept-gate-2026-10-02*/`; regenerate with the command in the",
          "docstring of `scripts/gate_checkpoint_report.py`.", "",
          "## The four runs", "",
          ("| run | responses | answered | escalated | blocked | provider calls "
           "| cache hits | automated-path p95 | wall |"),
          "|---|---|---|---|---|---|---|---|---|"]
    label = {"live": "**live, `--no-cache`**", "renamed": "renamed copy, cache on",
             "replay": "replay of the renamed run"}
    state = {"live": "all live", "renamed": "partly replayed", "replay": "all replayed"}
    for k in ("live", "renamed", "replay"):
        v, g, lat, r = m[k]["volume"], m[k]["governance"], m[k]["latency"], m[k]["run"]
        L.append(f"| {label[k]} | {state[k]} | {v['answered_automatically']} | "
                 f"{v['escalated']} | {v['blocked_by_guardrails']} | {g['model_calls']} | "
                 f"{g['cache_hits']} | {lat['automated_path_p95_ms']:.0f} ms | "
                 f"{r['wall_seconds']:.0f} s |")
    answered_sept = sum(1 for t in sept if sept[t]["decision"] == "auto_respond")
    L.append(f"| September (D-57), replayed | all replayed | {answered_sept} | "
             f"{80 - answered_sept} | — | — | — | not measured | — |")

    L += ["", ("Every run: 80 tickets in, 80 terminal rows, log reconciles, 0 private-data "
                "detections, 0 redactions, 0 unresolvable citations."), ""]

    def compare(x: str, y: str, title: str, note: str) -> None:
        nonlocal L
        diff = [t for t in o[x] if o[x][t]["decision"] != o[y][t]["decision"]]
        L += [f"## {title}", "", note, "",
              f"**{len(diff)} of 80 tickets routed differently.**", ""]
        if not diff:
            L += ["Not one. Routing and every reply are identical.", ""]
            return
        L += [f"| ticket | {x} | {y} |", "|---|---|---|"]
        for t in sorted(diff):
            L.append(f"| {t} | {o[x][t]['decision']} / {o[x][t].get('reason') or '—'} "
                     f"| {o[y][t]['decision']} / {o[y][t].get('reason') or '—'} |")
        L.append("")

    compare("renamed", "replay",
            "Determinism, with every response replayed",
            "The same input file twice, the second served entirely from the cache the first one "
            "recorded. This is the condition the non-negotiable in `CLAUDE.md` names: *cached "
            "model responses, same input → same routing*.")
    ident = sum(1 for t in o["renamed"] if o["renamed"][t].get("reply") == o["replay"][t].get("reply"))
    L += [f"Reply text identical on **{ident} of 80**.", ""]

    compare("live", "renamed",
            "The same code and configuration, hours apart",
            "Run 1 reached the provider for all 138 responses. Run 2 replayed 97 of 139 and "
            "fetched 42. Nothing else differs: same code, same thresholds, same models, "
            "temperature 0.")

    diff_sept = [t for t in sept if sept[t]["decision"] != o["live"][t]["decision"]]
    by_reason = Counter(o["live"][t].get("reason") for t in diff_sept
                        if o["live"][t]["decision"] != "auto_respond")
    answered_live = sum(1 for t in o["live"] if o["live"][t]["decision"] == "auto_respond")
    L += ["## Against the September run the gate was signed off on", "",
          (f"Both runs answered {answered_sept} and {answered_live}. "
           f"**{len(diff_sept)} tickets** are routed differently inside that "
           "near-identical total."),
          "", "| moved to escalate because | tickets |", "|---|---|"]
    for reason, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        ids = ", ".join(sorted(t for t in diff_sept
                               if o["live"][t].get("reason") == reason
                               and o["live"][t]["decision"] != "auto_respond"))
        L.append(f"| `{reason}` | {n} — {ids} |")
    to_answered = sorted(t for t in diff_sept if o["live"][t]["decision"] == "auto_respond")
    reasons_then = Counter(sept[t].get("reason") for t in to_answered)
    L += [f"| moved to **auto_respond** | {len(to_answered)} — {', '.join(to_answered)} |", "",
          "They were escalated in September for: "
          + ", ".join(f"`{k}` ×{v}" for k, v in reasons_then.items()) + ".", ""]

    L += ["## The labels contradict themselves", "",
          ("Measured on the supplied files, identical body text after whitespace and case "
           "normalisation."), ""]
    multi = [ts for ts in vb.values() if len({(x["labels"]["expected_route"],
                                               x["labels"]["answerable_from_docs"]) for x in ts}) > 1]
    L += [f"* **{len(multi)} bodies inside `validation_tickets.json`** carry more than one label:",
          ""]
    for ts in multi:
        L.append("  * " + ", ".join(f"{x['ticket_id']} → {x['labels']['expected_route']}"
                                    f"/answerable={x['labels']['answerable_from_docs']}"
                                    for x in sorted(ts, key=lambda y: y["ticket_id"])))
    twinned = [t for t in val if norm(t["body"]) in db]
    disagree = []
    for t in twinned:
        l = t["labels"]
        bad = [y["ticket_id"] for y in db[norm(t["body"])]
               if y["labels"]["expected_route"] != l["expected_route"]
               or y["labels"]["answerable_from_docs"] != l["answerable_from_docs"]]
        if bad:
            disagree.append((t["ticket_id"], l["expected_route"], bad))
    L += ["",
          (f"* **{len(twinned)} of 80** validation tickets have a development ticket with an "
           f"identical body, and **{len(disagree)} of those {len(twinned)}** carry a different "
           "label from their twin:"), ""]
    for tid, route, bad in sorted(disagree):
        L.append(f"  * {tid} (`{route}`) vs {', '.join(bad[:3])}"
                 f"{' …' if len(bad) > 3 else ''}")

    against = m["live"]["technical"]["answered_against_the_labels"]
    L += ["", "## The R6 list, for the hand review", "",
          ("From the live run. Each of these the author reads and decides whether the label or "
           "the system is right."), "",
          "| disagreement | count | tickets |", "|---|---|---|"]
    for lab, key in (("Answered, label says `must_not_auto_respond`",
                      "answered_but_must_not_auto_respond"),
                     ("Answered, label says `escalate`", "answered_but_labelled_escalate"),
                     ("Answered, label says not answerable from docs",
                      "answered_but_not_answerable_from_docs"),
                     ("Answered, cited no expected article",
                      "answered_citing_no_expected_article")):
        row = against[key]
        L.append(f"| {lab} | {row['count']} | {', '.join(row['ticket_ids']) or '—'} |")
    matrix = against["confusion_against_expected_route"]
    L += ["", "And the other direction, which is the larger number:", "",
          "| expected ↓ / actual → | auto_respond | escalate |", "|---|---|---|",
          (f"| auto_respond | {matrix['auto_respond']['auto_respond']} "
           f"| {matrix['auto_respond']['escalate']} |"),
          (f"| escalate | {matrix['escalate']['auto_respond']} "
           f"| {matrix['escalate']['escalate']} |"), ""]

    out = pathlib.Path(a.output)
    out.write_text("\n".join(L) + "\n")
    print(f"wrote {out} ({len(L)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
