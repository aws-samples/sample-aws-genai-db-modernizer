
# /modernize

Full end-to-end database modernization pipeline. The orchestrator only tracks state and dispatches subagents; it never reads large artifacts or produces LLM responses itself.

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it. State updates to `.modernizer-state.json` use the Edit/Write tools, never `sed` or another Bash edit.

## Arguments

- `<collector_file>` — path to collector output JSON (required); must be the `.json` file itself, not a `.zip` archive
- `--auto` — unattended run: never ask the user anything. Skips every decision gate (auto-approve); on a phase failure, abort (see Error Handling). Equivalent to `-y`.
- `--mode chat|ui|both` — experience mode. Default is `both`: results in chat with the local UI alongside. `--mode chat` is chat only, with no UI. `--mode ui` is UI-first: chat carries only the approval-gate numbers, UI pointers, and the final result line — no phase summaries. `--auto` without `--mode` also means `both`.

## Step 0: Experience Mode

Never ask the user which mode they want. Resolve `{experience_mode}`: `--mode` if given, else `both`.

**Unless `{experience_mode}` is `chat`**, start the local API and UI with one command, using a Bash timeout of 600000 ms (a fresh clone builds the UI from scratch, which takes several minutes):

```bash
uv run python scripts/start_local_ui.py
```

It builds the UI if needed, starts (or reuses) both servers, checks their health, and prints exactly one JSON line. A missing or unparseable JSON line, or a non-zero exit, counts the same as `"status": "error"` below.

- **`"status": "ready"`** — tell the user, in one sentence, that the UI is at <http://localhost:3000> and that the chat will walk them through what it shows, and continue immediately: the health checks already passed.
- **`"status": "error"`** — tell the user the `reason` field if there is one, that the UI will not be available this run, and that you are continuing in chat only. Set `{experience_mode}` to `chat` for the rest of the run and carry on: never fail the run over this, with or without `--auto`, and never ask whether to continue.

Pass `--mode {experience_mode}` to `scripts/run_assessment.py` in Phases 1-5: that command creates `.modernizer-state.json` and records the mode in it, so do not write the state file for this step. Do not create any other file for it (no notes, placeholders or sidecar files).

`/modernize` never stops the servers at the end of a `ui`/`both` run: the user keeps browsing the results (`uv run python scripts/start_local_ui.py --stop` stops them).

## CRITICAL: Subagent Isolation Rule

Every phase that involves LLM reasoning runs as a subagent (see Subagent Dispatch Rules). The orchestrator reads only `.modernizer-state.json` and script stdout, updates the state file with the Edit/Write tools, presents brief summaries, and handles errors and decision gates. It never reads collector output, analysis results, schema designs, `llm_requests/`, `llm_input.json` or `output_schema`, never writes `llm_responses/`, and never makes consolidation decisions itself.

## Pipeline

### Phases 1-5: Collect → Triage → Analysis → Assignment → Reality Check

Run the deterministic pipeline in one command (no subagent needed):

```bash
uv run python scripts/run_assessment.py --file {collector_file} --db {database_name} --mode {experience_mode}
```

The database name is derived from the collector filename (e.g., `wordpress-collection.json` → `wordpress`).

Its stdout is the whole tool result: one JSON status line per phase (`{"phase": "collect", ...}`, `"triage"`, `"analysis"`, `"assignment"`, `"reality_check"`), then one line that points at the progress log:

```text
{"log": "artifacts/{database_name}/{job_id}/_logs/run_assessment.log", "log_offset": 1, "log_lines": 1376}
```

- Take `job_id` from the `collect` line and the next step from the `reality_check` line. Run the command exactly as shown: do not pipe it through `tail` or anything else, and do not add `--verbose` (it prints the full progress to stdout, too large to see inline).
- `status` is `"complete"`, `"awaiting_llm"` (reality check only, see below), `"skipped"` (`reason` says why; continue as if it completed) or `"error"`. You never need to open the paths a line names.
- `"status": "error"` (or a non-zero exit) is a phase failure for that line's `phase`, with its `message` as the reason (see Error Handling). Only then, if the message is not enough, Read the `log` path with `offset` = `log_offset` and `limit` = `log_lines` (for a long log, just the last 200 lines of that range). A last line `{"log": null, "output_tail": ...}` means the job directory was never created; `output_tail` is the output.

**If reality check returns `awaiting_llm` (the expected path):**

1. Tell user: "Deterministic phases complete. Dispatching consolidation validator..."
2. **Dispatch a subagent** with this task text (never read its input or write its response yourself):

   ```text
   Run /reality-check for job_id={job_id} db={database_name}. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.
   ```

3. After the subagent completes, resume:

   ```bash
   uv run python scripts/run_assessment.py --job-id {job_id} --db {database_name} --resume-reality-check
   ```

   It prints a `{"phase": "reality_check", ...}` status line (`"complete"`, or `"error"` with a `message`), then the `{"log": ...}` line.

**After resume-reality-check completes:**

