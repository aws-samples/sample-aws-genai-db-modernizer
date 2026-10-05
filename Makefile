# Database Modernizer Assessment
# =====================================================================
# Local development and CI commands.
#
# Prerequisites:
#   - Python 3.12+ and uv (for local development)
#   - For "make test" to pass completely: the AWS Transform SDK, which ships
#     in the ATX container image but is not a project dependency --
#     `uv pip install "agent-builder-sdk-aws-transform>=1.0.0"` (see
#     tests/unit/atx_orchestrator/conftest.py). Without it, the two test
#     modules that import it fail loudly rather than silently skipping.
#
# Quick start:
#   1. make setup                 # Install dependencies and pre-commit hooks
#   2. make local                 # Start the local API + UI
# =====================================================================

# Bind address for `make local` / `make local-api`. Loopback by default; set
# API_HOST=0.0.0.0 to listen on all interfaces. (Not named HOST: zsh sets
# HOST to the machine name.)
API_HOST        ?= 127.0.0.1

# =====================================================================
# Top-level targets
# =====================================================================

.PHONY: help local local-api test lint e2e e2e-llm setup assess

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup: ## Install dependencies and pre-commit hooks
	./scripts/setup_dev.sh

local: ## Start local API + UI for development
	@echo "Starting local services..."
	@echo "  API:  http://localhost:8000"
	@echo "  UI:   http://localhost:3000"
	@ARTIFACT_DIR=./artifacts \
		uv run uvicorn src.api.main:app --host $(API_HOST) --port 8000 &
	@cd src/ui && npm start

local-api: ## Start only the local API server
	ARTIFACT_DIR=./artifacts \
		uv run uvicorn src.api.main:app --host $(API_HOST) --port 8000 --reload

test: ## Run all tests (unit, contract, property, graph; no integration or e2e). Needs agent-builder-sdk-aws-transform (see header above) for full green.
	./ci/test.sh --cov=src --cov-report=term

lint: ## Run all linters
	uv run pre-commit run --all-files

e2e: ## Deterministic end-to-end run (pipeline, HTML/PDF checks, UI smoke)
	./ci/e2e.sh

E2E_LLM_MODE     ?= chat
E2E_LLM_FIXTURE  ?= wordpress

e2e-llm: ## Headless /modernize run + deliverable checks + quality judge (needs model access; see ci/README.md)
	./ci/e2e-llm.sh $(E2E_LLM_MODE) $(E2E_LLM_FIXTURE)

assess: ## Run sample assessment (WordPress)
	uv run python scripts/run_assessment.py --file docs/examples/wordpress/wordpress-collection.json
