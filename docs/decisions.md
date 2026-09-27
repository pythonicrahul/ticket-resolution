# Design decisions

One entry per decision that someone might later ask "why?" about. Newest last.

## D-01 · Python 3.14 and dependencies managed with uv
Python 3.14 is the latest stable release; every dependency publishes 3.14 wheels (chromadb ships abi3 wheels, onnxruntime and scikit-learn ship cp314). Direct dependencies use compatible-release pins (~=), and uv.lock pins everything exactly. If a transitive dependency ever lacks 3.14 wheels, fall back to 3.13 by changing .python-version and requires-python.

uv resolves and locks everything (`uv.lock`), installs Python itself, and is fast in CI. The pack's pinned `requirements.txt` (langchain 0.1.0, chromadb 0.3.21, openai 1.0.0, …) is old and very likely conflicts; it is replaced here. A `requirements.txt` is exported from the lock for anyone not using uv. Record this in the Stage 5 revision log.

## D-02 · Embeddings via Chroma's built-in all-MiniLM-L6-v2 (ONNX)
The brief prescribes all-MiniLM-L6-v2. Chroma's default embedding function *is* that model, run through ONNX, so we avoid installing sentence-transformers and PyTorch (hundreds of MB, slow CI). The model downloads once (~80 MB) to the local cache on first use. The same function embeds tickets for the classifier. Revisit if retrieval quality needs a different model.

## D-03 · Intent classification without the language model
Embeddings + logistic regression with calibrated probabilities (FR-08). Model-reported confidence is poorly calibrated; the governance condition needs stated confidence within 5 points of observed accuracy, and routing must be deterministic. This is also free and fast.

## D-04 · Disclosure line written by code, not the model (FR-06)
A fixed template guarantees it appears and makes the check trivial.

## D-05 · Ingest keeps the original text and a cleaned copy, and never raises (FR-07)
`Ticket.subject` and `Ticket.body` hold the supplied text byte for byte; `Ticket.text` is the cleaned
subject-plus-body that the classifier, the retriever and every prompt read. Two fields rather than one
because FR-07 requires the original channel and text to survive, while unusual characters (zero-width,
BiDi overrides, NUL, lone surrogates) must never reach an index or a prompt. `normalise_ticket` catches
its own failures and returns a ticket carrying `normalisation_error`, so one bad row cannot end a run and
nothing is silently dropped: entries that are not even objects come back as flagged tickets too.

## D-06 · Defect codes, not exceptions, decide what escalates (FR-07 → FR-09)
Normalisation records machine-readable defect codes (`empty_text`, `unknown_channel`, `coerced_field:body`,
…). Four of them block: `not_an_object`, `missing_channel`, `unknown_channel`, `empty_text`, plus
`normalisation_error`. Blocking means `is_malformed`, which routing turns into an escalation with reason
`malformed_ticket` before any model call. Treating an unrecognised channel as blocking is the conservative
choice: we do not know the text contract of a channel nobody told us about. Non-blocking defects (missing
timestamp, unknown tier/region/fluency, coerced types, truncated text) travel with the ticket into the
decision log and the segment tables, so a strange ticket is visible rather than quietly ordinary.

## D-07 · Ground truth is unreachable from the runtime representation (FR-07, FR-14)
`Ticket` has no `labels` or `history` attribute. The supplied entry stays available as `Ticket.raw`, and the
harness reads labels through `evaluation_labels(ticket)`. Without this split it would be easy to score well
by accidentally reading `labels["intent"]` on the runtime path. A missing `labels` block is not a defect:
the hidden validation file may not carry one.

## D-08 · Generated ticket ids are deterministic; duplicate ids are flagged, not rewritten (FR-07, NFR-08)
A missing id becomes `GEN-{index:04d}-{sha256(entry)[:8]}`, so the same file always produces the same ids and
the decision log reconciles across reruns. A repeated id is kept as supplied with defect `duplicate_ticket_id`
rather than rewritten to something an operator would not recognise; reconciliation counts rows, not distinct
ids. Flagged as an open question in docs/specs/FR-07.md.

## D-09 · A failed normalisation escalates with its identity intact (FR-07, review finding)
The first version of the per-entry catch built the flagged ticket from a JSON dump of the whole entry and
generated a fresh id. The independent review showed three consequences: `labels`/`history` ground truth
landed in `Ticket.text` and so would have reached a prompt, the operator's `ticket_id` disappeared so the
decision log could not reconcile with the input file, and the defect string carried an exception-type suffix
that no longer matched `BLOCKING_DEFECTS`, so the ticket looked answerable. Now the fallback emits both
`normalisation_error` (blocking) and `normalisation_error:{Type}`, keeps the supplied id and `raw`, and builds
`text` from the supplied subject and body only. T-FR07-19 and T-FR07-20 hold the line.

## D-10 · An unrecognised wrapper object fails the run rather than becoming one ticket (FR-07, FR-14)
`load_tickets` accepts an array, a ticket-shaped object, or an array under `tickets`/`data`/`items`. Anything
else raises `TicketFileError`. Being generous here is dangerous rather than kind: `{"validation_tickets":
[… 500 …]}` read as a single malformed ticket would drop 500 tickets with no log row and no error, which is
exactly the silent loss CLAUDE.md forbids. A loud run failure on an unseen file is recoverable; silent loss
is not. Ticket-shaped keys are checked before wrapper keys so a ticket carrying an `items` field stays one
ticket. Files are decoded as `utf-8-sig` (a byte-order mark is common in exports) and non-UTF-8 input becomes
`TicketFileError` rather than a bare `UnicodeDecodeError`.

## D-11 · The green-tests stop hook is dev tooling, and now only BLOCKED opens it (build loop)
`.claude/hooks/require_green.py` used to skip the pytest gate whenever any backlog row was `BLOCKED` **or**
`HUMAN`. Since checkpoint rows legitimately sit at `HUMAN` for a human decision, that would have left the gate
open for the rest of the build. It now opens only for `BLOCKED`, which is the documented case where step 5 of
`/next-feature` leaves a red suite uncommitted, and it resolves the backlog path relative to itself rather
than the current directory. It remains a gate that editing a file can open, so it is not load-bearing for
FR-12: the system's guardrails live in `src/` where no flag, env var or file edit can switch them off.
