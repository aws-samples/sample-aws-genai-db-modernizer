
# /modernize

Full end-to-end database modernization pipeline. The orchestrator only tracks state and dispatches subagents; it never reads large artifacts or produces LLM responses itself.

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it. State updates to `.modernizer-state.json` use the Edit/Write tools, never `sed` or another Bash edit.

## Arguments

- `<collector_file>` — path to collector output JSON (required); the `.json` file (a bundled sample whose `.json` is not extracted yet, such as `docs/examples/wordpress/wordpress-collection.json`, is extracted from its tracked `.zip` by the script)
- `--auto` — unattended run: never ask the user anything. Skips every decision gate's question (auto-approve), never what the gate shows: the approval-gate block and the summary-page URL still appear. It also means "decide for me": every pending engine choice is settled by the deterministic rule and recorded as decided automatically; on a phase failure, abort (see Error Handling). Equivalent to `-y`.
- `--mode chat|ui|both` — experience mode. Default is `both`: results in chat with the local UI alongside. `--mode chat` is chat only, with no UI. `--mode ui` is UI-first: chat carries only the approval-gate numbers, UI pointers, the one-sentence database-name notice (Phases 1-5, only when the name was rewritten), and the final result line — no phase summaries. `--auto` without `--mode` also means `both`.
- `--engines-in-use <ids>` — optional, comma-separated engine ids (for example `opensearch,elasticache`) of the other AWS databases the application already runs today. Never ask the user for it, with or without `--auto`: absent means only the source engine counts. Pass it through unchanged as `--engines-in-use {engines_in_use}` in Phases 1-5, and omit the flag when it was not given.
- `--target-databases <ids>` — optional, comma-separated engine ids (for example `dynamodb,aurora_postgresql`): the customer's engine scope, the only engines the plan may route to. Never ask the user for it, with or without `--auto`: absent means every engine. Pass it through unchanged as `--target-databases {target_databases}` on the first `scripts/run_assessment.py` command only (the one with `--file`, see Phases 1-5), and omit the flag when it was not given. Never add it to `--resume-reality-check`, `--choose` or `--decide-for-me`: the job keeps the scope it started with, and those commands refuse the flag. If the scope leaves queries no in-scope engine can serve, the run goes on: the assignment line carries `unservable_queries` (how many), those queries have no engine, and the risk register lists them as a high risk. The gate block and the final summary state the scope and that number, from the job's facts, with the note that a new assessment whose scope adds an engine can serve them: quote them as printed, never state the number yourself, and never treat it as a phase failure.

## Step 0: Experience Mode

Never ask the user which mode they want. Resolve `{experience_mode}`: `--mode` if given, else `both`.

**Unless `{experience_mode}` is `chat`**, start the local API and UI with one command, using a Bash timeout of 600000 ms (a fresh clone builds the UI from scratch, which takes several minutes):

```bash
uv run python scripts/start_local_ui.py
```

It builds the UI if needed, starts (or reuses) both servers, checks their health, and prints exactly one JSON line. A missing or unparseable JSON line, or a non-zero exit, counts the same as `"status": "error"` below.

- **`"status": "ready"`** — tell the user, in one sentence, that the UI is at the line's `ui` URL and that the chat will give them the job's own pages as the run reaches them, and continue immediately: the health checks already passed.
- **`"status": "error"`** — tell the user the `reason` field if there is one, that the UI will not be available this run, and that you are continuing in chat only. Set `{experience_mode}` to `chat` for the rest of the run and carry on: never fail the run over this, with or without `--auto`, and never ask whether to continue.

Pass `--mode {experience_mode}` to `scripts/run_assessment.py` in Phases 1-5: that command creates `.modernizer-state.json` and records the mode in it, so do not write the state file for this step. Do not create any other file for it (no notes, placeholders or sidecar files).

`/modernize` never stops the servers at the end of a `ui`/`both` run: the user keeps browsing the results (`uv run python scripts/start_local_ui.py --stop` stops them).

## CRITICAL: Subagent Isolation Rule

