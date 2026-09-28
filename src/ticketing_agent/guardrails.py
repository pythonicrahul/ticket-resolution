"""FR-12, NFR-04: the five guardrails that run on every draft and can block it.

The Governance Framework §4 names them and sets the standard: *"A guardrail is a check that runs
on every response and can block it. Guardrails that only run in testing are not guardrails; they
are tests."* So there is no flag, setting or `except` clause here that skips one, and a check that
raises is a **failure** — a guardrail that could not run has not cleared the reply.

This module is also the single source of truth for the pattern tables. They used to live in
`tests/test_engineered_fixtures.py`, which meant the fixtures were checked against a copy of the
rules rather than the rules themselves (FR-12 §7); the tests now import them from here.

Two stages:

* **pre-draft** (`check_ticket`), on the ticket, before any model call — a hostile or secret-bearing
  ticket costs nothing and no draft is ever written (NFR-07);
* **post-draft** (`Guardrails.check_draft`), on every draft without exception. It blocks; it never
  redacts (NFR-04), and the detail names the pattern rather than the matched value, so the decision
  log cannot become the leak it exists to prevent.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .generate import DISCLOSURE, GREETING_WITHOUT_NAME, HUMAN_ROUTE, SOURCE_PREFIX
from .ingest import Ticket
from .prompts import load
from .provider import MalformedModelOutput, ProviderClient, ProviderFailure
from .retrieve import Passage
from .schemas import GroundingCheck

_log = logging.getLogger(__name__)

PROMPT_ID = "PR-03"
PROMPT_NUMBER = "1.0"
PROMPT_VERSION = f"{PROMPT_ID} v{PROMPT_NUMBER}"

#: The Governance Framework's own five names, in the order they run (FR-12 §3.2).
CHECK_ORDER = ("private_data", "grounding", "instruction_integrity", "tone_and_scope",
               "confidence_floor")
#: The decision-log reason each failing check produces (FR-12 §5).
CHECK_REASON = {
    "private_data": "private_data_in_draft",
    "grounding": "ungrounded_draft",
    "instruction_integrity": "instruction_leak_in_draft",
    "tone_and_scope": "commitment_in_draft",
    "confidence_floor": "threshold_not_applied",
}
CHECK_REQUIREMENTS = {
    "private_data": ("FR-12", "NFR-04"),
    "grounding": ("FR-12", "FR-11"),
    "instruction_integrity": ("FR-12",),
    "tone_and_scope": ("FR-12", "FR-03"),
    "confidence_floor": ("FR-12", "FR-02"),
}

# --- the pattern tables (FR-12 §3.1 and §3.2) ----------------------------------------

#: FR-12 §3.1.1. Phrases, not bare words: `override`, `act as` and `your guidelines` matched
#: ordinary support questions, which are in the corpus as `injection_lookalike` fixtures.
INJECTION_MARKERS = (
    "ignore all previous", "ignore previous instruction", "ignore the above",
    "ignore your instruction", "disregard your", "disregard all previous",
    "disregard the above", "you are now", "pretend to be", "new instructions",
    "system:", "assistant:", "</ticket>", "<ticket>", "reveal your",
    "show me your prompt", "system prompt", "repeat your instructions",
    "your instructions verbatim", "developer mode", "jailbreak", "bypass your",
)
#: D-24: a role label counts only at the start of a line. Inline, "restart requested by system:
#: worker-3" is ordinary prose, and a pasted log must not escalate.
LINE_ANCHORED = ("system:", "assistant:")

#: FR-12 §3.1.2: only high-risk secrets escalate a *ticket*, because they must never be embedded,
#: cached or sent to a provider.
CREDENTIAL_PATTERNS = (r"password\s*[:=]", r"passwd\s*[:=]", r"secret\s*[:=]", r"token\s*[:=]",
                       r"api[_-]?key\s*[:=]", r"private[_-]?key\s*[:=]", r"private key",
                       r"BEGIN [A-Z ]*PRIVATE KEY")
EMAIL = r"[\w.+-]+@[\w-]+\.[\w.]+"
#: §3.2.1's words are "7+ digits with separators", not "international format": the first version
#: required a leading `+` and missed `0207 946 0123`, `(212) 555-0199` and `555-0100` (D-51).
#: Candidates only — `phones_in` counts the digits, because a 16-digit invoice reference is a
#: long digit run and not a phone number (`SYN-PII-LOOKALIKE-002` exists to say so).
PHONE = r"(?:\+?\d[\d\s().-]{5,}\d)"
#: E.164 allows at most 15 digits, and 7 is the shortest local number in the fixtures.
PHONE_DIGITS = (7, 15)
#: §3.1.2 says "national-insurance/social-security-shaped": the separated and unseparated US
#: forms, and the UK NI format. Dashes-only missed `123456789` entirely (D-51).
NATIONAL_ID = (r"(?:\b\d{3}-\d{2}-\d{4}\b|\b\d{9}\b"
               r"|\b[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b)")
DIGIT_RUN = r"(?:\d[ -]?){12,19}"
#: FR-12 §3.2.1: a key-shaped token after a key/token/secret label.
API_KEY_TOKEN = r"(?:api[_-]?key|token|secret)\W{0,3}[A-Za-z0-9_-]{16,}"
#: §7 keeps this deliberately: repeating a customer's internal topology in a reply is poor practice.
#: All four octets are required: `10.1.2` is a version string, and matching it escalated any
#: documentation answer that named one (D-51).
PRIVATE_IP = (r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))"
              r"\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

#: FR-12 §3.2.4. The three phrases every one of the 200 ground-truth rows carries, verbatim,
#: and the close forms a model actually writes.
MUST_NOT_CLAIM = ("a refund has been issued", "the issue has been fixed on our side",
                  "a specific delivery date for a fix")
COMMITMENT_PHRASES = (*MUST_NOT_CLAIM,
                      "we have issued a refund", "a refund will be issued", "you will be refunded",
                      "we have fixed", "this is now fixed", "will be fixed by", "expect a fix on",
                      "we guarantee", "we promise", "we commit to")
#: FR-12 §3.2.3: evidence that the draft followed the ticket rather than its instructions.
LEAK_MARKERS = ("system prompt", "my instructions", "as an ai", "as an a.i.",
                "my system message", "the instructions say")

#: D-22, re-measured on real chunks at row 5. A cheap floor against invention; NFR-03's
#: hallucination budget rests on the PR-03 judgement, not on word counting.
OVERLAP_THRESHOLD = 0.3
#: FR-12 §3.2.2. A sentence opening with one of these asserts nothing about the product. Without
#: them an overlap floor rejects 37-48% of what CloudServe's own senior agents wrote (D-22).
CLAIM_EXEMPT_OPENERS = (
    "thank", "thanks", "welcome", "apolog", "i hope", "glad", "happy to", "please reply",
    "do let me know", "let me know", "i would suggest starting", "if you work through",
    "based on what you have described",
)
#: FR-12 §3.2.2's refusal case, spelled out: *"a draft that says plainly it does not know, with no
#: claims, passes"*. A refusal asserts nothing about the product — it reports what the system did —
#: so an overlap floor would block exactly the honest answer the PRD asks for (D-51).
REFUSAL_OPENERS = (
    "i could not find", "we could not find", "i was not able to find", "i cannot find",
    "i do not have", "we do not have", "the documentation does not", "our documentation does not",
    "there is nothing in", "i have passed", "we have passed", "i am passing", "i have escalated",
    "this has been passed", "a colleague will", "someone will look",
)
#: FR-06's own lines, imported rather than copied. Each has near-zero overlap with any passage, so
#: the only thing keeping mandatory text under the floor used to be a prefix typed twice in two
#: modules — and FR-06 explicitly allows that wording to change behind `DISCLOSURE_VERSION`
#: (D-51). Matching the constants means a permitted change cannot block every reply in a run.
FR06_LINES = (DISCLOSURE, HUMAN_ROUTE, GREETING_WITHOUT_NAME)
#: Matching the constants exactly is not enough on its own: FR-06 allows its wording to change
#: behind `DISCLOSURE_VERSION`, the greeting varies by name and the source line by article, and
#: the engineered corpus already carries an older, shorter wording of both lines. So the *shape*
#: of each obligation is exempt as well — a sentence that says it was drafted automatically, or
#: that offers a person, asserts nothing about the product either way (D-51).
FR06_PREFIXES = (SOURCE_PREFIX.lower(), "hi ", "hello",
                 "this reply was drafted", "if you would like a person",
                 "if anything here is wrong", "reply to this message")
#: Words too common to count as evidence of overlap.
_STOPWORD_TEXT = """a an and are as at be been by can do does for from has have if in into is
it its of on or that the their them then there these this to was were what when where which who
will with you your our we us i"""
STOPWORDS = frozenset(_STOPWORD_TEXT.split())


@dataclass(frozen=True)
class GuardrailResult:
    """One check: what it looked at, and what it found — whether or not it blocked."""

    name: str
    passed: bool
    detail: str
    requirement_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class GuardrailReport:
    """FR-12 §2: all five results, always, plus the reason the first failure produces."""

    passed: bool
    results: tuple[GuardrailResult, ...]
    blocking: tuple[str, ...] = ()
    reason: str | None = None
    detail: str | None = None
    all_reasons: tuple[str, ...] = ()
    prompt_version: str | None = None
    model_calls: int = 0
    cache_hits: int = 0

    def log_fields(self) -> dict[str, Any]:
        """FR-13 §5: the `validation` row. `block` is the framework's third `action_taken`."""
        requirements: list[str] = ["FR-12"]
        for result in self.results:
            if not result.passed:
                requirements.extend(r for r in result.requirement_ids if r not in requirements)
        return {
            "stage": "validation",
            "decision": "continue" if self.passed else "block",
            "reason": self.reason,
            # Every check's finding, not only the first failure's: the Build Specification asks
            # the validator to record "what it checked and what it found, whether or not it
            # blocked", and pairs alone cannot carry that.
            "detail": "; ".join(f"{r.name}: {r.detail}" for r in self.results),
            # FR-12 §5: a draft failing three checks must not lose two of them.
            "all_reasons": list(self.all_reasons),
            # Pairs, which is what FR-13 §2 declares and `governance_record` unpacks — a triple
            # crashed the assessor-facing projection on exactly the rows FR-12's criterion
            # requires (D-51). What each check *found* is in `detail` below, for all five.
            "guardrail_results": [[r.name, r.passed] for r in self.results],
            "prompt_version": self.prompt_version,
            "model_calls": self.model_calls,
            "cache_hits": self.cache_hits,
            "requirement_ids": requirements,
        }


