# Database Modernizer Assessment

[![CI](https://github.com/aws-samples/sample-aws-genai-db-modernizer/actions/workflows/ci.yml/badge.svg)](https://github.com/aws-samples/sample-aws-genai-db-modernizer/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/badge/coverage-93%25-brightgreen.svg)](https://github.com/aws-samples/sample-aws-genai-db-modernizer/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-MIT--0-green.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-help%20site-blue.svg)](https://aws-samples.github.io/sample-aws-genai-db-modernizer/)

Database Modernizer Assessment reads the schema and query statistics of a relational database and tells you which AWS purpose-built database should serve each query. It then designs the target schemas and writes an executive report, an engineering report and a cost projection, all from your real workload.

> **Disclaimer:** This is a sample project intended for educational and evaluation purposes. It requires proper review, testing, and modification before use in production environments. Use at your own risk.

![Animated demo of one /modernize run in Claude Code on the synthetic WordPress sample: the approval gate places 108 queries on Aurora MySQL and 55 on DynamoDB with an ElastiCache cache layer, schema design and reviews play as a time-lapse, the run ends with three waves at $1,300.45 per month and 9 medium risks, then the results UI and four pages of the executive report.](docs/assets/modernizer-demo.gif)

*A full `/modernize` run on the WordPress sample (about 21 minutes, time-lapsed to 76 seconds) with its one approval gate and final summary, then the job's results and summary pages in the local UI and four pages of the executive report PDF.*

The [help site's front page](https://aws-samples.github.io/sample-aws-genai-db-modernizer/) walks through a recorded run, and its [overview](https://aws-samples.github.io/sample-aws-genai-db-modernizer/overview/) says what the tool answers.

## Quick start

<!-- --8<-- [start:quickstart] -->
You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Node 22 is optional: with it, a local UI opens next to the chat. The assessment needs no AWS account, API key or Docker. Only the optional steps after it (`/test-routines` and `/move`, below) use an AWS account, with your own credentials.

### 1. Install

```bash
git clone https://github.com/aws-samples/sample-aws-genai-db-modernizer.git
cd sample-aws-genai-db-modernizer
uv sync
```

### 2. Run the bundled sample

The repository ships a WordPress + WooCommerce sample (50 tables, 163 queries). Open the folder in Claude Code and run:

```
/modernize docs/examples/wordpress/wordpress-collection.json
```

The recorded run in the demo above took about 21 minutes, including the first build of the local UI and the wait at the approval gate. It stops once, after the engine assignment, for your approval. After you approve, it finishes on its own. Add `--mode chat` for a terminal-only run with no UI.

The samples are synthetic: their data is generated test data, not customer data. The WordPress collection was taken after a seeded load of "16,071 HTTP requests in 1,643 seconds", and "each statement's call rate is its execution count over its own first-to-last-seen window, about 27 minutes of the workload" ([its README](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/examples/wordpress/README.md)). A larger PostgreSQL sample, Discourse (311 tables and 1,654 queries), is in `docs/examples/discourse/`.

### 3. Read the results

The run writes its deliverables to <!-- deliverables-list -->`artifacts/<db>/<job>/deliverables/`: `executive-report.pdf` (executive report: a landscape briefing), `engineering-report.md` (engineering report), `assessment-data.json` (Assessment Data, JSON), `engineering-report-detailed.md` (detailed engineering report: the full routing detail, for auditing the result), and `index.md`, which lists who each file is for and the assignment version<!-- /deliverables-list -->. The chat also links the job's results page in the local UI, with the same assessment and a download button for each report.

### 4. Collect your own database

`/modernize` reads one collection file. You make it yourself: run the read-only collection script for your engine on your own database, with your own credentials. The scripts are `scripts/collect-postgresql.sql`, `scripts/collect-mysql.sql` (MySQL and MariaDB), `scripts/collect-sqlserver.sql` and `scripts/collect-oracle.sql`. Each one writes the schema, the statements and their counters into one file. The MySQL and PostgreSQL scripts read table rows only to measure the longest value of each large-object column, in tables of at most 1,024 MB (`lob_scan_max_mb=0` turns that off); no row value goes into the file. On MySQL and PostgreSQL the statements come from the server's digests, with their values replaced by placeholders. The file also holds routine code and view definitions with their literals; the modernizer redacts those literals when it reads the collection in, and keeps each routine's exact text only in the job's routine sources, so step 1 can carry it. To leave routine code out, turn routine bodies off (for MySQL, `SET @include_routine_bodies=0`). The scripts are plain SQL, so you can read one before you run it. [Analyze your own database](https://aws-samples.github.io/sample-aws-genai-db-modernizer/use-claude-code/#analyzing-your-own-database) gives the command for each engine and the grants it needs. Then run `/modernize <database>-collection.json`.

### 5. After the assessment

These steps are optional. Each one is a command in the same Claude Code session:

- `/test-routines` tests the routines the plan carries (functions, procedures, triggers and events) on a throwaway Aurora test cluster in your account, before any data moves. Step 1 deploys only the routines this test found no difference on.
- `/move step-1` moves a MySQL or PostgreSQL source database into the Aurora engine the plan chose, with AWS DMS: a full load, then ongoing replication.
- `/wave-edit <N>` revisits one wave's design review warnings and redesigns only that wave.
- `/explore <question>` answers a question about the assessment from its context graph.

`/test-routines` and `/move` ask before each AWS write and bill your account for what they create. Neither one connects to your source database or your Aurora cluster: you run the SQL scripts step 1 writes, and DMS reads and writes the databases with the secrets you give it. [After the assessment](https://aws-samples.github.io/sample-aws-genai-db-modernizer/use-claude-code/#after-the-assessment) covers each command.

### No Claude Code?

One command runs the whole pipeline with no model and no credentials:

```bash
uv run python scripts/run_assessment.py --file docs/examples/wordpress/wordpress-collection.json --db wordpress --llm-mode none --all -y
```

It writes the same deliverables folder. Schema design is skipped and the executive summary is fixed text, because those two need a model. [Other ways to run it](https://aws-samples.github.io/sample-aws-genai-db-modernizer/other-ways-to-run-it/) covers this command, Amazon Bedrock, the Python library and the local UI on its own.
<!-- --8<-- [end:quickstart] -->

## Where to go next

The [help site](https://aws-samples.github.io/sample-aws-genai-db-modernizer/) has everything below:

- [Analyze your own database](https://aws-samples.github.io/sample-aws-genai-db-modernizer/use-claude-code/#analyzing-your-own-database): the read-only collection scripts, and how to limit the engines the plan may use.
- [Understand the results](https://aws-samples.github.io/sample-aws-genai-db-modernizer/understand-the-results/): what each deliverable shows and how to read an assignment.
- [After the assessment](https://aws-samples.github.io/sample-aws-genai-db-modernizer/use-claude-code/#after-the-assessment): test the routines, move step 1 with AWS DMS, revisit a wave, ask about the result.
- [Sample report](https://aws-samples.github.io/sample-aws-genai-db-modernizer/sample/): the deliverables of a real run, before you run anything.
- [Use Claude Code](https://aws-samples.github.io/sample-aws-genai-db-modernizer/use-claude-code/): every `/modernize` option and the one-phase-at-a-time commands.
- [FAQ](https://aws-samples.github.io/sample-aws-genai-db-modernizer/faq/): model access, permissions, the UI, collector grants.
- [Contribute](https://aws-samples.github.io/sample-aws-genai-db-modernizer/contribute/), [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md): the architecture, the tests and the review process.

## What it does

<!-- --8<-- [start:overview] -->
Moving off a monolithic relational database raises hard questions. Which queries belong in DynamoDB? Which need a document store? What stays relational? A wrong answer can force a redesign in the middle of the project.

**Database Modernizer Assessment answers these questions using your real workload.** It scores every query against the AWS purpose-built engines, places each query on its best-fit engine, checks that the resulting architecture holds together, and produces schema designs, a migration roadmap in waves and a cost projection you can implement. The engine placement is deterministic: the scores decide it, not a model, so it comes out the same on every run and can be audited. A model writes the schema designs and the executive summary.

- **Sources:** PostgreSQL, MySQL, MariaDB, SQL Server and Oracle. A SQL Server or Oracle source moves to whichever Aurora engine scores higher on its workload (Aurora PostgreSQL unless Aurora MySQL scores clearly higher), so wave 1 includes a schema and dialect conversion (the plan says that wave is not yet optimized for heterogeneous migrations).
- **Targets:** DynamoDB, DocumentDB, Aurora PostgreSQL and Aurora MySQL own queries; ElastiCache can sit in front as a cache, and OpenSearch can serve search as a read model.
- **Your data stays yours:** the collection scripts are read-only, and no row value goes into the collection. They collect the schema, query shapes and counters; query text can still carry literal values.

## Who is this for?

Teams that have **decided to refactor their application** to use purpose-built databases. It helps you figure out which queries go where and what the target schemas should look like.

## Who is this NOT for?

- **Lift-and-shift migrations**: If you're moving a database as-is to RDS or Aurora without changing the data model, you don't need this tool.
- **Tight deadline migrations**: This tool guides application refactoring, which takes time. If you need to migrate by next week, use AWS DMS for a straight move.
- **Teams that haven't committed to refactoring**: If you're still deciding whether to modernize, start with the [AWS Migration Evaluator](https://aws.amazon.com/migration-evaluator/) or a Well-Architected review first.
<!-- --8<-- [end:overview] -->

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