Every phase that involves LLM reasoning runs as a subagent (see Subagent Dispatch Rules). The orchestrator reads only `.modernizer-state.json` and script stdout, updates the state file with the Edit/Write tools, presents brief summaries, and handles errors and decision gates. It never reads collector output, analysis results, schema designs, `llm_requests/`, `llm_input.json` or `output_schema`, never writes `llm_responses/`, and never makes consolidation decisions itself.

## Pipeline

### Phases 1-5: Collect → Triage → Analysis → Assignment → Reality Check

Run the deterministic pipeline in one command (no subagent needed):

```bash
uv run python scripts/run_assessment.py --file {collector_file} --db-from-file-name --mode {experience_mode}
```

When `--engines-in-use` was given, add `--engines-in-use {engines_in_use}` to that command. When `--target-databases` was given, add `--target-databases {target_databases}` to it.

With `--db-from-file-name` the script names the database from the collector file name: drop `.json` and a trailing `-collection`, replace every character outside `[A-Za-z0-9_-]`, and a leading `-`, with `_`, keep the first 128 characters, and use `database` if nothing is left. It prints the name as `database_name` on the `collect` line, with `database_name_source` (`file_name`): take `{database_name}` from there, never derive it yourself, and pass it as `--db` in every later command. Examples: `wordpress-collection.json` → `wordpress`, `my.app-collection.json` → `my_app`, `sales data.json` → `sales_data`. When the name is not just the file name without `.json` and a trailing `-collection`, tell the user in one sentence which name it used, in every mode (`--mode ui` included), and continue (never ask).

Its stdout is the whole tool result: one JSON status line per phase (`{"phase": "collect", ...}`, `"triage"`, `"analysis"`, `"assignment"`, `"reality_check"`), then one line that points at the progress log:

```text
{"log": "artifacts/{database_name}/{job_id}/_logs/run_assessment.log", "log_offset": 1, "log_lines": 1376}
```

- Take `job_id` and `database_name` from the `collect` line and the next step from the `reality_check` line. Run the command exactly as shown: do not pipe it through `tail` or anything else, and do not add `--verbose` (it prints the full progress to stdout, too large to see inline).
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

- Go straight to the approval gate below: its block is the post-reality-check summary, so do not write a second one.

### Decision Gate: Assignment Approval

After reality check, present the final assignment — this is the approval gate, so these numbers appear in every mode, including `ui`. Print the block from the job's artifacts:

```bash
uv run python scripts/print_summary.py --job-id {job_id} --db {database_name} --gate --mode {experience_mode}
```

Its stdout is the whole block, in Markdown: the engine table (queries before and final, share of the final total, kept or dropped), for a job started with `--target-databases` the customer's engine scope and how many queries have no engine in it, the consolidations and the small engines kept, the cache layer with its hottest cache candidates (`Hottest cache candidates:`, the cached reads with the most calls per second, each with its table, owner engine, calls per second and share of calls, then how many more), the architectural patterns, and the engines whose schema design runs next (or, when engine choices are pending, the tables to settle). It says so itself when reality check changed nothing. Unless `{experience_mode}` is `chat`, its last line is the job's summary page (`http://localhost:<port>/analysis/monitor/summary/<job_id>`), which has the Sankey and the full query-to-engine breakdown: that line is the UI pointer for the gate, so never give the UI root or build the URL yourself. The user does not see a command's output: Claude Code shows the call collapsed. So once the gate succeeds, your next chat message must contain its stdout verbatim, the whole Markdown block (including the `Summary page` line unless `{experience_mode}` is `chat`), and only then ask the approval question below. Never write "the block above" or point at the command's output instead of quoting it. Do not retype, reorder or paraphrase it, and add no table or figure of your own. Its numbers never come from opening `reality-check/output.json` or an assignment artifact, and never tell the user you have not read them. A JSON line `{"status": "error", ...}` (or a non-zero exit) is a failure of the gate step: present its `message` (see Error Handling).