@dataclass(frozen=True)
class JudgeVerdict:
    """What PR-03 said, plus what it cost. `unsupported` is empty when every sentence is backed."""

    unsupported: tuple[int, ...]
    detail: str
    prompt_version: str | None = None
    model_calls: int = 0
    cache_hits: int = 0


# --- pre-draft, on the ticket ---------------------------------------------------------


def check_ticket(ticket: Ticket) -> tuple[tuple[str, str], ...]:
    """FR-12 §3.1: the reasons a ticket escalates *before* any model call.

    Returned in D-16 precedence order and handed to `Router.decide(extra_reasons=...)`, which
    ranks them against every other rule. Nothing here is a model call, so a hostile or
    secret-bearing ticket costs nothing and no draft is ever written (NFR-07).
    """
    text = ticket.text or ""
    found: list[tuple[str, str]] = []
    secrets = secrets_in(text)
    if secrets:
        found.append(("private_data_in_ticket", f"patterns: {', '.join(secrets)}"))
    hits = markers_in(text)
    if hits:
        found.append(("instruction_injection_detected", f"markers: {', '.join(hits)}"))
    return tuple(found)


def markers_in(text: str) -> list[str]:
    """FR-12 §3.1.1 with D-24's anchoring, in table order so the detail is deterministic."""
    low = (text or "").lower()
    hit = {m for m in INJECTION_MARKERS if m not in LINE_ANCHORED and m in low}
    for label in LINE_ANCHORED:
        if re.search(rf"(?m)^[\s>]*{re.escape(label)}", low):
            hit.add(label)
    return [m for m in INJECTION_MARKERS if m in hit]


