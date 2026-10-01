# The provider response cache, and why there are two of them

*(This file is tracked. The caches it describes are **not**: `storage/` is git-ignored, so neither database survives a clone. That is the point of writing it down — a reader with a fresh checkout has no cache at all and every run is live.)*

`storage/` is git-ignored runtime state. This file is here because one of the artefacts in it
turned out to matter for reproducing a published number.

## `llm_cache.sqlite` — the live cache

What `ProviderClient` reads and writes. Keyed on a SHA-256 of the request *plus* `prompt_id`,
`prompt_version` and `base_url` (D-?/`provider.py:_cache_key`), so a bumped prompt is never
replayed and the same model id on two providers is not the same entry.

**Its contents moved on 2026-10-01.** `--no-cache` (review row R5) originally skipped cache
*reads* and still wrote, and `_ResponseCache.put` is `INSERT OR REPLACE` with nothing in the key
to distinguish a September recording from today's. The first timing run this project made
therefore replaced **150 of 365 rows**. The consequence is not academic: replaying the same 80
validation tickets answered **42** against the September recordings and **43–45** against these,
because the model's text decides `no_cited_article`, `invalid_citation` and `ungrounded_draft`.

`--no-cache` now writes nothing, so a timing run leaves this file exactly as it found it
(`T-R5-2`).

## `llm_cache.2026-09-28-gate.sqlite` — the September recordings

A copy of the cache as it stood before 2026-10-01: 328 rows, newest `2026-09-30T18:16:26Z`. This
is what the figures in `evaluation/results/gate-openai-2/` and the gate sign-off in D-57 were
replayed from. Keep it until the R13 checkpoint decides which run the gate is signed off on.

To replay the September run:

```bash
LLM_CACHE_PATH=storage/llm_cache.2026-09-28-gate.sqlite \
  uv run python -m evaluation.harness --input data/validation_tickets.json --output /tmp/sept
```

It also contains six rows with `model: test-model`, written by the test suite before `R3`
gave `tests/test_fr04_api.py` its own cache path (D-64). They are harmless — the model name is
part of the key, so no real run can be served one — and are left in place rather than edited,
because this file's value is being an untouched copy.

## `llm_cache.sqlite.bak-before-test-row-cleanup`

The same September state, kept as the backup taken before those six test rows were deleted from
the live cache. Redundant with the file above; safe to delete once R13 is closed.

## What a fresh checkout gets

**Nothing.** `storage/` is git-ignored, so a clone has no cache, no decision log and no Chroma
index. The first run builds the index, and every provider call is live — which is the honest
default, and why `uv run pytest` must not need a key (it does not: the suite uses recorded
fixtures and `FakeTransport`).

It also means **the figures quoted in the README and in D-68 cannot be reproduced from the
repository alone**: they were measured against caches that only exist on the author's machine.
`evaluation/results/` is git-ignored too, with only a `.gitkeep`, so no report is committed
either. CLAUDE.md allows "dated reports you choose to keep" — review row R13 is where one gets
chosen, and `.gitignore` carries the exception pattern for it.
