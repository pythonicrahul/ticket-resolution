"""Record real embeddings once, so tests about meaning can run offline (FR-10 §6).

The behaviour tests inject a hashing embedder, which cannot say whether "my deployment keeps
dying" is *about* DOC-DEPLOY-001. This script embeds the indexed text of every chunk plus a short
list of queries with the real all-MiniLM-L6-v2 model, and writes them to
`tests/fixtures/recorded_embeddings.json`. CI then tests FR-04's criterion with no model
download and no network (CLAUDE.md: tests must pass with neither).

Run it again after a change to the chunking rules or the embedding model:

    uv run python scripts/record_embeddings.py [--docs data/documentation.json]

The fixture records the embedder's name, and the tests skip with a clear message rather than
lying if it no longer matches.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ticketing_agent.retrieve import (
    CHUNKER_VERSION,
    FINGERPRINT_PROBE,
    chunk_documents,
    load_documents,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "recorded_embeddings.json"

#: The queries the tests ask about meaning. Two are FR-04's own example and its opposite; the
#: rest are one paraphrased question per documentation area, so the sweep has something to chew.
QUERIES = (
    "my deployment keeps dying",
    "why is my invoice higher than usual this month",
    "what is your office holiday party dress code",
    "builds that worked last week now fail during dependency resolution",
    "one of our api keys started returning 401 without any change on our side",
    "how is proration calculated when we upgrade mid-month",
    "our spend cap stopped a production workload overnight",
    "a restore we started at the weekend is still running",
    "how do I configure single sign on with our identity provider",
    "webhook deliveries are failing and we want to know about retries",
    "can I export the access records our auditor asked for",
    "we are tightening permissions and need to understand group membership",
    "the api is returning rate limit errors under load",
    "where should secrets be stored so they do not appear in the logs",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs", default=str(ROOT / "data" / "documentation.json"),
                        help="the documentation corpus to embed")
    parser.add_argument("--output", default=str(FIXTURE))
    args = parser.parse_args()

    chunks = chunk_documents(load_documents(args.docs))
    # The index fingerprint embeds a probe string, so the fixture must carry it too or a test
    # using recorded vectors could not build an index at all.
    texts = [chunk.indexed_text for chunk in chunks] + list(QUERIES) + [FINGERPRINT_PROBE]
    print(f"embedding {len(chunks)} chunks, {len(QUERIES)} queries and the fingerprint probe "
          "with the real model (first run downloads ~80 MB)…")

    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    embedder = DefaultEmbeddingFunction()
    vectors = embedder(texts)

    payload = {
        "embedder": embedder.name(),
        "dimensions": len(vectors[0]),
        "chunks": len(chunks),
        "chunker_version": CHUNKER_VERSION,
        "queries": list(QUERIES),
        # Rounded to 6 decimals: plenty for a cosine comparison, and it halves the file.
        "vectors": {text: [round(float(v), 6) for v in vector]
                    for text, vector in zip(texts, vectors, strict=True)},
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    size_mb = output.stat().st_size / 1_000_000
    print(f"wrote {output} — {payload['embedder']}, {payload['dimensions']} dims, "
          f"{len(payload['vectors'])} texts, {size_mb:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
