# Old-job fixtures: artifacts a real older release wrote

Each folder is the artifact root of one job that a released version wrote, kept so today's code is tested against files it did not write (#1159). `tests/contract/test_old_job_loads.py` opens every folder through the real read paths: the artifact store and assignment versioning, the API routes the UI loads a finished job with, the report renderers, and the context graph.

Keep each folder as the release wrote it (a convention; no hook enforces it): do not regenerate or edit one to make a test pass. When the test fails, the change broke reading a saved job: add the legacy-map entry (CONTRIBUTING.md, "Renaming an id or an enum value"), not a new fixture.

## v0.4.0-beta.2

Made by checking out the `v0.4.0-beta.2` tag in a separate worktree and running its deterministic pipeline on the bundled synthetic WordPress sample: `uv run python scripts/run_assessment.py --file <wordpress-collection.json> --all -y --llm-mode none --artifact-root <dir>`. No model call, no AWS. The sample is synthetic and public; the files hold no absolute path, host name or account id (the collector's host name is `offline`).

Kept: the job cursor (`.meta/`), `collector/output.json`, `referee-triage/triage.json`, the five `analysis-<engine>/analysis.json`, the DocumentDB decision trace, assignment `v1` and `v2` (`v1` without its `access_patterns.json`), `assignment/v1/validation.json`, the reality check (`reality-check/output.json` and `v2/output.json`) and `synthesis/v2/report.json`.

Left out, because no reader needs them to load the job or because they are outputs: the rendered `deliverables/` (the test re-renders them from `report.json`), `uploads/`, `_logs/`, `collector/sql_shadow.json`, the `er-diagram.json` files, the other engines' `decision-trace.json` and `assignment/v1/access_patterns.json`. Whitespace was removed from the JSON (one line per file); no value was changed or trimmed. The folder has no `decisions/` or `schema-<engine>/`: `--llm-mode none` records no approval and skips schema design (every designer needs a model). The older decision and design-trace shapes have their own files in `tests/fixtures/` (`decision_*_pre_1257.json`, `design_trace_pre_1258/`).
