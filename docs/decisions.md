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

**Re-measured at row 5, on the real chunks** (90 chunks from the 29 articles, 646 claim-bearing expert
sentences). The answer depends on what a sentence is checked against:

| support the sentence is checked against | 0.2 rejects | 0.3 rejects | 0.4 rejects | 0.5 rejects |
|---|---|---|---|---|
| the whole article (the original measurement) | 0.0% | 3.6% | 6.7% | 19.3% |
| every chunk of the expected article | 0.0% | 3.6% | 9.8% | 20.7% |
| a single chunk (the worst case) | 3.9% | **9.1%** | 17.2% | 29.1% |

So **0.3 stands**, with one design consequence for row 12 that the measurement made obvious: the overlap
floor must be computed against the **union of the retrieved passages**, not only against the one chunk a
sentence happens to cite. Checked against a single chunk, 0.3 would reject 9% of what CloudServe's own
senior agents wrote. The two questions are different and both are needed: *does the citation resolve to a
retrieved chunk* (exact, strict, FR-11) and *is the claim supported by the material we retrieved* (the
overlap floor plus PR-03). Conflating them would make a correct answer fail because it cited one section
while drawing on two.

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

## D-33 · The retrieval index fingerprints the embedder by its output, not by its name (FR-10)
Row 5's review found that `DefaultEmbeddingFunction.name()` returns the string `"default"` and
`get_config()` returns `{}` on chromadb 1.5.9, so the first version of the index fingerprint could not tell
all-MiniLM-L6-v2 from whatever a future chromadb ships. A version bump would have served a persisted index
built with a different model: cosine scores that mean nothing, a threshold chosen at row 7 that no longer
applies, and no signal anywhere. The fingerprint now embeds one fixed probe string and hashes the resulting
vector, so a different model always produces a different fingerprint and the index is rebuilt. The
configured `EMBEDDING_MODEL` is in the signature too, which also stops that setting being dead code.
T-FR10-21 is the test: two embedders with the same name and config but different vectors must not share an
index.

## D-34 · Chunking is markdown sections, and nothing is dropped in silence (FR-10)
Measured before deciding: the 29 articles split on `##` headings give 145 sections, median 157 characters,
longest 658, none above 800. So a chunk is a section — a coherent answer unit, which the Build Specification
asks us to be able to justify — with the article title prepended as context, sections under 120 characters
merged (the heading of the **larger** part surviving, because the heading is embedded with the text), and
anything over 800 characters split with the pack's own 800/120 numbers. `chunk_id` is `DOC-BILL-001#1`,
which settles the format row 2's draft fixtures had assumed.

The review also found two silent-loss paths, both now closed: a document with no id or no content was
skipped without a word, and two documents sharing a `doc_id` aborted the whole build over colliding chunk
ids — losing all 29 articles instead of one. `IndexStats.skipped` now reports every skip with its reason,
`indexed_documents` says how many made it, and a duplicate keeps the first occurrence. The corpus is the
only source of answers, so a partial index has to be visible rather than implied by a smaller chunk count.

## D-35 · The sweep reports top-1 accuracy, precision against its own ceiling, and segment sizes (FR-10, NFR-06)
The first sweep report had three ways to mislead the person choosing the threshold, all found in review.
Precision at k counted correct passages over answerable tickets but divided by passages returned for *all*
tickets, so the column moved with the denominator; it has no attainable maximum near 100% (70.5% at
top_k=5 on this corpus, because an expected article has only so many chunks); and it rises as a higher
threshold returns fewer passages, which is not better ranking. The report now computes precision over one
population, states the ceiling, and adds **top-1 correct** — the column that answers the question the
threshold actually turns on, whether the drafter would see the right passage first. It is 89.9% at
threshold 0.

The fairness table now carries sample sizes and a noise caveat, and reports tier as well as fluency, because
the Governance Framework's fairness audit names both. The finding that matters: the fluent/non-fluent gap is
4.3 points at threshold 0 and **widens as the threshold rises** — 7.8 at 0.30, 12.3 at 0.60 — so the
threshold is a fairness decision as well as a quality one, and NFR-06's 5-point limit is breached by
retrieval alone at 0.30 and above. With the smallest bucket at n=87, a few points is inside sampling noise;
the report says so rather than quoting decimals as though they were precise.

## D-36 · The per-ticket guard covers the whole of one ticket, and an invalid call is not a broken log (FR-14, FR-13)
Row 6's review found the harness's isolation guard wrapped only `pipeline.process()`. Building the log row
and writing it sat outside it, so a pipeline that returned a `dict`, or an `Outcome` that FR-13 rejects — an
escalation with no reason, an unknown stage, an `auto_respond` with no `threshold_applied` — killed the entire
run. Worse, `InvalidDecision` is a `DecisionLogError`, so it surfaced through D-27's "the log cannot be
written" path: a component's bug about one ticket was reported to the operator as an unwritable log, with 76
of 80 tickets never attempted. That is the exact failure mode Build Specification §08 lists and A9 tests.

The guard now covers the whole of one ticket's handling, and the two failures are separated: **`InvalidDecision`
is one ticket's problem** (escalate it with a row that is valid by construction, carry on), while
**`DecisionLogUnavailable` is the run's** (stop, because unlogged processing breaks FR-13 for every ticket).
A pipeline that returns the wrong type is treated the same way as one that raises. T-FR14-19 covers six
variants; none of them existed before, which is why the gap survived.

## D-37 · A fairness figure that can only say "pass" is not a measurement (FR-14, NFR-06)
`variation_points` was computed over only the segments *not* flagged low-confidence, and emitted `0.0` when
fewer than two survived — so the markdown printed "Variation across segments: **0.0 points** (NFR-06 allows
under 5)" for a run where nothing had been measured at all, and silently excluded the 8 enterprise tickets
that are the segment NFR-06 names. It now includes every segment that has tickets, reports `null` with "not
measurable" when there are fewer than two, names the small segments beside the figure, and says whether the
variation is within or **above** the limit. Segments also carry retrieval hit rate, not only the answered
rate, because NFR-06 is about resolution rate *and quality*.