- Unless `{experience_mode}` is `ui`, present a brief summary: selected engines and why, query distribution across engines, reality check consolidations and any reversals, architecture patterns detected. Source the distribution and consolidations from the `reality_check` status line, not from memory.
- Unless `{experience_mode}` is `chat`, add: the Sankey/assignment page in the UI has the full query-to-engine breakdown.

### Decision Gate: Assignment Approval

After reality check, present the final assignment — this is the approval gate, so these numbers appear in every mode, including `ui`. Read them from the `reality_check` status line `run_assessment.py` printed (the `--resume-reality-check` line, or the direct `"status": "complete"` line), never from opening `reality-check/output.json` or an assignment artifact. That line always carries the data below, so never tell the user you have not read it or do not have the numbers.

Show a compact table, one row per engine, built from that line's fields:

- `before_distribution` vs `after_distribution`: each engine's query count v1 → final, and its share of the total
- `eliminated_engines`: the engines to label dropped. Read this list; never infer "dropped" from an `after_distribution` count (an eliminated engine can keep a nonzero count for an out-of-scope query). Every other engine in `after_distribution` survived
- `consolidations`: each entry's `from`, `to`, `queries` and `reason`
- `sweep_kept`, when non-empty: each entry's `engine` and `reason` (a small engine the sanity sweep kept), alongside `consolidations`
- `cache_overlay`, when present: `query_count` cached reads serving `call_share_percent`% of calls
- `schema_design_engines`: the engines Phase 6 designs next. With `cache_overlay`, its engine is in this list even though it owns no row in `after_distribution`.

When `before_distribution` and `after_distribution` are identical (or `consolidations` and `eliminated_engines` are both empty), say so explicitly — reality check changed nothing — instead of an empty-looking table.

Unless `{experience_mode}` is `chat`, also point the user at the Sankey/assignment page in the UI before they decide.

Ask: "Approve this assignment and continue to Schema Design, or modify?"
Only proceed to schema design after user approval (unless `--auto`).

### Phase 6: Schema Design (Parallel Subagents)

Design exactly the engines in `schema_design_engines` from the `reality_check` status line — never re-derive the list from `after_distribution` (it lists owners only, so the cache-layer engine would be dropped). The skill name replaces every `_` in the engine id with `-`: `aurora_mysql` → `/design-schema-aurora-mysql`.

DynamoDB is always designed as split → one subagent per group → merge, and **the orchestrator runs that flow itself** (6a-6c): never dispatch `/design-schema-dynamodb` as one subagent, because its group subagents would report to you, not to it.

**6a. Split DynamoDB** (only if `dynamodb` is in `schema_design_engines`):

```bash
uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --split
```

It prints one JSON line with `assignment_version` (`{N}` below) and `groups`: per group `group_index` (`{G}`), `primary_tables`, `input_file`, `input_pages` (the Read `offset`/`limit` pages that cover `input_file`) and `draft`. Use that line; do not read the manifest.

**6b. Launch every schema subagent in a SINGLE message:** one per non-DynamoDB engine, plus one per DynamoDB group.

- Each other engine:

  ```text
  Run /design-schema-<engine> for job_id={job_id} db={database_name}. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.
  ```

- Each DynamoDB group `{G}` (`{INPUT_FILE}`, `{INPUT_PAGES}` and `{DRAFT}` = that group's `input_file`, `input_pages` and `draft`, `{OTHER_GROUPS}` = the other groups' `group_index` and `primary_tables`):

  ```text
  Follow /design-schema-dynamodb **Group draft task** for job_id={job_id} db={database_name} assignment_version={N} group={G}. Input: {INPUT_FILE}, in Read pages {INPUT_PAGES}. Read the input with the Read tool, one call per page (`offset`, `limit`); search it with `uv run python scripts/search_artifacts.py <regex> <path>`; write the draft with one Write tool call. Never use `sed`, `cat`, `grep`, heredocs or other scripts to read, search or write files. Other groups' primary_tables: {OTHER_GROUPS}. Write only {DRAFT}. Do not run `--merge` or `--finalize` and do not update .modernizer-state.json. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.
  ```

Wait for all of them to report (see Waiting Rule). A non-DynamoDB subagent that returns `failed` (it set `phase_status.schema_design_<engine>` = "failed" after its 3 attempts) is a phase failure for `schema_design_<engine>`: see Error Handling.

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

Make **at most 3 `--merge` attempts in total**:

- `"status": "drafts_pending"` from `--merge`: it refused and wrote nothing (`missing_groups` have no draft, `invalid_groups` an unreadable one). This does not count as a `--merge` attempt; handle it like `drafts_pending` from `--status` above.
- `"status": "complete"` with no `DynamoDB merge review: …` entry in `warnings`: set `phase_status.schema_design_dynamodb` = "complete". Other `warnings` never fail the phase.
- `"status": "validation_failed"`, or `complete` with `DynamoDB merge review: …` warnings while attempts remain: dispatch one fix subagent, wait for it, then re-run `--merge`. Pass it the `errors` and `warnings` from the `--merge` line exactly as printed:

  ```text
  Follow /design-schema-dynamodb **Merge fix task** for job_id={job_id} db={database_name} assignment_version={N}. --merge printed: {MERGE_LINE}. Edit only the schema_draft_group_*.json files. Do not run `--merge` or `--finalize` and do not update .modernizer-state.json. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.
  ```

