"""FR-15, NFR-07: one real call to the configured provider, before row 11 depends on it.

    uv run python scripts/provider_smoke.py

Everything in `src/ticketing_agent/provider.py` is tested against `FakeTransport`, which is what
keeps the suite offline — and means nothing has ever verified that the *configured* model answers
at all, or that a free-tier model can return JSON that validates into `schemas.AnswerDraft`. Row 11
(answer drafting) is built on `complete_structured`, so finding that out here is cheaper than
finding it there.

Four checks, in order:

1. **a plain completion** — does the model, the key and the base URL work together;
2. **the same call again** — does the response cache replay it (NFR-08's determinism plumbing);
3. **`complete_structured` into `AnswerDraft`** — the row-11 dependency, including whether the
   repair attempt was needed;
4. **a deliberate failure** (a model id that does not exist) — does it come back as a typed
   `ProviderFailure` with the retries and backoff FR-15 promises, rather than something untyped.

Every request and reply is recorded to `tests/fixtures/recorded_provider_responses.json`, so row
11's tests can replay a *real* free-model reply through `FakeTransport` and still need no network.

**It refuses to run on a paid endpoint.** NFR-07 allows no spend, so the model id must carry
OpenRouter's `:free` suffix. There is deliberately no flag to override that.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from ticketing_agent.config import Settings, load_settings
from ticketing_agent.provider import (
    MalformedModelOutput,
    ProviderClient,
    ProviderFailure,
)
from ticketing_agent.schemas import AnswerDraft

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RECORDING = ROOT / "tests" / "fixtures" / "recorded_provider_responses.json"
#: A free-endpoint marker. Groq's ids do not use it, so a Groq base URL is accepted separately.
FREE_SUFFIX = ":free"
#: Anything that looks like a credential must never reach a committed fixture.
SECRET_SHAPES = (r"sk-[A-Za-z0-9_-]{8,}", r"Bearer\s+\S+", r"api[_-]?key\s*[:=]\s*\S+")

PASSAGE = (
    "DOC-BILL-001#2 (Invoices and usage breakdown → Resolution): Open Billing → Usage breakdown "
    "to see the charge for each service for the period. The breakdown is generated nightly."
)
SYSTEM = (
    "You answer customer support questions for a cloud platform using only the passages given. "
    "Cite the passage id for every sentence. If the passages do not answer the question, say so."
)
QUESTION = "Where can I see the breakdown of my invoice by service?"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", default=str(ROOT / "storage" / "smoke_cache.sqlite"),
                        help="kept apart from LLM_CACHE_PATH so a smoke run cannot seed a "
                             "harness run's cache")
    parser.add_argument("--recording", default=str(DEFAULT_RECORDING))
    parser.add_argument("--max-tokens", type=int, default=400)
    args = parser.parse_args()

    configured = load_settings()
    model = configured.model_name.strip()
    if not model:
        print("REFUSING: MODEL_NAME is not set. There is no default in code (NFR-07).")
        return 1
    if not configured.llm_api_key.strip():
        print("REFUSING: LLM_API_KEY is not set, so there is nothing to authenticate with.")
        return 1
    if FREE_SUFFIX not in model and "groq.com" not in configured.llm_base_url:
        print(f"REFUSING: MODEL_NAME={model!r} does not carry OpenRouter's {FREE_SUFFIX!r} "
              "suffix, which means it is the paid endpoint. NFR-07 allows no spend, and there is "
              "deliberately no flag to override this. Fix MODEL_NAME in .env first.")
        return 1

    _check_catalogue(configured.llm_base_url, model, configured.judge_model_name.strip())

    settings = Settings(**{**configured.__dict__, "llm_cache_path": Path(args.cache)})
    recorder = _Recorder(settings)
    client = ProviderClient(settings, transport=recorder)
    print(f"model {model} at {settings.llm_base_url}, cache {_relative(Path(args.cache))}\n")

    results = [
        _plain(client, args.max_tokens),
        _cached(client, args.max_tokens),
        _structured(client, args.max_tokens),
        _failure(client, args.max_tokens),
    ]

    ok = all(result for result in results)
    written = _write_recording(Path(args.recording), recorder.exchanges, model)
    print(f"\n{sum(1 for r in results if r)}/{len(results)} checks passed. "
          f"{len(recorder.exchanges)} exchange(s) recorded to {_relative(written)}.")
    print(f"provider requests: {client.provider_requests}, cache hits: {client.cache_hits}, "
          f"failures: {client.failures}")
    return 0 if ok else 1


def _plain(client: ProviderClient, max_tokens: int) -> bool:
    print("1. a plain completion")
    try:
        response = client.complete(
            [{"role": "system", "content": "Reply with one short sentence."},
             {"role": "user", "content": "Say that the connection works."}],
            prompt_id="SMOKE-plain", prompt_version="v0", max_tokens=max_tokens)
    except ProviderFailure as exc:
        print(f"   FAILED: {type(exc).__name__}: {exc}")
        return False
    print(f"   ok: {response.latency_ms:.0f} ms, model_version={response.model_version!r}, "
          f"attempts={response.attempts}")
    print(f"   reply: {response.text.strip()[:160]!r}")
    return bool(response.text.strip())


def _cached(client: ProviderClient, max_tokens: int) -> bool:
    print("2. the same call again, which must be replayed from the cache (NFR-08)")
    before = client.provider_requests
    try:
        response = client.complete(
            [{"role": "system", "content": "Reply with one short sentence."},
             {"role": "user", "content": "Say that the connection works."}],
            prompt_id="SMOKE-plain", prompt_version="v0", max_tokens=max_tokens)
    except ProviderFailure as exc:
        print(f"   FAILED: {type(exc).__name__}: {exc}")
        return False
    reached = client.provider_requests - before
    print(f"   cached={response.cached}, requests that reached the provider: {reached}")
    if not response.cached or reached:
        print("   FAILED: the second identical call was not replayed, so the same input can "
              "give a different answer (NFR-08).")
        return False
    return True


def _structured(client: ProviderClient, max_tokens: int) -> bool:
    print("3. complete_structured into AnswerDraft — what row 11 is built on")
    instruction = (
        "Answer the question using only the passage. Reply with JSON only, no other text, in "
        'this shape: {"answerable": true, "sentences": [{"text": "...", "citations": '
        '["DOC-BILL-001#2"]}], "unknown_reason": ""}. If the passage does not answer the '
        'question, use {"answerable": false, "sentences": [], "unknown_reason": "why"}.')
    try:
        result = client.complete_structured(
            [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": f"{instruction}\n\nPassage:\n{PASSAGE}\n\n"
                                         f"<ticket>\n{QUESTION}\n</ticket>"}],
            prompt_id="SMOKE-structured", prompt_version="v0", schema=AnswerDraft,
            max_tokens=max_tokens)
    except MalformedModelOutput as exc:
        print(f"   FAILED: this model could not produce {AnswerDraft.__name__} even after a "
              f"repair attempt: {exc}")
        print("   Row 11 needs either a different free model or a prompt with a worked example.")
        return False
    except ProviderFailure as exc:
        print(f"   FAILED: {type(exc).__name__}: {exc}")
        return False
    draft = result.value
    print(f"   ok: answerable={draft.answerable}, sentences={len(draft.sentences)}, "
          f"citations={draft.cited_ids}, repaired={result.repaired}, notes={result.notes}")
    print(f"   draft: {draft.text[:200]!r}")
    if result.repaired:
        print("   NOTE: it took a repair attempt, so PR-01 should carry a worked example.")
    if draft.answerable and not draft.cited_ids:
        print("   NOTE: answerable with no citation — FR-11's citation rule would block this.")
    return True


def _failure(client: ProviderClient, max_tokens: int) -> bool:
    print("4. a model id that does not exist, to see the failure path (FR-15)")
    try:
        client.complete([{"role": "user", "content": "This must not succeed."}],
                        prompt_id="SMOKE-failure", prompt_version="v0",
                        model="ticketing-agent/definitely-not-a-real-model:free",
                        max_tokens=max_tokens)
    except ProviderFailure as exc:
        print(f"   ok: {type(exc).__name__} after {getattr(exc, 'attempts', '?')} attempt(s): "
              f"{str(exc)[:200]}")
        return True
    except Exception as exc:  # noqa: BLE001 - the point of the check
        print(f"   FAILED: an untyped {type(exc).__name__} escaped the client: {exc}")
        return False
    print("   FAILED: a nonexistent model returned a usable response, which cannot be right.")
    return False


def _check_catalogue(base_url: str, model: str, judge: str) -> None:
    """Say plainly that an id has left the free roster, instead of retrying a 404 three times.

    The roster turns over — every free id this repository suggested in its first week has since
    gone — and `MalformedModelOutput after 3 attempts` is a poor way to learn that. A catalogue
    that cannot be read is only a warning: it must not stop a smoke run.
    """
    import json as _json
    import urllib.request

    if "openrouter.ai" not in base_url:
        return
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=20) as reply:
            catalogue = _json.load(reply)
    except Exception as exc:  # noqa: BLE001 - informational only
        print(f"   (could not read the model catalogue: {type(exc).__name__}; continuing)\n")
        return
    free = {m["id"] for m in catalogue.get("data", []) if str(m.get("id", "")).endswith(":free")}
    for name, value in (("MODEL_NAME", model), ("JUDGE_MODEL_NAME", judge)):
        if value and value not in free:
            print(f"   WARNING: {name}={value!r} is not in OpenRouter's current free catalogue. "
                  f"Free ids now include: {', '.join(sorted(free)[:6])}…")
    print(f"   catalogue: {len(free)} free ids, MODEL_NAME present: {model in free}\n")


class _Recorder:
    """Wraps the real transport and keeps every request/reply pair for row 11's fixtures."""

    def __init__(self, settings: Settings) -> None:
        from ticketing_agent.provider import LangChainTransport

        self._inner = LangChainTransport(settings)
        self.exchanges: list[dict[str, Any]] = []

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._inner.send(request)
        except Exception as exc:
            self.exchanges.append({"request": request, "error": f"{type(exc).__name__}: {exc}"})
            raise
        self.exchanges.append({"request": request, "response": response})
        return response


def _write_recording(path: Path, exchanges: list[dict[str, Any]], model: str) -> Path:
    payload = {
        "recorded_from": model,
        "why": ("Real replies from the configured free-tier model, captured by "
                "scripts/provider_smoke.py. Row 11's tests replay the `response` values through "
                "FakeTransport so they need no network and no key."),
        "exchanges": exchanges,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    for shape in SECRET_SHAPES:
        if re.search(shape, text, re.IGNORECASE):
            raise SystemExit(
                f"REFUSING to write {path}: it matches {shape!r}, which looks like a credential. "
                "No key may reach a fixture (CLAUDE.md).")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
