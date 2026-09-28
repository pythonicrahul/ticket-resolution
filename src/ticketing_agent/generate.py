"""FR-11, FR-06: cited answer drafting (PR-01) and the disclosure line.

The first component that calls a model. Everything before it is deterministic and offline, so
this module is where the system starts saying things to customers, and its rules are about
refusing rather than writing:

* a citation that was not retrieved for *this* ticket makes the whole draft unusable — dropping
  it would leave the sentence standing with nothing behind it (FR-11 §3.4);
* a sentence with no citation does the same (§3.5), because the greeting and the disclosure are
  added here in code, so every sentence the model wrote is a factual claim;
* `answerable: false` is a correct outcome, not a failure (§3.6). Marcus would rather it said
  nothing than said something wrong, and the PRD made that a requirement.

The three lines FR-06 requires are appended by code and never shown to the model, so a reply
cannot lose its disclosure to a paraphrase or an injection attempt.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .ingest import Ticket
from .prompts import Prompt, PromptError, load
from .provider import MalformedModelOutput, ProviderClient, ProviderFailure
from .retrieve import Passage
from .schemas import AnswerDraft

_log = logging.getLogger(__name__)

PROMPT_ID = "PR-01"
PROMPT_NUMBER = "1.0"
#: What FR-13 records on the row. Read from the register rather than typed here twice.
PROMPT_VERSION = f"{PROMPT_ID} v{PROMPT_NUMBER}"

#: FR-06's three obligations, in code because a model must not be able to alter them. The version
#: marker makes a change to customer-facing wording a visible, reviewable change (FR-06 §2).
DISCLOSURE_VERSION = "v1"
DISCLOSURE = "This reply was drafted automatically by CloudServe's support assistant."
HUMAN_ROUTE = (
    "If anything here is wrong or you would like a person to look at it, reply to this message "
    "and we will pass it to a support agent."
)
SOURCE_PREFIX = "Based on:"
#: FR-06, the author's decision (D-50): replies open with a greeting. The name is inserted here,
#: never sent to the model — FR-11 §3.2 keeps it out of the prompt, and that does not change.
GREETING = "Hi {name},"
GREETING_WITHOUT_NAME = "Hello,"
#: A name longer than this is not a name; a reply is not the place to find out what it is.
MAX_NAME_CHARS = 60

#: Any chunk or article id appearing in the prose. PR-01's rule 2 shows the citation as an inline
#: bracket (`[DOC-AUTH-001#2]`), so a model that follows the example literally writes the id into
#: the sentence text as well as the citations array — and the sentence text is what the customer
#: reads. Both are checked against what was retrieved (row-11 review).
INLINE_CITATION = re.compile(r"\bDOC-[A-Z]+-\d{3}(?:#\d+)?\b")


#: A reply's own upper bound: PR-01 asks for under 150 words, and the JSON around it is small.
MAX_DRAFT_TOKENS = 700

#: One sentence per failure, for the decision log. FR-13 rejects a terminal row without one, and
#: "uncited_sentence" is not language a support manager can act on.
EXPLANATIONS = {
    "no_answer_drafted":
        "The documentation did not answer this question, so a person picks it up instead.",
    "invalid_citation":
        "The drafted reply pointed at an article that was not among the ones found for this "
        "ticket, so it was not sent.",
    "no_cited_article":
        "The drafted reply did not point at any documentation, so it was not sent.",
    "empty_draft": "The drafted reply came back empty, so there was nothing to send.",
    "malformed_draft":
        "The drafted reply could not be read back reliably, so a person handles this ticket.",
    "provider_unavailable":
        "The system that writes replies could not be reached, so this ticket goes to a person.",
    "drafting_failed":
        "The system could not draft a reply for this ticket, so a person handles it.",
}
DEFAULT_EXPLANATION = "No reply could be drafted for this ticket, so it goes to a person."


@dataclass(frozen=True)
class DraftResult:
    """FR-11 §2: what drafting produced, and everything the log and the handover need."""

    usable: bool
    reply: str | None = None
    draft: AnswerDraft | None = None
    reason: str | None = None
    detail: str | None = None
    citations: tuple[str, ...] = ()
    articles: tuple[tuple[str, str], ...] = ()
    retrieved_doc_ids: tuple[str, ...] = ()
    prompt_version: str = PROMPT_VERSION
    model_name: str | None = None
    model_calls: int = 0
    cache_hits: int = 0
    latency_ms: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def unknown_reason(self) -> str:
        """FR-01: what the system was unsure about, in the model's own words, for the handover."""
        return (self.draft.unknown_reason if self.draft else "") or ""

    def log_fields(self) -> dict[str, Any]:
        """FR-13 §5: the `generation` row, ready to splat into a `DecisionEntry`.

        `continue` when a reply may go on to the guardrails, `escalate` when it may not. The
        draft text is not logged: FR-13 caps and scrubs free text, and the reply is the artefact.
        """
        requirements = ["FR-11"]
        if self.usable:
            requirements.append("FR-06")
        if self.reason == "provider_unavailable":
            requirements.append("FR-15")
        return {
            "stage": "generation",
            "decision": "continue" if self.usable else "escalate",
            "reason": self.reason,
            "detail": self.detail,
            # FR-13 refuses a terminal row with no explanation, and refusing the row means the
            # escalation happens with nothing written down. A reason added without a sentence
            # falls back rather than losing the row (row-11 review).
            "explanation": None if self.usable else (
                EXPLANATIONS.get(self.reason or "") or DEFAULT_EXPLANATION),
            "citations": list(self.citations),
            "retrieved_doc_ids": list(self.retrieved_doc_ids),
            "prompt_version": self.prompt_version,
            "model_name": self.model_name,
            "model_calls": self.model_calls,
            "cache_hits": self.cache_hits,
            "latency_ms": self.latency_ms,
            "requirement_ids": requirements,
        }


