"""FR-15: model client with timeout, retries, backoff, circuit breaker and response cache.

Spec: docs/specs/FR-15.md. This module decides nothing about a ticket. It either returns a
usable response or raises a `ProviderFailure` the pipeline escalates on — never an exception
nobody expected, and never a silent empty string.

Four properties matter, and each is a test rather than a hope:

* **Nothing untyped escapes** (CLAUDE.md: one ticket failing must never stop a run). Every
  public entry point raises `ProviderFailure` or `ValueError`, whatever the provider, the cache
  or the SDK does.
* **Deterministic** (NFR-08): temperature 0, and a cache keyed on the prompt version, so the
  same input gives the same answer across runs.
* **Bounded when the provider is down** (A11, FR-15): an explicit circuit-breaker state machine
  turns a dead provider into instant escalations instead of 120 tickets x 4 attempts x a
  20-second timeout.
* **Quiet about secrets** (NFR-04): neither the key nor the customer's words reach an exception
  message, its `__cause__`, a `repr` or the cache in readable form.

Calls go out through LangChain's chat model (D-31), and structured replies come back as
validated Pydantic objects from `schemas.py`. The retry, backoff, breaker and cache logic is
ours because A11's bounded outage and a prompt-version-keyed cache are requirements no library
provides.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from ticketing_agent.config import Settings
from ticketing_agent.schemas import SchemaError, parse_into

Message = dict[str, str]
T = TypeVar("T", bound=BaseModel)

#: No backoff wait may exceed this, however large a `Retry-After` the provider sends. A run
#: that sleeps for an hour inside one ticket defeats the bound the breaker exists to give (A11).
MAX_BACKOFF_SECONDS = 60.0


class ProviderFailure(Exception):
    """FR-15: anything that means this call produced no usable answer. Callers catch this."""


class ProviderUnavailable(ProviderFailure):
    """FR-15: retried and still nothing, or the circuit is open. Escalate and carry on.

    Carries the fields FR-13 has to log, so a caller never has to parse the message.
    """

    def __init__(self, message: str, *, attempts: int = 0, elapsed_ms: float = 0.0,
                 prompt_version: str | None = None, provider_requests: int = 0) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.elapsed_ms = elapsed_ms
        self.prompt_version = prompt_version
        self.provider_requests = provider_requests


class ProviderError(ProviderFailure):
    """FR-15 §3.4: the request, the model id or the key is wrong. Retrying changes nothing."""


class ProviderTimeout(ProviderFailure):
    """FR-15: one attempt timed out. Retryable."""


class RateLimited(ProviderFailure):
    """FR-15: the free tier throttled us. Retryable, and it may say when to come back."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServerError(ProviderFailure):
    """FR-15: a 5xx, or a reply this module cannot use. Retryable."""


