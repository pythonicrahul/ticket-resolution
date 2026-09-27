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

## D-21 · The conservative money and date triggers stay as they are (FR-03)
Decided by the author, 2026-09-27, on measured evidence: **0 of the 580 supplied tickets** (500 development,
80 validation) match any of the 25 money or 13 date triggers, so the over-escalation the spec worried about
(`dispute`, `promise`, `compensation`, `waive`, bare `eta`) costs nothing on this distribution. The zero cuts
both ways: the rule is entirely untested by real traffic, and its first live firing will be its first real
test, so `SYN-MONEY-010` ("any ETA on the docs update?") stays in the corpus labelled
`known_over_escalation` as the honest example of the cost. Revisit if a run escalates a ticket labelled
`auto_respond` for a money or date reason.

## D-22 · Grounding: the exemption list is the substance, and the overlap threshold is 0.3 provisionally (FR-12)
Set by Claude Code on the author's instruction ("configure as per your understanding for now"), from a
measurement over the 200 expert reference answers and the articles they cite — so it is a working default the
author has not yet endorsed on the merits, not a considered threshold choice.

A content-word overlap threshold applied to every sentence would reject 37–48% of what CloudServe's own
senior agents wrote, which would make the check unusable. The distribution is bimodal and the split is clean:
of 997 expert sentences, 351 are pleasantries and meta-sentences ("Thank you for getting in touch", "I would
suggest starting with the checks listed there") carrying no claim at all, and the 646 genuine claims sit much
higher (p1 0.25, p5 0.33, median 0.70). So **defining what counts as a claim comes first**; FR-12 §3.2.2 lists
the exemptions. Against the claim-bearing sentences only, 0.3 rejects 3.6%, 0.4 rejects 6.7% and 0.5 rejects
19.3%. 0.3 is chosen: the overlap check is a cheap floor against invention, and NFR-03's ≤5% hallucination
budget rests on the PR-03 judgement rather than on word counting.

Provisional for a specific reason: the measurement used **whole articles**, while retrieval will return
narrower chunks, which will push overlap down. Row 5 must measure again on real chunks, and the author
confirms or changes the number then.

## D-23 · A customer's own email address in a reply is still a leak (FR-12, NFR-04)
Decided by the author, 2026-09-27. NFR-04's "zero private data in outbound replies" is read strictly: the
draft is blocked even when the customer supplied the address themselves, because a reply may be read by others
on the account, and the guardrail blocks rather than redacts. The strict reading matches expert practice
exactly — **0 of the 200 expert reference answers** contain an email address or phone number — and costs
nothing on this data, since 0 of 580 tickets contain an address either. `SYN-PII-003` is the only fixture that
exercises it.

## D-24 · The role-label markers are anchored to the start of a line (FR-12)
Decided by the author, 2026-09-27. `system:` and `assistant:` stay on the injection marker list but match only
at the start of a line (after optional whitespace or a `>` quote marker). A role label at a line start is what
an injected transcript looks like; the same words inline are ordinary customer prose, and a pasted log line
("restart requested by system: worker-3") is the obvious false positive. Neither label appears in any of the
580 supplied tickets, so the anchoring has no measured cost. Both halves are held by fixtures:
`SYN-INJ-007` (line start, must escalate) and `SYN-INJ-LOOKALIKE-003` (inline, must not).

## D-25 · The decision log redacts what it must not store, and writes the row anyway (FR-13, FR-12, NFR-04)
The first version of the log rejected any row whose `reason` or `detail` looked like it contained private
data. The row 3 review showed that is reachable from ticket data, not just from a coding mistake: FR-12 §3.2.2
defines `detail` as including "the unsupported sentence", and an ungrounded sentence can perfectly well contain
an email address. Rejecting the row would mean a guardrail block with **no log row at all** — breaking FR-12's
acceptance criterion ("the block is recorded in the decision log") and NFR-05's 100% for exactly the tickets
NFR-04 cares most about.

So the store now redacts: the value becomes `[redacted:{pattern}]`, the pattern name goes into a `redactions`
column, and the row is written. Validation still *raises* for the six call-site mistakes (no requirement ids,
unknown decision or stage, escalation with no reason, model call with no prompt version, blank ticket id),
none of which ticket data can cause. The distinction is the point: **a wrong call fails loudly, wrong content
is cleaned and recorded.** Redaction here is about the log only — the reply itself is still blocked, never
redacted, which is what NFR-04 demands.

## D-26 · Reconciliation counts terminal rows, indexed by source_index, cross-checked against the run record (FR-13)
`auto_respond` and `escalate` are terminal: exactly one per ticket is what "logged decisions reconcile exactly
with tickets processed" means. `continue` rows are intermediate audit records and are not counted. Three
things make the check hard to fool: `source_index` coverage is authoritative (D-12), so duplicate ticket ids
reconcile honestly; a ticket with **no** index is reported rather than skipped, because otherwise one
forgotten `index=` argument would quietly turn the key off; and `start_run`'s `tickets_in` is compared against
the list handed to `reconcile`, so a ticket lost *before* that list was built cannot pass as ok. An id counts
as duplicated only when it has more terminal rows than the input had copies.

## D-27 · A log that cannot be written stops the run, and never loses the decision (FR-13, NFR-05)
One ticket failing must never stop a run (CLAUDE.md), but a log that cannot be written is not one ticket
failing: it breaks FR-13 and NFR-05 for every ticket in the run, and a per-ticket `except` would carry on
producing unauditable work. So the store retries once, appends the row to `<db>.fallback.jsonl`, and raises.
If the fallback cannot be written either — usually the same permission problem — the exception says so
explicitly rather than pretending the row was saved. Everything raised is a `DecisionLogError`, never a bare
`sqlite3.Error` or `OSError`, so the harness has one type to catch and cannot mistake an audit failure for an
ordinary ticket failure. Opening the log and writing the `runs` table behave the same way.

## D-28 · The decision log implements the Governance Framework's minimum record field for field (FR-13)
The pack's `03_Reference/Governance_Framework.docx` §1 specifies the record, and it was read on 2026-09-27,
after row 3 had been built against a reconstruction. Seven required things were missing: `decision_id`,
`model.version`, a generic `prediction` block, `threshold_applied`, `sources_used` **with scores**, the `block`
action, and a human-readable `explanation` distinct from the machine `reason`. The stage vocabulary was also
ours rather than the framework's. All are now implemented, and `governance_record()` projects a stored row into
exactly the framework's JSON so the two can be compared without translation (T-FR13-28).

Two of them are now **enforced**, not merely available: an `auto_respond` row must carry `threshold_applied`,
because the framework's confidence-floor guardrail is only demonstrable if the threshold that was applied is
recorded; and every terminal row must carry `explanation`, because the framework asks for "a human readable
explanation of why this action followed" and the Build Specification wants it "in language a support manager
could read". A log that cannot answer those is not auditable, which is the whole point of FR-13.

`block` is deliberately **not** terminal: a blocked reply is recorded as a block and the ticket still ends with
an `escalate` row, so reconciliation's "exactly one terminal row" holds while the metrics report can still
count "blocked by guardrails" as Build Spec §04 requires.

## D-29 · There are five guardrails, with the framework's names (FR-12, FR-03)
`Governance_Framework.docx` §4 names five: Private data, Grounding, Instruction integrity, Tone and scope,
Confidence floor. The FR-12 spec had four and called the fourth `commitments`. It is now `tone_and_scope` —
the framework's name, with commitments as its core — and `confidence_floor` is added: the routing threshold
must actually have been applied, and a missing confidence is not a high one, which is also FR-02's rule. It
reads the `threshold_applied` that FR-13 now requires, so the two requirements hold each other up: a reply
cannot be released without evidence the floor was enforced. Row 12 implements; the specs and backlog carry it.

## D-30 · Findings from the source documents are recorded, not silently folded into the PRD (process)
`docs/PRD.md` is source of truth #1 and its own front matter says changes go through the Stage 5 revision log,
so reading the pack did not become a quiet PRD edit. Everything found sits in `docs/pack_alignment.md`: what
was fixed in code, what was fixed in the specs, what the Stage 5 revision must record (A12 exists and was
untraced; the pack contradicts itself on whether the hidden set is 100 or 120 tickets), and the document work
only the author can do (risk register, incident response, the declaration, the kill-switch answers, retention).

## D-31 · LangGraph, LangChain and Pydantic, with the FR-15 guarantees kept in our own code
Decided by the author, 2026-09-27: use LangGraph for the pipeline, LangChain for the model calls and
Pydantic for the replies, because hand-rolled plumbing is fragile. A sweep confirmed the versions fit:
`langchain-openai` 1.6.6 resolves with `openai` 3.19.2, `langchain-core` 1.6.5, `langgraph` 1.2.12 and
`pydantic` 2.13.5 on Python 3.14, and a `StateGraph` over a Pydantic state with conditional edges works
(node exceptions propagate, so the harness still wraps each ticket).

How the pieces divide, which is the part worth recording:

* **LangChain makes the calls.** `LangChainTransport` wraps `ChatOpenAI` with `temperature=0`,
  `max_retries=0` and our timeout. LangChain owns the wire format and the message types.
* **FR-15's guarantees stay ours.** The retry schedule, the backoff cap, the circuit breaker and the
  prompt-version-keyed cache are not delegated, because A11 ("disconnect the provider entirely; the system
  degrades and continues"), NFR-08 (same input, same answer) and NFR-07 (zero spend) are requirements no
  library implements for us, and because `FakeTransport` then exercises that code for real in every test
  rather than replacing it. A client that leaned on LangChain's own retry would have left the code FR-15 is
  about untested.
* **Pydantic is where the fragility actually was.** `schemas.py` holds one model per prompt, matching the
  JSON shape the prompt text promises, and the validators enforce the prompts' own rules: a supported
  sentence must carry its quote (PR-03 rule 3), an unanswerable draft must say why (PR-01 rule 3), a
  handover must have both a summary and an uncertainty (FR-01). Parsing tolerates the prose and code
  fences models add, reports unexpected fields instead of dropping them silently, and allows exactly one
  repair attempt whose message never echoes the bad reply back into a prompt.
* **LangGraph arrives at row 14**, with a Pydantic state object, which is also where the pipeline must be
  shown to route every action through `DecisionLog.perform` (FR-13 §7).

## D-32 · The breaker is a state machine, and nothing untyped leaves the provider client (FR-15)
The row 4 review found three ways an untyped exception could escape `complete()` — a non-dict element in
`choices` (`AttributeError`), a corrupt cache row (`JSONDecodeError`) and a negative `Retry-After`
(`ValueError` from `time.sleep`) — each of which would have stopped a run, because a caller catching
`ProviderFailure` would not catch them and the harness's per-ticket guard cannot help with an exception it
was never told about. Every entry point now raises only `ProviderFailure` or `ValueError`, `_usable_text`
checks shape before content, the cache swallows and **counts** its own failures (including an unopenable
cache, which now degrades to no cache instead of killing the run), and every wait is clamped to
`[0, 60] s` so a hostile `Retry-After` can neither crash nor stall the run.

The breaker was also a counter pretending to be a state: half-open primed `consecutive_failures` to
`threshold - 1`, which left `circuit_open` reporting `True` for ever after a bad-key episode and could trip
the breaker on a single later timeout. It is now an explicit `closed / open / half_open` state with the
counter as evidence rather than a lever, a half-open trial gets exactly one attempt, and any failure
re-opens it whatever its type.
