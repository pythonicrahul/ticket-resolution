"""FR-10, FR-04: search over documentation.json with a relevance threshold.

Spec: docs/specs/FR-10.md. Three things are deliberate:

* **Chunks are markdown sections, not character windows.** Measured first: the 29 articles give
  145 `##` sections, median 157 characters, longest 658. A section is a coherent answer unit,
  and the Build Specification asks for a chunking strategy that can be justified.
* **Returning nothing is a real answer.** The documentation covers 71% of demand, so about three
  tickets in ten should retrieve nothing, and the Build Specification names "retrieval that
  returns something for every query" as a way to hide failure.
* **Nothing untyped escapes** (D-32). A corrupt index or a library that changes its mind raises
  `RetrievalError`, so one ticket fails rather than the run.

The embedder and the Chroma client are injectable, which is what lets every test run offline:
no 80 MB model download, no network.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

from ticketing_agent.config import Settings

_log = logging.getLogger(__name__)

#: Bumped whenever a rule in `chunk_documents` changes, so a stale index is never served.
CHUNKER_VERSION = 1

#: Measured on the supplied corpus: no section reaches 800 characters, so the splitter below
#: only bites on an unseen corpus. The overlap is the pack Setup Guide's own figure.
MAX_CHUNK_CHARS = 800
CHUNK_OVERLAP_CHARS = 120
#: Every article opens with a title-and-"Applies to" preamble of about 76 characters. On its own
#: it would match everything weakly and answer nothing, so anything this short merges forward.
MIN_CHUNK_CHARS = 120

COLLECTION_NAME = "cloudserve-docs"
#: Embedded once per build to identify the model by its output. `DefaultEmbeddingFunction.name()`
#: is the string "default" with an empty config, so a chromadb upgrade that changes the default
#: model would otherwise be invisible and a stale index would be served (row 5 review).
FINGERPRINT_PROBE = "cloudserve retrieval fingerprint probe"
CHUNK_ID_PATTERN = re.compile(r"^DOC-[A-Z]+-\d{3}#\d+$")


class RetrievalError(Exception):
    """FR-10 §4: the corpus or the index could not be used. Never a bare library exception."""


@dataclass(frozen=True)
class Chunk:
    """FR-10: one indexed passage of one article."""

    chunk_id: str
    doc_id: str
    title: str
    heading: str
    text: str
    ordinal: int
    category: str = ""
    applies_to: str = ""

    @property
    def indexed_text(self) -> str:
        """What is embedded: the heading alone means nothing without the article it is from."""
        prefix = f"{self.title} — {self.heading}" if self.heading else self.title
        return f"{prefix}\n{self.text}"


@dataclass(frozen=True)
class Passage:
    """FR-10: one retrieved chunk with its score, which is what a citation points at."""

    chunk_id: str
    doc_id: str
    title: str
    heading: str
    text: str
    score: float
    rank: int

    def as_source(self) -> tuple[str, float]:
        """FR-13: `sources_used` wants the doc id with its score, as the Governance record says."""
        return (self.doc_id, round(self.score, 4))


@dataclass(frozen=True)
class IndexStats:
    """What a build did, for the harness to report and for a test to assert."""

    documents: int
    chunks: int
    rebuilt: bool
    fingerprint: str
    embedder: str
    #: Documents that could not be indexed, by reason. The corpus is the only source of answers,
    #: so a partial index must be visible rather than implied by a smaller chunk count.
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def indexed_documents(self) -> int:
        return self.documents - len(self.skipped)


def load_documents(path: str | Path) -> list[dict[str, Any]]:
    """FR-10: read the corpus from the path given. No file name is hardcoded anywhere."""
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise RetrievalError(f"cannot read the documentation corpus at {path}: {exc}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetrievalError(f"the documentation corpus at {path} is not valid JSON: {exc}") from None

    if isinstance(payload, dict):
        for key in ("documents", "docs", "articles"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
    if not isinstance(payload, list) or not payload:
        raise RetrievalError(f"the documentation corpus at {path} holds no documents")
    return [doc for doc in payload if isinstance(doc, dict)]


def chunk_documents(docs: list[dict[str, Any]],
                    skipped: list[tuple[str, str]] | None = None) -> tuple[Chunk, ...]:
    """FR-10 §3.1: markdown sections, short ones merged forward, long ones split.

    Pure and deterministic: no Chroma, no embeddings, same input always the same chunks in the
    same order, which is what makes the index fingerprint meaningful. Anything that cannot be
    chunked is appended to `skipped` as `(doc_id, reason)` — never dropped in silence.
    """
    chunks: list[Chunk] = []
    notes = skipped if skipped is not None else []
    seen_ids: set[str] = set()
    for position, doc in enumerate(docs):
        doc_id = str(doc.get("doc_id") or doc.get("id") or "").strip()
        if not doc_id:
            notes.append((f"(position {position})", "no doc_id: it could never be cited"))
            continue
        if doc_id in seen_ids:
            # Two documents with one id would produce colliding chunk ids. Keeping the first and
            # reporting the second loses one article; letting it through loses the whole index.
            notes.append((doc_id, "duplicate doc_id: only the first occurrence is indexed"))
            continue
        seen_ids.add(doc_id)
        title = str(doc.get("title") or doc_id).strip()
        content = str(doc.get("content") or "").strip()
        if not content:
            notes.append((doc_id, "no content"))
            continue
        try:
            sections = _sections(content, title)
        except Exception as exc:  # noqa: BLE001 - one odd article must not stop the index
            notes.append((doc_id, f"could not be split: {type(exc).__name__}"))
            continue
        for ordinal, (heading, text) in enumerate(sections):
            chunks.append(Chunk(
                chunk_id=f"{doc_id}#{ordinal}",
                doc_id=doc_id,
                title=title,
                heading=heading,
                text=text,
                ordinal=ordinal,
                category=str(doc.get("category") or ""),
                applies_to=str(doc.get("applies_to") or ""),
            ))
    for doc_id, reason in notes:
        _log.warning("not indexing %s: %s", doc_id, reason)
    return tuple(chunks)


def _without_title_line(text: str) -> str:
    kept = [line for line in text.splitlines() if not line.strip().startswith("# ")]
    return "\n".join(kept).strip()


def _without_applies_to(text: str) -> str:
    kept = [line for line in text.splitlines() if not line.strip().startswith("**Applies to:**")]
    return "\n".join(kept).strip()


def _sections(content: str, title: str) -> list[tuple[str, str]]:
    """Split on markdown headings, merge the short ones, split the long ones."""
    from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

    splitter = MarkdownHeaderTextSplitter(
        [("#", "h1"), ("##", "h2"), ("###", "h3")], strip_headers=True)
    parts: list[tuple[str, str]] = []
    for part in splitter.split_text(content):
        heading = part.metadata.get("h3") or part.metadata.get("h2") or ""
        text = part.page_content.strip()
        if not heading:
            # The opening preamble is the title plus "**Applies to:** …", which is already a
            # field on the document. Indexed as prose it would match everything weakly, so it
            # is dropped when nothing else is in it.
            text = _without_applies_to(text)
        if text:
            parts.append((heading, text))
    if not parts:  # no headings at all: the whole article is one section, preamble stripped
        fallback = _without_applies_to(_without_title_line(content))
        if not fallback:
            return []
        parts = [("", fallback)]

    # When two sections merge, the heading of the *larger* part survives, because the heading is
    # embedded with the text and would otherwise describe the smaller half (row 5 review).
    merged: list[tuple[str, str]] = []
    for heading, text in parts:
        if merged and len(merged[-1][1]) < MIN_CHUNK_CHARS:
            previous_heading, previous_text = merged.pop()
            heading = heading if len(text) >= len(previous_text) else previous_heading
            text = f"{previous_text}\n{text}"
        merged.append((heading, text))
    if len(merged) > 1 and len(merged[-1][1]) < MIN_CHUNK_CHARS:
        heading, text = merged.pop()
        previous_heading, previous_text = merged.pop()
        merged.append((previous_heading or heading, f"{previous_text}\n{text}"))

    out: list[tuple[str, str]] = []
    for heading, text in merged:
        if len(text) <= MAX_CHUNK_CHARS:
            out.append((heading, text))
            continue
        pieces = RecursiveCharacterTextSplitter(
            chunk_size=MAX_CHUNK_CHARS, chunk_overlap=CHUNK_OVERLAP_CHARS).split_text(text)
        out.extend((heading, piece.strip()) for piece in pieces if piece.strip())
    return out


class HashingEmbedder(EmbeddingFunction):
    """A deterministic, offline embedder for tests: token hashing into a fixed vector.

    Good enough to test ordering, thresholds, emptiness and determinism. **Not** good enough for
    a claim about meaning — those tests use `RecordedEmbedder`, and the threshold sweep must be
    run with the real model (FR-10 §7).
    """

    def __init__(self, dim: int = 128) -> None:
        self.dim = dim

    def __call__(self, input: Documents) -> Embeddings:
        vectors = []
        for text in input:
            vector = [0.0] * self.dim
            for token in re.findall(r"[a-z0-9]+", str(text).lower()):
                digest = hashlib.sha1(token.encode("utf-8")).hexdigest()
                vector[int(digest, 16) % self.dim] += 1.0
            if not any(vector):
                vector[0] = 1.0  # Chroma rejects an all-zero vector in cosine space
            vectors.append(vector)
        return vectors

    @staticmethod
    def name() -> str:
        return "hashing-test-embedder"

    @staticmethod
    def is_legacy() -> bool:
        return False

    def get_config(self) -> dict[str, Any]:
        return {"dim": self.dim}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> HashingEmbedder:
        return HashingEmbedder(**config)


class RecordedEmbedder(EmbeddingFunction):
    """FR-10 §6: vectors recorded from the real model, so meaning can be tested offline.

    `scripts/record_embeddings.py` writes the fixture. Asking for a text that was not recorded
    raises rather than inventing a vector, because a fabricated embedding would make a test about
    meaning quietly meaningless.
    """

    def __init__(self, vectors: dict[str, list[float]], model: str) -> None:
        self.vectors = vectors
        self.model = model

    def __call__(self, input: Documents) -> Embeddings:
        missing = [text for text in input if str(text) not in self.vectors]
        if missing:
            raise RetrievalError(
                f"{len(missing)} text(s) are not in the recorded embeddings fixture; "
                "re-run scripts/record_embeddings.py")
        return [self.vectors[str(text)] for text in input]

    def name(self) -> str:
        return self.model

    @staticmethod
    def is_legacy() -> bool:
        return False

    def get_config(self) -> dict[str, Any]:
        return {"model": self.model}

    @staticmethod
    def build_from_config(config: dict[str, Any]) -> RecordedEmbedder:
        return RecordedEmbedder({}, config.get("model", "recorded"))


class Retriever:
    """FR-10, FR-04: the only way the system reads the documentation."""

    def __init__(self, settings: Settings, embedder: Any | None = None,
                 client: Any | None = None) -> None:
        self._settings = settings
        self._embedder = embedder if embedder is not None else _default_embedder()
        self._client = client if client is not None else _persistent_client(settings.chroma_path)
        self._chunks: dict[str, Chunk] = {}
        self._collection: Any | None = None

    # --- building -------------------------------------------------------------------

    def build_index(self, docs_path: str | Path | None = None,
                    rebuild: bool = False) -> IndexStats:
        """FR-10 §3.2: index the corpus, reusing an index whose fingerprint still matches."""
        try:
            path = (Path(docs_path) if docs_path is not None
                    else self._settings.require_path("docs_path"))
        except Exception as exc:  # noqa: BLE001 - a missing setting is still a retrieval failure
            raise RetrievalError(f"no documentation corpus to index: {exc}") from None
        docs = load_documents(path)
        skipped: list[tuple[str, str]] = []
        chunks = chunk_documents(docs, skipped)
        if not chunks:
            raise RetrievalError(f"the corpus at {path} produced no chunks")
        self._chunks = {chunk.chunk_id: chunk for chunk in chunks}
        fingerprint = _fingerprint(chunks, self._embedder, self._settings.embedding_model)

        collection = self._open_collection()
        existing = (collection.metadata or {}).get("fingerprint")
        if existing == fingerprint and not rebuild and collection.count() == len(chunks):
            self._collection = collection
            return IndexStats(len(docs), len(chunks), False, fingerprint,
                              _embedder_name(self._embedder), tuple(skipped))

        try:
            self._client.delete_collection(COLLECTION_NAME)
        except Exception as exc:  # noqa: BLE001 - absent is the normal case on a first build
            _log.debug("no existing collection to delete: %s", exc)
        collection = self._create_collection(fingerprint)
        try:
            collection.add(
                ids=[chunk.chunk_id for chunk in chunks],
                documents=[chunk.indexed_text for chunk in chunks],
                metadatas=[{
                    "doc_id": chunk.doc_id,
                    "title": chunk.title,
                    "heading": chunk.heading,
                    "ordinal": chunk.ordinal,
                    "category": chunk.category,
                } for chunk in chunks],
            )
        except Exception as exc:  # noqa: BLE001 - nothing untyped may leave this module
            raise RetrievalError(f"could not index the corpus at {path}: {exc}") from None
        self._collection = collection
        return IndexStats(len(docs), len(chunks), True, fingerprint,
                          _embedder_name(self._embedder), tuple(skipped))

    # --- searching ------------------------------------------------------------------

    def search(self, query: str, *, top_k: int | None = None,
               threshold: float | None = None) -> tuple[Passage, ...]:
        """FR-10: ranked passages above the threshold, or nothing at all."""
        text = (query or "").strip()
        if not text:
            return ()
        limit = top_k if top_k is not None else self._settings.retrieval_top_k
        floor = threshold if threshold is not None else self._settings.relevance_threshold
        collection = self._ready_collection()

        if len(text) > MAX_QUERY_CHARS:
            # A query longer than any real ticket is truncated rather than refused: the first
            # 2000 characters carry the question (the longest supplied ticket is 276).
            _log.debug("query truncated from %d to %d characters", len(text), MAX_QUERY_CHARS)
            text = text[:MAX_QUERY_CHARS]
        try:
            result = collection.query(query_texts=[text],
                                      n_results=max(1, limit))
        except Exception as exc:  # noqa: BLE001 - a broken index is one ticket, not the run
            raise RetrievalError(f"the documentation index could not be queried: {exc}") from None

        passages: list[Passage] = []
        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        for chunk_id, distance, metadata, document in zip(ids, distances, metadatas, documents,
                                                          strict=False):
            score = _similarity(distance)
            if score < floor:
                continue
            chunk = self._chunks.get(str(chunk_id))
            meta = metadata or {}
            passages.append(Passage(
                chunk_id=str(chunk_id),
                doc_id=str(meta.get("doc_id") or (chunk.doc_id if chunk else "")),
                title=str(meta.get("title") or (chunk.title if chunk else "")),
                heading=str(meta.get("heading") or ""),
                text=chunk.text if chunk else str(document or ""),
                score=score,
                rank=0,
            ))

        # Ties break on chunk_id so the order is the same on every run (NFR-08).
        passages.sort(key=lambda p: (-p.score, p.chunk_id))
        return tuple(
            Passage(**{**p.__dict__, "rank": position})
            for position, p in enumerate(passages[:limit], start=1)
        )

    def resolve(self, chunk_id: str) -> Chunk | None:
        """FR-11, FR-12: the chunk behind a citation, or None if it was invented."""
        return self._chunks.get(str(chunk_id).strip())

    @property
    def chunks(self) -> tuple[Chunk, ...]:
        return tuple(self._chunks.values())

    # --- internals ------------------------------------------------------------------

    def _open_collection(self) -> Any:
        try:
            return self._client.get_collection(COLLECTION_NAME,
                                               embedding_function=self._embedder)
        except Exception:  # noqa: BLE001 - not built yet, which is not an error
            return self._create_collection(fingerprint=None)

    def _create_collection(self, fingerprint: str | None) -> Any:
        metadata = {"fingerprint": fingerprint} if fingerprint else None
        try:
            return self._client.get_or_create_collection(
                COLLECTION_NAME, embedding_function=self._embedder, metadata=metadata,
                configuration={"hnsw": {"space": "cosine"}})
        except Exception as exc:  # noqa: BLE001
            raise RetrievalError(f"could not open the documentation index: {exc}") from None

    def _ready_collection(self) -> Any:
        if self._collection is None:
            raise RetrievalError(
                "the documentation index has not been built in this process: call build_index() "
                "first (the harness does this once per run)")
        return self._collection


MAX_QUERY_CHARS = 2000


def _similarity(distance: Any) -> float:
    """Cosine distance to a similarity in [0, 1]. A missing distance scores 0, never crashes."""
    try:
        return max(0.0, min(1.0, 1.0 - float(distance)))
    except (TypeError, ValueError):
        return 0.0


def _fingerprint(chunks: tuple[Chunk, ...], embedder: Any, configured_model: str = "") -> str:
    """FR-10 §3.2: the corpus, the chunking rules and the embedder, in one hash."""
    material = json.dumps({
        "chunker_version": CHUNKER_VERSION,
        "max_chunk_chars": MAX_CHUNK_CHARS,
        "min_chunk_chars": MIN_CHUNK_CHARS,
        "overlap": CHUNK_OVERLAP_CHARS,
        "embedder": _embedder_signature(embedder, configured_model),
        "chunks": [[c.chunk_id, c.indexed_text] for c in chunks],
    }, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _embedder_name(embedder: Any) -> str:
    try:
        name = embedder.name() if callable(getattr(embedder, "name", None)) else None
    except TypeError:
        name = None
    return str(name or type(embedder).__name__)


def _embedder_signature(embedder: Any, configured_model: str = "") -> str:
    """Identify the embedder by its **output**, not by what it calls itself.

    Chroma's default function reports the name "default" and an empty config, so neither
    distinguishes all-MiniLM-L6-v2 from whatever a future chromadb ships. Embedding one fixed
    probe string and hashing the result does: a different model gives a different vector, so a
    changed model always changes the fingerprint and the index is rebuilt (FR-10 §3.2 rule 7).
    """
    try:
        config = embedder.get_config() if callable(getattr(embedder, "get_config", None)) else {}
    except Exception:  # noqa: BLE001 - a config we cannot read still leaves the probe
        config = {}
    try:
        probe = embedder([FINGERPRINT_PROBE])[0]
        vector = [round(float(value), 4) for value in probe]
        behaviour = f"dim={len(vector)}:" + hashlib.sha256(
            json.dumps(vector).encode("utf-8")).hexdigest()[:16]
    except Exception as exc:  # noqa: BLE001 - an embedder that cannot embed is a real failure
        raise RetrievalError(
            f"the embedding model could not embed the fingerprint probe: {exc}") from None
    return (f"{_embedder_name(embedder)}:{configured_model}:"
            f"{json.dumps(config, sort_keys=True, default=str)}:{behaviour}")


def _default_embedder() -> Any:
    """D-02: Chroma's built-in all-MiniLM-L6-v2, the model the brief prescribes."""
    try:
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        return DefaultEmbeddingFunction()
    except Exception as exc:  # noqa: BLE001
        raise RetrievalError(
            f"could not load the embedding model: {exc}. It downloads once (~80 MB) to the local "
            "cache; a test should inject an embedder instead."
        ) from None


def _persistent_client(path: str | Path) -> Any:
    try:
        import chromadb

        Path(path).mkdir(parents=True, exist_ok=True)
        return chromadb.PersistentClient(path=str(path))
    except Exception as exc:  # noqa: BLE001
        raise RetrievalError(f"could not open the vector store at {path}: {exc}") from None
