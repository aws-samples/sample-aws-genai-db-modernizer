
# /collect

Parses raw collector output (MySQL/PostgreSQL/MariaDB offline collection) and initializes the modernization job.

## CRITICAL RULE

**NEVER read or inspect the collector JSON file.** Just pass its path to the script. The script handles all parsing. If it fails, let it fail and report the error.

## Prerequisites

- A collector output JSON file exists (from running the collection SQL script against the source database)
- The file path is provided as argument OR the user specifies it

## Steps

1. **Get collector file path**
   Ask the user for the path to the collector output JSON if not provided.

2. **Run collector**

   ```bash
   uv run python scripts/run_collect.py --file <collector_file> --db {database_name}
   ```

   The script auto-generates a `job_id` and returns it in the output JSON.

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
