"""FR-15 §4 acceptance tests T-FR15-34 … T-FR15-45: structured output as Pydantic objects.

Parsing a free-tier model's reply is the most fragile part of this system (D-31), so it gets its
own tests: the shape, the prompt's own rules, the awkward things models really do, and the one
repair attempt. Offline: no network, no API key.
"""
import pytest
from pydantic import ValidationError

from ticketing_agent.config import Settings
from ticketing_agent.provider import (
    FakeTransport,
    MalformedModelOutput,
    ProviderClient,
    ProviderFailure,
)
from ticketing_agent.schemas import (
    AnswerDraft,
    GroundingCheck,
    HandoverNote,
    SchemaError,
    extract_json_object,
    parse_into,
)

DRAFT_JSON = (
    '{"answerable": true, "sentences": [{"text": "The usage breakdown itemises charges by '
    'service.", "citations": ["DOC-BILL-001#1"]}], "unknown_reason": ""}'
)
MESSAGES = [{"role": "user", "content": "<ticket>Why is my invoice higher?</ticket>"}]


def client(tmp_path, script):
    return ProviderClient(
        Settings(llm_api_key="k", model_name="test-model", llm_cache_path=tmp_path / "c.sqlite"),
        transport=FakeTransport(script), clock=None,
    )


def reply(text):
    return {"choices": [{"message": {"content": text}}], "model": "test-model"}


# --- the schemas themselves ----------------------------------------------------------


def test_T_FR15_34_a_well_formed_draft_becomes_a_pydantic_object():
    parsed = parse_into(AnswerDraft, DRAFT_JSON)
    draft = parsed.value
    assert isinstance(draft, AnswerDraft)
    assert draft.answerable is True
    assert draft.text == "The usage breakdown itemises charges by service."
    assert draft.cited_ids == ("DOC-BILL-001#1",)
    assert draft.uncited_sentences() == ()
    assert parsed.notes == ()


def test_T_FR15_35_the_prompts_own_rules_are_enforced():
    """PR-01 rule 3: an unanswerable draft must say why. PR-03 rule 3: supported means quoted."""
    with pytest.raises(SchemaError, match="unknown_reason"):
        parse_into(AnswerDraft, '{"answerable": false, "sentences": [], "unknown_reason": ""}')
    with pytest.raises(SchemaError, match="no sentences"):
        parse_into(AnswerDraft, '{"answerable": true, "sentences": [], "unknown_reason": ""}')
    with pytest.raises(SchemaError, match="quote"):
        parse_into(GroundingCheck, '{"results": [{"i": 0, "supported": true, "quote": ""}]}')

    # FR-01 requires both halves of a handover to be present.
    with pytest.raises(SchemaError):
        parse_into(HandoverNote, '{"summary": "x", "system_uncertainty": "  "}')


def test_T_FR15_36_an_uncited_claim_is_visible_rather_than_rejected():
    """FR-11 is enforced by the guardrails, so the parser reports rather than refuses."""
    draft = parse_into(AnswerDraft, (
        '{"answerable": true, "sentences": ['
        '{"text": "Invoices are finalised three days after the period ends.", '
        '"citations": ["DOC-BILL-001#1"]},'
        '{"text": "Staging charges are always waived.", "citations": []}],'
        ' "unknown_reason": ""}'
    )).value
    assert draft.uncited_sentences() == ("Staging charges are always waived.",)
    assert draft.cited_ids == ("DOC-BILL-001#1",)


@pytest.mark.parametrize("wrapped", [
    "```json\n" + DRAFT_JSON + "\n```",
    "Here is the JSON you asked for:\n" + DRAFT_JSON,
    DRAFT_JSON + "\n\nLet me know if you need anything else.",
    "```\n" + DRAFT_JSON + "\n```",
])
def test_T_FR15_37_json_is_recovered_from_the_prose_models_actually_add(wrapped):
    parsed = parse_into(AnswerDraft, wrapped)
    assert parsed.value.answerable is True
    assert parsed.recovered_from_prose is True
    assert "json_recovered_from_prose" in parsed.notes