class MalformedModelOutput(ProviderFailure):
    """FR-15 §4: the reply could not be validated against the schema its prompt promised."""

    def __init__(self, message: str, *, attempts: int = 0, prompt_version: str | None = None,
                 schema: str | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.prompt_version = prompt_version
        self.schema = schema


#: Failures worth trying again. A bad key, a bad model id or a bad request is not among them.
RETRYABLE = (ProviderTimeout, RateLimited, ServerError)


@dataclass(frozen=True)
class ProviderResponse:
    """FR-15: one usable model response, plus what FR-13 needs to log about it."""

    text: str
    model: str
    model_version: str | None
    prompt_version: str
    cached: bool
    attempts: int
    latency_ms: float
    #: Requests that actually reached the provider, for this call only. FR-13 stores this per
    #: row as `model_calls`; the client's own counters are run totals (NFR-07).
    provider_requests: int = 0


@dataclass(frozen=True)
class StructuredResponse[TModel: BaseModel]:
    """A validated Pydantic object plus the call that produced it."""

    value: TModel
    response: ProviderResponse
    notes: tuple[str, ...] = ()
    repaired: bool = False


class Transport(Protocol):
    """FR-15 §2: the seam the tests replace, so retries and the breaker stay under test."""

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        ...


class FakeTransport:
    """FR-15: a scripted transport. Every test uses this, so no test needs a key or a network.

    Each scripted item is either a response payload (a dict) or an exception instance to raise.
    """

    def __init__(self, script: Sequence[Any]) -> None:
        self._script = list(script)
        self.requests: list[dict[str, Any]] = []

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        if not self._script:
            raise AssertionError(
                f"FakeTransport was called {len(self.requests)} times but its script is empty"
            )
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class LangChainTransport:
    """FR-15, D-31: the real transport — LangChain's `ChatOpenAI` against OpenRouter or Groq.

    LangChain owns the wire format and the message types; this module owns the timeout, the
    retries, the backoff, the breaker and the cache, so `max_retries=0` is deliberate.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.llm_api_key.strip():
            raise ProviderError(
                "no model provider key: set LLM_API_KEY in .env (see .env.example). "
                "Nothing is read from anywhere else, and the key is never logged."
            )
        if not settings.model_name.strip():
            raise ProviderError(
                "no model chosen: set MODEL_NAME in .env to a free-tier model id "
                "(NFR-07 allows no spend). There is deliberately no default in the code."
            )
        self._settings = settings
        self._model: Any | None = None

    def _chat_model(self, model: str) -> Any:
        if self._model is None or getattr(self._model, "model_name", None) != model:
            try:
                from langchain_openai import ChatOpenAI
            except ImportError:  # pragma: no cover - a broken install, not a run failure
                raise ProviderError(
                    "langchain-openai is not installed: run `uv sync`"
                ) from None
            self._model = ChatOpenAI(
                model=model,
                api_key=self._settings.llm_api_key,
                base_url=self._settings.llm_base_url,
                timeout=self._settings.llm_timeout_seconds,
                max_retries=0,  # retrying is this module's job, with its own backoff
                temperature=0,  # NFR-08
            )
        return self._model

    def send(self, request: dict[str, Any]) -> dict[str, Any]:
        """Map LangChain's and the SDK's failures onto this module's vocabulary."""
        messages = [(m.get("role", "user"), m.get("content", "")) for m in request["messages"]]
        try:
            reply = self._chat_model(request["model"]).invoke(messages)
        except Exception as exc:  # noqa: BLE001 - mapped below; nothing untyped may escape
            raise _map_provider_exception(exc) from None
        text = reply.content if isinstance(reply.content, str) else json.dumps(reply.content)
        metadata = getattr(reply, "response_metadata", {}) or {}
        return {
            "choices": [{"message": {"content": text}}],
            "model": metadata.get("model_name") or request["model"],
            "system_fingerprint": metadata.get("system_fingerprint"),
        }

    def __repr__(self) -> str:
        return f"LangChainTransport(base_url={self._settings.llm_base_url!r})"


#: Kept as an alias: the spec and the README call this "the real transport".
HttpTransport = LangChainTransport


def _map_provider_exception(exc: Exception) -> ProviderFailure:
    """FR-15 §3.3/§3.4: every provider failure becomes one of this module's types.

    `from None` everywhere it is raised: a provider's 4xx body often echoes the request, and a
    printed traceback shows `__cause__`, so the chain is dropped rather than logged (NFR-04).
    """
    if isinstance(exc, ProviderFailure):
        return exc
    name = type(exc).__name__
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None)

    if name in {"APITimeoutError", "Timeout", "APIConnectionError", "ConnectionError"}:
        return ProviderTimeout(f"could not reach the provider in time ({name})")
    if name == "RateLimitError" or status == 429:
        return RateLimited(f"the provider rate-limited this request{_throttle_source(exc)}",
                           retry_after=_retry_after(exc))
    if status in (400, 401, 402, 403, 404, 422):
        # 404 is a retired or mistyped model id and 402 is an exhausted allowance: retrying
        # four times would hide a configuration error as an outage.
        return ProviderError(f"the provider rejected the request with status {status}")
    if status is not None and 500 <= int(status) < 600:
        return ServerError(f"the provider returned status {status}")
    return ServerError(f"the provider call failed ({name})")


