# AWS Transform DB Modernization Orchestrator
#
# IMPORTANT: Must be built for linux/arm64 — Bedrock AgentCore runs on Graviton.
# Build command:
#   docker build --platform linux/arm64 -t db-modernization-orchestrator:latest \
#     -f src/atx_orchestrator/Dockerfile .
#
# The SDK requires Python 3.11+. This project uses 3.12 locally, but 3.11-slim
# is used here because that's what the Dockerfile template mandates and it
# satisfies the >=3.11 requirement.

FROM --platform=linux/arm64 public.ecr.aws/docker/library/python:3.11-slim

WORKDIR /app

# ── System deps ────────────────────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# ── AWS Transform SDK ───────────────────────────────────────────────────────
# Install SDK first so botocore models can be registered before other deps.
RUN pip install --no-cache-dir \
    "agent-builder-sdk-aws-transform>=1.0.0" \
    "agent-builder-agentic-mcp-aws-transform>=1.0.0" \
    "agent-builder-types-aws-transform>=1.0.0" \
    "agent-builder-mcp-client-aws-transform>=1.0.0"

# Register botocore service models (REQUIRED — without this the agent fails at
# startup with "Unknown service: 'transformagenticservice'")
# NOTE: `aws configure add-model` writes to $HOME/.aws/models/. This RUN
# executes as root so models land in /root/.aws/models/, which is 700 and
# unreadable by the non-root appuser we drop to below. Copy them to a
# world-readable location and set AWS_DATA_PATH so boto3 finds them
# regardless of who's running the container.
RUN pip install --no-cache-dir awscli && \
    SDK_MODELS=$(python -c "from importlib.resources import files; print(files('agent_builder_sdk').joinpath('botocore_models'))") && \
    aws configure add-model --service-name atxagentregistryexternal \
      --service-model "file://${SDK_MODELS}/atxagentregistryexternal/2022-07-26/service-2.json" && \
    aws configure add-model --service-name transformagenticservice \
      --service-model "file://${SDK_MODELS}/transformagenticservice/2018-05-10/service-2.json" && \
    mkdir -p /opt/botocore-models && \
    cp -r /root/.aws/models/* /opt/botocore-models/ && \
    chmod -R a+r /opt/botocore-models

# ── Project deps ────────────────────────────────────────────────────────────
COPY src/atx_orchestrator/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# ── MCP server shim ─────────────────────────────────────────────────────────
# AgentRuntimeServer looks for this exact binary path at startup.
RUN mkdir -p /home/amazon/AgentBuilderAgenticMCP/bin && \
    printf '#!/bin/bash\npython -m agent_builder_agentic_mcp "$@"\n' \
      > /home/amazon/AgentBuilderAgenticMCP/bin/agent-builder-agentic-mcp && \
    chmod +x /home/amazon/AgentBuilderAgenticMCP/bin/agent-builder-agentic-mcp

# ── Application source ──────────────────────────────────────────────────────
# Copy the full src/ tree so existing pipeline modules resolve correctly.
COPY src/ /app/src/

# ── Runtime dirs ────────────────────────────────────────────────────────────
RUN mkdir -p /tmp/agent_queue /tmp/orchestrator_agent /app/artifacts

# ── Non-root user (security) ─────────────────────────────────────────────────
RUN useradd --create-home --uid 1000 appuser && \
    chown -R appuser:appuser /app /tmp/agent_queue /tmp/orchestrator_agent \
      /home/amazon/AgentBuilderAgenticMCP
USER appuser

# ── Environment defaults ─────────────────────────────────────────────────────
# Override at runtime via ECS task definition environment variables.
ENV ARTIFACT_DIR=/app/artifacts \
    LLM_MODE=none \
    AWS_REGION=us-east-1 \
    AWS_DATA_PATH=/opt/botocore-models \
    PYTHONUNBUFFERED=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8080/ping || exit 1

ENTRYPOINT ["python", "-m", "src.atx_orchestrator.app"]