The same principle runs through the report after this row: `_pct` returns `None` rather than `0.0` for an
empty denominator, route agreement says in the report that it does not measure routing while everything
escalates, the FCR and escalation figures declare that 100% escalation is by construction until row 14, and
retrieval hit rate states that it has no false-positive counterpart. The report is the artefact an assessor
reads; a number that overstates what was measured is worse than a gap that names itself.

## D-38 · The relevance threshold is 0.25 (FR-10, checkpoint row 7)
Decided by the author, 2026-09-28, from `evaluation/reports/retrieval_sweep.md` over the 500 development
tickets. `RELEVANCE_THRESHOLD=0.25`.

Why this value and not a higher one, in the order the evidence came in:

1. **It costs nothing.** Hit rate on answerable tickets stays at 95.2% and answerable tickets lost stays at
   4.8% — identical to having no threshold at all. Top-1 accuracy is unchanged at 89.9%.
2. **Every decision it makes is right.** Of the tickets it leaves empty, 100% are genuinely unanswerable: 8
   tickets that would otherwise have been answered from passages that were not relevant. At 0.30 that
   precision falls to 85%, and it keeps falling — 66% at 0.45, 37% at 0.60.
3. **It is the last value that satisfies NFR-06.** The fluent/non-fluent hit-rate gap is 4.3 points at 0.25
   and 7.8 at 0.30, over the 5-point limit. Raising the threshold makes the system worse for non-fluent
   English faster than it makes it safer, which is what the Governance Framework's fairness audit predicts of
   retrieval.

**What this threshold is not for.** It cannot deliver the ≤30% escalation target and must never be raised to
chase it. 29% of the development tickets are unanswerable, but even at 0.60 — where a third of answerable
tickets have lost their article — only 36% of tickets return nothing and only 37% of *those* are genuinely
unanswerable. Retrieval's "nothing is relevant" signal does not separate the two populations. The escalation
rate is FR-02's confidence threshold and FR-12's grounding check doing their jobs.

**What the choice accepts.** 135 of the 143 unanswerable development tickets still retrieve something, so the
later stages must catch them: grounding has to fail on a draft built from irrelevant passages, and the
confidence threshold has to escalate a weak classification. If those do not hold at rows 9 and 12, the fix is
to strengthen them, not to raise this number.

## D-39 · Cross-validation folds are grouped by wording cluster, because this dataset repeats itself (FR-08)
The 500 development tickets hold only **215 distinct bodies**, which collapse into about **96 clusters** once
near-identical wordings are merged, and every repeated body carries the same intent. Row-wise
cross-validation therefore trains and tests on the same words, and the score measures near-duplicate lookup
rather than classification.

Measured with the real embedder, the same model and the same data:

| folds | intent accuracy |
|---|---|
| row-wise | 99.6% |
| grouped by exact body | 94.0% |
| grouped by wording cluster | **88.6%** |

