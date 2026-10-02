# The API, built the way the README says to build it: uv, the lockfile, Python 3.14.
FROM python:3.14-slim AS base

# uv installs from the lockfile, so the image gets the versions the tests ran against.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first, so a source change does not reinstall chromadb and onnxruntime.
# README.md is here because pyproject declares `readme = "README.md"`: without it, building the
# project wheel fails at the second `uv sync`, not at the first, which is a confusing place to
# discover a missing file.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
COPY evaluation/ ./evaluation/
COPY scripts/ ./scripts/
COPY prompts/ ./prompts/
COPY data/ ./data/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# `COPY` preserves the host's file modes, and the container runs as a non-root user — so a file
# the author happens to own at 0600 is unreadable inside the image. Review row R11 built this
# image for the first time and found that **all four data files and nine of the ten prompts**
# were `-rw------- root root`: `/search` could not read `documentation.json`, every model call
# would have raised `PromptError`, and training died with "Permission denied". The documented
# Docker path had never worked, and nothing in the suite could see it because the suite does not
# build an image.
#
# `a+rX` adds read for everyone and execute only where it already applies, so directories stay
# traversable and nothing becomes newly executable. It runs before `USER`, while we are still
# root (R11, D-80).
RUN chmod -R a+rX /app/src /app/evaluation /app/scripts /app/prompts /app/data

# Not root: the container writes only to /app/storage, which is a volume.
# `/home/support/.cache` is created and chowned **here**, not left to Docker. A named volume
# mounted over a path that does not exist in the image is created root-owned, and this container
# runs as uid 10001 — so the model cache volume added in R11 to stop the 79MB re-download broke
# the embedder outright: `RetrievalError: ... Permission denied: '/home/support/.cache/chroma'`,
# every ticket 503ing. Docker seeds a new named volume from the image directory, ownership
# included, so creating it here is what makes the volume writable (D-80).
RUN useradd --create-home --uid 10001 support \
    && mkdir -p /app/storage /app/evaluation/results /app/evaluation/reports \
                /home/support/.cache \
    && chown -R support:support /app/storage /app/evaluation/results /app/evaluation/reports \
                               /home/support/.cache
USER support

ENV PATH="/app/.venv/bin:$PATH"

# The kill switch is a file (FR-16), and it lives in the storage volume so an operator can
# `touch` it from the host without entering the container.
ENV DOCS_PATH=/app/data/documentation.json \
    TRAINING_TICKETS_PATH=/app/data/development_tickets.json \
    CLASSIFIER_PATH=/app/storage/classifier.joblib \
    CHROMA_PATH=/app/storage/chroma \
    DECISION_LOG_PATH=/app/storage/decisions.db \
    LLM_CACHE_PATH=/app/storage/llm_cache.sqlite \
    KILL_SWITCH_FILE=/app/storage/KILL_SWITCH

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "ticketing_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
