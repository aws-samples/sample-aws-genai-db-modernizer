
# /modernize

Full end-to-end database modernization pipeline. The orchestrator is LIGHTWEIGHT — it only tracks state and dispatches subagents. It NEVER reads large artifacts or produces LLM responses itself.

## Tool Use

Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains. State updates to `.modernizer-state.json` use the Edit/Write tools, never `sed` or another Bash edit.

## Arguments

- `<collector_file>` — path to collector output JSON (required)
- `--auto` — unattended run: never ask the user anything. Skips every decision gate (auto-approve) and the UI confirmation wait; on a phase failure, abort (see Error Handling). Equivalent to `-y`.
- `--mode chat|ui|both` — experience mode. With `--mode`, Step 0 is skipped. `--auto` without `--mode` means `chat`.

## Step 0: Experience Mode (ASK FIRST)

**If `--mode` was given (or `--auto`), use it and skip this question.** Otherwise, before anything else, ask the user:

> How would you like to follow the modernization?
>
> 1. **Chat only** — all results shown here in the terminal
> 2. **UI only** — I'll start the local API and frontend, check results at <http://localhost:3000>
> 3. **Both** — results in chat AND the UI running alongside
>
> (Pick 1, 2, or 3)

**If user picks 2 or 3**, start the local API and UI with one allowlisted command:

```bash
uv run python scripts/start_local_ui.py
```