class Drafter:
    """FR-11, FR-06: one drafted reply per answerable ticket, or an honest refusal."""

    def __init__(self, client: ProviderClient, prompt: Prompt | None = None,
                 max_tokens: int = MAX_DRAFT_TOKENS) -> None:
        self._client = client
        self._prompt = prompt or load(PROMPT_ID, PROMPT_NUMBER)
        self._max_tokens = max_tokens

    def draft(self, ticket: Ticket, passages: Sequence[Passage]) -> DraftResult:
        """FR-11: draft from these passages only, or say why nothing may be sent.

        Called only when FR-02 routed the ticket to `auto_respond`; a must-escalate ticket never
        reaches a model (NFR-07). Never raises: every failure is a `DraftResult` the caller logs
        and escalates, because one ticket failing must not stop a run (CLAUDE.md).
        """
        retrieved = tuple(passages)
        doc_ids = tuple(dict.fromkeys(p.doc_id for p in retrieved))
        if not retrieved:
            # Defensive: routing escalates on `no_retrieval` before this is reached (FR-02 §3),
            # so arriving here means the caller skipped it. Refuse rather than invent an answer.
            return DraftResult(usable=False, reason="no_answer_drafted",
                               detail="no passages were retrieved for this ticket")

        try:
            messages = [
                {"role": "system", "content": self._prompt.system},
                {"role": "user", "content": self._render_user(ticket, retrieved)},
            ]
            structured = self._client.complete_structured(
                messages, prompt_id=PROMPT_ID, prompt_version=PROMPT_VERSION,
                schema=AnswerDraft, max_tokens=self._max_tokens)
        except MalformedModelOutput as exc:
            return self._failed("malformed_draft", str(exc), doc_ids, exc)
        except ProviderFailure as exc:
            _log.warning("drafting %s: %s", ticket.ticket_id, exc)
            return self._failed("provider_unavailable", f"{type(exc).__name__}: {exc}",
                                doc_ids, exc)
        except Exception as exc:  # noqa: BLE001 - the docstring's promise, kept
            # `complete()` raises a bare ValueError when MODEL_NAME is blank, and a prompt file
            # that has changed shape raises PromptError. Neither is a `ProviderFailure`, and
            # before this the drafter broke its own "never raises" contract: the harness caught
            # the exception generically and logged an opaque reason instead of a typed one.
            _log.warning("drafting %s failed: %s", ticket.ticket_id, exc)
            return self._failed("drafting_failed", f"{type(exc).__name__}: {exc}", doc_ids, exc)

        return self._check(ticket, structured.value, structured, retrieved, doc_ids)

    # --- internals ------------------------------------------------------------------

    def _check(self, ticket: Ticket, draft: AnswerDraft, structured: Any,
               retrieved: Sequence[Passage], doc_ids: Sequence[str]) -> DraftResult:
        """FR-11 §3.4-§3.7, in order. Each failure refuses the whole draft, never part of it."""
        response = structured.response
        counters: dict[str, Any] = {
            "draft": draft,
            "retrieved_doc_ids": tuple(doc_ids),
            # The prompt that was actually loaded, not a module constant: `Drafter(prompt=...)`
            # can send different words, and a log naming a version nobody sent is worse than
            # no version at all (row-11 review).
            "prompt_version": self._prompt.label,
            "model_name": response.model,
            "model_calls": response.provider_requests,
            "cache_hits": 1 if response.cached else 0,
            "latency_ms": response.latency_ms,
            "notes": (*structured.notes,
                      *(("repaired: the first reply did not parse",) if structured.repaired
                        else ()),
                      f"prompt fingerprint {self._prompt.fingerprint}"),
        }

        if not draft.answerable:
            return DraftResult(usable=False, reason="no_answer_drafted",
                               detail=draft.unknown_reason or "the model gave no reason",
                               **counters)

        allowed = {p.chunk_id for p in retrieved} | {p.doc_id for p in retrieved}
        declared = [c for s in draft.sentences for c in s.citations]
        inline = INLINE_CITATION.findall(draft.text)
        unknown = [c for c in [*declared, *inline] if c not in allowed]
        if unknown:
            return DraftResult(
                usable=False, reason="invalid_citation",
                detail=f"cited passages that were not retrieved: {', '.join(sorted(set(unknown)))}",
                **counters)

        # A chunk id in the citations array must be a chunk that was retrieved, not merely an
        # article that was: `DOC-BILL-001#9` is not a passage anyone saw.
        chunks = {p.chunk_id for p in retrieved}
        off_chunk = [c for c in declared if c not in chunks]
        if off_chunk:
            return DraftResult(
                usable=False, reason="invalid_citation",
                detail=f"cited passages that were not retrieved: {', '.join(sorted(set(off_chunk)))}",
                **counters)

        # D-50, the author's decision: an uncited sentence is **sent**, not refused and not
        # dropped — the reply stays the artefact the model wrote. Nothing here checks whether it
        # is true, so the log records that it went out and FR-12's grounding check (row 12) is
        # what stands between an unsupported sentence and a customer.
        uncited = draft.uncited_sentences()
        if uncited:
            _log.info("sending a draft with %d uncited sentence(s)", len(uncited))
            counters["notes"] = (*counters["notes"],
                                 f"sent with {len(uncited)} uncited sentence(s) (D-50)")
            # `notes` has no column of its own, and this is the one thing about a *sent* reply
            # an auditor must be able to find: it went out with something unsourced in it.
            counters["detail"] = (f"sent with {len(uncited)} uncited sentence(s) (D-50); "
                                  f"first: {uncited[0][:160]}")

        # Second lock. `schemas` already rejects a blank sentence and an answerable draft with
        # no sentences, so this cannot fire today — it is here because the schema is one edit
        # away from allowing it, and an empty reply must never be sendable (FR-11 §3.7).
        if not draft.text.strip():
            return _refuse("empty_draft", "the draft parsed but had no text", counters)

        articles = _articles(draft.cited_ids, retrieved)
        if not articles:
            # FR-06 §3.3: "names the article(s) it came from" cannot be met by a reply that came
            # from nothing. Reachable since D-50 let uncited sentences through: a draft where
            # *every* sentence is uncited has no article to name, so it is still not sendable.
            return _refuse("no_cited_article",
                           f"no sentence cited a passage; {len(uncited)} uncited", counters)

        return DraftResult(usable=True,
                           reply=assemble_reply(draft, articles, greeting=greeting_for(ticket)),
                           citations=draft.cited_ids, articles=articles, **counters)

    def _failed(self, reason: str, detail: str, doc_ids: Sequence[str],
                exc: Exception | None = None) -> DraftResult:
        """A failure still reports the requests it cost.

        `ProviderUnavailable` carries `provider_requests` precisely so the caller does not have
        to parse the message (FR-15 §2). Hardcoding zero hid up to four real free-tier requests
        per failed ticket from the one column that says whether a run fits the allowance.
        """
        return DraftResult(usable=False, reason=reason, detail=detail,
                           retrieved_doc_ids=tuple(doc_ids),
                           prompt_version=self._prompt.label,
                           model_calls=int(getattr(exc, "provider_requests", 0) or 0))

    def _render_user(self, ticket: Ticket, passages: Sequence[Passage]) -> str:
        """Fill PR-01's USER template, taking its structure from the file rather than repeating it.

        Walking the template's own lines means the prompt register stays the source of truth: a
        changed layout is a changed prompt file and a bumped version, not a silent difference
        between what `prompts/` documents and what the model was sent.
        """
        lines: list[str] = []
        filled: set[str] = set()
        for line in self._prompt.user_template.splitlines():
            stripped = line.strip()
            if stripped == "...":
                continue  # the template's "and so on" marker, not a line to send
            if "{chunk_id}" in line:
                # Every slot is filled from a mapping in one pass. Chained `.replace` calls
                # substituted a title containing the literal "{text}" with the passage body, and
                # unescaped passage text could close `</passage>` and open one retrieval never
                # returned — a way to put unsourced words in front of the model (row-11 review).
                filled.update({"chunk_id", "title", "text"})
                lines.extend(
                    _fill(line, {"{chunk_id}": _attribute(p.chunk_id),
                                 "{title}": _attribute(p.title),
                                 "{text}": _as_data(p.text)})
                    for p in passages)
            elif "{channel}" in line:
                filled.add("channel")
                lines.append(_fill(line, {"{channel}": _attribute(ticket.channel)}))
            elif stripped == "{subject}":
                filled.add("subject")
                lines.append(_as_data(ticket.subject))
            elif stripped == "{body}":
                filled.add("body")
                lines.append(_as_data(ticket.body))
            else:
                lines.append(line)

        # `filled` records the slots actually substituted, so a template that writes
        # `Subject: {subject}` — which this code does not recognise and would send literally —
        # is caught. Comparing against the rendered text instead would flag an article
        # legitimately titled `A "{text}" title` (row-11 review).
        left = sorted(self._prompt.placeholders() - filled)
        if left:
            # A PR-01 v1.1 that writes `Subject: {subject}` on one line would otherwise send the
            # literal placeholder and drop the customer's subject silently (row-11 review).
            raise PromptError(
                f"{self._prompt.path.name} has placeholders this code does not fill: {left}. "
                "A changed prompt layout needs a matching change here, not a silent gap.")
        return "\n".join(lines)


