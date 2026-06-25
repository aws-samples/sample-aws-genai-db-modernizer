# =============================================================================
# Stage 1: Download pre-built k6 binary
# =============================================================================
# Stock k6 binary from official releases. xk6-mongo (DocumentDB) and xk6-redis
# (ElastiCache) extensions are loaded at runtime via k6's JavaScript API rather
# than compiled in — the test scripts use the k6/x/redis module which is bundled
# in the official k6 binary since v0.49.0, and MongoDB operations go through the
# k6 HTTP API against DocumentDB's REST-compatible endpoints.
#
# If a custom xk6 build is needed in the future, the CI runner needs outbound
# HTTPS access to github.com or a GOPROXY mirror configured.

FROM python:3.12-slim@sha256:9e01bf1ae5db7649a236da7be1e94ffbbbdd7a93f867dd0d8d5720d9e1f89fab
# checkov:skip=CKV_DOCKER_2:Agent is run-to-completion, HEALTHCHECK not applicable

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_CACHE_DIR=/app/.uv-cache

ARG K6_VERSION=v0.56.0

WORKDIR /app

# Install system dependencies (curl + ca-certificates for downloads, gnupg for legacy reasons)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gnupg ca-certificates && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Download official k6 binary (includes xk6-redis since v0.49.0)
RUN curl -fsSL "https://github.com/grafana/k6/releases/download/${K6_VERSION}/k6-${K6_VERSION}-linux-amd64.tar.gz" \
      | tar -xz --strip-components=1 -C /usr/local/bin/ && \
    chmod +x /usr/local/bin/k6 && \
    k6 version

# Download DocumentDB CA bundle for TLS connections to Amazon DocumentDB clusters.
# Path matches DOCDB_CA_BUNDLE_PATH constant in src/agents/load_test/documentdb/provisioner.py
RUN curl -fsSL https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
      -o /etc/ssl/certs/docdb-global-bundle.pem && \
    chmod 644 /etc/ssl/certs/docdb-global-bundle.pem

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install dependencies (locked, no dev extras)
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy application code
COPY src/ ./src/

# Copy entrypoint
COPY src/agents/entrypoint.py .

# Pre-bundle k6 AWS jslib for offline use (avoids runtime download for DynamoDB scenarios)
RUN mkdir -p /app/k6-libs && \
    (curl -fsSL "https://jslib.k6.io/aws/0.13.0/aws.js" -o /app/k6-libs/aws.js || true)

# Create non-root user
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --no-create-home appuser && \
    chown -R appuser:appuser /app

USER appuser

# Agent is run-to-completion — no HEALTHCHECK needed
# Exit 0 = success (Step Functions moves to next state)
# Exit non-zero = failure (Step Functions retries or catches)
CMD ["uv", "run", "python", "entrypoint.py"]
