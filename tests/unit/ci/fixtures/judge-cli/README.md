# Recorded judge CLI fixtures

Inputs for `tests/unit/ci/test_judge_sampling.py` and `tests/unit/ci/test_judge_noise.py`. No model is called in those tests.

| File | Provenance |
|---|---|
| `result-record.json` | The final `type: "result"` record the CLI printed for a headless wordpress e2e-llm run on 2026-10-18 (CLI 2.1.288). `claude -p --output-format json` prints this same record, so it is the shape `ci/llm/judge.py` reads. `result` is replaced by each test with a judge reply; `session_id` and `uuid` are zeroed. The model is reported only under `modelUsage` (keyed by model id); there is no top-level `model` field, which is why every saved `judge.json` before #952 recorded `"model": null`. |
| `../judge-noise/wordpress-repeat-scores.json` | Per-criterion scores from 93 saved `judge.json` files of wordpress e2e-llm runs, 2026-10-09 to 2026-10-18, in the two largest groups whose deliverables share the same routing and risk content (same `reality_check.after_distribution` and the same open and resolved risk counts in `report.json`). Only the scores and the run label are kept. |
