"""FR-15 acceptance tests T-FR15-1 … T-FR15-20 (docs/specs/FR-15.md).

Offline: no network, no API key. Every test drives the real client through `FakeTransport`, so
the retry, backoff, circuit-breaker and cache code is exercised rather than replaced.
"""
from typing import ClassVar

import pytest

from ticketing_agent.config import Settings
from ticketing_agent.provider import (
    FakeTransport,
    HttpTransport,
    ProviderClient,
    ProviderError,
    ProviderFailure,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
    ServerError,
)

MESSAGES = [
    {"role": "system", "content": "Answer only from the passages given."},
    {"role": "user", "content": "<ticket>Why is my invoice higher this month?</ticket>"},
]


def settings(tmp_path, **overrides):
    values = {
        "llm_api_key": "test-key-not-a-real-one",
        "model_name": "test-model",
        "llm_timeout_seconds": 5.0,
        "llm_max_retries": 3,
        "llm_cache_path": tmp_path / "llm_cache.sqlite",
        "breaker_failure_threshold": 5,
        "breaker_cooldown_seconds": 60.0,
    }
    values.update(overrides)
    return Settings(**values)


class Clock:
    """A clock the tests drive, so backoff and cooldown are assertions rather than waits."""

    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def client(tmp_path, transport, clock=None, **overrides):
    return ProviderClient(settings(tmp_path, **overrides), transport=transport,
                          clock=clock or Clock())


def reply(text="The usage breakdown itemises charges by service.", model="test-model"):
    return {"choices": [{"message": {"content": text}}], "model": model,
            "system_fingerprint": "2026-05-01"}


def test_T_FR15_1_every_request_is_deterministic(tmp_path):
    transport = FakeTransport([reply()])
    provider = client(tmp_path, transport)
    response = provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    assert response.text.startswith("The usage breakdown")
    assert response.prompt_version == "PR-01 v1.0"
    request = transport.requests[0]
    assert request["temperature"] == 0
    assert request["model"] == "test-model"
    assert "top_p" not in request
    assert request["messages"] == MESSAGES


def test_T_FR15_2_an_identical_call_is_served_from_the_cache(tmp_path):
    transport = FakeTransport([reply()])
    provider = client(tmp_path, transport)
    first = provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    second = provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    assert len(transport.requests) == 1, "the second call must not reach the provider"
    assert second.cached is True and first.cached is False
    assert second.text == first.text
    assert (provider.completions, provider.cache_hits) == (1, 1)
    assert provider.provider_requests == 1, "a cache hit is not a request to the provider"
    assert second.provider_requests == 0 and first.provider_requests == 1


def test_T_FR15_3_the_cache_persists_across_clients(tmp_path):
    transport = FakeTransport([reply()])
    first = client(tmp_path, transport)
    first.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    empty = FakeTransport([])  # nothing scripted: a call would raise
    second = ProviderClient(settings(tmp_path), transport=empty, clock=Clock())
    replayed = second.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    assert replayed.cached is True
    assert replayed.text == "The usage breakdown itemises charges by service."
    assert empty.requests == []


def test_T_FR15_4_the_prompt_version_is_part_of_the_cache_key(tmp_path):
    transport = FakeTransport([reply("first version"), reply("second version")])
    provider = client(tmp_path, transport)
    first = provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    second = provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.1")

    assert len(transport.requests) == 2, "a bumped prompt version must not reuse the old answer"
    assert (first.text, second.text) == ("first version", "second version")


def test_T_FR15_5_a_timeout_is_retried_and_then_succeeds(tmp_path):
    transport = FakeTransport([ProviderTimeout("timed out"), reply()])
    clock = Clock()
    provider = client(tmp_path, transport, clock)
    response = provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    assert response.attempts == 2
    assert response.cached is False
    assert clock.slept == [0.5]
    assert provider.completions == 1
    assert response.provider_requests == 2, "FR-13 logs requests per row, not completions"
    assert provider.provider_requests == 2