Run it on its own, never in the same batch of tool calls as another command, and start Phase 6 only in a later message: the gate block you quote is what the user sees at the gate, so it comes before any Phase 6 command or dispatch. A hook (`scripts/hooks/remind_quote_printed_summary.py`) adds a reminder to paste it next to the gate's output, and the e2e-llm transcript check fails a run whose chat text never carried the block. A hook (`scripts/hooks/require_gate_shown.py`) holds you to this: it refuses a Phase 6 command or `/design-schema-*` dispatch for the job unless a successful gate ran in an earlier message. If it refuses one (`Blocked: schema design waits for the approval gate ...`), run the gate on its own first, then start schema design.

**Pending engine choices.** The line's `pending_choices` lists, one entry per table, queries that tie between engines, so no routing rule picks one (`choice`, `table`, `queries`, `engines` with each `score`, `unrounded_score` and `trade_off`, `why_tied`, `listed_under`, `decide_for_me` and `decide_for_me_reason`). A model never picks one: the customer does, or the deterministic "decide for me" rule does.

- **With `--auto`** and a non-empty `pending_choices`: do not ask. Run `uv run python scripts/run_assessment.py --job-id {job_id} --db {database_name} --decide-for-me`. It prints one `{"phase": "engine_choices", ...}` line: tell the user each `decided` entry's `table`, `engine` and `reason`, marked "decided automatically, review if needed", and design the `schema_design_engines` of that line in Phase 6, not the `reality_check` line's.
- **Without `--auto`**: ask one question per entry, for example "Table `theme_fields` (13 queries) ties between DocumentDB and DynamoDB: which engine?", with its `why_tied`, each engine's scores and `trade_off`, and what "decide for me" would pick and why. Then run `uv run python scripts/run_assessment.py --job-id {job_id} --db {database_name} --choose <choice>=<engine>` with one `--choose` per answer (add `--decide-for-me` when the user asks you to decide the rest). Use its `engine_choices` line as above.

Ask: "Approve this assignment and continue to Schema Design, or modify?"
Only proceed to schema design after user approval (unless `--auto`).

With `--auto`, skip only that question: the gate block and, unless `chat`, the summary-page `ui_url` are still shown, in that order, before Phase 6 starts.

### Phase 6: Schema Design (Parallel Subagents)

Design exactly the engines in `schema_design_engines` from the `reality_check` status line (or the `engine_choices` line, when you settled pending engine choices) — never re-derive the list from `after_distribution` (it lists owners only, so the cache-layer engine would be dropped). The skill name replaces every `_` in the engine id with `-`: `aurora_mysql` → `/design-schema-aurora-mysql`.

DynamoDB is always designed as split → one subagent per group → merge, and **the orchestrator runs that flow itself** (6a-6d): never dispatch `/design-schema-dynamodb` as one subagent, because its group subagents would report to you, not to it.

**6a. Split DynamoDB** (only if `dynamodb` is in `schema_design_engines`):

```bash
uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --split
```

It prints one JSON line with `assignment_version` (`{N}` below) and `query_total` and `groups`: per group `group_index` (`{G}`), `primary_tables`, `context_tables`, `input_file`, `query_count`, `query_first`, `query_last`, `input_pages` (the Read `offset`/`limit` pages that cover `input_file`) and `draft`. Use that line; do not read the manifest.

**DynamoDB labels.** Each group is a *part* of one DynamoDB design: name the work by what it does, never "group {G} draft" or "merge fix". `{P}` = `group_index` + 1 (parts count from 1), `{T}` = the number of `groups`, `{F}` and `{L}` = that group's `query_first` and `query_last`, `{X}` = the line's `query_total` (the script numbers the queries in group order; never count them yourself). A `group N` in a `--merge` line is part N + 1.

- Each group subagent's dispatch description: `DynamoDB design, part {P} of {T} (queries {F}-{L} of {X})`; its review in 6c: `DynamoDB review, part {P} of {T}`.
- As each group subagent reports: `DynamoDB design: {D} of {T} parts done` ({D} = the parts reported so far).
- Before each `--merge`: `Merging the {T} DynamoDB parts into one design`.
- A Merge fix task subagent's dispatch description: `Reconciling overlapping tables between parts (attempt {A} of 3)` ({A} = the `--merge` attempt whose line it fixes), then one short line naming what overlapped, from that line's `errors` and `warnings` (for example `wp_postmeta was designed in parts 1 and 3`).

