# Database Modernizer Assessment

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-MIT--0-green.svg)](LICENSE)

Modernizing off a monolithic relational database is hard. Which queries belong in DynamoDB? Which need a document store? What stays relational? Getting it wrong means failed modernizations, re-architecture mid-project, and wasted months.

**Database Modernizer Assessment answers that question automatically.** Point it at your PostgreSQL or MySQL database, and it analyzes every query pattern, scores each one against 6 AWS purpose-built engines, validates the architecture, and produces ready-to-implement schema designs with TCO projections.

**Supported sources:** PostgreSQL, MySQL, MariaDB
**Target engines:** DynamoDB, DocumentDB, ElastiCache/Redis, OpenSearch, Aurora PostgreSQL, Aurora MySQL

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

---

## Getting Started

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (Python package manager)

### Install

```bash
git clone https://github.com/aws-samples/aws-genai-db-modernizer.git
cd aws-genai-db-modernizer
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

# Run all tests
uv run pytest tests/ -v --cov=src

# Run specific suites
uv run pytest tests/unit/ -v
uv run pytest tests/contract/ -v
uv run pytest tests/integration/ -v

# Code quality
uv run black src/ tests/
uv run ruff check src/ tests/
uv run mypy src/

# Full dev setup (pre-commit hooks, cfn-nag, etc.)
./scripts/setup_dev.sh
```

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
STORAGE_TYPE=local ARTIFACT_ROOT=./artifacts uv run uvicorn src.api.main:app --host 0.0.0.0 --port 8000

# Build and serve the UI (in another terminal)
cd src/ui && npm install
REACT_APP_API_URL=http://localhost:8000/api/v1/ npx react-scripts build
npx serve -s build -l 3000
```

Then open `http://localhost:3000` to browse your modernization results.

---

## Project Structure

```
src/
  agents/           # Pipeline agents (collector, analysis, referee, schema_design)
  contracts/        # Pydantic I/O contracts between phases
  orchestrator/     # Local and Step Functions orchestrators
  storage/          # Artifact store (S3 or local filesystem)
  tools/            # Analysis tools, scoring, pattern catalogs
  api/              # FastAPI backend
  ui/               # React frontend
scripts/            # CLI entry points and collection scripts
infrastructure/     # CloudFormation templates for AWS deployment
docs/               # Architecture docs, contracts, guides
tests/              # Unit, contract, and integration tests
```

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