def test_T_FR15_6_retry_after_overrides_the_backoff(tmp_path):
    transport = FakeTransport([RateLimited("slow down", retry_after=7.5), reply()])
    clock = Clock()
    provider = client(tmp_path, transport, clock)
    provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    assert clock.slept == [7.5], "a free tier that says when to come back is obeyed"


def test_T_FR15_7_backoff_is_exponential_and_deterministic(tmp_path):
    transport = FakeTransport([ProviderTimeout("t"), ServerError("503"),
                               ProviderTimeout("t"), reply()])
    clock = Clock()
    provider = client(tmp_path, transport, clock)
    response = provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    assert clock.slept == [0.5, 1.0, 2.0]
    assert response.attempts == 4


def test_T_FR15_8_exhausted_retries_raise_provider_unavailable(tmp_path):
    transport = FakeTransport([ProviderTimeout("t")] * 4)
    provider = client(tmp_path, transport)
    with pytest.raises(ProviderUnavailable) as raised:
        provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    assert len(transport.requests) == 4, "one attempt plus llm_max_retries"
    # FR-13 needs these as fields, not as a message a caller would have to parse.
    assert raised.value.attempts == 4
    assert raised.value.provider_requests == 4
    assert raised.value.prompt_version == "PR-01 v1.0"
    assert raised.value.elapsed_ms >= 0
    assert provider.failures == 4


@pytest.mark.parametrize("status", [400, 401, 403])
def test_T_FR15_9_a_bad_request_or_key_is_not_retried(tmp_path, status):
    transport = FakeTransport([ProviderError(f"status {status}")])
    provider = client(tmp_path, transport)
    with pytest.raises(ProviderError):
        provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    assert len(transport.requests) == 1, "retrying a bad key wastes the free tier"


@pytest.mark.parametrize("payload", [
    {"choices": []},
    {"choices": [{"message": {"content": ""}}]},
    {"choices": [{"message": {"content": "   "}}]},
    {"model": "test-model"},
])
def test_T_FR15_10_unusable_output_is_a_failure_not_an_empty_answer(tmp_path, payload):
    transport = FakeTransport([payload] * 4)
    provider = client(tmp_path, transport)
    with pytest.raises(ProviderUnavailable):
        provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert len(transport.requests) == 4


def test_T_FR15_11_the_circuit_opens_after_consecutive_failures(tmp_path):
    transport = FakeTransport([ProviderTimeout("t")] * 100)
    provider = client(tmp_path, transport, llm_max_retries=0, breaker_failure_threshold=3)

    for _ in range(3):
        with pytest.raises(ProviderUnavailable):
            provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert len(transport.requests) == 3
    assert provider.circuit_open is True

    with pytest.raises(ProviderUnavailable, match="circuit"):
        provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert len(transport.requests) == 3, "an open circuit must not touch the provider"


def test_T_FR15_12_the_circuit_half_opens_and_closes_on_success(tmp_path):
    transport = FakeTransport([ProviderTimeout("t"), ProviderTimeout("t"), reply()])
    clock = Clock()
    provider = client(tmp_path, transport, clock, llm_max_retries=0,
                      breaker_failure_threshold=2, breaker_cooldown_seconds=30.0)
    for _ in range(2):
        with pytest.raises(ProviderUnavailable):
            provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert provider.circuit_open is True

    clock.advance(31.0)
    response = provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert response.text.startswith("The usage breakdown")
    assert provider.circuit_open is False
    assert provider.consecutive_failures == 0


def test_T_FR15_13_a_failed_trial_reopens_the_circuit(tmp_path):
    transport = FakeTransport([ProviderTimeout("t")] * 5)
    clock = Clock()
    provider = client(tmp_path, transport, clock, llm_max_retries=0,
                      breaker_failure_threshold=2, breaker_cooldown_seconds=30.0)
    for _ in range(2):
        with pytest.raises(ProviderUnavailable):
            provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    clock.advance(31.0)
    with pytest.raises(ProviderUnavailable):
        provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert provider.circuit_open is True

    with pytest.raises(ProviderUnavailable, match="circuit"):
        provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert len(transport.requests) == 3, "still not touching a provider known to be down"