`npm run serve` (which this script runs for you, only when the UI isn't already built) uses the `serve` dev dependency pinned in `src/ui/package.json`, in single-page-app mode, so deep links load on refresh. Do not swap in another static server, because one without SPA fallback returns 404 on every deep link.

The script does the build (if needed), starts both servers in the background, polls API health and the UI (including a deep link) for up to 180s, and prints exactly one JSON line to stdout. Read that line:

- **`"status": "ready"`** — tell the user:
  - API running at <http://localhost:8000>
  - Frontend running at <http://localhost:3000>
  - Unless `--auto`, wait for the user to confirm the UI is loaded before proceeding. With `--auto`, continue immediately — `"ready"` already means the health and deep-link checks passed, so there is nothing left to wait for.
- **`"status": "error"`** — report the `reason` field to the user (e.g. the npm registry token expired, a port was already taken, or the servers never became healthy in time).
  - With `--auto`, do not ask anything: stop the pipeline and end the run with `MODERNIZE_RESULT: failed phase=setup reason=<reason>`.
  - Without `--auto`, ask the user whether to continue in chat mode instead, or abort.

Record the choice by passing it as `--mode {experience_mode}` (`chat`, `ui` or `both`) to `scripts/run_assessment.py` in Phases 1-5. That command creates `.modernizer-state.json` and stores `"experience_mode"` in it, so do not write the state file for this step. Do not create any other file for it (no notes, placeholders or sidecar files next to `.modernizer-state.json`).

Stop the servers later with `uv run python scripts/start_local_ui.py --stop`. `/modernize` itself never stops them at the end of a `ui`/`both` run — the user keeps browsing the results after the pipeline finishes; only CI's own cleanup stops them.

## CRITICAL: Subagent Isolation Rule

**Every phase that involves LLM reasoning MUST run as a subagent.** This prevents context bloat and hallucination.

The orchestrator's job is ONLY:

- Read `.modernizer-state.json` for current state
- Dispatch subagents for each phase
- Read the script's stdout (1-line JSON status)
- Update `.modernizer-state.json` (via the Edit/Write tools, never `sed` or another Bash edit)
- Present brief summaries to the user
- Handle errors and decision gates

The orchestrator NEVER:

- Reads collector output, analysis results, or schema designs
- Reads `llm_requests/` or `llm_input.json` files
- Produces LLM responses or writes to `llm_responses/`
- Reads `output_schema` contents
- Makes consolidation validation decisions

## Pipeline

### Phases 1-5: Collect → Triage → Analysis → Assignment → Reality Check

Run the full deterministic pipeline in one command (no subagent needed):

```bash
uv run python scripts/run_assessment.py --file {collector_file} --db {database_name} --mode {experience_mode}
```

The database name is derived from the collector filename (e.g., `wordpress-collection.json` → `wordpress`). The script outputs one JSON line per phase to stdout and updates `.modernizer-state.json` after each phase so the UI shows progress.

**If reality check returns `awaiting_llm` (this is the expected path):**

1. Tell user: "Deterministic phases complete. Dispatching consolidation validator..."
2. **Dispatch a subagent** with this task text:

   ```text
   Run /reality-check for job_id={job_id} db={database_name}. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains.
   ```

3. After subagent completes, resume:

   ```bash
   uv run python scripts/run_assessment.py --job-id {job_id} --db {database_name} --resume-reality-check
   ```

**DO NOT read the LLM input file yourself. DO NOT produce the consolidation response yourself. The subagent handles this with a clean context following the /reality-check skill.**

**After resume-reality-check completes**, present a brief summary:

- Selected engines and why
- Query distribution across engines
- Reality check consolidations and any reversals
- Architecture patterns detected

**If UI mode:** Tell user "Assessment complete — check the UI for full results."

### Decision Gate: Assignment Approval

After reality check, present the final assignment to the user:

- Which engines survived consolidation
- Query distribution across engines
- Any queries that were redirected by the LLM validator

Ask: "Approve this assignment and continue to Schema Design, or modify?"
Only proceed to schema design after user approval (unless `--auto`).

### Phase 6: Schema Design (Parallel Subagents)

Only engines in `selected_engines` after reality check get a schema design. Build each skill name by replacing every `_` in the engine id with `-`, so `aurora_mysql` → `/design-schema-aurora-mysql` and `aurora_postgresql` → `/design-schema-aurora-postgresql`.

DynamoDB is always designed as split → one subagent per group → merge. **The orchestrator runs that flow itself** (6a-6c): it never dispatches `/design-schema-dynamodb` as one subagent, because that subagent would have to dispatch the group subagents one level deeper, and their results would never reach it.

**6a. Split DynamoDB** (only if `dynamodb` is selected):

```bash
uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --split
```

It prints one JSON line with `assignment_version` (`{N}` below) and `groups`: one entry per group with `group_index` (`{G}`), `primary_tables`, `input_file` and `draft`. Use that line; do not read the manifest yourself.

**6b. Launch every schema subagent in a SINGLE message:** one per non-DynamoDB engine, plus one per DynamoDB group.

- Each other engine:

  ```text
  Run /design-schema-<engine> for job_id={job_id} db={database_name}. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains.
  ```

- Each DynamoDB group `{G}` (`{INPUT_FILE}` and `{DRAFT}` = that group's `input_file` and `draft` from the `--split` line, `{OTHER_GROUPS}` = the other groups' `group_index` and `primary_tables`):

  ```text
  Follow /design-schema-dynamodb **Group draft task** for job_id={job_id} db={database_name} assignment_version={N} group={G}. Input: {INPUT_FILE}. Other groups' primary_tables: {OTHER_GROUPS}. Write only {DRAFT}. Do not run `--merge` or `--finalize` and do not update .modernizer-state.json. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains.
  ```

Wait for all of them to report (see Waiting Rule). A non-DynamoDB subagent that returns `failed` (it set `phase_status.schema_design_<engine>` = "failed", e.g. its design still failed validation after its 3 attempts) is a phase failure for `schema_design_<engine>`: see Error Handling.

**6c. Merge DynamoDB** once every group subagent has reported. First check which drafts exist:

```bash
uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --status
```

- `"status": "drafts_pending"`: the groups in `drafts_missing` wrote no draft, and those in `drafts_invalid` wrote one that is not a readable JSON object. That is a phase failure for `schema_design_dynamodb` with reason `group drafts missing or invalid: <groups>` (see Error Handling: the retry is one fresh group subagent, same task text, per listed group). Never run `--merge` while drafts are missing or invalid.
- `"status": "merged"`: the last `--merge` ran on exactly these drafts and passed. Set `phase_status.schema_design_dynamodb` = "complete".
- `"status": "merge_failed"`: the last `--merge` ran on exactly these drafts and printed `validation_failed` (the same verdict; a group draft's own `validation_passed: false` does not make a merge fail). Treat it as a `--merge` attempt that printed `validation_failed` with these `errors` (below).
- `"status": "merge_pending"`: run the merge:

  ```bash
  uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --merge
  ```

Make **at most 3 `--merge` attempts in total** (this is the DynamoDB retry budget of `/design-schema-dynamodb` step 5):

- `"status": "drafts_pending"` from `--merge`: it refused and wrote nothing, because the groups in `missing_groups` have no draft and those in `invalid_groups` have an unreadable one. This does not count as a `--merge` attempt; handle it like `drafts_pending` from `--status` above.
- `"status": "complete"` with no `DynamoDB merge review: …` entry in `warnings`: set `phase_status.schema_design_dynamodb` = "complete". Other `warnings` (scope warnings) never fail the phase.
- `"status": "validation_failed"`, or `complete` with `DynamoDB merge review: …` warnings while attempts remain: dispatch one fix subagent, wait for it, then re-run `--merge`. Pass it the `errors` and `warnings` from the `--merge` line exactly as printed:

  ```text
  Follow /design-schema-dynamodb **Merge fix task** for job_id={job_id} db={database_name} assignment_version={N}. --merge printed: {MERGE_LINE}. Edit only the schema_draft_group_*.json files. Do not run `--merge` or `--finalize` and do not update .modernizer-state.json. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains.
  ```

- A `complete` merge whose review warnings are still there after the last attempt is still `complete`: those notes stay in the design's trade-offs for review before migration.
- If the third attempt still prints `"status": "validation_failed"`, set `phase_status.schema_design_dynamodb` = "failed". That is a phase failure for `schema_design_dynamodb` (see Error Handling). Do not mark the phase complete.

Any other output or a non-zero exit is a phase failure for `schema_design_dynamodb` (from `--status` that includes `not_split`).

Do not run `--finalize` for DynamoDB; `--merge` is its final step.

**If UI mode:** Tell user "Schema designs ready — browse table definitions, access patterns, and GSIs in the UI."

### Phase 7: Synthesis

**Dispatch subagent** with this task text:

```text
Run /synthesize for job_id={job_id} db={database_name}. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains.
```

### Completion

**If chat or both:**

- Show final report summary (engines, architecture recommendation, TCO)
- Show the deliverables printed by the synthesize step's render command: decision report
  (HTML), interactive analysis report (HTML), engineering report (Markdown), and
  `summary-executive-report.pdf`, all under `./artifacts/{db}/{job}/synthesis/v{N}/`.

**If UI mode:**

- Show the deliverables printed by the synthesize step's render command: decision report
  (HTML), interactive analysis report (HTML), engineering report (Markdown), and
  `summary-executive-report.pdf`, all under `./artifacts/{db}/{job}/synthesis/v{N}/`.

End the run with exactly one line `MODERNIZE_RESULT: complete job_id=<id> db=<db> mode=<mode>` (also when not `--auto`). Under `--auto`, your final message must always contain a `MODERNIZE_RESULT` line: `complete` as above, or `failed phase=<phase> reason=<one line>`.

## Waiting Rule

This applies headless and interactive. Never end a turn waiting unless a dispatched subagent is still running. A headless run ends as soon as a turn ends with nothing running, so a turn that ends "waiting" on a subagent that has already reported ends the run with no result.

When every subagent you dispatched has reported, do not wait for anything else. Read `phase_status` in `.modernizer-state.json` (and, for DynamoDB, the `--status` line from 6c), then either finish the phase yourself (for example, run `--merge` when `--status` prints `merge_pending`, or set the phase complete and dispatch the next phase) or end with `MODERNIZE_RESULT: failed phase=<phase> reason=<one line>`. A subagent's completion notification is its final report: it will not come back with more, and nobody else will run the next step for you.

## Subagent Dispatch Rules

1. **Every phase = fresh subagent.** No exceptions. Each gets a clean context window. DynamoDB schema design is one fresh subagent per group plus fix subagents, all dispatched by the orchestrator (Phase 6).
2. **Parallel phases launch in a SINGLE message** to enable true concurrency.
3. **Only dispatch for selected engines.** If triage selects 2 engines, launch 2 subagents — not 4.
4. **Subagent task descriptions are minimal.** Use the task-text templates above: the skill name, the required args, and the fixed rules below. The subagent loads the skill and follows it.
5. **The orchestrator reads ONLY `.modernizer-state.json` and script stdout.** Never artifact contents.
6. **The reality check subagent is NON-OPTIONAL.** The orchestrator must NEVER attempt to read llm_input.json or write llm_responses/ itself.
7. **Every dispatch's task text includes the tool-use rule:** "Inspect files with the Read and Grep tools. Use Bash only for the documented `uv run python scripts/…` commands; do not use `cat`, `jq`, `python3 -c`, `sed`, `ls` or `cd` chains." The subagent loads its own skill, which repeats the same rule, but the dispatch text carries it too so the rule holds even before the skill loads.
8. **Nesting is one level deep.** Only the orchestrator dispatches subagents. Every dispatch's task text includes "Do not dispatch subagents yourself." (and "Unattended: do not ask the user anything."): a subagent's own subagents report to the orchestrator, not to it, so a subagent that dispatches and then ends its turn leaves its work unfinished.

## Error Handling

If any phase fails:

- Present the error to the user
- Ask: "Retry this phase, skip it, or abort?"
- If retry: dispatch a new subagent for that phase
- If skip: mark phase as "skipped" in state, continue
- If abort: stop pipeline, preserve all artifacts produced so far
- **With `--auto`:** do not ask. Retry the failed phase once with a fresh subagent; if it fails again, stop the pipeline, preserve artifacts, and end with the line `MODERNIZE_RESULT: failed phase=<phase> reason=<one line>`.
- **DynamoDB under `--auto`:** the retry for `schema_design_dynamodb` is one fresh group subagent per missing or invalid group (`drafts_pending`), or one more 6c round with a fresh fix subagent and a new budget of 3 `--merge` attempts (merge still `validation_failed` / `merge_failed`). The phase gets one retry in total. It does not get a second one if a different failure follows the first. Set `phase_status.schema_design_dynamodb` = "failed" before retrying. If it fails again, end with `MODERNIZE_RESULT: failed phase=schema_design_dynamodb reason=<first error>`.
- **Schema design under `--auto`:** a `/design-schema-<engine>` subagent returning `failed` (validation, contract or scope, still failing after its 3 `--finalize`/`--merge` attempts) is a phase failure like any other. Retry it once with a fresh subagent; if it fails again, end with `MODERNIZE_RESULT: failed phase=schema_design_<engine> reason=<first validation error>`. Never mark that engine's schema design complete.

**Note on subagents under `--auto`:** every subagent dispatched by this pipeline (`/reality-check`, `/design-schema-*`, `/synthesize`) must also not ask the user anything. These sub-commands have no prompts today — keep it that way.