def secrets_in(text: str) -> list[str]:
    """FR-12 §3.1.2: the high-risk secrets that escalate a ticket. Names only, never values."""
    low = text or ""
    found: list[str] = []
    if re.search(NATIONAL_ID, low):
        found.append("national_id")
    if any(_luhn_ok(m.group()) for m in re.finditer(DIGIT_RUN, low)):
        found.append("card_number")
    if any(re.search(p, low, re.IGNORECASE) for p in CREDENTIAL_PATTERNS):
        found.append("credential")
    return found


def phones_in(text: str) -> list[str]:
    """Phone-shaped runs: 7 to 15 digits with separators.

    A digits-only reading of "7+ digits" blocks a routine billing question that quotes two
    16-digit invoice references, which is exactly the false positive the lookalike fixtures were
    written to catch (D-51).
    """
    found = []
    for match in re.finditer(PHONE, text or ""):
        digits = sum(c.isdigit() for c in match.group())
        if PHONE_DIGITS[0] <= digits <= PHONE_DIGITS[1]:
            found.append(match.group().strip())
    return found


def private_data_in(text: str) -> list[str]:
    """FR-12 §3.2.1: everything that must not appear in an outbound reply.

    Wider than `secrets_in`: a customer's own email or phone is normal in a ticket and a leak in
    a reply (D-23), because the reply may be read by others on the account.
    """
    low = text or ""
    found = secrets_in(low)
    if re.search(EMAIL, low):
        found.append("email")
    if phones_in(low):
        found.append("phone")
    if re.search(API_KEY_TOKEN, low, re.IGNORECASE):
        found.append("api_key")
    if re.search(PRIVATE_IP, low):
        found.append("private_ip")
    return sorted(set(found))