def test_T_FR15_14_a_dead_provider_over_a_whole_run_stays_bounded(tmp_path):
    """A11: with the provider disconnected the run completes, and quickly."""
    transport = FakeTransport([ProviderTimeout("down")] * 1000)
    clock = Clock()
    provider = client(tmp_path, transport, clock, breaker_failure_threshold=5)

    escalated = 0
    for ticket in range(120):
        try:
            provider.complete(
                [{"role": "user", "content": f"<ticket>ticket {ticket}</ticket>"}],
                prompt_id="PR-01", prompt_version="PR-01 v1.0",
            )
        except ProviderFailure:
            escalated += 1

    assert escalated == 120, "every ticket ends as an escalation, none is lost"
    assert len(transport.requests) <= 20, (
        f"{len(transport.requests)} attempts: the breaker must stop the run from spending "
        "120 tickets x 4 attempts on a provider that is down"
    )
    assert sum(clock.slept) < 60, "and it must not spend the run asleep in backoff"


def test_T_FR15_15_a_success_resets_the_failure_count(tmp_path):
    transport = FakeTransport([ProviderTimeout("t"), reply("one"),
                               ProviderTimeout("t"), reply("two")])
    provider = client(tmp_path, transport, llm_max_retries=0, breaker_failure_threshold=2)

    with pytest.raises(ProviderUnavailable):
        provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    provider.complete([{"role": "user", "content": "a"}], prompt_id="PR-01",
                      prompt_version="PR-01 v1.0")
    assert provider.consecutive_failures == 0

    with pytest.raises(ProviderUnavailable):
        provider.complete([{"role": "user", "content": "b"}], prompt_id="PR-01",
                          prompt_version="PR-01 v1.0")
    assert provider.circuit_open is False, "intermittent failures must not open the circuit"


def test_T_FR15_16_no_secret_or_customer_text_leaks_into_errors(tmp_path):
    secret = "test-key-not-a-real-one"
    transport = FakeTransport([ProviderTimeout("connection reset")] * 4)
    provider = client(tmp_path, transport)
    with pytest.raises(ProviderUnavailable) as raised:
        provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    message = str(raised.value)
    assert secret not in message
    assert "invoice higher this month" not in message
    assert secret not in repr(provider)
    assert "invoice" not in repr(provider)


def test_T_FR15_17_a_cache_that_cannot_be_written_does_not_fail_the_call(tmp_path):
    transport = FakeTransport([reply()])
    provider = client(tmp_path, transport)
    provider._cache.close()  # simulate a cache that has gone away mid-run
    response = provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    assert response.text.startswith("The usage breakdown"), "a cache is an optimisation"
    assert provider.cache_write_failures == 1


