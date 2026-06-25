# =============================================================================
# Stage 1: Build custom k6 binary with xk6-mongo extension
# =============================================================================
# xk6-mongo is required for DocumentDB load testing. Stock k6 does not include
# MongoDB protocol support; we build a custom k6 binary that includes the
# extension. Final image only contains the resulting binary.
#
# Repo and version are configurable via build args so we can swap the upstream
# fork without modifying the Dockerfile (xk6-mongo has multiple community forks).

FROM golang:1.23-bookworm AS xk6-builder

ARG XK6_MONGO_PACKAGE=github.com/szkiba/xk6-mongo
ARG XK6_MONGO_VERSION=latest
ARG XK6_VERSION=v0.11.0

ENV CGO_ENABLED=0
WORKDIR /build

RUN go install go.k6.io/xk6/cmd/xk6@${XK6_VERSION} && \
    xk6 build \
      --output /usr/local/bin/k6 \
      --with ${XK6_MONGO_PACKAGE}@${XK6_MONGO_VERSION}

# =============================================================================
# Stage 2: Agent runtime image
# =============================================================================
FROM python:3.12-slim@sha256:9e01bf1ae5db7649a236da7be1e94ffbbbdd7a93f867dd0d8d5720d9e1f89fab
# checkov:skip=CKV_DOCKER_2:Agent is run-to-completion, HEALTHCHECK not applicable

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_CACHE_DIR=/app/.uv-cache

WORKDIR /app

# Install system dependencies (curl + ca-certificates for downloads, gnupg for legacy reasons)
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl gnupg ca-certificates && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copy custom k6 binary from build stage (stock k6 + xk6-mongo extension)
COPY --from=xk6-builder /usr/local/bin/k6 /usr/local/bin/k6
RUN chmod +x /usr/local/bin/k6

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