def commitments_in(text: str) -> list[str]:
    """FR-12 §3.2.4, shared with FR-03 §3.6: a promise about money or a date."""
    low = (text or "").lower()
    return [p for p in COMMITMENT_PHRASES if p in low]


def _as_data(text: str) -> str:
    """Neutralise the delimiters of the prompt this text is about to sit inside.

    The same rule `generate.py` applies to a ticket: anything interpolated into an XML-shaped
    prompt is data. Here the text is the *draft*, which is model output derived from customer
    text, so it can carry `</draft>` or a `<passage>` of its own (D-51).
    """
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _luhn_ok(text: str) -> bool:
    digits = [int(c) for c in text if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# --- grounding ------------------------------------------------------------------------


def split_sentences(text: str) -> list[str]:
    """Sentences, for the overlap floor and for PR-03's numbering. Blank lines are separators."""
    parts = re.split(r"(?<=[.!?])\s+|\n{2,}", (text or "").strip())
    return [p.strip() for p in parts if p.strip()]


def is_claim(sentence: str) -> bool:
    """FR-12 §3.2.2: does this sentence assert something about the product?

    Getting this wrong makes every threshold unusable, which is why the exemption list came before
    the number (D-22): 351 of 997 expert sentences are pleasantries carrying no claim at all.
    Four things are not claims — a pleasantry, an honest refusal, one of FR-06's own lines, and a
    sentence with nothing in it.
    """
    text = sentence.strip()
    low = text.lower()
    if not low:
        return False
    if low.startswith(CLAIM_EXEMPT_OPENERS) or low.startswith(REFUSAL_OPENERS):
        return False
    if low.startswith(FR06_PREFIXES):
        return False
    stripped = text.rstrip(".")
    return not any(stripped == line.rstrip(".") or text == line for line in FR06_LINES)


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if w not in STOPWORDS and len(w) > 2}