The headline is the last one. Grouping by exact body was the first fix and it was not enough: 168 of the 215
distinct bodies have another *distinct* body with the same intent above 0.85 character similarity ("a restore
we started last Friday…" against "a restore we started yesterday morning…"), so exact-body folds still test
on a paraphrase of something they trained on. Clusters are built by union-find over that similarity, and the
row-wise figure is kept beside the grouped one so the size of the leak stays visible in the report.

Two consequences worth stating. First, **99.6% was never a real number** and reporting it would have claimed
NFR-03 was comfortably beaten. Second, the fold count printed beside "grouped" must be the count actually
used — a class with four distinct wordings cannot be split five ways — because printing the requested count
misdescribes the measurement.

## D-40 · The stated confidence is calibrated on its own, and logistic rather than isotonic (FR-08, NFR-03)
NFR-03 asks that *stated confidence* be within 5 points of observed accuracy. That is a claim about the one
number the system reports, not about the whole probability distribution, and per-class sigmoid calibration
over 22 classes does not deliver it: it left the classifier stating 72% while being right 100% of the time, a
30-point gap in the direction that matters, because a 0.80 threshold would then escalate work the system gets
right every time.

So a one-dimensional fit maps the model's top probability onto observed correctness. **Logistic, not
isotonic**: isotonic's flat regions emit *exactly* 1.0, and on this data that meant 488 of 500 tickets stating
certainty from a 94%-accurate classifier, with four distinct confidence values in total — nothing for FR-02 to
threshold on. The logistic fit gives 213 distinct values and never saturates.

Two mistakes were made and fixed on the way, both of which produced flattering numbers:

* The calibrator was first **scored on the predictions it was fitted on**, reporting a 0.0-point gap. The
  table is now cross-fitted: each half scored by a calibrator fitted only on the other, split by wording.
* The estimator that produced the measured probabilities was **not the estimator that ships** — a different
  inner calibration CV shifted the top-probability distribution by 7.5 points, so the calibrator was fitted to
  a distribution the shipped model does not produce. Both now come from one factory, pinned by T-FR08-15.

Where it lands: the band holding 475 of 500 predictions is within **2.3 points**; one band of exactly 25
predictions is 42.6 points out. The report states both. Whether that is acceptable is the author's call at
checkpoint row 10.

## D-41 · Urgency has a ceiling of 73% and `low` is effectively unreachable (FR-05)
The urgency classifier reaches 48.4% on wording it has not seen. Before reading that as a failure: **67
ticket bodies in the development set carry more than one urgency label**, so no model that sees only the text
can exceed **73.0%**. The gap between 48% and 73% is the model's; the gap between 73% and 100% is the
labelling's, and the Stage 1 notes already said urgency was labelled inconsistently.

Within that, `low` is effectively unreachable — 0.8% recall across 128 tickets, even with class weighting —
because its embedding centroid sits 0.960 cosine from `medium`. `high` is separable (0.854 from `low`) and is
the level that matters for the queue. **FR-05's three levels are in practice two**, and the escalation queue
should be read that way: it orders correctly, it just cannot often tell low from medium.

This is reported rather than fixed. Options if the author wants better: rules for urgency instead of a
classifier, merging low and medium into one level, or relabelling. All three are decisions about the product,
not the code.

## D-42 · Row 9's review: three fixes that each had a way of looking fine (FR-16, FR-03, FR-02)
Written after the independent review of row 9. All three were code that read correctly and behaved wrongly.

**The kill switch failed *open*.** `Settings.kill_switch_on` and `Router._switch_is_on` both used
`Path.exists()`, which is `os.path.exists` and swallows every `OSError` to return `False`. A switch file
inside a directory the process cannot stat therefore read as **off**: an operator would `touch
storage/KILL_SWITCH`, believe automation was stopped, and the run would keep answering with
`kill_switch=false` on every row — and the governance declaration written from FR-16 §2 would be false.
There is now **one** implementation, `Settings.kill_switch_on`, using `stat`: absent is off, unreadable is
on. The test that was supposed to cover this put a *directory* at the switch path, which `exists()` reports
as present, so it passed while the fail-safe branch was dead code; T-FR16-6b now makes the stat call
genuinely fail (skipped as root) and T-FR16-6c fails if routing ever re-implements the check.

**FR-03's money rule missed plurals and hyphens.** The trigger tables are singular and space-separated, and
matching was literal, so "please issue refunds for both accounts", "we are claiming the service credits",
"these disputes", "we will raise chargebacks", "a write-off of the balance" all routed to `auto_respond` —
the PRD's must-escalate list, answered automatically. `route.matches_triggers` now also accepts a plural
(`(?:e?s)?`) and a hyphen where the phrase has a space, and it is the **single** implementation: the fixture
module used to carry its own copy, which is how the fixtures agreed with the rule that a plural was
answerable. Re-measured after the widening: **still 0 of the 580 supplied tickets** match any trigger, so
D-21's conclusion survives. `SYN-MONEY-013` (plural) and `SYN-MONEY-014` (hyphen) are the regression cases.

**`unknown_intent` was a rank in no precedence table.** Row 9 introduced it at rank 6, which silently moved
FR-03's ranks and would have broken row 12's fixtures, whose `expected_reason` is derived from the tables
(T-FR12-21) — exactly the drift D-16 was written to prevent. It now sits **below** the money and date rules,
where every rank already written down keeps its place, and it is in D-16's table (FR-12 §3.4), FR-02 §3 and
FR-09 §4. A reason with no rank is refused rather than ranked last, because the alternative is a primary
reason that depends on evaluation order.

Also from the same review: `RoutingDecision.log_fields()` now carries `prediction_confidence` (a row that
records the floor but not the number compared with it cannot answer "was this right?") and merges the
classification's half itself, because both halves fill `detail` and splatting them raises `TypeError`;
`CONFIDENCE_THRESHOLD=0.0` from the environment logs a warning (it answers 415 development tickets, 10 of
them must-escalate); the sweep refuses any `--input` that is not `TRAINING_TICKETS_PATH`, with no override;
and a missing classification logs `unknown_intent` rather than the spec's earlier claim of `unclear_request`,
because putting a prediction no model made into the log misreports what happened.

## D-43 · The confidence threshold is a fairness and a calibration decision, not only a quality one (FR-02)
`evaluation/reports/confidence_sweep.md` is written for checkpoint row 10 and says three things that the
author has to weigh together, because optimising any one of them alone picks a bad T.

1. **T is not the escalation lever it looks like.** At T = 0, 17% of tickets still escalate on rules T cannot
   move. The report's right-hand column counts only the tickets whose *sole* reason is confidence.
2. **FR-09's rule is exact but fires on the predicted intent.** On grouped out-of-fold predictions, **10 of
   500 development tickets labelled `must_not_auto_respond` would be answered** — nine `security_incident`
   read as `account_access`/`api_key_issue`, one `unclear_request` as `database_issue` — all stating 0.79 to
   0.85 confidence. T ≥ 0.85 catches every one of them. On in-sample predictions the column reads 0 at every
   T, which is how the script read before the basis was fixed: the row-8 leak (D-39) in a new place.
3. **The two T values the wrong-answer columns favour breach NFR-06.** The fluent/non-fluent answer-rate gap
   is 10.1 points at T = 0.85 and 13.8 at T = 0.90, against NFR-06's 5-point limit, and the confidence being
   thresholded is **42.6 points out of calibration** in the 0.60–0.80 band (25 predictions stating 78.6%,
   right 36.0% of the time) — the band the must-escalate leaks sit in.

The report chooses nothing and the placeholder stays at 0.80. Whether to accept a 2% must-escalate leak, a
64% answer rate, or an NFR-06 breach is the author's call at row 10, and the PRD revision should record it.

## D-44 · The author sets T = 0.85, and the NFR-06 breach is declared rather than hidden (FR-02)
Checkpoint row 10, decided by the author from `evaluation/reports/confidence_sweep.md`.

**0.85 is the lowest T at which no must-escalate ticket is auto-answered on unseen wording.** The ten leaks
at lower thresholds state 0.7929 to 0.8452 confidence (D-43), so 0.85 clears all of them and 0.80 clears one.
The PRD's criterion for FR-09 is "zero auto-responses to tickets labelled `must_not_auto_respond` in any run",
and a security incident answered by a robot is the failure both Marcus and Daniel described in discovery.

**What it costs, on the development set:** 64.4% of tickets answered (35.6% escalated, against the PRD's
hoped-for reduction), 84 tickets answered although their label says escalate, and — the item that must be
declared — a **10.1-point gap between fluent and non-fluent answer rates (66.8% vs 56.7%), against NFR-06's
5-point limit**. Populations are 380 and 120. This is the second threshold in the system whose fairness cost
rises with its safety (D-35 found the same shape in retrieval), and it is now the binding one.

**Consequences that follow from this choice and are not optional:**
1. The PRD revision (Stage 5) records the NFR-06 breach, its size, and this trade-off. Declaring it is the
   condition on which the choice was made.
2. `evaluation/harness.py` already prints both thresholds in use, so a gate run shows 0.85 rather than a
   placeholder. The segment tables carry the fluency gap into every metrics report (NFR-06's own measure).
3. **Targeted floors stay open as the way out.** A higher floor only where a must-escalate intent is among
   FR-08's alternatives could restore answer rate without reopening the leak. It is unmeasured — the
   out-of-fold alternatives are not saved — and the author chose the simple threshold first, deliberately.

## D-45 · The free-model roster turned over, and the free tier cannot finish a gate run (NFR-07, FR-14)
Checked against `https://openrouter.ai/api/v1/models` on 2026-09-28, before the first real provider call.

**Every free id this repository had suggested was gone.** `.env` held `meta-llama/llama-3.1-8b-instruct`
*without* the `:free` suffix, which on OpenRouter is the **paid** endpoint — NFR-07 allows no spend, so the
first real call would have been both a charge and a requirement breach. `.env.example` suggested the same id
with the suffix, and that id no longer exists either; nor do llama-3.3-70b, deepseek-r1 or qwen-2.5-72b. Of
458 catalogued models, 21 are zero-cost today.

**Chosen, with the reason in `.env.example`:** `google/gemma-4-31b-it:free` for `MODEL_NAME` (dense
instruction-tuned, 262k context, advertises structured outputs, which is what `complete_structured` needs for
`schemas.AnswerDraft`) and `qwen/qwen3.8-27b:free` for `JUDGE_MODEL_NAME` — a different vendor and family on
purpose, because FR-12's grounding check is not independent if the judge is the model being judged.
`openrouter/free` is rejected despite being free: it is a router alias that picks a model per request, so the
same input can reach different models and NFR-08's determinism is gone.

**The operational finding, which belongs in the PRD revision.** OpenRouter's free tier allows 20 requests a
minute and **50 a day**, rising to 1000/day only for an account that has purchased $10 of credits at any
point. A gate run over the 80 validation tickets needs roughly 100 calls at T = 0.85 — a draft and a
grounding check for each of ~51 answered tickets — so **the free tier as configured cannot complete one gate
run in a day**. Three ways out, none of them code: buy the $10 once, batch the run across days, or rely on
the response cache (which makes re-runs nearly free but does nothing for the first pass). Row 15 has to
account for this, and `scripts/provider_smoke.py` now warns when a configured id has left the free roster
rather than retrying a 404 three times.

## D-46 · The first real provider calls: what the fakes could not have told us (FR-15, NFR-07)
`scripts/provider_smoke.py`, run 2026-09-28 against the configured free models. Everything in
`provider.py` had been tested against `FakeTransport` only, so this was the first time the system talked to a
model at all. Four checks; three passed, and the failures were the useful part.

**What works.** A plain completion returns (`'The connection works.'`) and needed `attempts=2` — FR-15's
retry earned its keep on the very first real call. The response cache replays an identical call with zero
provider requests and survives across processes, so NFR-08's determinism plumbing is real and not a
fake-transport artefact. A nonexistent model id comes back as a typed `ProviderError` at status 400 with no
retry storm: a configuration mistake is not treated as an outage.

**`google/gemma-4-31b-it:free` is unusable and `qwen/qwen3.8-27b:free` is intermittent.** Chosen on paper for
its structured-output support (D-45), gemma answered 429 to every single request. Qwen answered once, then
also began refusing. The cause is not our account: `GET /api/v1/key` reports `is_free_tier: true`,
`usage: 0`, `limit_remaining: 5`, and the 429 body says **`limit_source: upstream_provider_shared_pool`** with
no `Retry-After`. The free endpoints route through a pool shared by every free user, and at this time of day
it rejects nearly everything: 20 consecutive live attempts over four minutes, spaced 45 seconds apart, all
throttled. **Buying credits would not fix this** — the $10 tier raises OpenRouter's own 50/day cap (D-45),
and this limit is upstream of that.

**So `complete_structured` into `AnswerDraft` is still unverified**, and it is the one thing row 11 is built
on. That is a gap in what we know, not a defect we have found: no request reached a model.

**One defect was found and fixed.** Every 429 became the same sentence, "the provider rate-limited this
request", because `_map_provider_exception` drops the provider's body — deliberately, since a 4xx body can
echo the request (NFR-04). But a decision log reading `provider_unavailable` then cannot distinguish a busy
shared pool (wait) from an exhausted account allowance (stop and fix the account), and those need opposite
responses. `RateLimited` now carries `limit_source` and `provider_name` — two short provider-side
identifiers that cannot contain a ticket — while the body's free text stays unread. T-FR15-34 plants a secret
in the body's `raw` field and fails if it ever reaches the message; T-FR15-35 covers bodies of every other
shape.

**Where this leaves the runtime model.** Three ways forward, none of which costs money, and the author picks:
1. **Groq** (`LLM_BASE_URL=https://api.groq.com/openai/v1`, already documented in `.env.example`): a real free
   tier with per-account limits rather than a shared pool — `llama-3.3-70b-versatile` and
   `openai/gpt-oss-20b` are the candidates. This is the recommendation: the failure mode we hit is
   specifically *shared-pool* saturation, and a per-account limit does not have it.
2. **Bring your own key to OpenRouter** (Settings → Integrations): free models then run against your own
   upstream allowance, which is what the 429's own `remedy_hint` suggests.
3. **Accept it and batch**: the cache makes re-runs nearly free, so a gate run could be assembled over
   several sittings. Honest, but it makes row 15's "unattended run" claim awkward.

Row 11 is not blocked by any of this: CLAUDE.md requires its tests to pass with no network and no key, so it
is built against `FakeTransport` either way. The decision is needed before row 15's gate run.

## D-47 · Groq, not OpenRouter, and the models are the account's own (NFR-07, FR-15, FR-11)
The author moved `LLM_BASE_URL` to Groq after D-46. Everything below was measured, not remembered.

**The switch works, and it fixes the failure mode.** OpenRouter's free endpoints share one pool across all
free users; Groq's free tier gives **per-account** limits — 1000 requests/day per model and 8000 tokens/minute
— so the `upstream_provider_shared_pool` rejection that made a gate run impossible does not arise. No code
changed: the provider client is OpenAI-compatible and never knew which host it was talking to.

**The model ids had to change, and could not be guessed.** `vendor/model:free` is OpenRouter syntax; it does
not exist on Groq. The account's live catalogue has 11 active models, and `llama-3.3-70b-versatile` — which
Groq's own documentation page lists — is **not** among them, so the catalogue is account-specific and the
docs are not a substitute for reading it.

**Chosen:** `MODEL_NAME=openai/gpt-oss-120b`, `JUDGE_MODEL_NAME=qwen/qwen3.8-27b` (a different vendor, because
FR-12's grounding check is not independent if the judge is the model being judged). `openai/gpt-oss-20b` is
the fallback: it passes every check too, and since both models share the same 8000 tokens/minute budget the
larger one costs nothing extra.

**`complete_structured` is verified against a real model at last.** Both gpt-oss models returned a valid
`AnswerDraft` **on the first attempt, with no repair**, citing `DOC-BILL-001#2` correctly — the row-11
dependency that D-46 could not test. The replies are recorded in
`tests/fixtures/recorded_provider_responses.json`, so row 11's tests replay a real model's JSON while still
running with no network and no key.

**The binding limit is tokens, not requests.** 8000 tokens/minute against a drafting call of roughly 1300
tokens is about six calls a minute, so a gate run over 80 validation tickets (~100 calls at T = 0.85) takes
15 to 20 minutes and must pace itself. A harness that bursts will collect 429s and escalate good tickets as
`provider_unavailable` — correct behaviour producing a meaningless gate result. Row 14's wiring has to allow
for it, and row 15 should expect a run measured in tens of minutes.

**Noted, not adopted:** the account also serves `meta-llama/llama-prompt-guard-2-86m`, a prompt-injection
classifier that looks directly relevant to FR-12's `instruction_integrity` check. It is not adopted, and the
reason is not quality: a guardrail that needs the provider stops working during an outage, and CLAUDE.md
requires guardrails to run on every reply. Pattern matching (D-24) keeps working when the model does not.

## D-48 · Drafting refuses more than it writes, and the review found three ways round it (FR-11, FR-06)
Row 11. The component that finally calls a model is mostly rules about not sending things.

**What it refuses.** A citation outside this ticket's retrieval kills the whole draft rather than being
dropped, because dropping it leaves the sentence standing with nothing behind it. A sentence with no
citation does the same — stricter than the PRD's "citation accuracy ≥95%", which measures a reviewed
sample, while this decides whether a reply reaches a customer at all. `answerable: false` is a correct
outcome, not a failure. **This strictness is the author's to confirm** (FR-11 §7): if it escalates too much
in practice, the alternative is to drop the uncited sentence and send the rest, which is a different
artefact from the one the model wrote.

**The independent review found three highs, all fixed:**
1. **An unretrieved id could still reach the customer, inline in the prose.** PR-01 rule 2 shows the
   citation as `[DOC-AUTH-001#2]`, so a model following the example writes the id into the sentence text —
   and the sentence text is what the customer reads. Only the `citations` array was checked. A reply could
   therefore cite `DOC-BILL-009#1`, which retrieval never returned, while passing every test: none of the
   fixtures, and not the recorded real reply, happened to contain a bracket. Both are now checked, as is a
   chunk of a retrieved article that was not itself retrieved.
2. **The drafter's "never raises" docstring was false.** `complete()` raises a bare `ValueError` when
   `MODEL_NAME` is blank, which is not a `ProviderFailure`. With a mistyped `.env`, every answerable ticket
   would have escalated through the harness's generic exception handler with an opaque reason instead of a
   typed row. Now `drafting_failed`, with the exception type in `detail`.
3. **`model_calls` under-reported real requests.** The repair attempt inside `complete_structured` logged
   two provider requests as one, and a failed draft logged up to four retries as zero. On a free tier that
   column is what says whether a run fits the allowance (D-47's 8000 tokens/minute), so it has to be true.

**Mediums fixed:** the row now names the prompt actually loaded and carries its fingerprint, rather than a
module constant a constructor argument could contradict; a prompt template declaring a slot this code does
not fill is refused instead of sending the literal `{subject}`; passage text and titles are escaped like
ticket text, since an article containing `</passage>` could otherwise present the model with content
attributed to a legitimately retrieved id; an escalating row falls back to a generic explanation rather than
being refused by FR-13 at `record()` time, which would mean escalating with nothing written down.

**My own three mistakes while fixing those**, each caught by the tests: the placeholder guard first compared
against the *rendered* text, so an article titled `A "{text}" title` looked like an unfilled slot; the
`PromptError` was raised outside the guard it was meant to be caught by; and a loop over four failure
reasons shared one response cache, so it tested the first reason four times.

**The prompt loader is now real.** `prompts.py` was a one-line placeholder while `prompts/README.md`
promised "code loads prompts by id and version". It reads the file, splits SYSTEM/USER, fingerprints the
sent block, and refuses a file whose shape it cannot read confidently — including a bad-output example shown
above the real prompt, which taking the first fenced block would have loaded silently. It serves both shapes
in the register: the build prompts with roles, and the development and evaluation prompts without.

## D-49 · The disclosure, and what it may not say (FR-06)
The three lines FR-06 requires are module constants marked `DISCLOSURE_VERSION = "v1"` and appended in code,
never shown to the model, so a reply cannot lose its disclosure to a paraphrase or an injection attempt.

**No contact details are invented.** The corpus carries none — 0 of 200 expert answers contain an email,
phone number or URL (measured at row 2) — so "how to reach a person" is *reply to this message*, which is
true on all four channels. Inventing a support address would be a fabricated fact in an outbound reply,
which is the thing FR-11 exists to prevent.

**The source line names articles, not chunks:** `Based on: Invoices and usage breakdown (DOC-BILL-001)`. A
chunk ordinal means nothing to a customer; the decision log keeps the chunk id, which is what makes the
citation checkable.

**For the author:** the wording of both constants is a business decision and should be read by Marcus or
Ravi before a customer sees it. There is no greeting and no sign-off, because the ground-truth replies have
none — adding "Hi {name}" would reintroduce the customer's name into outbound text, which FR-11 keeps out of
the model's sight entirely.

## D-50 · The author's three answers on drafting: send uncited sentences, keep the wording, add a greeting (FR-11, FR-06)
The open questions row 11 raised, answered by the author.

**1. An uncited sentence is sent, not refused and not dropped.** The reply stays the artefact the model
wrote. This is a deliberate relaxation of what row 11 shipped, and its cost is stated rather than hidden: a
sentence with nothing behind it can now reach a customer, and **FR-12's grounding guardrail at row 12 is the
only thing standing in its way**. Two locks remain — a citation that *is* given must still resolve to a
passage retrieved for this ticket, and a draft citing nothing at all is still refused (`no_cited_article`),
because FR-06's "names the article(s) it came from" cannot be met by a reply that came from nothing. The
`generation` row's `detail` records that an uncited sentence went out, so a reviewed sample can go straight
to the replies that carried one. Revisit if row 12's check lets unsupported sentences through.

**2. The disclosure wording stands** as written at row 11 (`DISCLOSURE_VERSION = "v1"`). It should still be
read by Marcus or Ravi before a customer sees it; that is a review, not a blocker.

**3. Replies open with a greeting**: `Hi {first name},`, inserted by code from the ticket. **The name still
never reaches the model** — FR-11 §3.2 keeps it out of the prompt so a draft cannot be steered by it, and
that does not change. The first name only, because a full legal name reads like a form letter. A name field
holding markup, an email address, a newline or anything over 60 characters falls back to `Hello,`: a broken
name field is a data problem, and "Hello," is always correct.

## D-51 · Row 12's review: the guardrails were checking a copy of themselves (FR-12, NFR-04)
Row 12 shipped the five Governance Framework checks and moved the pattern tables out of the tests, which
FR-12 §7 asked for. The independent review found that the move fixed the ticket-side half and left the
draft-side half unchecked, and that the unchecked half disagreed with the code.

**The corpus said clean, the code said blocked.** `tests/fixtures/draft_replies.json` exists, in FR-12 §2's
own words, "so every post-draft check can be tested offline with no model call" — and no test had ever run it
through `check_draft`. Run through it, **both drafts the corpus declares clean were blocked**. Two causes,
both worth more than the fixture:

1. **"A draft that says plainly it does not know passes" was never implemented.** FR-12 §3.2.2 says so in
   those words; an honest refusal has ~0 overlap with any passage, so the floor blocked exactly the answer
   the PRD asks for. Refusals are now an exemption, listed in the spec. The test that claimed to prove this
   asserted it with a *supported factual sentence*, so it was green while the behaviour was broken.
2. **FR-06's mandatory lines were exempted by copied string literals.** The disclosure, the human route, the
   greeting and the source line each have ~0 overlap, so the only thing keeping mandatory text under the
   floor was a prefix typed twice in two modules — and FR-06 explicitly allows that wording to change behind
   `DISCLOSURE_VERSION`. The day it changed, **every reply in a run would have failed grounding**. The
   fixtures already carried an older wording, which is what surfaced it. Now matched against `generate.py`'s
   constants and by shape.

**The assessor-facing projection crashed on every guardrail row.** `log_fields()` emitted
`[name, passed, detail]` triples; FR-13 §2 declares pairs and `governance_record()` unpacks two. So
`governance_record` raised `ValueError` on exactly the rows FR-12's acceptance criterion requires to exist.
Fixed both ways: the row carries pairs and the details moved into `detail`, and the projection now reads
`result[:2]` so a future writer cannot crash it either.

**Three more that could each release something.** PR-03 was asked about *exempt* sentences, where its own
rules give it no lawful answer — `supported` demands a quote, a pleasantry has none, and the schema rejects
supported-without-quote — so any reply containing "Thank you for getting in touch" could block; only the
claims are sent now. `grounding` read `sentences` without checking they reconstruct `reply`, so a caller
passing an empty list silently disabled the one check D-50 made load-bearing; that is now a failure. And
`overlap` returned **1.0** for a sentence with no content words — a fail-open default in the one arithmetic
guard, now 0.0.

**Patterns, in both directions.** `PHONE` required a leading `+` and missed `0207 946 0123`, `(212)
555-0199` and `555-0100`; `NATIONAL_ID` was dashes-only and missed `123456789`. Widening `PHONE` then
matched a 16-digit *invoice reference*, which `SYN-PII-LOOKALIKE-002` exists to catch — so it counts digits
now (7 to 15, E.164's ceiling). `PRIVATE_IP` matched `version 10.1.2` and now requires four octets.

**Also fixed:** a judge that answered badly is `check_error`, not `provider_unavailable` (one is prompt
drift, the other is FR-15 availability, and they need opposite responses); a failed check reports the
requests it cost, the same defect the row-11 review fixed in `generate.py`; `all_reasons` is written, so a
draft failing three checks loses none of them; the draft is escaped before it enters PR-03's prompt, since
it is model output derived from customer text; and a reply that already failed `private_data` is **not**
sent to the provider — §3.1.2's rationale is that a secret must never be transmitted, and a reply that is
certainly blocked does not need transmitting to confirm it.

**What the review confirmed holds:** no flag, kwarg, env var or `except` can skip a check or turn a failure
into a pass; no matched private value reaches any detail, report or log row; the overlap floor is computed
against the union of retrieved passages (D-22), and uncited sentences are checked (D-50); verdict indices
fail safe in every direction; and `guardrails.py` is now genuinely the only copy of every table.

## D-52 · The handover: what the model may not write, and what it may not be shown (FR-01)
Row 13. FR-01's criterion is coverage — *100% of escalated tickets carry a non-empty summary and
uncertainty reason* — so every failure path in `handover.py` ends in a note rather than an exception, and
the template is the guaranteed path rather than a degraded one. The discovery line this answers is the one
nobody said out loud: **all 108 repeat contacts in the supplied data are on escalated tickets.**

**The uncertainty is code's sentence, never the model's.** Found by running PR-02 against the real model on
`DEV-0015`: the note came back well written except for the field that matters most, which read *"the system
flagged the intent as a security incident but required escalation (must_escalate_intent)"*. That is the
reason code echoed back into the sentence a human reads, and the PRD asks for "a plain statement". The
system knows exactly why it escalated; the model can only guess. It still writes the summary, the goal, what
was tried and where to start — PR-02 keeps asking for the uncertainty because writing it holds the model's
attention on the escalation reason.

**The severe finding: the check and the payload read different strings.** `_withhold` tested `ticket.text`,
which FR-07 caps at 8000 characters, while the prompt renders the raw `subject` and `body`. So a secret or an
injection marker **past the cap** was invisible to the check and transmitted anyway — and written to the
response cache on disk. A handover is the one component that always meets truncated tickets, because
`text_truncated` is itself an escalation reason, so this was not a corner case. Both rules now test every
string the prompt can carry. The two tests that covered them used short bodies and saw nothing.

**A secret in a ticket subject reached the decision log.** FR-13 truncates `summary` but deliberately does
not scrub it (FR-13 §7 leaves that to the author), and the template interpolated the raw subject. PR-02's
rule 5 — *"if you see anything that looks like a password, key or token, write `[secret present in ticket]`
instead"* — is now applied by code on the template path, where no model is there to apply it. An ordinary
name or address in a summary is still stored: that is the author's open question, not a defect.

**Also fixed:** the row now carries the intent, urgency, confidence and retrieved articles the PRD names,
and claims `FR-05` only when an urgency actually reaches it; the row names the prompt that was sent rather
than a module constant; the prompt is loaded on first use, so a missing file degrades every note to the
template instead of aborting the run at construction; a template that stops wrapping the ticket in
`<ticket>` is refused, because the slot guard could not see customer text being concatenated into the
instruction block; slots are filled in one pass, so a subject of literally `{body}` cannot expand into it;
and `_first_sentence` no longer reads "Dr." as a whole sentence.

**Tests that were passing for the wrong reason:** the injection case passed both because of the text and
because of an injected reason, so either half could be deleted; the "100%" test skipped the three entries in
`malformed_tickets.json` that are not objects at all — the ones the corpus exists for — passed
`allow_model=False` so it never exercised the withholding rules, and asserted `seen >= 25` while iterating
45. It now iterates every `*_tickets.json`, asserts per file, and counts the tickets actually withheld.

## D-53 · The graph, and what only a real run could show (FR-14, and FR-01…FR-16 as a system)
Row 14. `SupportPipeline` is a LangGraph `StateGraph` over a Pydantic state (D-31). The graph earns the
dependency by putting the order of the steps and the conditions between them in one place, so "what happens
to a ticket" is read off the edges. **It is not an agent**: no model chooses the next step, because a support
system that decides its own control flow cannot be shown to escalate when it should (A5, NFR-08).

**The exact-quote check was destroying the answer rate.** The first real harness run answered 1 ticket in 6,
with 3 drafts blocked as `ungrounded_draft`. The drafts were fine: PR-03 returned `supported: true` for every
sentence, and the overlap floor passed everything at 0.62–1.00. What failed was our own check — the judge
answers "copy the exact words" by copying **two spans joined with `; `**, and the joined string is a
substring of nothing. Sentences with 1.00 content-word overlap were rejected on punctuation, and one
rejected sentence blocks a whole reply. The check now folds case, whitespace and curly quotes, splits the
quote on separators, and requires **every substantial part** to appear verbatim — strict as before, a judge
that invents a quote still fails, but not defeated by a semicolon. On ten development tickets the answer rate
went from 1-in-6 to 4-in-10.

**The severe finding: a refused draft wrote two terminal rows.** `DraftResult.log_fields()` says `escalate`
when the draft is unusable, and the pipeline recorded it as the intermediate `generation` row — so the
harness's own terminal row was the second. Reconciliation then fails and the run exits 1. The trigger is
`answerable: false`, which FR-11 §3.6 calls the *documented normal* outcome for the ~29% of tickets the
documentation cannot answer, so the gate run at row 15 would have failed on the first such ticket. The
intermediate row now records what generation *produced*; the ticket's fate is the terminal row's business.

**Three more that each broke a requirement in the wiring rather than in a component:**
1. **A guardrail that raised skipped the handover entirely** — the graph went to `END`, and the row carried
   `summary=NULL, uncertainty=NULL` against FR-01's 100%.
2. **The handover was handed routing's decision**, so a ticket blocked for leaking an email address was
   described with the *generic* uncertainty and a summary reading "the assistant is sure enough of the
   answer to send it". All nine of FR-01's post-draft sentences existed and **none was reachable**.
3. **`explanation` on an escalate row was routing's answerable sentence** — the field FR-13 §2 exists for a
   support manager to read, stating the contrary of the decision beside it.

**And an `except` that switched logging off.** `_record` swallowed everything: `DecisionLogUnavailable`,
which D-27 makes a run-level stop, and `InvalidDecision` on a block row — so FR-12 §5's "the block is
recorded" could silently not happen while the ticket escalated anyway. The first now propagates, the second
is logged at error and turns into the ticket's failure, and a pipeline with no log attached says so once
rather than quietly writing nothing.

**Also fixed:** a failed pre-draft check now withholds the ticket instead of being read as "no findings"
(FR-12 §3.3), and a withheld ticket is no longer embedded; `process` builds the outcome inside its own
guard, so "never raises" is true; the harness checks the key and the classifier *before* building the index,
and reports a missing key as a `HarnessError` rather than a traceback; `model_calls` counts each ticket's own
total rather than summing rows that each carry a running total; and `prediction_value` stopped being written
as `decision.reason and None or (...)`, an expression that always evaluates to its right-hand side — correct
by accident, and a trap for whoever "fixed" it.

**Two tests that could not fail** are replaced: the `DecisionLog.perform` ordering test bracketed
`super().perform` and so asserted the wrapper's own structure, and a `assert index < len(...)` tautology. The
new one asks the log, from inside the action, whether the row is already committed.

## D-54 · The system paces itself, because a refused call becomes a wrong escalation (FR-15, NFR-07)
Decided by the author after the gate run of 2026-09-28: *"maintain how much we have consumed on our 6/min
limit and throttle API calls — this will make responses slower but we will escalate less, and that's the
point."*

**What the gate showed.** 282 model calls in 27 minutes is 10.4 a minute against the ~6 that 8000
tokens/minute allows. Groq throttled the account, the circuit breaker opened after five consecutive
failures, and because an open breaker **fails fast**, the last 21 of 80 tickets were consumed in seconds and
escalated as `provider_unavailable`. Every one of them was a ticket the system might have answered. The
breaker did its job — it stopped hammering a provider that was refusing us — but the run "completed" by not
trying, and a reader of `metrics.md` would have seen a 78.8% escalation rate that was mostly an availability
artefact.

**The fix is to spend the budget at the rate it is granted.** `_RateLimiter` keeps a sliding 60-second
window of requests and tokens and waits before a call that would exceed either. Settings
`PROVIDER_TOKENS_PER_MINUTE` and `PROVIDER_REQUESTS_PER_MINUTE`, both 0 (off) by default so nothing changes
for a caller that has not configured them; `.env.example` sets them to Groq's published free-tier limits.

Four properties worth stating, because each is a way this could have been done wrong:

1. **Tokens bind, not requests.** Groq gives 1000 requests a day and 8000 tokens a minute, so pacing on
   request count alone would still be throttled (D-47). Both are tracked; either can be left at 0.
2. **A cache hit costs nothing.** The reservation happens inside the attempt loop, which a replayed reply
   never reaches — so a re-run over the same tickets is still nearly free (NFR-07).
3. **The estimate is corrected by the provider's own count** when it reports one, and **only upwards**. The
   transport now keeps `usage` — integers about the call, never any part of its content. Pacing too slowly
   is a delay; pacing too fast is an escalation.
4. **It cannot stall a run.** Any single wait is capped at the window, and a call larger than the entire
   budget is sent rather than waited on forever — the provider's own 429 and the breaker are still behind
   this.

**The cost is time, and that is the trade the author made.** An 80-ticket unattended run gets slower; the
alternative was a quarter of the run escalating without being tried. NFR-07 forbids spending money to go
faster, so waiting is the only lever there is.

## D-55 · NFR-07 is amended: the runtime model may be a paid one, within a stated budget (NFR-07)
Decided by the author, 2026-09-28, after two gate runs were spoiled by free-tier throttling:
*"this is not making sense anymore, let's use the OpenAI API key, I have $5 there."*

**What the requirement said.** NFR-07: no spend; CLAUDE.md repeats it as a non-negotiable — *"Runtime model
is a free tier only (`MODEL_NAME` in `.env`)"*. This decision changes that, and the PRD revision has to
record it rather than let it drift, because the assessment gate checks the claim.

**Why.** Two full runs and a smoke investigation went on the free tier. The evidence:

| attempt | result |
|---|---|
| OpenRouter free endpoints (D-46) | 20 consecutive attempts refused, `upstream_provider_shared_pool`; unusable |
| Groq free tier, unpaced (D-54) | 80 tickets, **21 escalated without being tried** |
| Groq free tier, paced + throttle-aware | stopped at 65 tickets, **8 escalated without being tried** |

Client-side pacing and treating a throttle as pacing rather than as an outage both helped and neither was
enough. The remaining cost is not engineering time well spent: the system's quality cannot be measured while
a quarter of the sample never reaches a model.

**What changes, and what does not.**
- `LLM_BASE_URL` and `MODEL_NAME` move to OpenAI. **No code changes**: the provider client is
  OpenAI-compatible and has never known which host it talks to (D-47 made the same point when Groq replaced
  OpenRouter).
- The rate limiter stays and is switched off by setting both budgets to 0. It remains the answer for any
  future free tier, and D-54's finding — that throttling must not open the circuit breaker — is a
  correctness fix that has nothing to do with who is paying.
- **The budget is $5 and the run is the only thing spending it.** A full 80-ticket run is roughly 250 calls;
  the metrics report now carries an estimated cost so a run's spend is visible in the report rather than on
  a bill, and the response cache means a re-run over the same tickets costs nothing.

**What the PRD revision must say.** NFR-07 becomes a *budget* rather than a prohibition: the system must run
within a stated spend, the spend must be reported, and the free-tier path must remain available (the
`--stub-pipeline` flag and the rate limiter both survive). The three measurements above are the evidence for
the change, and they belong in the revision beside it.

## D-56 · Reading the refused drafts: the guardrail was wrong nine times out of twenty-five (FR-12)
Row 15's remaining question was whether the grounding check refusing 25 of 58 drafts meant the guardrail was
strict or the drafts were unsupported. `scripts/grounding_review.py` lays each refusal out — the ticket, the
sentences, each sentence's overlap, PR-03's verdict and the passages — and reading them answered it.

**Not the arithmetic.** All 25 refusals came from the judge, and **every claim sentence in every refused
draft cleared the 0.3 overlap floor** (minimum 0.33, median 0.76). D-22's threshold, the number the author
was asked to confirm at row 5, is doing nothing on this data: PR-03 is the whole of the check in practice.

**Not the judge either.** PR-03 returned `supported: true` for every sentence in the cases read. The
refusals were **our own quote check**, and the cause is specific: a model whose support is a bulleted list
copies several lines,

    - The authenticator code is rejected as invalid
    - Device clock drift of more than thirty seconds invalidates time-based codes

each verbatim, often from different chunks, and their concatenation is a substring of nothing. D-53 had
already fixed the same shape for semicolons — but folded whitespace *before* splitting, which destroyed the
newlines, so the split never happened. My own fix, half-applied.

**The measurement.** Splitting the **raw** quote on newlines, semicolons, ellipses and bullet markers, then
folding each part, with every part still required verbatim:

| | answered | escalation rate | blocked by grounding |
|---|---|---|---|
| before | 33 of 80 (41.2%) | 58.8% | 25 |
| after | **42 of 80 (52.5%)** | **47.5%** | 16 |

Nine validation tickets were being escalated by a string-matching artefact. The check is no less strict: an
invented line among real ones still fails (T-FR12-11j), and the 16 remaining refusals are still unread.

**What this says about the design.** Three times now — D-53, D-54 and this — the thing costing answers was
not the model, the prompt or the threshold, but code deciding whether a model's output matched a string.
That is worth remembering when the next number looks like a quality result: **the first thing to check is
whether anything is being compared by substring.** The exemption list and the overlap floor were reviewed
carefully at row 5 and row 12; the quote comparison never was, because it looked like plumbing.