def test_T_FR15_18_empty_messages_is_a_programming_error(tmp_path):
    transport = FakeTransport([reply()])
    provider = client(tmp_path, transport)
    with pytest.raises(ValueError, match="messages"):
        provider.complete([], prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert transport.requests == []


def test_T_FR15_19_one_except_catches_everything_worth_escalating():
    assert issubclass(ProviderUnavailable, ProviderFailure)
    assert issubclass(ProviderError, ProviderFailure)
    assert issubclass(RateLimited, ProviderFailure)
    assert issubclass(ProviderTimeout, ProviderFailure)
    assert issubclass(ServerError, ProviderFailure)


def test_T_FR15_20_the_real_transport_refuses_to_start_without_a_key(tmp_path):
    with pytest.raises(ProviderError) as raised:
        HttpTransport(settings(tmp_path, llm_api_key=""))
    message = str(raised.value)
    assert ".env" in message and "LLM_API_KEY" in message
    assert "test-key" not in message


# --- findings from the row 4 review, and the LangChain/Pydantic layer (D-31) -----------


@pytest.mark.parametrize("payload", [
    {"choices": ["oops"]},
    {"choices": [{"message": "not a dict"}]},
    {"choices": "nope"},
    "a bare string",
    None,
])
def test_T_FR15_21_a_malformed_payload_is_a_typed_failure(tmp_path, payload):
    """Nothing untyped may escape: a caller's `except ProviderFailure` must catch everything."""
    transport = FakeTransport([payload] * 4)
    provider = client(tmp_path, transport)
    with pytest.raises(ProviderFailure):
        provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")


def test_T_FR15_22_a_corrupt_cache_row_is_a_miss_not_a_crash(tmp_path):
    import sqlite3

    transport = FakeTransport([reply()])
    provider = client(tmp_path, transport)
    provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    with sqlite3.connect(tmp_path / "llm_cache.sqlite") as conn:
        conn.execute("UPDATE responses SET payload = '{truncated'")

    second = FakeTransport([reply("fetched again")])
    fresh = ProviderClient(settings(tmp_path), transport=second, clock=Clock())
    response = fresh.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert response.text == "fetched again"
    assert fresh.cache_read_failures == 1, "a corrupt row must be counted, not silently ignored"


def test_T_FR15_23_a_cached_reply_that_is_unusable_is_dropped_and_refetched(tmp_path):
    import json as _json
    import sqlite3

    transport = FakeTransport([reply()])
    provider = client(tmp_path, transport)
    provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    with sqlite3.connect(tmp_path / "llm_cache.sqlite") as conn:
        conn.execute("UPDATE responses SET payload = ?", (_json.dumps({"choices": []}),))

    second = FakeTransport([reply("refetched")])
    fresh = ProviderClient(settings(tmp_path), transport=second, clock=Clock())
    response = fresh.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert response.text == "refetched"
    assert fresh.cache_hits == 0, "an unusable stored reply is not a hit"


@pytest.mark.parametrize("retry_after", [-5, -0.1, 3600, 99999])
def test_T_FR15_24_a_hostile_retry_after_cannot_break_or_stall_the_run(tmp_path, retry_after):
    from ticketing_agent.provider import MAX_BACKOFF_SECONDS

    transport = FakeTransport([RateLimited("slow down", retry_after=retry_after), reply()])
    clock = Clock()
    provider = client(tmp_path, transport, clock)
    provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")

    assert clock.slept and 0.0 <= clock.slept[0] <= MAX_BACKOFF_SECONDS


def test_T_FR15_25_an_unopenable_cache_does_not_stop_the_run(tmp_path):
    """Spec §4: a cache is an optimisation. Zero tickets processed is not an acceptable failure."""
    blocked = tmp_path / "unwritable"
    blocked.mkdir()
    blocked.chmod(0o500)
    try:
        transport = FakeTransport([reply()])
        provider = ProviderClient(settings(tmp_path, llm_cache_path=blocked / "cache.sqlite"),
                                  transport=transport, clock=Clock())
        response = provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
        assert response.text.startswith("The usage breakdown")
        assert provider.cache_available is False
    finally:
        blocked.chmod(0o700)


def test_T_FR15_26_a_bad_key_while_half_open_leaves_the_state_honest(tmp_path):
    """The review found a primed counter could trip the breaker on one later failure."""
    transport = FakeTransport([ProviderTimeout("t"), ProviderTimeout("t"),
                               ProviderError("status 401"), reply()])
    clock = Clock()
    provider = client(tmp_path, transport, clock, llm_max_retries=0,
                      breaker_failure_threshold=2, breaker_cooldown_seconds=30.0)
    for _ in range(2):
        with pytest.raises(ProviderUnavailable):
            provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert provider.circuit_state == "open"

    clock.advance(31.0)
    with pytest.raises(ProviderError):
        provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert provider.circuit_state == "open", "a failed trial re-opens, whatever the failure was"

    clock.advance(31.0)
    provider.complete([{"role": "user", "content": "b"}], prompt_id="PR-01",
                      prompt_version="PR-01 v1.0")
    assert provider.circuit_state == "closed"
    assert provider.consecutive_failures == 0


def test_T_FR15_27_a_cached_reply_is_served_while_the_circuit_is_open(tmp_path):
    """Load-bearing for A11: an outage must not stop the cached part of a run."""
    transport = FakeTransport([reply("from the provider"), ProviderTimeout("down"),
                               ProviderTimeout("down")])
    provider = client(tmp_path, transport, llm_max_retries=0, breaker_failure_threshold=2)
    provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    for _ in range(2):
        with pytest.raises(ProviderUnavailable):
            provider.complete([{"role": "user", "content": "other"}], prompt_id="PR-01",
                              prompt_version="PR-01 v1.0")
    assert provider.circuit_open is True

    replayed = provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    assert replayed.cached is True and replayed.text == "from the provider"


def test_T_FR15_28_the_timeout_and_model_reach_the_transport(tmp_path):
    transport = FakeTransport([reply(model="answering-model")])
    provider = client(tmp_path, transport, llm_timeout_seconds=7.5)
    response = provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0",
                                 max_tokens=256)

    assert transport.requests[0]["max_tokens"] == 256
    assert response.model == "answering-model", "FR-13 logs what actually answered"
    assert response.model_version == "2026-05-01"
    assert provider._settings.llm_timeout_seconds == 7.5