- A `complete` merge whose review warnings remain after the last attempt is still `complete`: those notes stay in the design's trade-offs.
- If the third attempt still prints `"status": "validation_failed"`, set `phase_status.schema_design_dynamodb` = "failed". That is a phase failure for `schema_design_dynamodb` (see Error Handling). Do not mark the phase complete.

Any other output or a non-zero exit is a phase failure for `schema_design_dynamodb` (from `--status` that includes `not_split`). Never run `--finalize` for DynamoDB; `--merge` is its final step.

Unless `{experience_mode}` is `ui`, tell the user schema design is complete. Unless `{experience_mode}` is `chat`, add: table definitions, access patterns and GSIs are in the UI.

### Phase 7: Synthesis

**Dispatch subagent** with this task text:

```text
Run /synthesize for job_id={job_id} db={database_name}. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.
```

### Completion

Unless `{experience_mode}` is `ui`, show the final report summary (engines, architecture recommendation, TCO) and the deliverables the synthesize step rendered: decision report (HTML), interactive analysis report (HTML), engineering report (Markdown), and `summary-executive-report.pdf`, all under `./artifacts/{db}/{job}/synthesis/v{N}/`. If the `/synthesize` subagent reported `cache_safety_net_notes`, relay them verbatim: they explain why the cache layer is smaller than at the assignment gate.

Unless `{experience_mode}` is `chat`, add: the full interactive report is in the UI.

End the run with exactly one line `MODERNIZE_RESULT: complete job_id=<id> db=<db> mode=<mode>` (also when not `--auto`), where `<mode>` is `{experience_mode}` — `chat` if the UI never started or fell back. Under `--auto`, your final message must always contain a `MODERNIZE_RESULT` line: `complete` as above, or `failed phase=<phase> reason=<one line>`.

## Waiting Rule

This applies headless and interactive. Never end a turn waiting unless a dispatched subagent is still running: a headless run ends as soon as a turn ends with nothing running.

When every subagent you dispatched has reported, do not wait for anything else. Read `phase_status` in `.modernizer-state.json` (and, for DynamoDB, the `--status` line from 6c), then either finish the phase yourself (for example, run `--merge` when `--status` prints `merge_pending`, or set the phase complete and dispatch the next phase) or end with `MODERNIZE_RESULT: failed phase=<phase> reason=<one line>`. A subagent's completion notification is its final report: nobody else will run the next step for you.

## Subagent Dispatch Rules

1. **Every phase = fresh subagent** with a clean context. DynamoDB schema design is one fresh subagent per group plus fix subagents, all dispatched by the orchestrator (Phase 6).
2. **Parallel phases launch in a SINGLE message.**
3. **Only dispatch for selected engines.**
4. **Use the task-text templates above** unchanged: the skill name, the required args and the fixed rules. The subagent loads the skill and follows it.
5. **The orchestrator reads ONLY `.modernizer-state.json` and script stdout**, and the `run_assessment.py` progress log only to diagnose a phase that printed `"status": "error"`.
6. **The reality check subagent is NON-OPTIONAL.**
7. **Every dispatch's task text carries the tool-use rule** from Tool Use, word for word, so it holds before the subagent's skill loads.
8. **Nesting is one level deep.** Only the orchestrator dispatches subagents. Every dispatch's task text includes "Do not dispatch subagents yourself." and "Unattended: do not ask the user anything.": a subagent's own subagents would report to the orchestrator, leaving its work unfinished.

## Error Handling

If any phase fails:

- Present the error to the user
- Ask: "Retry this phase, skip it, or abort?"
- If retry: dispatch a new subagent for that phase
- If skip: mark phase as "skipped" in state, continue
- If abort: stop pipeline, preserve all artifacts produced so far
- **With `--auto`:** do not ask. Retry the failed phase once with a fresh subagent; if it fails again, stop the pipeline, preserve artifacts, and end with the line `MODERNIZE_RESULT: failed phase=<phase> reason=<one line>`.
- **DynamoDB under `--auto`:** the retry for `schema_design_dynamodb` is one fresh group subagent per missing or invalid group (`drafts_pending`), or one more 6c round with a fresh fix subagent and a new budget of 3 `--merge` attempts (merge still `validation_failed` / `merge_failed`). The phase gets one retry in total. It gets no second one if a different failure follows the first. Set `phase_status.schema_design_dynamodb` = "failed" before retrying. If it fails again, end with `MODERNIZE_RESULT: failed phase=schema_design_dynamodb reason=<first error>`.
- **Schema design under `--auto`:** a `/design-schema-<engine>` subagent returning `failed` (still failing after its 3 `--finalize`/`--merge` attempts) is a phase failure like any other. Retry it once with a fresh subagent; if it fails again, end with `MODERNIZE_RESULT: failed phase=schema_design_<engine> reason=<first validation error>`. Never mark that engine's schema design complete.
