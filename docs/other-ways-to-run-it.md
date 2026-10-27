# Other ways to run it

Claude Code is the main way to run the tool, but the pipeline itself is a set of plain scripts and a Python library. You can also run it in these ways.

=== "Deterministic CLI (no LLM)"

    No setup, no credentials and no network calls. It runs Collect through Reality Check and gives the engine recommendations in seconds, which suits a quick check or CI:

    ```bash
    uv run python scripts/run_assessment.py \
      --file docs/examples/wordpress/wordpress-collection.json --db wordpress --llm-mode none
    ```

    Artifacts land in `./artifacts/{db_name}/{job_id}/`. What you get without a model:

    - Workload signal detection (key-value, text search, aggregations, session stores, etc.)
    - Per-engine analysis with workload-fit scores for every query
    - Query-to-engine assignment with co-dependency resolution
    - Reality Check: engine consolidation, architectural pattern detection, cost savings analysis

    Schema design and the executive summary need a model, so `--llm-mode none` stops after Reality Check (schema design is always skipped in this mode: every schema designer needs a model).

    Add `--all -y` to go on through Synthesis: schema design is skipped, the executive summary is fixed deterministic text, and the run ends by rendering every deliverable (see [Where the deliverables land](#where-the-deliverables-land)).

=== "Amazon Bedrock"

    For production use, automation, or running without Claude Code:

    ```bash
    uv run python scripts/run_assessment.py \
      --file docs/examples/wordpress/wordpress-collection.json --db wordpress \
      --llm-mode bedrock --all -y
    ```

    AWS setup:

    1. Configure AWS credentials (`aws configure`, environment variables, or `aws sso login`).
    2. Make sure the account can invoke the Anthropic models this pipeline uses: Claude Sonnet (Reality Check validation and Synthesis), Claude Opus (Schema Design) and Claude Haiku (answer validation). Most Bedrock model access is enabled by default; Anthropic models need a use-case form, submitted once per account (select the model in the Bedrock console's model catalog, or call `PutUseCaseForModelAccess`). See [Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html).
    3. Re-run the command above.

    With `--all` the run ends by rendering every deliverable (see [Where the deliverables land](#where-the-deliverables-land)).

    Each model call has a named role in `src/shared/models.py`, which also builds request settings valid for the model's family. Override a role's model with an environment variable (the first one set wins):

    | Role | Used by | Default model | Environment variable |
    |---|---|---|---|
    | Schema design | The six schema designers and PE reviewers | Claude Opus 5.5 | `SCHEMA_AGENT_MODEL_ID`, then `BEDROCK_MODEL_ID` (output cap: `SCHEMA_AGENT_MAX_TOKENS`, at least 16000 on models that think by default) |
    | Summary | Reality Check and Synthesis executive summaries | Claude Sonnet 5.5 | `SUMMARY_MODEL_ID` |
    | Validator | Reality Check consolidation validation | Claude Sonnet 5.5 | `VALIDATOR_MODEL_ID`, then `SUMMARY_MODEL_ID` |
    | Analysis advisor | Optional explanation text for one engine's analysis, only when you run `scripts/run_analysis.py --llm-mode bedrock` on its own; `run_assessment.py` and `/modernize` run analysis with no model, and the advisor never changes routing | Claude Sonnet 5.5 | `ANALYSIS_MODEL_ID` |
    | Light check | Free-text answer validation (agent interaction API) | Claude Haiku 5.5 | `LIGHT_CHECK_MODEL_ID` |
    | AWS Transform agent | AWS Transform orchestrator and subagents | Claude Sonnet 5.5 | `MODEL_ID` |

    The defaults use the `us.` cross-region inference profiles.

    ### Attach a guardrail (optional)

    `--llm-mode bedrock` can attach an [Amazon Bedrock Guardrail](https://docs.aws.amazon.com/bedrock/latest/userguide/guardrails.html) that you own to every model call it makes. It is off by default, and with it off the requests are exactly the ones described above. Set both variables to turn it on:

    ```bash
    export BEDROCK_GUARDRAIL_ID=<guardrail-id>
    export BEDROCK_GUARDRAIL_VERSION=<guardrail-version>
    ```

    | Variable | Meaning |
    |---|---|
    | `BEDROCK_GUARDRAIL_ID` | The guardrail's ID or ARN |
    | `BEDROCK_GUARDRAIL_VERSION` | A published, numbered version (`DRAFT` is not accepted) |
    | `BEDROCK_GUARDRAIL_TRACE` | Optional: `enabled` (the default), `disabled` or `enabled_full` |
    | `BEDROCK_GUARDRAIL_STREAM_MODE` | Optional: `sync` (the default) or `async`; `async` turns off masking for the Strands phases (see Streaming mode) |

    Set both or neither. One without the other stops every entry point that can reach Bedrock before any model call, with a clear error and a non-zero exit: `run_assessment.py` and each per-phase script (`run_analysis.py`, `run_reality_check.py`, `run_schema_design.py`, `run_synthesis.py`) with `--llm-mode bedrock`, the local API at start-up, and the AWS Transform agents at start-up. No phase turns it into a retry or a fallback. When a guardrail is active, `run_assessment.py` prints it in the run header.

    It applies to the AWS Transform conversation agent (the guardrail ID and version are passed to the SDK, which has no setting for the trace or the stream mode, so `BEDROCK_GUARDRAIL_TRACE` and `BEDROCK_GUARDRAIL_STREAM_MODE` do not apply to that agent), the schema designers and PE reviewers, the Reality Check validator and summary, the Synthesis executive summary, the analysis advisor and the free-text answer check. When the guardrail blocks a call, that phase stops with an error that names it (for example "blocked the model call in the schema_design phase"), and the call is not retried, because the same input would be blocked again. Where a phase has a deterministic fallback (the executive summary, the validator, the analysis advisor, the answer check) the run goes on without the model's contribution and logs the block. A guardrail that only masks content, such as PII set to anonymize, is not an error (masking does not apply to the Strands phases with `BEDROCK_GUARDRAIL_STREAM_MODE=async`). With `BEDROCK_GUARDRAIL_TRACE=disabled`, Bedrock cannot tell a mask from a block, so every intervention counts as a block.

    The role that makes the calls needs `bedrock:ApplyGuardrail` on the guardrail. The default `us.` model IDs worked in a live test with a guardrail that has no cross-region guardrail profile (in `us-east-1`), so `CrossRegionGuardrailProfileArn` is optional. Set it (for example `.../guardrail-profile/us.guardrail.v1:0`) only if Bedrock asks for one in your region or setup.

    The prompt-attack filter looks at the customer-derived text only (table, column and query text inside the `untrusted_customer_data` blocks, sent as guard content), not at this project's own prompt text, which the filter flags as an injection attempt. The AWS Transform conversation agent is created by the AWS Transform SDK from a model ID alone, so this setting does not reach it; the pipeline phases it runs go through the calls above.

    **Streaming mode and timeout.** By default Bedrock evaluates a streamed reply in `sync` mode: it holds the whole reply back until the guardrail has evaluated it, and masking (for example PII set to anonymize) works. A long reply, such as an ElastiCache schema design with thinking, then sends nothing for minutes and used to hit the 300-second read timeout on every attempt ([#1062](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/1062)). So when a guardrail is attached, the Strands agents use a 900-second read timeout; with no guardrail the timeout is unchanged. The direct `converse` and `invoke_model` calls are not streamed and are unaffected. `BEDROCK_GUARDRAIL_STREAM_MODE=async` is an opt-in that streams the reply while the guardrail evaluates it, which is faster to first token. The tradeoff: Bedrock does not support masking in async streaming, so with `async` the Strands phases (schema design, PE review, analysis advisor, summaries) get no masking, for example no PII anonymization of outputs, and some output can reach this code before a block (the block still stops the phase and the blocked reply is never used). Leave it unset to keep masking.

    **Known limits.** The Claude Code header setup (see [Use Claude Code](use-claude-code.md#optional-attach-an-amazon-bedrock-guardrail)) is unverified.

    [`infrastructure/cloudformation/bedrock-guardrail.yaml`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/infrastructure/cloudformation/bedrock-guardrail.yaml) defines a guardrail with a prompt-attack filter on inputs, PII filters on outputs and a published version; its `GuardrailId` and `GuardrailVersion` outputs are the two values above. Deploying it is up to you. For Claude Code runs, see [Use Claude Code](use-claude-code.md#optional-attach-an-amazon-bedrock-guardrail).

=== "Python library"

    `assess()` runs an assessment in one call and returns a typed, versioned result: each statement's engine, the groups that move together and each table's owner. `load_collection` reads a collection script's output (a `.json` or `.txt` file, or a `.zip` holding one, as the bundled samples are shipped):

    ```python
    from pathlib import Path

    from src.assessment import AssessmentRequest, assess, load_collection

    collection = load_collection(Path("docs/examples/wordpress/wordpress.zip"))
    result = assess(
        AssessmentRequest(
            database_name="wordpress",  # names the run; the collection's own name keys its tables
            collection=collection,
            options={"run_through": "reality_check"},
        )
    )
    for statement in result.statements:
        print(statement.query_id, statement.engine, statement.reason_code)
    ```

    `"analysis"` and `"reality_check"` (the default) need no model and take seconds. `"schema_design"` adds a data model per kept engine (the `designs` block) and `"synthesis"` adds the report every deliverable renders; both always use a model through Amazon Bedrock (set `"deterministic": False`). `options.target_engines` is the engine scope. The full contract, with every field, the cost of a designed run and what is stable across releases, is in [`docs/contracts/assess.md`](https://github.com/aws-samples/sample-aws-genai-db-modernizer/blob/main/docs/contracts/assess.md).

    To see every feature run on a bundled sample, free and offline by default, try the [library demo](https://github.com/aws-samples/sample-aws-genai-db-modernizer/tree/main/docs/examples/library): `uv run python docs/examples/library/demo_assess.py`.

=== "Local UI on its own"

    Start the local API and the React UI with one command, without Claude Code (it needs Node 22, and the first start builds the UI, which takes a few minutes):

    ```bash
    uv run python scripts/start_local_ui.py
    ```

    It binds both servers to `127.0.0.1` and prints one JSON line with the URLs. Open <http://localhost:3000> to browse every job under `./artifacts`, or upload a collection from the create-analysis page. Stop both servers with `uv run python scripts/start_local_ui.py --stop`.

    To work on the UI with hot reload, run `make local` instead: it starts the API on `127.0.0.1:8000` and the UI's development server (`npm start` in `src/ui`) on `localhost:3000`, which proxies API calls to the API. Leave `API_HOST` unset: it defaults to `127.0.0.1`, and `API_HOST=0.0.0.0` opens the API to your network. Press Ctrl+C in that terminal to stop both servers (`--stop` above only stops what `start_local_ui.py` started).

    The local API only answers requests addressed to `localhost`, `127.0.0.1`, or `::1`; set `MODERNIZER_ALLOWED_HOSTS` to reach it under another host name.

## Where the deliverables land

`run_assessment.py --all` (in `--llm-mode none` and `bedrock` alike) ends with the same render step `/modernize` runs, `scripts/run_report.py`. It writes the deliverables into one folder, under the same names every time:

<!-- deliverables-list -->
`artifacts/<db>/<job>/deliverables/` holds `executive-report.pdf` (executive report: a landscape briefing), `engineering-report.md` (engineering report), `assessment-data.json` (Assessment Data, JSON), `engineering-report-detailed.md` (detailed engineering report: the full routing detail, for auditing the result), and `index.md`, which lists who each file is for and the assignment version.
<!-- /deliverables-list -->

To render them again for a finished job, for example after an edit made a new assignment version:

```bash
uv run python scripts/run_report.py --job-id <job_id> --db <db_name>
```

The folder always holds the latest assignment version; an older version's `report.json` stays in its own synthesis folder, and `run_report.py` renders only the latest. See [Understand the results](understand-the-results.md) for what each file shows.

## Analyzing your own database

Every option above accepts a collector output file in place of the sample. See [Use Claude Code → Analyzing your own database](use-claude-code.md#analyzing-your-own-database) for the collection scripts; `--db` names the run, and the collection's own database name keys its tables.

## Run it for you (early, opt-in)

For customers who can't run the collector scripts themselves, the API has an opt-in path that provisions an SSM automation instance in your VPC and runs the collection on your behalf against an RDS cluster you name. It is gated behind an environment variable because it is not yet wired end to end (tracked in [#342](https://github.com/aws-samples/sample-aws-genai-db-modernizer/issues/342)):

```bash
export MODERNIZER_ENABLE_AUTOMATION=1
```

This is an early preview and not supported yet. The automation instance ID is not yet threaded through to the collector environment, the region is hard-coded, and the instance and its ingress rule have no automated teardown. Until #342 lands, run the collector scripts yourself (see [Analyzing your own database](use-claude-code.md#analyzing-your-own-database)).

## LLM modes at a glance

| Mode | Credentials needed | Phases covered | Best for |
| --- | --- | --- | --- |
| `none` | None | Collect through Reality Check (through Synthesis with `--all -y`; no schema design) | Quick evaluation, CI/CD, deterministic audits |
| `external` | Claude Code license | Full pipeline | Local development, interactive exploration |
| `bedrock` | AWS credentials + Bedrock access | Full pipeline | Production, automation, team use |
