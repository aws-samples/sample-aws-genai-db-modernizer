# Other ways to run it

Claude Code is the main way to run the tool, but the pipeline itself is a set of plain scripts. You can also run them in these ways.

=== "Deterministic CLI (no LLM)"

    No setup, no credentials and no network calls. It runs Collect through Reality Check and gives the engine recommendations in seconds, which suits a quick check or CI:

    ```bash
    uv run python scripts/run_assessment.py \
      --file docs/examples/wordpress/wordpress-collection.json --db wordpress --llm-mode none
    ```

    Artifacts land in `./artifacts/{db_name}/{job_id}/`. What you get without a model:

    - Workload signal detection (key-value, text search, aggregations, session stores, etc.)
    - Per-engine analysis with confidence scores for every query
    - Query-to-engine assignment with co-dependency resolution
    - Reality Check: engine consolidation, architectural pattern detection, cost savings analysis

    Schema design and the executive summary need a model, so `--llm-mode none` stops after Reality Check (schema design is always skipped in this mode: every schema designer needs a model).

=== "Amazon Bedrock"

    For production use, automation, or running without Claude Code:

    ```bash
    uv run python scripts/run_assessment.py \
      --file docs/examples/wordpress/wordpress-collection.json --db wordpress \
      --llm-mode bedrock --all -y
    ```

    AWS setup:

    1. Configure AWS credentials (`aws configure`, environment variables, or `aws sso login`).
    2. Make sure the account can invoke the Anthropic models this pipeline uses: Claude Sonnet (Reality Check validation and Synthesis) and Claude Opus (Schema Design). Most Bedrock model access is enabled by default; Anthropic models need a use-case form, submitted once per account (select the model in the Bedrock console's model catalog, or call `PutUseCaseForModelAccess`). See [Request access to models](https://docs.aws.amazon.com/bedrock/latest/userguide/model-access.html).
    3. Re-run the command above.

    ![Bedrock mode demo](assets/local-modernizer-bedrock.gif)

=== "Local UI on its own"

    Run the API and the React UI directly, without Claude Code:

    ```bash
    # Start the API server
    ARTIFACT_DIR=./artifacts uv run uvicorn src.api.main:app --host 127.0.0.1 --port 8000

    # Build and serve the UI (in another terminal)
    cd src/ui && npm ci
    REACT_APP_API_URL=http://localhost:8000/api/v1/ npm run build
    npm run serve
    ```

    Then open <http://localhost:3000>. The local API only answers requests addressed to `localhost`, `127.0.0.1`, or `::1`; set `MODERNIZER_ALLOWED_HOSTS` to reach it under another host name.

## Analyzing your own database

Every option above accepts a collector output file in place of the sample. See [Use Claude Code → Analyzing your own database](use-claude-code.md#analyzing-your-own-database) for the collection scripts.

## Run it for you (early, opt-in)

--8<-- "README.md:run-it-for-you"

## LLM modes at a glance

| Mode | Credentials needed | Phases covered | Best for |
| --- | --- | --- | --- |
| `none` | None | Collect through Reality Check | Quick evaluation, CI/CD, deterministic audits |
| `external` | Claude Code license | Full pipeline | Local development, interactive exploration |
| `bedrock` | AWS credentials + Bedrock access | Full pipeline | Production, automation, team use |