The section names (**Group draft task**, **Merge fix task**), file names and `--status` keys stay as they are. Under `--auto` or `--mode ui`, skip the progress lines (the ones that are not dispatch descriptions), and write no other text while subagents run: no "part N is done", "still waiting on ...", "reviews recorded so far" or other count of parts or reviews done. Which subagents are still running is never your own tally of reports: the status lines say it (see **Done signal** in 6b and 6c).

**6b. Launch every schema subagent in a SINGLE message:** one per non-DynamoDB engine, plus one per DynamoDB group.

- Each other engine:

  ```text
  Run /design-schema-<engine> for job_id={job_id} db={database_name}. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.
  ```

- Each DynamoDB group `{G}` (`{INPUT_FILE}`, `{INPUT_PAGES}` and `{DRAFT}` = that group's `input_file`, `input_pages` and `draft`, `{OTHER_GROUPS}` = the other groups' `group_index` and `primary_tables`):

  ```text
  Follow /design-schema-dynamodb **Group draft task** for job_id={job_id} db={database_name} assignment_version={N} group={G}. Input: {INPUT_FILE}, in Read pages {INPUT_PAGES}. Read the input with the Read tool, one call per page (`offset`, `limit`); search it with `uv run python scripts/search_artifacts.py <regex> <path>`; write the draft with one Write tool call. Never use `sed`, `cat`, `grep`, heredocs or other scripts to read, search or write files. Other groups' primary_tables: {OTHER_GROUPS}. Write only {DRAFT}. Do not run `--merge` or `--finalize` and do not update .modernizer-state.json. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.
  ```

**Done signal.** After every design subagent's report, before anything else, run the DynamoDB `--status` command of 6d (only when `dynamodb` is in `schema_design_engines`) and, for a non-DynamoDB engine's report, that engine's `--status` below. The status lines are the only record of which designs are in:

- DynamoDB `drafts_pending` whose `drafts_missing` (group indexes; part = index + 1) names a group whose part has not reported yet: that part is still running. End the turn with no text; its report wakes you. The line lists only those groups (with `group_count`, the number of groups in all, and the `manifest` path), so it is the whole list of what is still out.
- DynamoDB `merge_pending` (every group draft is written, or DynamoDB is not designed), and every non-DynamoDB engine's `--status` run after its report: every design is in. Go on at once, in the same turn: set the phase status below, then run 6c.
- DynamoDB `drafts_pending` whose `drafts_missing` and `drafts_invalid` name only groups whose part has already reported: those parts wrote no draft. That is the 6d `drafts_pending` phase failure, not a wait.

Never end a turn after a report without that status line, and never conclude from your own count that parts are still running.

Wait for all of them to report (see Waiting Rule). Then, for each non-DynamoDB engine, set `phase_status.schema_design_<engine>` yourself from the script, not from the subagent's report (it may not have written the state file):

```bash
uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine <engine> --status
```

Run it exactly as shown, on its own, and read `status` and the keys named here from the one short JSON line it prints. Never pipe it into another command (`uv run python`, `tail`, `jq`, ...): a hook refuses a piped command. For DynamoDB the line lists only the groups to redo; every group's entry is in the `--split` line and in the `manifest` file the line names, under `artifacts/`: read that file with the Read tool.

- `"status": "complete"`: set `phase_status.schema_design_<engine>` = "complete".
- `"status": "validation_failed"`, `"finalize_pending"` or `"not_finalized"`: set `phase_status.schema_design_<engine>` = "failed". That is a phase failure for `schema_design_<engine>`, with the line's `errors` (or its `status`) as the reason: see Error Handling. A subagent that returned `failed` after its 3 attempts ends here too.

**6c. PE review** once every schema subagent from 6b has reported: the review Bedrock mode runs after every design. Its routing flags are what the post-schema reroute (6e) reads.

