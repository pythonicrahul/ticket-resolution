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

## D-12 · The decision log is keyed on a row id, not on ticket_id (FR-13, FR-07)
Decided by the author, 2026-09-27, resolving the first open question in `docs/specs/FR-07.md`. Ingest keeps a
duplicate `ticket_id` exactly as supplied and records `duplicate_ticket_id`; it never rewrites an id into
something (`DEV-0001#2`) that exists in no operator's system and could surface in a handover or a reply. For
that to reconcile, the log rows carry a surrogate primary key with `ticket_id` and `source_index` as columns,
and FR-13's reconciliation compares row count and `source_index` coverage against the input file rather than
counting distinct ids. Defensive only: dev and validation hold 500 and 80 distinct ids with no overlap.

## D-13 · An unrecognised channel escalates, and the metrics report counts them (FR-07, FR-14)
Decided by the author, 2026-09-27. A channel outside the four named in FR-07 gives `channel = "unknown"`,
which is blocking, so the ticket escalates before any model call: we cannot reason about the text conventions
of a channel nobody described, and a fifth channel may not be a customer at all. If the unseen file contains
one, the cost is escalation rate (target ≤30%), not a wrong answer. So that this is visible rather than
mysterious, the metrics report counts `unknown_channel` tickets as their own line; a count above zero is the
evidence that would justify relaxing the rule.

## D-14 · An over-long ticket is escalated, not answered from its first 8000 characters (FR-07, FR-09)
Decided by the author, 2026-09-27. The `MAX_TEXT_CHARS` cap (8000) stays as prompt safety, and routing
escalates any ticket carrying `text_truncated`. The longest subject+body in the 580 supplied tickets is 276
characters, so anything hitting the cap is ~30x out of distribution: far more likely a thread dump, a log
paste or an injection attempt than a question, and answering its first 8000 characters would answer half a
question, which is the "confidently and incorrectly" failure in the PRD risk register. `text_truncated` is
deliberately **not** in `BLOCKING_DEFECTS`: `is_malformed` means "no usable representation", whereas this is a
routing policy, and the agent receives the whole `body` in the handover.

## D-15 · Labels are optional and the metrics report states what it scored (FR-14, FR-07)
Decided by the author, 2026-09-27. A ticket file with no `labels` block is valid input, since FR-14 says the
harness runs on any ticket file. The harness always computes the metrics that need no ground truth —
escalation rate, citation presence, disclosure line, guardrail blocks, reconciliation, runtime, segment
splits — prints `scored against labels: N of M`, and marks the accuracy sections not computable when labels
are absent. `ground_truth_responses.json` is not a fallback: it is 200 rows keyed by development ticket ids,
so using it would silently change what is being measured between files.

## D-16 · One fixed precedence for the pre-model escalation rules (FR-03, FR-07, FR-09, FR-12, FR-16)
Several rules can fire on one ticket: `SYN-INJ-001` is an injection attempt *and* a refund request. Without a
stated order, the reason written to the decision log depends on the order the code happens to evaluate in,
which breaks determinism (NFR-08) and auditability (FR-13), and lets rows 9 and 12 each implement an order
that makes the other's test fail. The order, highest first: `kill_switch` (FR-16), `private_data_in_ticket`
(FR-12), `instruction_injection_detected` (FR-12), `malformed_ticket` (FR-07), `must_escalate_intent` (FR-09),
`money_commitment_requested` then `date_commitment_requested` (FR-03), `text_truncated` (D-14), `no_retrieval`
(FR-10), `low_confidence` (FR-02). Rationale for the top: an operator's kill switch outranks everything; a
secret must not be embedded, cached or sent anywhere, so that decision is taken before the text is read for
anything else; hostile text cannot be trusted for any other reading. The log carries `reason` (the primary)
**and** `all_reasons` (every match, in this order), so nothing is lost. Table in `docs/specs/FR-12.md` §3.4.

## D-17 · Injection markers are phrases, not words, and the lookalike fixtures are why (FR-12)
The first FR-12 marker list carried bare `override`, `act as` and `your guidelines`. Writing the negative
fixtures exposed them immediately: "how do I override the default retry interval" and "can a webhook act as a
health check" are ordinary answerable questions that would have escalated, costing first-contact resolution
for no safety gain — customer text is already confined to `<ticket>` tags and the multi-word markers still
catch every engineered attack. The two questions are kept as `injection_lookalike` fixtures so the narrower
list cannot quietly widen again. `system:` and `assistant:` are kept despite the same risk (a pasted log line
would escalate), recorded as the marker most likely to over-fire on real tickets.

## D-18 · Fixture expectations are derived from the fixture text, never hand-written (FR-03, FR-12)
`expected_reason`, `expected_all_reasons` and the evidence lists (`secrets`, `contact_details`, `markers`,
`money_triggers`, `date_triggers`) are computed from each ticket's own words by the corpus generator and
checked again by the contract tests. The first hand-written version of the corpus claimed two triggers that
its text did not contain — `SYN-INJ-001` claimed the marker "you are now" when the body said "you are a", and
`SYN-MONEY-003` claimed "dispute" when the text said "disputing". A fixture that lies about itself is worse
than no fixture, because the rule it is supposed to prove will be written to satisfy the lie.

## D-19 · Engineered drafts ship with their retrieved passages (FR-12, FR-11)
Four of FR-12's checks and the FR-03 reply-side rule act on a **draft**, not a ticket, so row 2 also builds
`tests/fixtures/draft_replies.json`: eleven candidate replies, each carrying the passages it was supposedly
written from. That lets row 12 test grounding, private data, commitments and prompt leakage with no Chroma
index, no model call and no network. Drafts declare every check they should fail, in FR-12 §3.2 order, with
the primary reason derived from the first — several fail two checks honestly, and saying so beats pretending
each fixture isolates one rule. The `chunk_id` format (`DOC-BILL-001#1`) is provisional: row 5 owns it, and
either adopts this convention or these fixtures are updated with the one it chooses.

## D-20 · `must_not_auto_respond` means something narrower in the pack data than in this corpus
In the engineered corpus the flag is exactly `expected_route == "escalate"`. In the supplied data it is not:
87 of 189 escalate tickets carry it and 102 do not, because there it marks the four always-escalate intent
classes of FR-09 rather than every escalation. The corpus convention is the more useful one for guardrail
tests, but anything that mixes the two sources — a fairness table, an FR-09 count, the metrics report — has to
account for the difference. Recorded in `tests/fixtures/README.md` as well, where whoever writes those tables
will be looking.
