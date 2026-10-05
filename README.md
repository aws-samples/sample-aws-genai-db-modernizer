# Database Modernizer Assessment

[![CI](https://github.com/aws-samples/sample-aws-genai-db-modernizer/actions/workflows/ci.yml/badge.svg)](https://github.com/aws-samples/sample-aws-genai-db-modernizer/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-66%25-yellowgreen.svg)](https://github.com/aws-samples/sample-aws-genai-db-modernizer/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-MIT--0-green.svg)](LICENSE)

> **Disclaimer:** This is a sample project intended for educational and evaluation purposes. It requires proper review, testing, and modification before use in production environments. Use at your own risk.

Modernizing off a monolithic relational database is hard. Which queries belong in DynamoDB? Which need a document store? What stays relational? Getting it wrong means failed modernizations, re-architecture mid-project, and wasted months.

**Database Modernizer Assessment answers that question automatically.** Point it at your PostgreSQL or MySQL database, and it analyzes every query pattern, scores each one against 6 AWS purpose-built engines, validates the architecture, and produces ready-to-implement schema designs with TCO projections.

**Supported sources:** PostgreSQL, MySQL, MariaDB
**Target engines:** DynamoDB, DocumentDB, Aurora PostgreSQL, Aurora MySQL as query owners, plus ElastiCache as a cache layer and OpenSearch as a search read model

## Who is this for?

This tool is for teams that have **decided to refactor their application** to use purpose-built databases. It helps you figure out which queries go where and what the target schemas should look like.

## Who is this NOT for?

- **Lift-and-shift migrations**: If you're moving a database as-is to RDS or Aurora without changing the data model, you don't need this tool.
- **Tight deadline migrations**: This tool guides application refactoring, which takes time. If you need to migrate by next week, use AWS DMS for a straight move.
- **Teams that haven't committed to refactoring**: If you're still deciding whether to modernize, start with the [AWS Migration Evaluator](https://aws.amazon.com/migration-evaluator/) or a Well-Architected review first.

---

## How It Works

The modernizer runs a multi-phase pipeline that progressively narrows from "all possible targets" to a concrete, validated modernization architecture:

```
Collect --> Triage --> Analyze --> Assign --> Reality Check --> Schema Design --> Synthesis
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

The core pipeline through Reality Check is **fully deterministic**. Pattern detection, scoring, assignment, and consolidation all run without any LLM dependency. GenAI enhances the pipeline at key decision points (Schema Design, Synthesis executive summaries) but the analysis and recommendations are reproducible and auditable every time.

### What the Deliverables Show

- One `report.json` feeds every deliverable — the decision report, the engineering report, the executive deck/PDF, and the UI — so a fact that changes in one changes in all of them.
- Query-to-engine assignment is deterministic; a model may explain a decision, never make it.
- Each engine's confidence score is the mean fit of the queries actually routed to it, not every table it analyzed. A score with no table-level evidence behind it is labelled "signal only".
- ElastiCache is a cache-aside layer recommended only for genuinely hot reads — it never owns a query or holds a system of record.
- OpenSearch is a read model only: every table it serves keeps a durable owner in Aurora, DynamoDB, or DocumentDB, and OpenSearch stays in sync rather than holding the only copy.
- The deliverables lay out migration waves: a suggested incremental path from the source database to the recommended architecture, not the only one. The fully decomposed target schema is offered as the direct, single-step alternative.

---

## Getting Started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package manager)

### Install

```bash
git clone https://github.com/aws-samples/sample-aws-genai-db-modernizer.git
cd sample-aws-genai-db-modernizer
uv sync
```

That's it. No AWS account, no API keys, no Docker required.

---

## Usage

The project includes sample databases you can run immediately. Two collector outputs are provided:

- `docs/examples/wordpress/wordpress.zip` WordPress + WooCommerce (50 tables, 107 queries)
- `docs/examples/discourse/discourse.zip` Discourse forum (170+ tables, 500+ queries)

Unzip whichever you want to try:

```bash
unzip docs/examples/wordpress/wordpress.zip -d docs/examples/wordpress/
```

### Option 1: Deterministic Mode (zero config)

Run the assessment pipeline with no credentials, no LLM, no network calls. This executes Triage through Reality Check and produces architecture recommendations in seconds:

```bash
uv run python scripts/run_assessment.py --file docs/examples/wordpress/wordpress-collection.json --db wordpress
```

![Deterministic mode demo](docs/assets/local-modernizer.gif)

Artifacts land in `./artifacts/{db_name}/{job_id}/`.

**What you get without LLM:**

- Workload signal detection (key-value, text search, aggregations, session stores, etc.)
- Per-engine analysis with confidence scores for every query
- Query-to-engine assignment with co-dependency resolution
- Reality Check: engine consolidation, architectural pattern detection, cost savings analysis

### Option 2: Full Pipeline with Claude Code

If you have [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed, you already have everything needed for the full pipeline, including AI-powered Schema Design and Synthesis. No AWS account required.

Use the built-in Claude Code commands for an interactive experience:

```
/modernize           # Run the full pipeline end-to-end
/collect             # Parse collector output and initialize a job
/triage              # Select target engines based on workload signals
/analyze             # Run analysis for all selected engines
/assign              # Assign queries to best-fit engines
/reality-check       # Consolidate engines and validate decisions
/design-schema       # Design target schemas (LLM required)
/synthesize          # Generate the final migration report
```

These commands are defined in `.claude/commands/` and available automatically when you open the project in Claude Code.

**What you get with Claude Code:**

- Everything from deterministic mode, plus:
- Target schema designs (DynamoDB table definitions, DocumentDB collections, OpenSearch mappings, etc.)
- Full migration assessment report with executive summary
- TCO projections and risk analysis

### Option 3: Full Pipeline with Amazon Bedrock

For production use, automation, or running without Claude Code, use Amazon Bedrock as the LLM backend:

```bash
uv run python scripts/run_assessment.py --file docs/examples/wordpress/wordpress-collection.json --db wordpress --llm-mode bedrock --all -y
```

**AWS setup required:**

1. Configure AWS credentials (any standard method works):

   ```bash
   # Option A: AWS CLI profile
   aws configure

   # Option B: Environment variables
   export AWS_ACCESS_KEY_ID=...
   export AWS_SECRET_ACCESS_KEY=...
   export AWS_DEFAULT_REGION=us-east-1

   # Option C: AWS SSO
   aws sso login --profile your-profile
   export AWS_PROFILE=your-profile
   ```

2. Enable model access in [Amazon Bedrock console](https://console.aws.amazon.com/bedrock/home#/modelaccess):
   - Enable **Anthropic Claude Sonnet** (used for Reality Check validation)
   - Enable **Anthropic Claude Opus** (used for Schema Design and Synthesis)

3. Run with Bedrock:

   ```bash
   uv run python scripts/run_assessment.py --file docs/examples/wordpress/wordpress-collection.json --db wordpress --llm-mode bedrock --all -y
   ```

![Bedrock mode demo](docs/assets/local-modernizer-bedrock.gif)

### Option 4: Analyze Your Own Database

To analyze your own database, run the collection script to extract schema and query patterns:

**PostgreSQL** (requires `pg_stat_statements` extension):

```bash
psql -U <user> -h <host> -d <database> -t -A -f scripts/collect-postgresql.sql > my-collection.json
```

**MySQL** (requires `performance_schema`):

```bash
mysql -N -u <user> -p -h <host> -D <database> < scripts/collect-mysql.sql > my-collection.json
```

Then run the pipeline against your collection:

```bash
uv run python scripts/run_assessment.py --file my-collection.json --db my_database --llm-mode bedrock --all -y
```

> **Note:** The collection scripts are read-only and do not modify your database. They need SELECT access to `information_schema`, `pg_stat_statements` (PostgreSQL), or `performance_schema` (MySQL).

---

## LLM Modes Summary

| Mode       | Credentials needed | Phases covered | Best for |
| ---------- | ------------------ | -------------- | -------- |
| `none`     | None               | Collect through Reality Check | Quick evaluation, CI/CD, deterministic audits |
| `external` | Claude Code license | Full pipeline | Local development, interactive exploration |
| `bedrock`  | AWS credentials + Bedrock access | Full pipeline | Production, automation, team use |

---

## Key Design Patterns

### LLM Seam Pattern

Every agent exposes three methods:

1. `run_deterministic()` always runs, produces baseline results
2. `prepare_llm_input()` formats context for the LLM
3. `apply_llm_output()` merges LLM feedback into deterministic results

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
# Install with dev dependencies
uv sync --extra dev

# Code quality: ruff, black, isort, mypy, markdownlint, the command validator
make lint         # or: uv run pre-commit run --all-files

# Unit, contract, property and graph tests (no network, no integration/e2e)
make test         # or: ./ci/test.sh --cov=src --cov-report=term

# Deterministic end-to-end: full pipeline, rendered HTML/PDF, UI smoke test
# (needs the `e2e` extra, Node 22, and Playwright browsers -- ci/e2e.sh
# installs/builds all of that for you)
make e2e          # or: ./ci/e2e.sh

# Headless /modernize --auto run against a real model (chat, ui or both
# mode, on the wordpress or discourse sample), checked with the same
# deliverable checks plus a rubric-based quality judge -- needs model
# access and costs real tokens
make e2e-llm      # or: ./ci/e2e-llm.sh chat wordpress

# Full dev setup (pre-commit hooks, cfn-nag, etc.)
./scripts/setup_dev.sh
```

[AGENTS.md](AGENTS.md) is the working guide for coding agents (and a fast
map for everyone else): the full repo map, the test-tier table, and the
headless `/modernize --auto` rules. What each CI script runs and why is in
[ci/README.md](ci/README.md). Every change is also checked by an internal
validation pipeline before it merges.

### Running Individual Phases

```bash
# Assessment only (phases 1-5, stops after reality-check):
uv run python scripts/run_assessment.py --file <collector-output.json> --db <name>

# Resume after providing LLM response (external mode):
uv run python scripts/run_assessment.py --job-id <id> --db <name> --resume-reality-check

# Full pipeline including schema design + synthesis:
uv run python scripts/run_assessment.py --file <collector-output.json> --db <name> --llm-mode bedrock --all -y
```

### Local Web UI

Run the React web interface locally to visualize results, browse query journeys, and review schema designs:

```bash
# Start the API server
ARTIFACT_DIR=./artifacts uv run uvicorn src.api.main:app --host 127.0.0.1 --port 8000

# Build and serve the UI (in another terminal)
cd src/ui && npm ci
REACT_APP_API_URL=http://localhost:8000/api/v1/ npm run build
npm run serve
```

`npm run serve` uses the `serve` dev dependency pinned in `package.json`, so it works offline after the first install. It runs in single-page-app mode, so deep links such as `/analysis/monitor/summary/<job_id>` load on refresh.

The local API only answers requests addressed to `localhost`, `127.0.0.1` or `::1`. To reach it under another host name, set `MODERNIZER_ALLOWED_HOSTS` to a comma-separated list of names.

Then open `http://localhost:3000` to browse your modernization results.

### Hosted Deployment (Retired)

The hosted platform — ECS Fargate, Step Functions orchestration, Cognito authentication, and the per-environment CloudFormation stacks — is retired. The tool now runs locally: the CLI (`run_assessment.py`), Claude Code (`/modernize` and the other slash commands), or the local API + UI described above. The hosted code (`infrastructure/cloudformation/` and the service Dockerfiles, the `make deploy-*`/`make destroy-*` targets, the Step Functions orchestrator, and the Step Functions/S3 service paths in `src/api`) was removed in #175.

---

## Project Structure

```
src/
  agents/           # Pipeline agents (collector, analysis, referee, schema_design)
  contracts/        # Pydantic I/O contracts between phases
  orchestrator/     # Local phase orchestrator (the Step Functions path was removed, #175)
  storage/          # Artifact store (S3 — AWS Transform integration only — or local filesystem)
  tools/            # Analysis tools, scoring, pattern catalogs
  api/              # FastAPI backend (local-only)
  ui/               # React frontend
  atx_orchestrator/ # AWS Transform integration (separate from the local API/UI)
scripts/            # CLI entry points and collection scripts
infrastructure/     # agent-load-test Docker image; example-ci-runner-iam.yaml and automation.yaml (opt-in live-collection bastion, #342) remain; the hosted deployment templates were removed, #175
docs/               # Architecture docs, contracts, guides
tests/              # Unit, contract, and integration tests
```

---

## Documentation

| Document                                            | Description                       |
| --------------------------------------------------- | --------------------------------- |
| [Architecture](docs/architecture/high-level-design.md) | System architecture and decisions |
| [Agent Contracts](docs/contracts/README.md)         | Pydantic I/O specifications       |
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