def overlap(sentence: str, support: str) -> float:
    """The share of a sentence's content words that appear in the support text."""
    words = content_words(sentence)
    if not words:
        # 0.0, not 1.0. A sentence with no content words has nothing supporting it, and a
        # fail-open default in the one arithmetic guard is the wrong way round (D-51).
        return 0.0
    return len(words & content_words(support)) / len(words)


class GroundingJudge:
    """PR-03 as a guardrail: a model's verdict, with an exact-quote check behind it.

    The judge is a model and can be lenient — especially one from the generator's own family —
    so code checks that each claimed quote is really in a cited passage. That turns half of the
    judgement into a deterministic test (PR-03's own "known weaknesses").
    """

    def __init__(self, client: ProviderClient, max_tokens: int = 900) -> None:
        self._client = client
        self._prompt = load(PROMPT_ID, PROMPT_NUMBER)
        self._max_tokens = max_tokens

    def check(self, sentences: Sequence[tuple[str, Sequence[str]]],
              retrieved: Sequence[Passage],
              indices: Sequence[int] | None = None) -> JudgeVerdict:
        """Ask PR-03 about these sentences. Raises `ProviderFailure`; the caller blocks on it.

        `indices` are the numbers the sentences carry in the draft, so a verdict can be traced
        back to the sentence it judged even though only the claims are sent.
        """
        passages = "\n".join(
            f'<passage id="{_as_data(p.chunk_id)}">{_as_data(p.text)}</passage>'
            for p in retrieved)
        numbers = list(indices) if indices is not None else list(range(len(sentences)))
        # The draft is model output derived from customer text, so it is data here too: a draft
        # containing `</draft>` could otherwise restructure the judge's own prompt (D-51).
        numbered = "\n".join(
            f'<s i="{number}" cites="{_as_data(",".join(cites))}">{_as_data(text)}</s>'
            for number, (text, cites) in zip(numbers, sentences, strict=True))
        messages = [
            {"role": "system", "content": self._prompt.system},
            {"role": "user", "content": f"<passages>\n{passages}\n</passages>\n"
                                        f"<draft>\n{numbered}\n</draft>\n"
                                        "Check every sentence now, as JSON."},
        ]
        structured = self._client.complete_structured(
            messages, prompt_id=PROMPT_ID, prompt_version=PROMPT_VERSION,
            schema=GroundingCheck, max_tokens=self._max_tokens)

        corpus = _comparable(" ".join(p.text for p in retrieved))
        asked = set(numbers)
        unsupported: list[int] = []
        for verdict in structured.value.results:
            if verdict.i not in asked:
                continue  # a verdict for a sentence we did not ask about proves nothing
            # PR-03 rule 3: a supported sentence carries the words that support it. A quote the
            # passages do not contain is not support, whatever the judge said.
            if not verdict.supported or not _quote_found(verdict.quote, corpus):
                unsupported.append(verdict.i)
        answered = {v.i for v in structured.value.results}
        unsupported.extend(i for i in numbers if i not in answered)

        response = structured.response
        return JudgeVerdict(
            unsupported=tuple(sorted(set(unsupported))),
            detail=(f"{len(sentences) - len(set(unsupported))} of {len(sentences)} sentences "
                    "supported by a quote found in the passages"),
            prompt_version=PROMPT_VERSION,
            model_calls=response.provider_requests,
            cache_hits=1 if response.cached else 0,
        )


#: A quote shorter than this cannot be checked meaningfully, so it is not required to match.
MIN_QUOTE_CHARS = 12


def _comparable(text: str) -> str:
    """Case, whitespace and quote characters folded, so only the words have to match."""
    folded = (text or "").lower().replace("’", "'").replace("‘", "'")
    folded = folded.replace("“", '"').replace("”", '"').replace("—", "-")
    return " ".join(folded.split())