```bash
uv run python scripts/run_pe_review.py --job-id {job_id} --db {database_name}
```

It prints one JSON line. `"status": "complete"`: there is nothing to review; go on. `"status": "awaiting_llm"`: `reviews` lists one review per design that has none recorded yet (one per group for DynamoDB), each with `engine`, `group` (`null` except for DynamoDB), `request`, `response` and `finalize`; `skipped` names the engines with nothing to review and why (an Aurora or ElastiCache review carries no routing flags). Launch one review subagent per entry, all in a SINGLE message, with this task text (`{ENGINE}`, `{GROUP}`, `{REQUEST}`, `{RESPONSE}` and `{FINALIZE}` = that entry's `engine`, `group`, `request`, `response` and `finalize`):

```text
Follow /review-schema for job_id={job_id} db={database_name} engine={ENGINE} group={GROUP}. Request: {REQUEST}. Write only {RESPONSE}, then run exactly: {FINALIZE}. Do not update .modernizer-state.json. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.
```

**Done signal.** After every review subagent's report, run the same `scripts/run_pe_review.py` command again, on its own: it lists only the reviews not recorded yet. `"status": "complete"` means every review is in: go on at once to 6d (or 6e). An `awaiting_llm` line lists the reviews still to record: end the turn with no text while one of them has a subagent that has not reported, and never dispatch a second subagent for it; one whose subagent already reported is recorded as `--failed` (below).

Wait for all of them to report (see Waiting Rule). A review subagent that returns `failed` is not a phase failure: that design keeps no routing flags, as when a Bedrock review errors. If it did not record that itself, record it with that entry's `finalize` command plus `--failed` (DynamoDB adds `--group {G}`), so 6e does not wait for it:

```bash
uv run python scripts/run_pe_review.py --job-id {job_id} --db {database_name} --engine {ENGINE} --finalize --failed
```

Run this step once per design round; one review per design is the whole budget.

**6d. Merge DynamoDB** once every group subagent has reported. First check which drafts exist:

```bash
uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --status
```

Run it exactly as shown, on its own, and read `status` and the keys named here from the one short JSON line it prints. Never pipe it into another command (`uv run python`, `tail`, `jq`, ...): a hook refuses a piped command. For DynamoDB the line lists only the groups to redo; every group's entry is in the `--split` line and in the `manifest` file the line names, under `artifacts/`: read that file with the Read tool.

- `"status": "drafts_pending"`: the groups in `drafts_missing` wrote no draft, and those in `drafts_invalid` wrote one that is not a readable JSON object. That is a phase failure for `schema_design_dynamodb` with reason `group drafts missing or invalid: <groups>` (see Error Handling: the retry is one fresh group subagent, same task text, per listed group). Never run `--merge` while drafts are missing or invalid. After a redone group draft, run 6c again before `--merge` (it lists only the reviews not yet recorded).
- `"status": "merged"`: the last `--merge` ran on exactly these drafts and passed. Set `phase_status.schema_design_dynamodb` = "complete".
- `"status": "merge_failed"`: the last `--merge` ran on exactly these drafts and printed `validation_failed` (the same verdict; a group draft's own `validation_passed: false` does not make a merge fail). Treat it as a `--merge` attempt that printed `validation_failed` with these `errors` (below).
- `"status": "merge_pending"`: run the merge:

  ```bash
  uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} --engine dynamodb --merge
  ```

Make **at most 3 `--merge` attempts in total**:

- `"status": "drafts_pending"` from `--merge`: it refused and wrote nothing (`missing_groups` have no draft, `invalid_groups` an unreadable one). This does not count as a `--merge` attempt; handle it like `drafts_pending` from `--status` above.
- `"status": "complete"` with no `DynamoDB merge review: …` entry in `warnings`: set `phase_status.schema_design_dynamodb` = "complete". Other `warnings` never fail the phase.
- `"status": "validation_failed"` with `"contract_failure": "merge"` (from `--merge`, or from `--status` as `merge_failed`): every group draft passes the output contract and only the merged design fails it, so no draft edit can fix it. Do not dispatch a fix subagent: set `phase_status.schema_design_dynamodb` = "failed", a phase failure with reason `merged design fails its output contract: <errors>`.
- `"status": "validation_failed"`, or `complete` with `DynamoDB merge review: …` warnings while attempts remain: dispatch one fix subagent, wait for it, then re-run `--merge`. Pass it the `errors` and `warnings` from the `--merge` line exactly as printed:

  ```text
  Follow /design-schema-dynamodb **Merge fix task** for job_id={job_id} db={database_name} assignment_version={N}. --merge printed: {MERGE_LINE}. Edit only the schema_draft_group_*.json files. Do not run `--merge` or `--finalize` and do not update .modernizer-state.json. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.
  ```

- A `complete` merge whose review warnings remain after the last attempt is still `complete`: those notes stay in the design's trade-offs.
- If the third attempt still prints `"status": "validation_failed"`, set `phase_status.schema_design_dynamodb` = "failed". That is a phase failure for `schema_design_dynamodb` (see Error Handling). Do not mark the phase complete.

Any other output or a non-zero exit is a phase failure for `schema_design_dynamodb` (from `--status` that includes `not_split`). Never run `--finalize` for DynamoDB; `--merge` is its final step.

**6e. Post-schema reroute** once every engine is designed and reviewed (after 6d, or after 6c without DynamoDB). It runs the router Bedrock mode runs after schema design: each flagged query (a review's routing flag or a designer's unsupported pattern) moves, with its co-dependency group, to its best-scoring engine among those the assignment keeps. The scorer decides, never the review.

```bash
uv run python scripts/run_post_schema_routing.py --job-id {job_id} --db {database_name}
```

It prints one JSON line:

- `"status": "complete"`: nothing (more) to move. Set `phase_status.post_schema_routing` = "complete" and go on. `rerouted` counts the queries moved after approval in this run.
- `"status": "awaiting_redesign"`: the router moved `pass_rerouted` queries and wrote them as assignment version `assignment_version` (`rerouted_after_approval`). Design again exactly the engines in `missing_designs`, the way 6a-6d design them: DynamoDB split → group drafts → reviews → merge, every other engine one `/design-schema-<engine>` subagent (6b task text) and then its review (6c). Launch the design subagents in a single message; the scripts pick the new version themselves. Then run this command again. While `missing_reviews` lists a design (its review is not recorded yet), no pass runs either: run 6c for it (and `--merge` again for a DynamoDB group), then run this command again. It prints `awaiting_redesign` at most twice in a run: the same cascade bound as Bedrock mode.
- A redesign that fails (its phase failure, after the one retry under `--auto`): run the rollback once, which writes a version restating the one the reroute started from, with its designs (as Bedrock mode does), then end as that `schema_design_<engine>` phase failure (see Error Handling):

  ```bash
  uv run python scripts/run_post_schema_routing.py --job-id {job_id} --db {database_name} --rollback
  ```

- `"status": "error"` or a non-zero exit is a phase failure for `post_schema_routing`.

Unless `{experience_mode}` is `ui`, tell the user schema design is complete, with the count of queries moved after approval when 6e's `rerouted` is not 0. Unless `{experience_mode}` is `chat`, add: table definitions, access patterns and GSIs will be on the job's results page, linked when the run completes.

### Phase 7: Synthesis

**Dispatch subagent** with this task text:

```text
Run /synthesize for job_id={job_id} db={database_name}. Unattended: do not ask the user anything. Do not dispatch subagents yourself. Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.
```

### Completion

Print the final summary from the synthesis `report.json` facts, with `--mode {experience_mode}`:

```bash
uv run python scripts/print_summary.py --job-id {job_id} --db {database_name} --final --mode {experience_mode}
```

Its stdout is the whole summary, in Markdown, at the depth of the results page: the executive summary, the headline figures (workload, target engines, projected cost, with the source cost and the savings when the collection carries a baseline, the retired engines, the migration waves), one line per engine with why, the open risks by severity with the most severe ones and their mitigations, and where to look next: the files of the deliverables folder <!-- deliverables-list -->`./artifacts/{db}/{job}/deliverables/`: `executive-report.pdf` (executive report: a landscape briefing), `engineering-report.md` (engineering report), `assessment-data.json` (Assessment Data, JSON), `engineering-report-detailed.md` (detailed engineering report: the full routing detail, for auditing the result), and `index.md`, which lists each file, who it is for and the assignment version<!-- /deliverables-list --> (it names any file that was not rendered), and, unless `{experience_mode}` is `chat`, the URLs of the results page and the summary page. The user does not see a command's output (Claude Code shows the call collapsed), so your next chat message must contain its stdout verbatim in every mode but `ui`, where chat carries only the UI pointers: quote only its `Results page` line there. Never write "the summary above" or point at the command's output instead of quoting it. The same hook reminds you. Do not retype, reorder or paraphrase it, add a figure of your own, or open `report.json` or any other artifact for it. The cache layer's figure is the reads it fronts, never shown as a workload fit. The summary is about the assessment, never about the run: do not comment on what you or a subagent did or did not read, and do not name a phase or step number. The copy `/synthesize` printed went to its subagent report, not to the user. A JSON line `{"status": "error", ...}` (or a non-zero exit) is a failure of the completion step: present its `message` (see Error Handling).

Run it only after the `/synthesize` subagent has reported back: the summary you quote is what the user sees, so its `Results page` line is the pointer to the results page, the same as the gate's `Summary page` line.

End the run with exactly one line `MODERNIZE_RESULT: complete job_id=<id> db=<db> mode=<mode>` (also when not `--auto`), in that same chat message after the quoted summary, where `<mode>` is `{experience_mode}` — `chat` if the UI never started or fell back. Under `--auto`, your final message must always contain a `MODERNIZE_RESULT` line: `complete` as above, or `failed phase=<phase> reason=<one line>`.

## Waiting Rule

This applies headless and interactive. Never end a turn waiting unless a dispatched subagent is still running: a headless run ends as soon as a turn ends with nothing running. In Phase 6, a subagent is still running only when the last status line (the **Done signal** of 6b or 6c) names its design or review as missing and it has not reported; when that line says every design or review is in, the next step runs in the same turn. Under `--auto`, a Stop hook (`scripts/hooks/require_modernize_result.py`) holds you to this: it refuses to end a turn with nothing running and no `MODERNIZE_RESULT` line (`Blocked: nothing is running ...`). When it does, run the step it names, or end with the `failed` result line.

When every subagent you dispatched has reported, do not wait for anything else. Read `phase_status` in `.modernizer-state.json` (and, for DynamoDB, the `--status` line from 6d), then either finish the phase yourself (for example, run `--merge` when `--status` prints `merge_pending`, or set the phase complete and dispatch the next phase) or end with `MODERNIZE_RESULT: failed phase=<phase> reason=<one line>`. A subagent's completion notification is its final report: nobody else will run the next step for you.

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
- **DynamoDB under `--auto`:** the retry for `schema_design_dynamodb` is one fresh group subagent per missing or invalid group (`drafts_pending`), or one more 6d round with a fresh fix subagent and a new budget of 3 `--merge` attempts (merge still `validation_failed` / `merge_failed`). The phase gets one retry in total. It gets no second one if a different failure follows the first. Set `phase_status.schema_design_dynamodb` = "failed" before retrying. If it fails again, end with `MODERNIZE_RESULT: failed phase=schema_design_dynamodb reason=<first error>`.
- **Schema design under `--auto`:** any `--status` other than `"complete"` for a non-DynamoDB engine after 6b (`validation_failed`, `finalize_pending` or `not_finalized`: the subagent returned `failed` after its 3 `--finalize` attempts, or reported success without a current finalize) is a phase failure like any other. Retry it once with a fresh subagent, then run `--status` again; if it is still not `"complete"`, end with `MODERNIZE_RESULT: failed phase=schema_design_<engine> reason=<first validation error, or the status>`. Never mark that engine's schema design complete.
