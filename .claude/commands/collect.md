
# /collect

Parses raw collector output (MySQL, MariaDB, PostgreSQL, SQL Server or Oracle offline collection) and initializes the modernization job.

## Tool Use

Read files with the Read tool (use `offset`/`limit` for large files). Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` (or the Grep tool if this session has one). Use Bash only for the documented `uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, `ls`, `cd` chains, heredocs or `grep`. Use a search's output as is: never pipe, chain or post-process it.

## CRITICAL RULE

**NEVER read or inspect the collector JSON file.** Just pass its path to the script. The script handles all parsing. If it fails, let it fail and report the error.

## Prerequisites

- A collector output JSON file exists (from running the collection SQL script against the source database)
- The file path is provided as argument OR the user specifies it
- Optional: `--engines-in-use <ids>`, comma-separated engine ids (for example `opensearch,elasticache`) of the other AWS databases the application already runs today. Never ask for it: absent means only the source engine counts.
- The path points at the `.json` file; a bundled sample's missing `.json` is extracted from its tracked `.zip` by the script, so no unzip step is needed

## Steps

1. **Get collector file path**
   Ask the user for the path to the collector output JSON if not provided.

2. **Run collector**

   ```bash
   uv run python scripts/run_collect.py --file <collector_file> --db-from-file-name
   ```

   When `--engines-in-use` was given, add `--engines-in-use <ids>` to the command.

   The script auto-generates a `job_id` and returns it in the output JSON, together with the `database_name` it derived from the file name (the same rule as `/modernize`). Never make up the name yourself.

3. **Initialize state**
   Using the output from the script, create `.modernizer-state.json`:

   ```json
   {
     "job_id": "<from script output>",
     "database_name": "<from script output>",
     "current_phase": "collect",
     "selected_engines": [],
     "llm_mode": "full",
     "phase_status": {
       "collect": "complete"
     }
   }
   ```

4. **Present results**
   Summarize:
   - Number of tables found
   - Number of query patterns captured
   - Job ID assigned

5. **Update state**
   Set `current_phase` = "triage"
