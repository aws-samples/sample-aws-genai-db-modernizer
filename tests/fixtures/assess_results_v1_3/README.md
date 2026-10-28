# Saved contract 1.3 `assess()` results: never regenerate

These files are `assess()` results (`AssessmentResult.model_dump_json()`) written by the code released as v0.4.0-beta.3, the first release of contract `1.3`, from the bundled synthetic samples, with `deterministic=True` and no artifacts:

| File | Sample | `run_through` |
|---|---|---|
| `wordpress_analysis.json` | WordPress (MySQL) | `analysis` |
| `wordpress_reality_check.json` | WordPress (MySQL) | `reality_check` |
| `trailgear_analysis.json` | trailgear (SQL Server, foreign key statistics) | `analysis` |

They are the read-back guarantee from contract `1.3` on: every later release must read each one back through `AssessmentResult.model_validate_json` with every saved value unchanged (`tests/contract/test_assess_saved_results.py`). A later MINOR may add fields; it may not drop, rename or change a saved one, nor refuse `contract_version` `"1.3"`.

Never regenerate or edit them, not even when the code's output changes: they record what a caller may have stored. The Claude Code edit hook (`scripts/hooks/block_baseline_edit.py`) blocks hand edits. When the test fails, the change broke reading a stored result: fix the code, not the file.
