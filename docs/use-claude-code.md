# Use Claude Code

`/modernize` is the main entry point. Each phase is also its own command, so you can run one step at a time or re-run a step after a change.

## How `/modernize` works

```mermaid
graph LR
    A[Collect] --> B[Triage]
    B --> C[Analyze]
    C --> D[Assign]
    D --> E[Reality Check]
    E -->|approve| F[Schema Design]
    F --> G[Synthesis]
```

`/modernize` runs in one of three modes: `chat`, `ui` or `both` (the default). In `ui` and `both` it starts the local API and UI before the pipeline. The chat always shows the key numbers for each phase, so you can follow the run and decide from the chat alone. In `both` mode it also points to the matching UI view: the Sankey and assignment page at the Reality Check approval, the schema designs after phase 6, and the full report at the end. If the UI can't start (a busy port, a failed build, or no Node), the run switches to chat and continues.

Use `/modernize docs/examples/wordpress/wordpress-collection.json --mode chat` for a terminal-only run: no browser, no Node required, works in headless environments. `wordpress-collection.json` is bundled as `docs/examples/wordpress/wordpress.zip`; the script extracts it next to the zip the first time you name it, so there is nothing to unzip by hand.

## The individual commands

| Command | What it does |
| --- | --- |
| `/collect` | Parses collector output and initializes a job |
| `/triage` | Detects workload signals and selects candidate engines |
| `/analyze` | Runs analysis agents for every triage-selected engine |
| `/assign` | Resolves query-to-engine assignments |
| `/reality-check` | Consolidates under-committed engines and validates with a model |
| `/design-schema` | Designs target schemas per engine (needs a model) |
| `/synthesize` | Produces the final report and renders the deliverables into `artifacts/<db>/<job>/deliverables/` (see [Understand the results](understand-the-results.md#the-deliverables)) |

Each command is defined in `.claude/commands/` and reads the job state from `.modernizer-state.json`, so you can run them out of order to re-do a single phase after changing an earlier decision.

## Analyzing your own database

Run the read-only collection script for your database engine instead of the sample zip:

```bash
# PostgreSQL (needs the pg_stat_statements extension)
psql -U <user> -h <host> -d <database> -t -A -f scripts/collect-postgresql.sql > my-collection.json

# MySQL (needs performance_schema)
mysql -N -u <user> -p -h <host> -D <database> < scripts/collect-mysql.sql > my-collection.json
```

The scripts only run `SELECT` on `information_schema`, `pg_stat_statements` or `performance_schema`. They never change your database. Then run `/modernize` (or `/collect`) and point it at the generated file instead of the sample.

Every ingestion path (`/modernize`, `scripts/run_assessment.py --file`, `scripts/run_collect.py --file`, the local UI upload, and the AWS Transform upload) rejects a collection above 100 MB or above 50,000 tables or 20,000 queries, with a clear error naming the limit that was hit. These defaults comfortably cover the bundled samples and the larger query collections proposed in [#384](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/384); raise them with the `MODERNIZER_MAX_UPLOAD_BYTES`, `MODERNIZER_MAX_UPLOAD_TABLES`, and `MODERNIZER_MAX_UPLOAD_QUERIES` environment variables.

### Tell the assessment which other engines you already run

A saving for retiring an engine counts only if you run that engine today, so the collection records which ones. The source engine is always recorded. If the application also uses another AWS database, for example an OpenSearch domain or an ElastiCache cluster, declare it when you ingest the collection: `uv run python scripts/run_collect.py --file my-collection.json --engines-in-use opensearch,elasticache` (`scripts/run_assessment.py` takes the same option). The ids are the engine ids (`opensearch`, `elasticache`, `documentdb`, `dynamodb`, `aurora_mysql`, `aurora_postgresql`; the legacy `aurora` is refused); an unknown id is refused with the list. They are added to any `metadata.engines_in_use` the file already lists, and a collection that declares nothing behaves as before: only the source engine counts. `/modernize` and `/collect` accept the same `--engines-in-use` argument and pass it on. A live collector run reads the same list from the `ENGINES_IN_USE` environment variable.

## Optional: attach an Amazon Bedrock Guardrail

If Claude Code reaches the model through Amazon Bedrock (`CLAUDE_CODE_USE_BEDROCK=1`), you can attach a guardrail you own to every model call it makes, including the ones `/modernize` triggers. It is off unless you set it. Claude Code sends the two headers below with each request, and Bedrock applies the guardrail to it.

Set `ANTHROPIC_CUSTOM_HEADERS` in your Claude Code settings (for example `~/.claude/settings.json`). The two headers are separated by a newline:

```json
{
  "env": {
    "ANTHROPIC_CUSTOM_HEADERS": "X-Amzn-Bedrock-GuardrailIdentifier: <guardrail-id>\nX-Amzn-Bedrock-GuardrailVersion: <guardrail-version>"
  }
}
```

- The version must be a published, numbered version. `DRAFT` is not accepted.
- If Claude Code uses a cross-region inference profile (the `us.` model IDs are one), a guardrail without a cross-region guardrail profile worked with the default `us.` model IDs when attached to this project's own Bedrock calls (`--llm-mode bedrock`), so the template's `CrossRegionGuardrailProfileArn` is optional. The Claude Code header setup itself has not been verified against a deployed guardrail. The prompt-attack filter may flag Claude Code's own prompts and skill text; that has not been tested, so try a run with the guardrail on before relying on it.
- The role that makes the calls needs `bedrock:ApplyGuardrail` on the guardrail.
- When the guardrail blocks a reply while it streams, the text received so far stays in the chat and the reply ends with the guardrail's blocked message.

Known limits: this header setup is unverified against a deployed guardrail. With `--llm-mode bedrock`, ElastiCache schema design can time out when a guardrail is attached ([#1062](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1062)).

[`infrastructure/cloudformation/bedrock-guardrail.yaml`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/infrastructure/cloudformation/bedrock-guardrail.yaml) defines a guardrail with a prompt-attack filter on inputs and PII filters on outputs, plus a published version. Deploying it is up to you; its `GuardrailId` and `GuardrailVersion` outputs are the two values above. For the same guardrail on `--llm-mode bedrock` runs, see [Other ways to run it](other-ways-to-run-it.md#attach-a-guardrail-optional).

## Headless runs

`claude -p "/modernize <collection.json> --auto --mode <chat|ui|both>"` runs the whole pipeline without prompts. `/modernize` uses `--mode` when given and `both` otherwise, and `--auto` approves the one decision for you. CI uses this to test the pipeline against a real model; see `ci/e2e-llm.sh` and [AGENTS.md](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/AGENTS.md#headless-rules-modernize---auto) for the full rules. Most users don't need it.