class _BreakerState(str, Enum):
    """FR-15 §3.6: an explicit state, so no counter has to be primed to fake one."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class _Clock:
    """Real time, injectable so tests assert backoff instead of waiting for it."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, seconds))


class _ResponseCache:
    """FR-15 §3.2, NFR-07: a persistent response cache, so a rerun replays without calling.

    A cache is an optimisation: opening, reading or writing it may fail and **must not** fail a
    ticket. That is the opposite of the decision log, where losing a row is fatal (D-27). Every
    failure is counted so a silently cache-less run is visible in the metrics report.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.available = False
        self.read_failures = 0
        self.write_failures = 0
        self._lock = threading.Lock()
        self._connection: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(
                self.path, timeout=30.0, isolation_level=None, check_same_thread=False)
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS responses ("
                "key TEXT PRIMARY KEY, payload TEXT NOT NULL, stored_at TEXT NOT NULL)")
            self.available = True
        except (sqlite3.Error, OSError):
            self.write_failures += 1  # a run without a cache, not a run that fails

    def get(self, key: str) -> dict[str, Any] | None:
        if self._connection is None:
            return None
        try:
            with self._lock:
                row = self._connection.execute(
                    "SELECT payload FROM responses WHERE key = ?", (key,)).fetchone()
            return json.loads(row[0]) if row else None
        except (sqlite3.Error, OSError, json.JSONDecodeError, TypeError, IndexError):
            self.read_failures += 1  # a corrupt or unreadable row is a miss, and it is counted
            return None

    def put(self, key: str, payload: dict[str, Any]) -> bool:
        if self._connection is None:
            return False
        try:
            with self._lock:
                self._connection.execute(
                    "INSERT OR REPLACE INTO responses (key, payload, stored_at) "
                    "VALUES (?, ?, ?)",
                    (key, json.dumps(payload, sort_keys=True), _utc_now()))
        except (sqlite3.Error, OSError, TypeError, ValueError):
            self.write_failures += 1
            return False
        return True

    def drop(self, key: str) -> None:
        """Forget a stored reply this module cannot use, so the next call refreshes it."""
        if self._connection is None:
            return
        try:
            with self._lock:
                self._connection.execute("DELETE FROM responses WHERE key = ?", (key,))
        except (sqlite3.Error, OSError):
            self.write_failures += 1

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass


class ProviderClient:
    """FR-15: the only way the system talks to a model."""

    def __init__(self, settings: Settings, transport: Transport | None = None,
                 clock: Any | None = None) -> None:
        self._settings = settings
        self._transport = transport if transport is not None else LangChainTransport(settings)
        self._clock = clock or _Clock()
        self._cache = _ResponseCache(settings.llm_cache_path)
        self._state = _BreakerState.CLOSED
        self._opened_at: float | None = None
        self.completions = 0
        self.provider_requests = 0
        self.cache_hits = 0
        self.failures = 0
        self.consecutive_failures = 0

    # --- the public calls -----------------------------------------------------------

    def complete(self, messages: Sequence[Message], *, prompt_id: str, prompt_version: str,
                 model: str | None = None, max_tokens: int | None = None) -> ProviderResponse:
        """FR-15: a usable response, or a `ProviderFailure` the caller escalates on."""
        if not messages:
            raise ValueError("messages is empty: there is nothing to ask the model")
        chosen = (model or self._settings.model_name).strip()
        if not chosen:
            raise ValueError("no model: set MODEL_NAME in .env or pass model=")

        request: dict[str, Any] = {
            "model": chosen,
            "messages": [dict(m) for m in messages],
            "temperature": 0,  # NFR-08: the same input must ask for the same answer
        }
        if max_tokens is not None:
            request["max_tokens"] = max_tokens

        key = _cache_key(request, prompt_id=prompt_id, prompt_version=prompt_version,
                         base_url=self._settings.llm_base_url)
        started = self._clock.monotonic()

        replayed = self._from_cache(key, prompt_version, started)
        if replayed is not None:
            return replayed

        self._check_circuit()
        return self._attempt_loop(request, key, prompt_version, started)

    def complete_structured(self, messages: Sequence[Message], *, prompt_id: str,
                            prompt_version: str, schema: type[T], model: str | None = None,
                            max_tokens: int | None = None,
                            repair: bool = True) -> StructuredResponse[T]:
        """FR-15 §4, D-31: a reply validated into the Pydantic object its prompt promised.

        One repair attempt is allowed: the model is told, in our words, what was wrong with the
        shape. The repair message never quotes the reply, so no customer text is echoed back
        into a prompt. A reply that still does not validate is `MalformedModelOutput`, which the
        caller escalates on — a half-understood answer is never passed downstream.
        """
        response = self.complete(messages, prompt_id=prompt_id, prompt_version=prompt_version,
                                 model=model, max_tokens=max_tokens)
        try:
            parsed = parse_into(schema, response.text)
        except SchemaError as first:
            if not repair:
                raise MalformedModelOutput(
                    f"{prompt_id} reply did not match {schema.__name__}: {first}",
                    attempts=response.attempts, prompt_version=prompt_version,
                    schema=schema.__name__) from None
            retry_messages = [*[dict(m) for m in messages], {
                "role": "user",
                "content": (
                    "Your previous reply could not be parsed: "
                    f"{first}. Reply again with JSON only, in exactly the shape the "
                    "instructions gave. Do not add any text outside the JSON object."),
            }]
            repaired_response = self.complete(
                retry_messages, prompt_id=prompt_id, prompt_version=prompt_version,
                model=model, max_tokens=max_tokens)
            try:
                parsed = parse_into(schema, repaired_response.text)
            except SchemaError as second:
                raise MalformedModelOutput(
                    f"{prompt_id} reply did not match {schema.__name__} after a repair "
                    f"attempt: {second}",
                    attempts=response.attempts + repaired_response.attempts,
                    prompt_version=prompt_version, schema=schema.__name__) from None
            # The row must show both requests. `repaired_response.provider_requests` counts only
            # the repair call, so a model that broke its JSON contract and was asked again would
            # be logged as one call — and on a free tier that column is what says whether a run
            # fits the allowance (FR-15 §2, FR-13 §3; row-11 review).
            whole = replace(
                repaired_response,
                provider_requests=(response.provider_requests
                                   + repaired_response.provider_requests),
                attempts=response.attempts + repaired_response.attempts,
                latency_ms=response.latency_ms + repaired_response.latency_ms,
                cached=response.cached and repaired_response.cached,
            )
            return StructuredResponse(value=parsed.value, response=whole,
                                      notes=parsed.notes, repaired=True)
        return StructuredResponse(value=parsed.value, response=response, notes=parsed.notes)

    # --- internals -----------------------------------------------------------------

    def _from_cache(self, key: str, prompt_version: str,
                    started: float) -> ProviderResponse | None:
        """A stored reply, or None. A stored reply this module cannot use is dropped, not served."""
        payload = self._cache.get(key)
        if payload is None:
            return None
        try:
            text = _usable_text(payload)
        except ProviderFailure:
            self._cache.drop(key)
            return None
        self.cache_hits += 1
        return self._response(payload, prompt_version, cached=True, attempts=1, started=started,
                              text=text, provider_requests=0)

    def _attempt_loop(self, request: dict[str, Any], key: str, prompt_version: str,
                      started: float) -> ProviderResponse:
        attempts = 0
        requests_made = 0
        last: ProviderFailure | None = None
        # A half-open circuit gets exactly one trial: retrying a provider we already know is
        # down is what the breaker exists to prevent.
        total = 1 if self._state is _BreakerState.HALF_OPEN else self._settings.llm_max_retries + 1

        while attempts < total:
            attempts += 1
            try:
                requests_made += 1
                self.provider_requests += 1
                payload = self._transport.send(request)
                text = _usable_text(payload)
            except RETRYABLE as exc:
                last = exc
                if self._record_failure():
                    break  # the circuit just opened: stop spending attempts on it
                if attempts < total:
                    self._clock.sleep(_backoff_seconds(attempts, exc))
                continue
            except ProviderFailure:
                # A bad key, model id or request (§3.4): retrying wastes the free tier. It
                # still counts towards the breaker, so we stop hammering a provider that keeps
                # rejecting us.
                self._record_failure()
                raise
            except Exception as exc:  # noqa: BLE001 - nothing untyped may leave this module
                last = _map_provider_exception(exc)
                if self._record_failure():
                    break
                if attempts < total:
                    self._clock.sleep(_backoff_seconds(attempts, last))
                continue

            self._record_success()
            if not self._cache.put(key, payload):
                pass  # counted inside the cache; a ticket is never failed for it
            return self._response(payload, prompt_version, cached=False, attempts=attempts,
                                  started=started, text=text, provider_requests=requests_made)

        elapsed = (self._clock.monotonic() - started) * 1000
        raise ProviderUnavailable(
            f"the model provider did not answer after {attempts} attempt(s) in {elapsed:.0f} ms"
            f"{' (circuit opened)' if self._state is _BreakerState.OPEN else ''}: {last}",
            attempts=attempts, elapsed_ms=elapsed, prompt_version=prompt_version,
            provider_requests=requests_made)

    def _check_circuit(self) -> None:
        """FR-15 §3.6: an open circuit fails fast; after the cooldown, one trial is allowed."""
        if self._state is not _BreakerState.OPEN:
            return
        waited = self._clock.monotonic() - (self._opened_at or 0.0)
        if waited < self._settings.breaker_cooldown_seconds:
            raise ProviderUnavailable(
                "the model provider circuit is open after "
                f"{self.consecutive_failures} consecutive failures; "
                f"{self._settings.breaker_cooldown_seconds - waited:.0f} s before the next try",
                attempts=0, elapsed_ms=0.0)
        self._state = _BreakerState.HALF_OPEN

    def _record_failure(self) -> bool:
        """Count a failure and say whether the circuit is now open."""
        self.failures += 1
        self.consecutive_failures += 1
        if self._state is _BreakerState.HALF_OPEN:
            self._state = _BreakerState.OPEN
            self._opened_at = self._clock.monotonic()
            return True
        if self.consecutive_failures >= self._settings.breaker_failure_threshold:
            self._state = _BreakerState.OPEN
            self._opened_at = self._clock.monotonic()
            return True
        return False

    def _record_success(self) -> None:
        self.completions += 1
        self.consecutive_failures = 0
        self._state = _BreakerState.CLOSED
        self._opened_at = None

    def _response(self, payload: dict[str, Any], prompt_version: str, *, cached: bool,
                  attempts: int, started: float, text: str,
                  provider_requests: int) -> ProviderResponse:
        return ProviderResponse(
            text=text,
            model=str(payload.get("model") or self._settings.model_name),
            model_version=payload.get("system_fingerprint"),
            prompt_version=prompt_version,
            cached=cached,
            attempts=attempts,
            latency_ms=(self._clock.monotonic() - started) * 1000,
            provider_requests=provider_requests,
        )

    # --- what the harness and FR-13 read --------------------------------------------

    @property
    def circuit_open(self) -> bool:
        """FR-15: true while the breaker is holding calls back."""
        return self._state is _BreakerState.OPEN

    @property
    def circuit_state(self) -> str:
        return self._state.value

    @property
    def cache_write_failures(self) -> int:
        return self._cache.write_failures

    @property
    def cache_read_failures(self) -> int:
        """NFR-08: a cache that cannot be read makes a run non-reproducible. Count it."""
        return self._cache.read_failures

    @property
    def cache_available(self) -> bool:
        return self._cache.available

    def close(self) -> None:
        self._cache.close()

    def __repr__(self) -> str:
        """NFR-04: no key, no customer text — a repr ends up in logs."""
        return (f"ProviderClient(model={self._settings.model_name!r}, "
                f"completions={self.completions}, provider_requests={self.provider_requests}, "
                f"cache_hits={self.cache_hits}, failures={self.failures}, "
                f"circuit={self.circuit_state})")


def _usable_text(payload: Any) -> str:
    """FR-15 §3.5: unusable output is a failure, never an empty answer passed downstream.

    Defensive about shape as well as content: a provider or a corrupt cache row can hand back
    anything, and an `AttributeError` here would escape as an exception no caller catches.
    """
    if not isinstance(payload, dict):
        raise ServerError(f"the provider returned a {type(payload).__name__}, not an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ServerError("the provider returned no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ServerError(f"the provider returned a {type(first).__name__} where a choice was due")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ServerError("the provider returned a choice with no message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ServerError("the provider returned an empty message")
    return content.strip()


def _backoff_seconds(attempt: int, failure: ProviderFailure) -> float:
    """FR-15 §3.3: 0.5, 1, 2 … deterministic, `Retry-After` wins, and nothing sleeps for ever."""
    retry_after = getattr(failure, "retry_after", None)
    if retry_after is not None:
        try:
            requested = float(retry_after)
        except (TypeError, ValueError):
            requested = 0.0
        # A negative or absurd Retry-After is a bug or an attack, not an instruction.
        return min(max(requested, 0.0), MAX_BACKOFF_SECONDS)
    return min(0.5 * (2 ** (attempt - 1)), MAX_BACKOFF_SECONDS)


def _cache_key(request: dict[str, Any], *, prompt_id: str, prompt_version: str,
               base_url: str) -> str:
    """FR-15 §3.2: the prompt version is in the key, so a bumped prompt is never replayed.

    The base url is in it too: the same model id on OpenRouter and on Groq is not the same model.
    """
    material = {
        "base_url": base_url,
        "model": request["model"],
        "messages": request["messages"],
        "temperature": request["temperature"],
        "max_tokens": request.get("max_tokens"),
        "prompt_id": prompt_id,
        "prompt_version": prompt_version,
    }
    canonical = json.dumps(material, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()


#: The only fields read out of a 429 body. Both are short provider-side identifiers; neither can
#: contain the request, which is why the rest of the body is still dropped unread (NFR-04).
THROTTLE_FIELDS = ("limit_source", "provider_name")


def _throttle_source(exc: Exception) -> str:
    """Why we were throttled, when the provider says so in a structured field.

    Measured against the real provider: a saturated free endpoint answers 429 with
    `limit_source: upstream_provider_shared_pool` and **no `Retry-After`**, while an exhausted
    account quota is a different `limit_source` entirely. The two need opposite responses — wait,
    versus stop and fix the account — and "the provider rate-limited this request" cannot tell
    them apart, which is what a decision log reading `provider_unavailable` inherited.

    Only the fields in `THROTTLE_FIELDS` are read. The body's free text is left alone: a 4xx body
    can echo the request, and no part of a ticket may reach a log (NFR-04).
    """
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    metadata = error.get("metadata") if isinstance(error, dict) else None
    if not isinstance(metadata, dict):
        return ""
    found = [f"{field}={metadata[field]}" for field in THROTTLE_FIELDS
             if isinstance(metadata.get(field), str | int | float)]
    return f" ({', '.join(found)})" if found else ""


def _retry_after(exc: Exception) -> float | None:
    """Read a `Retry-After` header, whatever case it arrived in. Seconds only, never a date."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    value = None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None  # an HTTP-date Retry-After falls back to the computed backoff


def _utc_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
