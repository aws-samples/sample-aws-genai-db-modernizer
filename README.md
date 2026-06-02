# Database Modernizer

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-MIT--0-green.svg)](LICENSE)

Modernizing off a monolithic relational database is hard. Which queries belong in DynamoDB? Which need a document store? What stays relational? Getting it wrong means failed modernizations, re-architecture mid-project, and wasted months.

**Database Modernizer answers that question automatically.** Point it at your PostgreSQL or MySQL database, and it analyzes every query pattern, scores each one against 6 AWS purpose-built engines, validates the architecture, and produces ready-to-implement schema designs with TCO projections. You can run from your laptop, with Claude Code, or deploy to your own AWS account.

The core pipeline is **fully deterministic** - pattern detection, scoring, assignment, and consolidation all run without any LLM dependency. GenAI enhances the pipeline at key decision points (analysis advisors, consolidation validation, executive summaries) but is never required. You get reproducible, auditable results every time, with AI refinement layered on top when available.

**Supported sources:** PostgreSQL, MySQL, MariaDB

**Target engines:** DynamoDB, DocumentDB, ElastiCache/Redis, OpenSearch, Aurora PostgreSQL, Aurora MySQL

---

## How It Works

The modernizer runs a multi-phase pipeline that progressively narrows from "all possible targets" to a concrete, validated modernization architecture:

```
Collect → Triage → Analyze → Assign → Reality Check → Schema Design → Synthesis
```

| Phase             | What it does                                                                                               |
| ----------------- | ---------------------------------------------------------------------------------------------------------- |
| **Collect**       | Connects to the source database (or parses offline output), extracts schema + query patterns               |
| **Triage**        | Detects workload signals (key-value lookups, text search, time-series, etc.) and selects candidate engines |
| **Analyze**       | Runs parallel analysis agents per engine, deterministic scoring + optional LLM advisor                     |
| **Assign**        | Resolves query-to-engine assignments using confidence scores and co-dependency analysis                    |
| **Reality Check** | Consolidates under-committed engines, validates with LLM, redirects unserviceable queries                  |
| **Schema Design** | Designs target schemas per engine (DynamoDB tables, DocumentDB collections, OpenSearch indices, etc.)      |
| **Synthesis**     | Produces the final migration assessment report with TCO, risk analysis, and recommendations                |

**Stack:** Strands SDK (AI agents) · Amazon Bedrock (Claude) · ECS Fargate · FastAPI · React · Pydantic contracts

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- AWS credentials (for Bedrock LLM calls in `bedrock` mode)

### Setup

```bash
git clone https://github.com/aws-samples/aws-genai-db-modernizer.git
cd aws-genai-db-modernizer

# Install dependencies
pip install uv
uv sync --extra dev

# Run tests
uv run pytest tests/ -v
```

### Run Locally (Offline Mode)

You can run the full pipeline locally against a collector output JSON file, no live database connection required:

```bash
# Deterministic only (no LLM calls)
uv run python scripts/test_local_phased.py <collector-output.json> -y

# With LLM advisor (requires AWS credentials for Bedrock)
uv run python scripts/test_local_phased.py <collector-output.json> --llm-mode bedrock -y
```

Artifacts land in `./artifacts/{db_name}/{job_id}/`.

### LLM Modes

| Mode       | Description                                                                                                       |
| ---------- | ----------------------------------------------------------------------------------------------------------------- |
| `none`     | Fully deterministic, no LLM calls, fastest                                                                        |
| `bedrock`  | Production: uses Amazon Bedrock (Claude) for analysis advisors, consolidation validation, and executive summaries |
| `external` | Uses Claude Code as the LLM backend (local development)                                                           |

---

## Key Design Patterns

### LLM Seam Pattern

Every agent exposes three methods:

1. `run_deterministic()` - always runs, produces baseline results
2. `prepare_llm_input()` - formats context for the LLM
3. `apply_llm_output()` - merges LLM feedback into deterministic results

This allows the pipeline to run fully deterministic (`--llm-mode none`) or with LLM enhancement (`--llm-mode bedrock`).

### Group Splitting

Large workloads (1000+ queries) exceed LLM context windows. The `LlmAdvisorBase` automatically:

- Splits queries into groups of 30
- Filters schema to only tables referenced per group
- Calls the LLM per group with retry + exponential backoff
- Merges results across groups

### Reality Check & Consolidation

After assignment, the referee identifies under-committed engines (few queries, low confidence) and consolidates them into stronger engines. An LLM validator confirms the target can serve the moved queries. If not, they redirect to Aurora (the relational safety net).

### Contract-Driven

All agent I/O flows through Pydantic contracts (`src/contracts/`). This enables:

- Automated contract validation in CI
- Deterministic replay of any pipeline stage
- Clear boundaries between pipeline phases

---

## Development

```bash
# Run all tests
uv run pytest tests/ -v --cov=src

# Run specific suites
uv run pytest tests/unit/ -v
uv run pytest tests/integration/ -v
uv run pytest tests/contract/ -v

# Code quality
uv run black src/ tests/
uv run ruff check src/ tests/
uv run mypy src/


# Run a single phase
uv run python scripts/run_triage.py <collector-output.json>
uv run python scripts/run_analysis.py <collector-output.json> --engine dynamodb
uv run python scripts/run_reality_check.py <artifacts-dir>
```

### Claude Code Skills

The project includes Claude Code skills (`.claude/skills/`) that let you run individual pipeline phases interactively from your terminal:

```bash
# Run the full pipeline end-to-end
/modernize

# Run individual phases
/collect           # Parse collector output and initialize a job
/triage            # Select target engines based on workload signals
/analyze           # Run analysis for all selected engines
/analyze-dynamodb  # Run analysis for a specific engine
/assign            # Assign queries to best-fit engines
/reality-check     # Consolidate engines and validate decisions
/design-schema     # Design target schemas in parallel
/synthesize        # Generate the final migration report
```

These skills orchestrate the same pipeline code but through Claude Code's interactive workflow, useful for step-by-step debugging or running with `--llm-mode external` where Claude Code itself acts as the LLM backend.

### Local Web UI

You can also run the full React web interface locally to visualize results, browse query journeys, and review schema designs in a richer format:

```bash
# Start the API server
STORAGE_TYPE=local ARTIFACT_ROOT=./artifacts uv run python -m src.api.main

# Build and serve the UI (in another terminal)
cd src/ui
REACT_APP_API_URL=http://localhost:8000/api/v1/ npm run build
npx serve -s build -l 3000
```

Then open `http://localhost:3000` to browse your modernization results.

---

## CI/CD Pipeline

GitHub Actions with the following stages:

1. **Security** - Semgrep, cfn-nag, Checkov, Bandit
2. **Lint** - ruff, black, mypy, isort
3. **Test** - pytest with coverage (unit, contract, integration)
4. **Deploy** - CloudFormation stack deployment

---

## Documentation

| Document                                            | Description                       |
| --------------------------------------------------- | --------------------------------- |
| [Architecture](docs/architecture/high-level-design.md) | System architecture and decisions |
| [Agent Contracts](docs/contracts/README.md)         | Pydantic I/O specifications       |
| [API Guide](docs/API_GUIDE.md)                      | REST API reference                |
| [Implementation Guides](docs/guides/README.md)      | Development patterns              |

---

## Contributing

1. Review [agent contracts](docs/contracts/README.md)
2. Pick a component from [implementation guides](docs/guides/README.md)
3. Follow TDD: contracts → tests → implementation
4. Submit PR with tests and documentation

See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

---

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
