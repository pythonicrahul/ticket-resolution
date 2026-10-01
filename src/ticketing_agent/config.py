"""FR-15, FR-02, FR-10, FR-16: settings from the environment. Nothing else reads os.environ.

Every default here is also in `.env.example`. Three rules earn their keep:

* **No data file name or path is defaulted in code** (CLAUDE.md's first non-negotiable). The
  documentation corpus and the training tickets have no in-code default; a component that needs
  one and does not get it says so.
* **No model name is defaulted either.** A missing `MODEL_NAME` must refuse to start rather than
  silently pick a model, because the wrong id on OpenRouter is a paid endpoint (NFR-07).
* **A malformed setting fails loudly.** `LLM_MAX_RETRIES=three` used to fall back to the
  default in silence, and the two thresholds the author sets from a sweep are exactly where a
  typo must not restore a placeholder.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)


class ConfigError(Exception):
    """A setting is present but unusable. Better to stop than to run with a guessed value."""


@dataclass(frozen=True)
class Settings:
    """Everything the system reads from the environment, in one place."""

    # Model provider (FR-15, NFR-07, NFR-08)
    llm_api_key: str = ""
    #: The documented default (D-55, review row R9). It was `openrouter.ai`, whose free pool
    #: refused every request when it was measured (D-46) — so commenting out `LLM_BASE_URL`
    #: silently pointed an OpenAI key at a provider that would 401 and escalate every ticket.
    llm_base_url: str = "https://api.openai.com/v1"
    model_name: str = ""          # required: set MODEL_NAME, never defaulted here
    judge_model_name: str = ""
    llm_timeout_seconds: float = 20.0
    llm_max_retries: int = 3
    llm_cache_path: Path = Path("./storage/llm_cache.sqlite")
    breaker_failure_threshold: int = 5
    breaker_cooldown_seconds: float = 60.0

    # Retrieval (FR-10, FR-04)
    embedding_model: str = "all-MiniLM-L6-v2"
    chroma_path: Path = Path("./storage/chroma")
    docs_path: Path | None = None  # DOCS_PATH; no file name is hardcoded
    retrieval_top_k: int = 5
    relevance_threshold: float = 0.0

    # Classification and routing (FR-08, FR-02)
    training_tickets_path: Path | None = None  # TRAINING_TICKETS_PATH
    classifier_path: Path = Path("./storage/classifier.joblib")
    confidence_threshold: float = 0.80

    # Decision log (FR-13) and the kill switch (FR-16)
    decision_log_path: Path = Path("./storage/decisions.db")
    kill_switch_file: Path = Path("./storage/KILL_SWITCH")

    log_level: str = "INFO"

    def __post_init__(self) -> None:
        # Paths may arrive as strings from the environment or from a test, and a relative one
        # would make the cache (and so determinism, NFR-08) depend on the working directory.
        for name in ("llm_cache_path", "chroma_path", "docs_path", "training_tickets_path",
                     "decision_log_path", "kill_switch_file", "classifier_path"):
            value = getattr(self, name)
            if value is None:
                continue
            object.__setattr__(self, name, Path(str(value)).expanduser().resolve())
        if self.breaker_failure_threshold < 1:
            raise ConfigError(
                "BREAKER_FAILURE_THRESHOLD must be at least 1: a threshold below that would "
                "mean the circuit never closes (FR-15 §3.6)")
        if self.breaker_cooldown_seconds < 0:
            raise ConfigError("BREAKER_COOLDOWN_SECONDS cannot be negative")
        if self.llm_max_retries < 0:
            raise ConfigError("LLM_MAX_RETRIES cannot be negative")
        if self.llm_timeout_seconds <= 0:
            raise ConfigError("LLM_TIMEOUT_SECONDS must be positive")
        if not 0.0 <= self.relevance_threshold <= 1.0:
            raise ConfigError(
                f"RELEVANCE_THRESHOLD={self.relevance_threshold} is outside [0, 1]. Scores are "
                "cosine similarities; a value above 1 makes every ticket escalate and a negative "
                "one switches FR-10's threshold off entirely.")
        if self.retrieval_top_k < 1:
            raise ConfigError(
                f"RETRIEVAL_TOP_K={self.retrieval_top_k} must be at least 1: zero would make "
                "retrieval return nothing for every ticket.")
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ConfigError(
                f"CONFIDENCE_THRESHOLD={self.confidence_threshold} is outside [0, 1].")

    def require_path(self, name: str) -> Path:
        """The path a component needs, or a message naming the setting that is missing."""
        value = getattr(self, name)
        if value is None:
            raise ConfigError(
                f"{name.upper()} is not set: put it in .env (see .env.example). No data file "
                "name is hardcoded anywhere in this system.")
        return value

    def require_model(self) -> str:
        value = self.model_name.strip()
        if not value:
            raise ConfigError(
                "MODEL_NAME is not set: choose a small, cheap, instruction-following model id "
                "(D-55 made NFR-07 a budget rather than a prohibition). There is deliberately "
                "no default in the code.")
        return value

    def require_api_key(self) -> str:
        """FR-15, R9 review: an unset key is a setup error, not a provider outage.

        Without this a run finished with every ticket escalated as `provider_unavailable` and
        exit code 0 — which reads as a result, and `metrics.md` reported 80/80 escalated. The
        classifier already refuses to start when it is missing ("a setup error, not a silent
        fallback"); the only secret got the opposite treatment.
        """
        value = self.llm_api_key.strip()
        if not value:
            raise ConfigError(
                "LLM_API_KEY is not set. Copy `.env.example` to `.env` and fill it in, or run "
                "with --stub-pipeline to exercise the machinery without a provider.")
        return value

    def __repr__(self) -> str:
        """Never print the key: a repr ends up in logs and in exception context."""
        return (f"Settings(model_name={self.model_name!r}, llm_base_url={self.llm_base_url!r}, "
                f"llm_api_key={'set' if self.llm_api_key else 'unset'}, "
                f"llm_timeout_seconds={self.llm_timeout_seconds}, "
                f"llm_max_retries={self.llm_max_retries})")

    @property
    def kill_switch_on(self) -> bool:
        """FR-16 §3: the switch is the existence of the file, checked freshly every time.

        `Path.exists()` is not usable here. It is `os.path.exists`, which swallows every OSError
        and returns False, so a switch file inside a directory the process cannot read would have
        read as *off* — an operator would `touch storage/KILL_SWITCH`, believe automation was
        stopped, and the run would keep answering. `stat` distinguishes the two cases: absent is
        off, unreadable is **on** (§4). If we cannot tell whether automation was switched off, the
        safe reading is that it was.
        """
        path = self.kill_switch_file
        try:
            path.stat()
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as exc:
            _log.warning("cannot read the kill switch at %s (%s): treating it as ON", path, exc)
            return True
        return True


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    """Read `.env` (if present) and the environment into a `Settings`.

    Called once at start-up by the harness and the API. Nothing else reads the environment,
    which is what lets every test construct `Settings(...)` directly and stay offline.
    """
    if env_file is not None:
        _load_env_file(Path(env_file))
    settings = Settings(
        llm_api_key=os.environ.get("LLM_API_KEY", ""),
        llm_base_url=os.environ.get("LLM_BASE_URL", Settings.llm_base_url),
        model_name=os.environ.get("MODEL_NAME", ""),
        judge_model_name=os.environ.get("JUDGE_MODEL_NAME", ""),
        llm_timeout_seconds=_number("LLM_TIMEOUT_SECONDS", Settings.llm_timeout_seconds),
        llm_max_retries=_whole_number("LLM_MAX_RETRIES", Settings.llm_max_retries),
        llm_cache_path=Path(os.environ.get("LLM_CACHE_PATH", Settings.llm_cache_path)),
        breaker_failure_threshold=_whole_number(
            "BREAKER_FAILURE_THRESHOLD", Settings.breaker_failure_threshold),
        breaker_cooldown_seconds=_number(
            "BREAKER_COOLDOWN_SECONDS", Settings.breaker_cooldown_seconds),
        embedding_model=os.environ.get("EMBEDDING_MODEL", Settings.embedding_model),
        chroma_path=Path(os.environ.get("CHROMA_PATH", Settings.chroma_path)),
        docs_path=_optional_path("DOCS_PATH"),
        retrieval_top_k=_whole_number("RETRIEVAL_TOP_K", Settings.retrieval_top_k),
        relevance_threshold=_number("RELEVANCE_THRESHOLD", Settings.relevance_threshold),
        training_tickets_path=_optional_path("TRAINING_TICKETS_PATH"),
        classifier_path=Path(os.environ.get("CLASSIFIER_PATH", Settings.classifier_path)),
        confidence_threshold=_number("CONFIDENCE_THRESHOLD", Settings.confidence_threshold),
        decision_log_path=Path(os.environ.get("DECISION_LOG_PATH", Settings.decision_log_path)),
        kill_switch_file=Path(os.environ.get("KILL_SWITCH_FILE", Settings.kill_switch_file)),
        log_level=os.environ.get("LOG_LEVEL", Settings.log_level),
    )
    if settings.confidence_threshold == 0.0:
        # Not an error: `Settings(confidence_threshold=0.0)` is how the sweep measures the floor's
        # own cost. But reaching it from the environment means FR-02's floor is off, and the log
        # would still read `threshold_applied=0.0` as though a threshold had been applied. On the
        # development set that setting answers 415 tickets, 112 of them against their label and 10
        # of them labelled must-escalate (evaluation/reports/confidence_sweep.md).
        _log.warning(
            "CONFIDENCE_THRESHOLD is 0.0, so FR-02's confidence floor never fires: every ticket "
            "that clears the other rules will be answered whatever the model's confidence. Set a "
            "threshold from evaluation/reports/confidence_sweep.md.")
    return settings


def _load_env_file(path: Path) -> None:
    """A tiny .env reader: values already in the environment win, as dotenv does."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        line = line.removeprefix("export ").strip()
        key, _, value = line.partition("=")
        value = _strip_comment(value)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]  # one matching pair, not every quote character
        os.environ.setdefault(key.strip(), value)


def _optional_path(name: str) -> Path | None:
    raw = os.environ.get(name, "").strip()
    return Path(raw) if raw else None


def _number(name: str, default: float) -> float:
    """A malformed number is a `ConfigError`, never a silent fallback to the placeholder."""
    raw = _strip_comment(os.environ.get(name, ""))
    if not raw:
        return float(default)
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{name}={raw!r} is not a number. Fix it in .env; it will not be guessed."
        ) from exc


def _whole_number(name: str, default: int) -> int:
    value = _number(name, default)
    if value != int(value):
        raise ConfigError(f"{name}={value} must be a whole number")
    return int(value)


def _strip_comment(raw: str) -> str:
    """Drop a trailing ` # comment`, but never from a quoted value."""
    value = raw.strip()
    if value[:1] in {'"', "'"}:
        return value
    return value.split(" #")[0].strip()