def test_T_FR15_29_a_different_model_can_be_asked(tmp_path):
    """The PR-05 judge runs on a different free model (JUDGE_MODEL_NAME)."""
    transport = FakeTransport([reply(), reply()])
    provider = client(tmp_path, transport)
    provider.complete(MESSAGES, prompt_id="PR-05", prompt_version="PR-05 v1.0",
                      model="judge-model")
    assert transport.requests[0]["model"] == "judge-model"

    provider.complete(MESSAGES, prompt_id="PR-05", prompt_version="PR-05 v1.0")
    assert transport.requests[1]["model"] == "test-model", "and the default is still the default"


def test_T_FR15_30_the_cache_file_holds_no_readable_customer_text(tmp_path):
    transport = FakeTransport([reply()])
    provider = client(tmp_path, transport)
    provider.complete(MESSAGES, prompt_id="PR-01", prompt_version="PR-01 v1.0")
    provider.close()

    stored = (tmp_path / "llm_cache.sqlite").read_bytes()
    assert b"invoice higher this month" not in stored, "the key is a hash, not the prompt"
    assert b"test-key-not-a-real-one" not in stored


@pytest.mark.parametrize(("status", "expected"), [
    (400, ProviderError), (401, ProviderError), (402, ProviderError), (403, ProviderError),
    (404, ProviderError), (429, RateLimited), (500, ServerError), (503, ServerError),
])
def test_T_FR15_31_provider_status_codes_map_to_the_right_type(status, expected):
    """404 is a retired model id and 402 an exhausted allowance: neither is an outage."""
    from ticketing_agent.provider import _map_provider_exception

    class Response:
        status_code = status
        headers: ClassVar[dict[str, str]] = {"Retry-After": "3"}

    class SDKError(Exception):
        def __init__(self):
            super().__init__("provider said no")
            self.status_code = status
            self.response = Response()

    mapped = _map_provider_exception(SDKError())
    assert isinstance(mapped, expected)
    if isinstance(mapped, RateLimited):
        assert mapped.retry_after == 3.0, "the Retry-After header is read, in any case"


@pytest.mark.parametrize("name", ["APITimeoutError", "APIConnectionError"])
def test_T_FR15_32_connection_failures_map_to_a_retryable_timeout(name):
    from ticketing_agent.provider import _map_provider_exception

    mapped = _map_provider_exception(type(name, (Exception,), {})("boom"))
    assert isinstance(mapped, ProviderTimeout)


def test_T_FR15_33_an_unknown_sdk_exception_is_still_typed():
    from ticketing_agent.provider import _map_provider_exception

    mapped = _map_provider_exception(RuntimeError("something new in the SDK"))
    assert isinstance(mapped, ProviderFailure)
    assert "something new" not in str(mapped), "a provider message may echo the request"
