FROM python:3.12-slim@sha256:9e01bf1ae5db7649a236da7be1e94ffbbbdd7a93f867dd0d8d5720d9e1f89fab
# checkov:skip=CKV_DOCKER_2:Agent is run-to-completion, HEALTHCHECK not applicable

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_CACHE_DIR=/app/.uv-cache

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install dependencies (locked, no dev extras)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY src/ ./src/

# Copy entrypoint
COPY src/agents/entrypoint.py .

# Create non-root user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --no-create-home appuser && \
    chown -R appuser:appuser /app

USER appuser

# Agent is run-to-completion — no HEALTHCHECK needed
# Exit 0 = success (Step Functions moves to next state)
# Exit non-zero = failure (Step Functions retries or catches)
CMD ["uv", "run", "python", "entrypoint.py"]