def test_T_FR15_38_a_brace_inside_a_string_does_not_confuse_the_scanner():
    payload = (
        '{"answerable": true, "sentences": [{"text": "Use the {placeholder} form.", '
        '"citations": ["DOC-API-001#0"]}], "unknown_reason": ""}'
    )
    text, _ = extract_json_object("Reply: " + payload)
    assert text == payload
    assert parse_into(AnswerDraft, "Reply: " + payload).value.text == "Use the {placeholder} form."


@pytest.mark.parametrize("bad", [
    "no json here at all",
    "{unterminated",
    '{"answerable": "yes", "sentences": [], "unknown_reason": "x"}',
    '[1, 2, 3]',
    '"just a string"',
])
def test_T_FR15_39_unusable_replies_raise_schema_error_without_quoting_the_reply(bad):
    with pytest.raises(SchemaError) as raised:
        parse_into(AnswerDraft, bad)
    assert bad not in str(raised.value), "the message reaches the log; it must not carry the reply"


def test_T_FR15_40_unexpected_fields_are_ignored_but_reported():
    parsed = parse_into(GroundingCheck, (
        '{"results": [{"i": 0, "supported": false, "quote": "", "why": "not in the passage"}],'
        ' "confidence": 0.8}'
    ))
    assert parsed.value.unsupported() == (0,)
    assert parsed.unexpected_fields == ("confidence",)
    assert "unexpected_field:confidence" in parsed.notes


def test_T_FR15_41_a_grounding_check_cannot_verdict_a_sentence_twice():
    with pytest.raises(SchemaError, match="more than one verdict"):
        parse_into(GroundingCheck, (
            '{"results": [{"i": 0, "supported": true, "quote": "a"},'
            ' {"i": 0, "supported": false, "quote": ""}]}'
        ))


def test_T_FR15_42_the_models_are_immutable():
    """A draft must not be edited into compliance after the guardrails have seen it."""
    draft = parse_into(AnswerDraft, DRAFT_JSON).value
    with pytest.raises(ValidationError):
        draft.answerable = False


# --- the client's structured call ----------------------------------------------------


def test_T_FR15_43_complete_structured_returns_a_validated_object(tmp_path):
    provider = client(tmp_path, [reply(DRAFT_JSON)])
    result = provider.complete_structured(MESSAGES, prompt_id="PR-01",
                                          prompt_version="PR-01 v1.0", schema=AnswerDraft)
    assert isinstance(result.value, AnswerDraft)
    assert result.value.cited_ids == ("DOC-BILL-001#1",)
    assert result.repaired is False
    assert result.response.prompt_version == "PR-01 v1.0"


def test_T_FR15_44_one_repair_attempt_is_made_and_never_echoes_the_reply(tmp_path):
    provider = client(tmp_path, [reply("Sure! I cannot produce JSON."), reply(DRAFT_JSON)])
    result = provider.complete_structured(MESSAGES, prompt_id="PR-01",
                                          prompt_version="PR-01 v1.0", schema=AnswerDraft)
    assert result.repaired is True
    assert result.value.answerable is True

    repair_request = provider._transport.requests[1]
    repair_message = repair_request["messages"][-1]["content"]
    assert "JSON only" in repair_message
    assert "I cannot produce JSON" not in repair_message, "never feed the bad reply back in"


def test_T_FR15_45_a_reply_that_never_validates_is_a_typed_failure(tmp_path):
    provider = client(tmp_path, [reply("not json"), reply("still not json")])
    with pytest.raises(MalformedModelOutput) as raised:
        provider.complete_structured(MESSAGES, prompt_id="PR-01",
                                     prompt_version="PR-01 v1.0", schema=AnswerDraft)
    assert isinstance(raised.value, ProviderFailure), "the pipeline escalates on one type"
    assert raised.value.schema == "AnswerDraft"
    assert raised.value.prompt_version == "PR-01 v1.0"

    provider2 = client(tmp_path / "second", [reply("not json")])
    with pytest.raises(MalformedModelOutput):
        provider2.complete_structured(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0",
                                      schema=AnswerDraft, repair=False)
    assert len(provider2._transport.requests) == 1, "repair=False makes exactly one call"