def assemble_reply(draft: AnswerDraft, articles: Sequence[tuple[str, str]],
                   greeting: str = GREETING_WITHOUT_NAME) -> str:
    """FR-06 §2: a greeting, the drafted sentences, then the sources, disclosure and human route.

    Pure string work over the draft, the passages and the name, so the same inputs assemble
    byte-identically every time (NFR-08). The model's words are used verbatim between the
    greeting and the sources, and nothing is added to them.
    """
    sources = [f"{SOURCE_PREFIX} {title} ({doc_id})" if title else f"{SOURCE_PREFIX} {doc_id}"
               for doc_id, title in articles]
    return "\n\n".join([greeting, draft.text.strip(), *sources, DISCLOSURE, HUMAN_ROUTE])


def greeting_for(ticket: Ticket) -> str:
    """FR-06, D-50: "Hi {first name}," when the ticket carries a usable one, "Hello," otherwise.

    The first name only, because the ground-truth replies are informal and a full legal name reads
    like a form letter. The name comes from the ticket, never from the model, and never goes *to*
    the model: FR-11 §3.2 keeps it out of the prompt precisely so a draft cannot be steered by it.
    """
    raw = (getattr(ticket, "customer_name", "") or "").strip()
    if not raw or len(raw) > MAX_NAME_CHARS:
        return GREETING_WITHOUT_NAME
    first = raw.split()[0].strip(",.;:")
    # Anything that is not a plain name is not put in front of a customer: a name field holding
    # markup, a newline or an address is a data problem, and "Hello," is always correct.
    if not first or not all(ch.isalpha() or ch in "-'’" for ch in first):
        return GREETING_WITHOUT_NAME
    return GREETING.format(name=first)


