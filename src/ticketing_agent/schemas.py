"""Pydantic models for every structured model output, and the parser that produces them.

One model per prompt in `prompts/`, matching the JSON shape the prompt text promises. Parsing
a free-tier model's reply is the most fragile part of this system, so it is concentrated here
and tested on its own (D-31):

* The prompts say "JSON only", and models still wrap it in fences or add a sentence first, so
  `extract_json_object` finds the first balanced object rather than trusting the whole reply.
* Shape is enforced by Pydantic, and the **prompt's own rules** are enforced by validators: a
  supported sentence must carry a quote (PR-03 rule 3), an unanswerable draft must say why
  (PR-01 rule 3). A reply that breaks its own contract is a failure, not something to paper over.
* Unexpected fields are ignored but **reported**, so prompt drift shows up in the decision log
  instead of disappearing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

#: A citation is a retrieval chunk id: DOC-AUTH-001#2 (FR-10's format, row 5 owns it).
CITATION_PATTERN = r"^DOC-[A-Z]+-\d{3}(#\d+)?$"


class SchemaError(Exception):
    """The model's reply could not be turned into the object the prompt promised."""


class _Output(BaseModel):
    """Shared configuration: ignore extra keys, but the parser reports that they were there."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True, frozen=True)


# --- PR-01, answer drafting (FR-11, FR-06) -------------------------------------------


class DraftSentence(_Output):
    """One sentence of a drafted reply, with the passages it claims to come from."""

    text: str = Field(min_length=1)
    citations: list[str] = Field(default_factory=list)


class AnswerDraft(_Output):
    """PR-01's output: a reply built only from retrieved passages, or an honest refusal."""

    answerable: bool
    sentences: list[DraftSentence] = Field(default_factory=list)
    unknown_reason: str = ""

    @model_validator(mode="after")
    def _honour_the_prompts_own_rules(self) -> AnswerDraft:
        if self.answerable and not self.sentences:
            raise ValueError("answerable is true but no sentences were drafted")
        if not self.answerable and not self.unknown_reason.strip():
            raise ValueError("answerable is false but unknown_reason is empty (PR-01 rule 3)")
        return self

    @property
    def text(self) -> str:
        """The draft as prose, which is what the guardrails and the customer see."""
        return " ".join(sentence.text for sentence in self.sentences).strip()

    @property
    def cited_ids(self) -> tuple[str, ...]:
        """Every passage id the draft cites, deduplicated, in first-seen order."""
        seen: dict[str, None] = {}
        for sentence in self.sentences:
            for citation in sentence.citations:
                seen.setdefault(citation, None)
        return tuple(seen)

    def uncited_sentences(self) -> tuple[str, ...]:
        """Sentences claiming a fact with nothing behind them (FR-11's citation rule)."""
        return tuple(s.text for s in self.sentences if not s.citations)


# --- PR-02, escalation handover (FR-01) ----------------------------------------------


class HandoverNote(_Output):
    """PR-02's output: what a tier-two engineer needs in order not to re-ask the customer."""

    summary: str = Field(min_length=1)
    customer_goal: str = ""
    already_tried: list[str] = Field(default_factory=list)
    system_uncertainty: str = Field(min_length=1)
    relevant_passages: list[str] = Field(default_factory=list)
    suggested_first_check: str | None = None

    @model_validator(mode="after")
    def _fr01_needs_both_halves(self) -> HandoverNote:
        # FR-01's acceptance criterion: a non-empty summary *and* uncertainty reason, on 100%
        # of escalations. An empty one is a failure here rather than a gap found in the report.
        if not self.summary.strip() or not self.system_uncertainty.strip():
            raise ValueError("FR-01 requires a non-empty summary and system_uncertainty")
        return self


# --- PR-03, grounding check (FR-12, FR-11) -------------------------------------------


class SentenceVerdict(_Output):
    """One sentence's verdict, with the exact words that support it."""

    i: int = Field(ge=0)
    supported: bool
    quote: str = ""

    @model_validator(mode="after")
    def _supported_means_quoted(self) -> SentenceVerdict:
        # PR-03 rule 3: a supported sentence carries the exact words from the passage. Without
        # the quote there is nothing to verify, and "supported" would be the judge's opinion.
        if self.supported and not self.quote.strip():
            raise ValueError(f"sentence {self.i} is supported but carries no quote (PR-03 rule 3)")
        return self


class GroundingCheck(_Output):
    """PR-03's output: one verdict per numbered sentence of the draft."""

    results: list[SentenceVerdict] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_verdict_per_sentence(self) -> GroundingCheck:
        indexes = [verdict.i for verdict in self.results]
        if len(indexes) != len(set(indexes)):
            raise ValueError("the judge returned more than one verdict for a sentence")
        return self

    @property
    def all_supported(self) -> bool:
        return all(verdict.supported for verdict in self.results)

    def unsupported(self) -> tuple[int, ...]:
        return tuple(v.i for v in self.results if not v.supported)


# --- parsing --------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedOutput[TModel: BaseModel]:
    """A validated object, plus what had to be forgiven to get it."""

    value: TModel
    unexpected_fields: tuple[str, ...] = ()
    recovered_from_prose: bool = False

    @property
    def notes(self) -> tuple[str, ...]:
        """Machine-readable notes for the decision log's detail field (FR-13)."""
        notes = [f"unexpected_field:{name}" for name in self.unexpected_fields]
        if self.recovered_from_prose:
            notes.append("json_recovered_from_prose")
        return tuple(notes)


def extract_json_object(text: str) -> tuple[str, bool]:
    """The first balanced JSON object in a reply, and whether it had to be dug out.

    Models wrap JSON in ```json fences or introduce it with a sentence, however plainly the
    prompt says "JSON only". Scanning for a balanced object handles both without a regex that
    breaks on a brace inside a string.
    """
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        return stripped, False

    start = stripped.find("{")
    if start == -1:
        raise SchemaError("the reply contains no JSON object")

    depth, in_string, escaped = 0, False, False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return stripped[start:index + 1], True
    raise SchemaError("the reply contains an unterminated JSON object")


def parse_into[TModel: BaseModel](schema: type[TModel], text: str) -> ParsedOutput[TModel]:
    """Turn a model reply into a validated object, or raise `SchemaError` saying why.

    The message never quotes the reply: a malformed reply can contain customer text, and this
    message reaches the decision log (NFR-04).
    """
    payload, recovered = extract_json_object(text)
    try:
        raw: Any = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"the reply is not valid JSON: {exc.msg} at position {exc.pos}") from exc
    if not isinstance(raw, dict):
        raise SchemaError(f"the reply is a JSON {type(raw).__name__}, not an object")

    try:
        value = schema.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in error['loc']) or '(root)'}: {error['msg']}"
            for error in exc.errors()[:4]
        )
        raise SchemaError(f"the reply does not match {schema.__name__}: {problems}") from exc

    unexpected = tuple(sorted(set(raw) - set(schema.model_fields)))
    return ParsedOutput(value=value, unexpected_fields=unexpected, recovered_from_prose=recovered)
