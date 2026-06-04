# Contributing to Database Modernizer Assessment

Thank you for your interest in contributing to Database Modernizer Assessment! This document provides guidelines for contributing to the project.

## Table of Contents

1. [Code of Conduct](#code-of-conduct)
2. [Reporting Bugs/Feature Requests](#reporting-bugsfeature-requests)
3. [Getting Started](#getting-started)
4. [Development Workflow](#development-workflow)
5. [Branching Strategy](#branching-strategy)
6. [Commit Message Format](#commit-message-format)
7. [Pull Request Process](#pull-request-process)
8. [Code Standards](#code-standards)
9. [Testing Requirements](#testing-requirements)
10. [Documentation](#documentation)
11. [Security](#security)
12. [Licensing](#licensing)

---

## Code of Conduct

This project has adopted the [Amazon Open Source Code of Conduct](https://aws.github.io/code-of-conduct).
For more information see the [Code of Conduct FAQ](https://aws.github.io/code-of-conduct-faq) or contact
<opensource-codeofconduct@amazon.com> with any additional questions or comments.

---

## Reporting Bugs/Feature Requests

We welcome you to use the GitHub issue tracker to report bugs or suggest features.

When filing an issue, please check existing open, or recently closed, issues to make sure somebody else hasn't already reported the issue. Please try to include as much information as you can:

- A reproducible test case or series of steps
- The version of our code being used
- Any modifications you've made relevant to the bug
- Anything unusual about your environment or deployment

---

## Getting Started

### Prerequisites

- Python 3.12+
- Git
- uv (Python package manager)
- Node.js 18+ (for UI development)
- AWS CLI configured with credentials (for cloud deployment)

### Quick Setup

```bash
# Clone repository
git clone https://github.com/aws-samples/sample-aws-genai-db-modernizer.git
cd sample-aws-genai-db-modernizer

# Run setup script (installs deps, pre-commit hooks, validates environment)
./scripts/setup_dev.sh

# Verify installation
uv run python scripts/run_assessment.py --file docs/examples/wordpress/wordpress-collection.json
```

The setup script will:

1. Check Python 3.12+ is installed
2. Install uv if not present
3. Create virtual environment and install all dependencies
4. Install pre-commit hooks (formatting, linting, type checking, security scanning)
5. Run initial validation

### Pre-commit Hooks

Pre-commit hooks run automatically on every commit to ensure code quality:

- **Black** - format Python code
- **isort** - sort imports
- **Ruff** - lint Python code
- **mypy** - type checking
- **Bandit** - security scanning
- **markdownlint** - lint Markdown files
- **Conventional Commits** - validate commit message format
- General checks: YAML/JSON syntax, large files, private keys, merge conflicts

If hooks don't run after cloning, install manually:

```bash
uv run pre-commit install
uv run pre-commit install --hook-type commit-msg
```

---

## Development Workflow

### 1. Create an Issue

Before starting work, create an issue describing:

- What you want to build/fix
- Why it's needed
- Proposed approach (for larger changes)

### 2. Fork and Branch

```bash
# Fork the repo on GitHub, then clone your fork
git clone https://github.com/YOUR_USERNAME/sample-aws-genai-db-modernizer.git
cd sample-aws-genai-db-modernizer

# Add upstream remote
git remote add upstream https://github.com/aws-samples/sample-aws-genai-db-modernizer.git

# Create feature branch
git checkout -b feat/your-feature-name
```

### 3. Make Changes

- Write code following our [Code Standards](#code-standards)
- Add tests for new functionality
- Update documentation as needed
- Run tests locally before committing

### 4. Commit Changes

Follow our [Commit Message Format](#commit-message-format). Set up the commit template to guide your messages:

```bash
git config commit.template .gitmessage
```

Then commit as usual:

```bash
git add .
git commit -m "feat(collector): add CloudWatch metrics collection"
```

### 5. Push and Create Pull Request

```bash
git push origin feat/your-feature-name
```

Then open a Pull Request on GitHub against `main`.

---

## Branching Strategy

We use a **trunk-based** strategy with short-lived feature branches:

### Branch Types

| Branch Type | Naming Convention | Purpose | Base Branch |
|---|---|---|---|
| **main** | `main` | Production-ready code | - |
| **feature** | `feat/<description>` | New features | main |
| **fix** | `fix/<description>` | Bug fixes | main |
| **hotfix** | `hotfix/<description>` | Urgent production fixes | main |
| **docs** | `docs/<description>` | Documentation updates | main |

---

## Commit Message Format

We follow [**Conventional Commits**](https://www.conventionalcommits.org/en/v1.0.0/#summary) specification:

### Format

```
<type>(<scope>): <subject>

[optional body]

[optional footer]
```

### Types

| Type | Description | Example |
|------|-------------|---------|
| **feat** | New feature | `feat(collector): add MySQL collector agent` |
| **fix** | Bug fix | `fix(api): handle connection timeout errors` |
| **docs** | Documentation only | `docs(readme): update installation instructions` |
| **refactor** | Code refactoring | `refactor(agent): simplify error handling` |
| **perf** | Performance improvements | `perf(collector): optimize schema collection` |
| **test** | Adding or updating tests | `test(collector): add contract validation tests` |
| **chore** | Maintenance tasks | `chore(deps): update boto3 to 1.29.7` |
| **ci** | CI/CD changes | `ci(github): add contract validation workflow` |

### Scopes

Common scopes in this project:

- `collector` - Collector agents
- `analysis` - Analysis agents
- `referee` - Referee agent
- `schema-design` - Schema design agents
- `api` - API server
- `ui` - Frontend/UI
- `contracts` - Contract models (Pydantic)
- `infra` - Infrastructure/deployment

### Rules

1. Use imperative mood: "add" not "added" or "adds"
2. Don't end subject with period
3. Limit subject line to 50 characters
4. Separate subject from body with blank line
5. Use body to explain what and why, not how
6. Reference issues: Use "Closes #123" or "Fixes #456"

---

## Pull Request Process

### Before Creating a PR

1. **Update your branch**

   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

2. **Run all tests**

   ```bash
   uv run pytest tests/ -v
   ```

3. **Verify linters pass** (pre-commit runs these automatically on commit, but you can run manually):

   ```bash
   uv run pre-commit run --all-files
   ```

4. **Update documentation** if needed

### PR Title Format

Use the same format as commit messages:

```
feat(collector): add MySQL collector agent with RDS support
fix(api): handle timeout errors in job status endpoint
```

### PR Size Guidelines

- **Small PR**: < 200 lines changed (preferred)
- **Medium PR**: 200-500 lines changed
- **Large PR**: > 500 lines changed (consider splitting)

Smaller PRs are reviewed faster and merged more easily.

---

## Code Standards

### Python Code Style

We follow **PEP 8** enforced via pre-commit hooks. On every commit, the following run automatically:

- **Black** - code formatting
- **isort** - import sorting
- **Ruff** - linting
- **mypy** - type checking
- **Bandit** - security scanning

If you need to run them manually:

```bash
uv run pre-commit run --all-files
```

### Code Quality Rules

1. **Type Hints**: Use type hints for all function signatures
2. **Docstrings**: Use Google-style docstrings for public functions
3. **Error Handling**: Handle errors gracefully with specific exception types
4. **Logging**: Use structured logging via `structlog`
5. **Constants**: Use uppercase for module-level constants

### File Organization

```
src/
├── agents/          # Pipeline agents (collector, referee, schema_design, load_test)
├── api/             # FastAPI application and routes
├── contracts/       # Pydantic models for agent I/O
├── orchestrator/    # Pipeline orchestration (local + Step Functions)
├── skills/          # Markdown prompts for LLM behavior
├── storage/         # Artifact storage abstraction
├── tools/           # Deterministic tools
│   ├── analysis/    #   Workload analysis (DynamoDB, DocumentDB, Redis, OpenSearch)
│   ├── aws/         #   AWS service integrations (RDS, S3, SSM)
│   └── database/    #   Database-specific tooling (MySQL, PostgreSQL)
└── ui/              # React frontend
```

---

## Testing Requirements

### Test Types

1. **Unit Tests** (`tests/unit/`) - Test individual functions/tools
2. **Contract Tests** (`tests/contract/`) - Validate Pydantic model conformance
3. **Integration Tests** (`tests/integration/`) - Test agent workflows end-to-end

### Running Tests

```bash
# Run all tests
uv run pytest tests/ -v

# Run with coverage
uv run pytest tests/ --cov=src --cov-report=html

# Run only contract tests
uv run pytest tests/contract/ -v
```

### Local Pipeline Testing

For end-to-end testing of the full analysis pipeline:

```bash
# Run assessment (collect through reality-check):
uv run python scripts/run_assessment.py --file docs/examples/wordpress/wordpress-collection.json

# Resume from existing job:
uv run python scripts/run_assessment.py --job-id <id> --db <name>

# Finalize reality check after LLM response:
uv run python scripts/run_assessment.py --job-id <id> --db <name> --resume-reality-check
```

This runs the deterministic pipeline (Collect, Triage, Analyze, Assign, Reality Check) and stops. Schema design and synthesis require LLM reasoning and are handled separately via Claude Code slash commands or `--all --llm-mode bedrock`.

---

## Documentation

All contributions should include appropriate documentation:

1. **Docstrings** - All public functions must have docstrings
2. **README updates** - Update relevant README files for new features
3. **Guide updates** - Update implementation guides in `docs/guides/` if behavior changes

---

## Security

If you discover a potential security issue in this project we ask that you notify AWS/Amazon Security via our [vulnerability reporting page](http://aws.amazon.com/security/vulnerability-reporting/). Please do **not** create a public GitHub issue.

---

## Licensing

See the [LICENSE](LICENSE) file for our project's licensing. We will ask you to confirm the licensing of your contribution.

By contributing, you agree that your contributions will be licensed under the MIT-0 License.

---

## Finding Contributions to Work On

Looking at the existing issues is a great way to find something to contribute on. Issues labeled `good first contribution` or `help wanted` are great places to start.

---

## Contract Changes

Breaking changes to contracts (`src/contracts/`) require special process:

1. Create a proposal issue with rationale and impact analysis
2. Increment the contract version
3. Provide a migration guide
4. Update all affected agents

See `docs/contracts/agent-contracts-spec.md` for details.

---

## Questions?

- **General questions**: Create a discussion on GitHub
- **Bug reports**: Use the bug report issue template
- **Feature requests**: Use the feature request issue template

---

**Thank you for contributing to Database Modernizer Assessment!**