def _articles(citations: Sequence[str], passages: Sequence[Passage]) -> tuple[tuple[str, str], ...]:
    """FR-06 §3.2: a customer gets articles, not chunk ordinals. First-cited order, deduplicated."""
    by_chunk = {p.chunk_id: p for p in passages}
    out: dict[str, str] = {}
    for chunk_id in citations:
        passage = by_chunk.get(chunk_id)
        if passage is not None:
            out.setdefault(passage.doc_id, passage.title.strip())
    return tuple(out.items())


def _refuse(reason: str, detail: str, counters: dict[str, Any]) -> DraftResult:
    """A refusal states its own reason. `detail` may already hold the uncited-sentence note from
    D-50, and that note describes a reply that was sent — not this one."""
    return DraftResult(usable=False, reason=reason, **{**counters, "detail": detail})


def _fill(line: str, slots: dict[str, str]) -> str:
    """Substitute every placeholder in one pass, so a filled value cannot be re-substituted."""
    out, index = [], 0
    pattern = re.compile("|".join(re.escape(k) for k in slots))
    for match in pattern.finditer(line):
        out.append(line[index:match.start()])
        out.append(slots[match.group()])
        index = match.end()
    out.append(line[index:])
    return "".join(out)


def _as_data(text: str) -> str:
    """CLAUDE.md: customer text is data. Angle brackets are neutralised before insertion.

    Without this a ticket can close `</ticket>` and open its own `<passage>`, which is a passage
    the retrieval never returned — i.e. a way to smuggle a fact past FR-11's citation rule. The
    text itself is preserved so the model still reads what the customer wrote.
    """
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _attribute(value: str) -> str:
    """The same, plus quotes, for a value that sits inside an XML-ish attribute."""
    return _as_data(value).replace('"', "&quot;")