def _quote_found(quote: str, corpus: str) -> bool:
    """Is every substantial part of this quote really in the passages?

    Measured against the real judge (D-53): PR-03 answers "copy the exact words" by copying
    **two** spans joined with `; `, and the joined string is a substring of nothing. Three of the
    four drafts in the first real harness run were blocked by that alone — sentences with 0.74
    and 1.00 content-word overlap with the passages, rejected on punctuation.

    Each part is still required to appear verbatim, so this is not a loosening of the check: a
    judge that invents a quote fails exactly as it did before.
    """
    folded = _comparable(quote)
    if not folded:
        return True  # an empty quote is the schema's problem, not this function's
    parts = [p.strip() for p in re.split(r"\s*;\s*|\s*\.\.\.\s*|\n+", folded) if p.strip()]
    checkable = [p for p in parts if len(p) >= MIN_QUOTE_CHARS]
    if not checkable:
        return folded in corpus
    return all(part in corpus for part in checkable)


# --- post-draft, on every draft -------------------------------------------------------


class Guardrails:
    """FR-12: the five checks. All of them, on every draft, with no way to skip one."""

    def __init__(self, judge: Any) -> None:
        self._judge = judge

    def check_draft(self, *, ticket: Ticket, reply: str | None,
                    sentences: Sequence[tuple[str, Sequence[str]]],
                    retrieved: Sequence[Passage], citations: Sequence[str],
                    confidence: float | None, threshold_applied: float | None) -> GuardrailReport:
        """Run every check and report all five results, whatever any of them finds.

        There is deliberately no parameter that selects, disables or relaxes a check: the
        Governance Framework's definition of a guardrail rules it out, and CLAUDE.md forbids it.
        """
        if not (reply or "").strip():
            # FR-12 §4: nothing to check means nothing may be released.
            empty = tuple(GuardrailResult(name, False, "no draft to check",
                                          CHECK_REQUIREMENTS[name]) for name in CHECK_ORDER)
            return GuardrailReport(passed=False, results=empty, blocking=CHECK_ORDER,
                                   reason="empty_draft", all_reasons=("empty_draft",),
                                   detail="the draft was empty")

        split = _numbered(sentences)
        # §3.1.2's rationale for the pre-draft rule is that a secret "must never be embedded,
        # cached or sent to a provider". A draft that has already failed `private_data` is
        # certainly blocked, so sending it to the judge would transmit the secret for nothing
        # (D-51). The check still runs and still reports — the deterministic half of it — the
        # provider call is what is skipped.
        leaking = bool(private_data_in(reply))
        checks = (
            lambda: self._private_data(reply),
            lambda: self._grounding(reply, split, retrieved, citations, ask_judge=not leaking),
            lambda: self._instruction_integrity(ticket, reply),
            lambda: self._tone_and_scope(reply),
            lambda: self._confidence_floor(confidence, threshold_applied),
        )

        results: list[GuardrailResult] = []
        failure_reason: dict[str, str] = {}
        calls = hits = 0
        prompt_version: str | None = None
        for name, run in zip(CHECK_ORDER, checks, strict=True):
            try:
                result, extra = run()
            except Exception as exc:  # noqa: BLE001 - FR-12 §3.3: a check that raises fails
                # FR-12 §4 separates the two: a provider that was down is an availability signal
                # (FR-15), while a judge that answered and broke its JSON contract is prompt
                # drift. Logging both as `provider_unavailable` hid the second (D-51).
                if isinstance(exc, MalformedModelOutput) or not isinstance(exc, ProviderFailure):
                    failure_reason[name] = "check_error"
                else:
                    failure_reason[name] = "provider_unavailable"
                _log.warning("guardrail %s failed for %s: %s", name, ticket.ticket_id, exc)
                # The requests a failed check cost are still requests (NFR-07): the exception
                # carries them precisely so the caller does not have to parse the message.
                calls += int(getattr(exc, "provider_requests", None)
                             or getattr(exc, "attempts", 0) or 0)
                prompt_version = getattr(exc, "prompt_version", None) or prompt_version
                results.append(GuardrailResult(name, False, f"check_error:{type(exc).__name__}",
                                               CHECK_REQUIREMENTS[name]))
                continue
            results.append(result)
            if extra:
                calls += extra.model_calls
                hits += extra.cache_hits
                prompt_version = extra.prompt_version or prompt_version

        blocking = tuple(r.name for r in results if not r.passed)
        first = next((r for r in results if not r.passed), None)
        reason = None if first is None else failure_reason.get(first.name,
                                                                CHECK_REASON[first.name])
        return GuardrailReport(
            passed=not blocking,
            results=tuple(results),
            blocking=blocking,
            reason=reason,
            all_reasons=tuple(failure_reason.get(name, CHECK_REASON[name])
                              for name in blocking),
            detail=None if first is None else f"{first.name}: {first.detail}",
            prompt_version=prompt_version,
            model_calls=calls,
            cache_hits=hits,
        )

    # --- the five checks -------------------------------------------------------------

    def _private_data(self, reply: str) -> tuple[GuardrailResult, None]:
        """NFR-04: block, never redact, and name the pattern rather than the value."""
        found = private_data_in(reply)
        detail = (f"patterns: {', '.join(found)}" if found
                  else "no email, phone, card, national id, credential, key or private ip")
        return GuardrailResult("private_data", not found, detail,
                               CHECK_REQUIREMENTS["private_data"]), None

    def _grounding(self, reply: str, sentences: Sequence[tuple[str, Sequence[str]]],
                   retrieved: Sequence[Passage],
                   citations: Sequence[str],
                   ask_judge: bool = True) -> tuple[GuardrailResult, JudgeVerdict | None]:
        """Three questions, all of which must answer yes.

        *Does every citation resolve to a retrieved chunk* (exact, FR-11); *is each claim
        supported by the material we retrieved* (the overlap floor, against the **union** of the
        passages — against a single chunk it rejects 9% of expert claims, D-22); and *does PR-03
        agree, with a quote the passages actually contain*.
        """
        # D-50 made grounding the only thing standing between an unsupported sentence and a
        # customer, so a caller that hands over no sentences — or a list that is not the reply —
        # must not silently disable it (D-51). There is no flag for this; it is a failure.
        if not sentences:
            return GuardrailResult("grounding", False,
                                   "no sentences were given for a non-empty reply, so nothing "
                                   "could be checked", CHECK_REQUIREMENTS["grounding"]), None
        missing = [text for text, _ in sentences if _normalised(text) not in _normalised(reply)]
        if missing:
            return GuardrailResult(
                "grounding", False,
                f"{len(missing)} sentence(s) given to the check are not in the reply that would "
                "be sent, so the check would not be about the reply",
                CHECK_REQUIREMENTS["grounding"]), None

        allowed = {p.chunk_id for p in retrieved}
        unresolved = sorted({c for c in citations if c not in allowed})
        if unresolved:
            return GuardrailResult("grounding", False,
                                   f"citations not in the retrieved set: {', '.join(unresolved)}",
                                   CHECK_REQUIREMENTS["grounding"]), None

        support = " ".join(p.text for p in retrieved)
        claims = [(i, text) for i, (text, _) in enumerate(sentences) if is_claim(text)]
        thin = [(i, round(overlap(text, support), 2)) for i, text in claims
                if overlap(text, support) < OVERLAP_THRESHOLD]
        if thin:
            index, score = thin[0]
            return GuardrailResult(
                "grounding", False,
                f"sentence {index} has {score} content-word overlap with the retrieved "
                f"passages, below the {OVERLAP_THRESHOLD} floor",
                CHECK_REQUIREMENTS["grounding"]), None

        # Only the claims are judged. PR-03 rule 2 says a pleasantry "counts as supported", but
        # rule 3 requires a quote for anything supported and the schema enforces it, so a judge
        # asked about "Thank you for getting in touch" has no lawful answer and every reply
        # containing one would block (D-51). Indices are the sentence's own, so a verdict still
        # names the sentence it is about.
        if not claims:
            # FR-12 §3.2.2: "a draft that says plainly it does not know, with no claims, passes".
            # There is nothing to ask PR-03 about, and asking would spend a request to be told so.
            return GuardrailResult("grounding", True,
                                   "no claim-bearing sentence to support", 
                                   CHECK_REQUIREMENTS["grounding"]), None

        if not ask_judge:
            # The deterministic half ran and cleared; the judge was not asked because the reply
            # is already blocked and a secret must not be sent to a provider (§3.1.2's rationale).
            # Reporting a failure here would put "grounding: fail" in the governance record for a
            # reply whose grounding was never in doubt — the log should say what happened (D-51).
            return GuardrailResult(
                "grounding", True,
                f"{len(claims)} claim-bearing sentence(s) cleared the {OVERLAP_THRESHOLD} overlap "
                "floor; PR-03 was not asked, because the reply carries private data and is "
                "blocked, and the text must not be sent to a provider",
                CHECK_REQUIREMENTS["grounding"]), None
        verdict = self._judge.check(sentences=[(text, sentences[i][1]) for i, text in claims],
                                    retrieved=retrieved, indices=[i for i, _ in claims])
        if verdict.unsupported:
            return GuardrailResult(
                "grounding", False,
                f"PR-03 found sentence(s) {list(verdict.unsupported)} unsupported; "
                f"{verdict.detail}", CHECK_REQUIREMENTS["grounding"]), verdict
        return GuardrailResult("grounding", True,
                               f"{len(claims)} claim-bearing sentence(s); {verdict.detail}",
                               CHECK_REQUIREMENTS["grounding"]), verdict

    def _instruction_integrity(self, ticket: Ticket,
                               reply: str) -> tuple[GuardrailResult, None]:
        """Evidence the draft followed the ticket rather than its instructions."""
        low = reply.lower()
        found = [m for m in LEAK_MARKERS if m in low]
        found += [m for m in markers_in(reply) if m not in found]
        detail = (f"markers: {', '.join(found)}" if found
                  else "no prompt text, role label or injection marker echoed")
        return GuardrailResult("instruction_integrity", not found, detail,
                               CHECK_REQUIREMENTS["instruction_integrity"]), None

    def _tone_and_scope(self, reply: str) -> tuple[GuardrailResult, None]:
        found = commitments_in(reply)
        detail = (f"commitments: {', '.join(found)}" if found
                  else "no money or date commitment")
        return GuardrailResult("tone_and_scope", not found, detail,
                               CHECK_REQUIREMENTS["tone_and_scope"]), None

    def _confidence_floor(self, confidence: float | None,
                          threshold: float | None) -> tuple[GuardrailResult, None]:
        """The framework's words: "a missing confidence score is not a high one"."""
        if threshold is None:
            return GuardrailResult("confidence_floor", False,
                                   "no threshold_applied was recorded for this reply",
                                   CHECK_REQUIREMENTS["confidence_floor"]), None
        if confidence is None:
            return GuardrailResult("confidence_floor", False,
                                   f"no confidence recorded against threshold {threshold}",
                                   CHECK_REQUIREMENTS["confidence_floor"]), None
        if confidence < threshold:
            return GuardrailResult("confidence_floor", False,
                                   f"confidence {confidence} is under the threshold {threshold}",
                                   CHECK_REQUIREMENTS["confidence_floor"]), None
        return GuardrailResult("confidence_floor", True,
                               f"confidence {confidence} at or above threshold {threshold}",
                               CHECK_REQUIREMENTS["confidence_floor"]), None


def _normalised(text: str) -> str:
    """Whitespace-folded, for comparing a sentence against the reply it should have come from."""
    return " ".join((text or "").split())


def _numbered(
        sentences: Iterable[tuple[str, Sequence[str]]],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """One entry per sentence, carrying the citations of the block it came from.

    FR-11 hands over the model's sentences; a block may hold several sentences, and PR-03 numbers
    them one by one, so they are split here rather than in the prompt.
    """
    out: list[tuple[str, tuple[str, ...]]] = []
    for text, cites in sentences:
        for sentence in split_sentences(text):
            out.append((sentence, tuple(cites)))
    return tuple(out)
