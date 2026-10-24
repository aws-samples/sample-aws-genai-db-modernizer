"""Zero-token replay of a saved real-model run (#1409).

A real-model run (``ci/e2e-llm.sh``) costs about $4 and ten minutes, so it cannot
run on every push; but most of what it checks is deterministic once the model has
answered. This keeps one passing run's model-written artifacts as a fixture
(``tests/fixtures/real_runs/<name>/``) and replays the checks over them with the
current code and no model call:

- ``ci/llm/run.py check-transcript`` over the saved transcript, against the current
  allowlist (``.claude/settings.ci.json``);
- the deliverables re-rendered from the saved ``report.json`` by the current
  renderers (``scripts/run_report.py``), then the external-mode e2e checks
  (``tests/e2e``, the job parametrized as ``external``), which include the
  customer-text guard and ``ci/llm/judge_facts.py``'s agreement with the report's
  own figures.

Synthesis and the facts layer are not re-run: the saved ``report.json`` (its
``facts`` included) is what that run wrote, so a change there is covered by the
deterministic shards and e2e-llm, not here, until the fixture is refreshed.

``ci/replay.sh`` runs both; ``ci/pre_merge.sh`` and the ``replay`` e2e shard call
it. The unit tests read the same fixture
(``tests/unit/ci/test_replay.py``,
``tests/unit/tools/validation/test_customer_text_guard_id_fields.py``).

The e2e-llm upload carries the synthesis folder, the deliverables and the
transcript, not the rest of the job (the assignment versions, the collector
output, the triage, the reality-check output), so the checks that read those are not replayed:
``NEEDS_FULL_JOB`` lists them.

Usage::

    uv run python ci/llm/replay.py save --from test-results/llm-both-wordpress \\
        --to tests/fixtures/real_runs/wordpress-both
    uv run python ci/llm/replay.py prepare --fixture tests/fixtures/real_runs/wordpress-both \\
        --root test-results/replay/artifacts
    uv run python ci/llm/replay.py deselect
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "real_runs" / "wordpress-both"

# The synthesis files a saved run keeps: the report every deliverable renders, and
# the model's input and answer (the judge's facts read the input's table scope).
SYNTHESIS_FILES = ("report.json", "llm_input.json", "llm_response.json")
RUN_FILE = "run.json"
TRANSCRIPT_FILE = "transcript.jsonl"

# The saved transcript's working directory, in place of the runner's own path, and
# the placeholders for the session's home and temp folders: a saved run names no
# machine, user or runner.
PLACEHOLDER_CWD = "/workspace/repo"
PLACEHOLDER_HOME = "/home/user"
PLACEHOLDER_TMP = "/tmp/claude"  # nosec B108 -- a placeholder written into text, never opened
ALLOWED_PATH_PREFIXES = (PLACEHOLDER_CWD, PLACEHOLDER_HOME, PLACEHOLDER_TMP)

# Rewrites applied in order, after the working directory: a home folder (macOS,
# Linux, root), the macOS per-user temp folder, the session's temp folder and
# socket, and any other temp path (under the temp placeholder). A path segment
# stops at a quote, whitespace or a JSON escape. Every /tmp here is text to match
# or write, never a file this module opens (hence the B108 marks).
_SEG = r"[^/\s\"'\\]+"
_PATH_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"(?:/private)?/var/folders/{_SEG}/{_SEG}/T(?=/|\b)"), "/tmp"),  # nosec B108
    (re.compile(r"/private/tmp(?=/|\b)"), "/tmp"),  # nosec B108
    (re.compile(rf"/tmp/claude-{_SEG}"), PLACEHOLDER_TMP),  # nosec B108
    (re.compile(rf"/tmp/cc-socks/{_SEG}"), f"{PLACEHOLDER_TMP}/agent.sock"),  # nosec B108
    (re.compile(r"(?<![\w.-])/tmp/(?!claude(?=/|\b))"), f"{PLACEHOLDER_TMP}/"),
    (re.compile(rf"/(?:Users|home)/(?!user(?=/|\b)){_SEG}"), PLACEHOLDER_HOME),
    (re.compile(r"(?<![\w.-])/root(?=/|\b)"), PLACEHOLDER_HOME),
)
# An absolute path under a machine's own folders, for private_data(): any one that
# does not start with an allowed placeholder names the machine it ran on.
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![\w.:/~-])/(?:Users|home|root|tmp|var|private|builds|opt|mnt|srv|Volumes|runner|github|workspace)"
    r"(?=/|\b)[^\s\"'\\]*"
)
_PRIVATE_VALUE_RES = (
    ("aws access key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("aws account id", re.compile(r"(?<![\w.-])\d{12}(?![\w])")),
    ("aws arn", re.compile(r"\barn:aws[\w-]*:")),
)

# A tool result longer than this keeps its first and last TRIM_KEEP characters:
# the checks read short results (a script's JSON status line, a hook's refusal),
# never a file's contents.
TRIM_OVER = 1000
TRIM_KEEP = 400
# Tool-call inputs the transcript checks read whole (a Bash command, an Agent prompt).
KEEP_INPUT_KEYS = frozenset({"command", "prompt", "description", "file_path", "skill"})

# The external-mode e2e checks that read a job artifact the saved run does not
# carry, so the replay deselects them. A check that needs only report.json and the
# deliverables is replayed; one added later that needs more goes here.
_ASSIGNMENT = "reads assignment/v*/assignment.json"
_COLLECTOR = "reads collector/output.json"
_TRIAGE = "reads referee-triage/triage.json"
# With no reality-check folder the check returns early and scans nothing: it would
# count as a replayed pass that checked nothing.
_REALITY_CHECK = "reads reality-check/output.json"
_ALL_ARTIFACTS = "lists every artifact of the job"
NEEDS_FULL_JOB: dict[str, str] = {
    "tests/e2e/test_confidence_follows_engine.py::test_every_version_holds_its_engines_score[external]": _ASSIGNMENT,
    "tests/e2e/test_confidence_follows_engine.py::test_a_consolidated_query_shows_its_new_engines_score[external]": _ASSIGNMENT,
    "tests/e2e/test_confidence_follows_engine.py::test_an_assignment_saved_before_the_fix_is_corrected_on_read[external]": _ASSIGNMENT,
    "tests/e2e/test_customer_facing_wording.py::test_assignment_reasons_have_no_internal_wording[external]": _ASSIGNMENT,
    "tests/e2e/test_customer_facing_wording.py::test_reality_check_output_has_no_internal_wording[external]": _REALITY_CHECK,
    "tests/e2e/test_data_movement_plan.py::test_each_sample_job_gets_one_valid_plan_per_wave[external]": _ASSIGNMENT,
    "tests/e2e/test_data_movement_plan.py::test_the_plans_ignore_the_hash_seed[external]": _ASSIGNMENT,
    "tests/e2e/test_data_movement_plan.py::test_priced_plans_state_each_waves_monthly_cost[external]": _ASSIGNMENT,
    "tests/e2e/test_one_writer_per_table.py::test_no_table_has_writes_on_two_engines[external]": _ASSIGNMENT,
    "tests/e2e/test_one_writer_per_table.py::test_report_json_states_each_tables_source_of_truth_and_replicas[external]": _ASSIGNMENT,
    "tests/e2e/test_pipeline.py::test_contract_artifacts_validate[external]": _ALL_ARTIFACTS,
    "tests/e2e/test_pipeline.py::test_every_versioned_artifact_is_a_registered_contract_file[external]": _ALL_ARTIFACTS,
    "tests/e2e/test_pipeline.py::test_every_read_back_artifact_loads_a_newer_file_with_unknown_fields[external]": _ALL_ARTIFACTS,
    "tests/e2e/test_relational_gaps.py::test_each_sample_finds_its_reviewed_gaps[external]": _TRIAGE,
    "tests/e2e/test_relational_gaps.py::test_every_found_gap_names_evidence_that_exists[external]": _TRIAGE,
    "tests/e2e/test_report_pdf.py::test_the_file_is_report_json_rendered_byte_for_byte[external]": _COLLECTOR,
    "tests/e2e/test_report_pdf.py::test_every_wave_its_counts_and_its_reason[external]": _ASSIGNMENT,
    "tests/e2e/test_report_pdf.py::test_no_collected_table_name_appears[external]": _COLLECTOR,
    "tests/e2e/test_report_pdf.py::test_nothing_is_cut[external]": _COLLECTOR,
    "tests/e2e/test_table_profile_writes.py::test_every_profile_write_count_equals_its_write_group[external]": _ASSIGNMENT,
    "tests/e2e/test_table_profile_writes.py::test_only_writes_not_typed_as_dml_change_a_profile[external]": _ASSIGNMENT,
    "tests/e2e/test_utility_pin_label.py::test_every_utility_pin_is_labelled_in_the_review_and_the_journey[external]": _ASSIGNMENT,
    "tests/e2e/test_utility_pin_label.py::test_an_assignment_saved_before_the_field_still_labels_its_pins[external]": _ASSIGNMENT,
    "tests/e2e/test_utility_pin_label.py::test_the_mean_fit_leaves_pins_out_and_says_how_many[external]": _ASSIGNMENT,
    "tests/e2e/test_utility_pin_label.py::test_an_old_assignment_still_leaves_its_pins_out_of_the_mean[external]": _ASSIGNMENT,
    "tests/e2e/test_waived_counts_follow_their_table.py::test_no_waived_count_sits_off_its_tables_source_of_truth[external]": _ASSIGNMENT,
}


def _trim(value: Any) -> Any:
    if isinstance(value, str) and len(value) > TRIM_OVER:
        cut = len(value) - 2 * TRIM_KEEP
        return f"{value[:TRIM_KEEP]}\n[... {cut} characters trimmed ...]\n{value[-TRIM_KEEP:]}"
    if isinstance(value, list):
        return [_trim(v) for v in value]
    if isinstance(value, dict):
        return {k: _trim(v) for k, v in value.items()}
    return value


def _session_cwds(lines: list[str]) -> set[str]:
    cwds = set()
    for line in lines:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("type") == "system" and rec.get("subtype") == "init":
            cwd = rec.get("cwd")
            if isinstance(cwd, str) and cwd not in ("", "/"):
                cwds.add(cwd)
    return cwds


def scrub_paths(text: str) -> str:
    """``text`` with every home, temp and socket path replaced by its placeholder."""
    for pattern, placeholder in _PATH_REWRITES:
        text = pattern.sub(placeholder, text)
    return text


def private_data(text: str) -> list[str]:
    """What in ``text`` would name a machine, a user or an account: an absolute path
    outside the placeholders, an AWS access key, account id or ARN."""
    found = [
        f"absolute path {m.group(0)}"
        for m in _ABSOLUTE_PATH_RE.finditer(text)
        if not m.group(0).startswith(ALLOWED_PATH_PREFIXES)
    ]
    found += [f"{kind} {m.group(0)}" for kind, rx in _PRIVATE_VALUE_RES for m in rx.finditer(text)]
    return found


def trim_transcript(text: str) -> str:
    """The transcript with the runner's working directory replaced by
    ``PLACEHOLDER_CWD``, every home, temp and socket path by its placeholder, every long tool result cut to its ends and every thinking
    block emptied. The records, their order and every tool call stay as they were,
    so ``check-transcript`` reads the same run."""
    lines = text.splitlines()
    cwds = sorted(_session_cwds(lines), key=len, reverse=True)
    out = []
    for line in lines:
        for cwd in cwds:
            # The path itself, and Claude Code's project folder name made from it.
            line = line.replace(cwd, PLACEHOLDER_CWD)
            line = line.replace(cwd.replace("/", "-"), PLACEHOLDER_CWD.replace("/", "-"))
        line = scrub_paths(line)
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)
            continue
        message = rec.get("message") if isinstance(rec, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    block["content"] = _trim(block.get("content"))
                elif block.get("type") == "tool_use" and isinstance(block.get("input"), dict):
                    # A file the model wrote is cut; a command and a dispatch prompt,
                    # which the checks read whole, are kept.
                    block["input"] = {
                        k: v if k in KEEP_INPUT_KEYS else _trim(v)
                        for k, v in block["input"].items()
                    }
                elif block.get("type") == "thinking":
                    block["thinking"] = ""
                    block.pop("signature", None)
        if isinstance(rec, dict) and "tool_use_result" in rec:
            rec["tool_use_result"] = _trim(rec["tool_use_result"])
        out.append(json.dumps(rec, separators=(",", ":")))
    return "\n".join(out) + "\n"


def _load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a JSON object")
    return data


def save(source: Path, dest: Path) -> dict[str, Any]:
    """Keep the passing run in ``source`` (an e2e-llm output folder,
    ``test-results/llm-<mode>-<fixture>``) as the replay fixture ``dest``."""
    results = _load(source / "results.json")
    if results.get("pass") is not True:
        raise ValueError(f"{source}: results.json does not pass; keep a passing run only")
    db, job = results.get("db"), results.get("job_id")
    if not db or not job:
        raise ValueError(f"{source}: results.json names no db and job_id")
    synthesis = source / "deliverables" / str(db) / "synthesis"
    if not (synthesis / "report.json").is_file():
        raise ValueError(f"{synthesis}: no report.json")
    files = {
        name: (synthesis / name).read_text()
        for name in SYNTHESIS_FILES
        if (synthesis / name).is_file()
    }
    files[TRANSCRIPT_FILE] = trim_transcript((source / TRANSCRIPT_FILE).read_text())
    found = [f"{name}: {hit}" for name, text in files.items() for hit in private_data(text)]
    if found:
        # The fixture is public: nothing is written until every file is clean.
        raise ValueError(f"{source}: the run names a machine or an account: {found[:10]}")
    dest.mkdir(parents=True, exist_ok=True)
    for name in SYNTHESIS_FILES:
        (dest / name).unlink(missing_ok=True)
    for name, text in files.items():
        (dest / name).write_text(text if text.endswith("\n") else text + "\n")
    judge = source / "judge.json"
    judge_mean = _load(judge).get("mean") if judge.is_file() else None
    run = {
        "db": db,
        "job_id": job,
        "mode": results.get("mode"),
        "fixture": results.get("fixture"),
        "git_sha": results.get("git_sha"),
        "timestamp": results.get("timestamp"),
        "cost_usd": results.get("cost_usd"),
        "judge_mean": judge_mean,
        "checks": results.get("checks"),
    }
    (dest / RUN_FILE).write_text(json.dumps(run, indent=2) + "\n")
    return run


def prepare(fixture: Path, root: Path) -> dict[str, Any]:
    """Lay the saved run out as a job under ``root`` (its synthesis folder as
    version 1) and render its deliverables with the current renderers."""
    from scripts.run_report import run as render_job

    run = _load(fixture / RUN_FILE)
    db, job = str(run["db"]), str(run["job_id"])
    job_dir = root / db / job
    if job_dir.exists():
        shutil.rmtree(job_dir)
    synthesis = job_dir / "synthesis" / "v1"
    synthesis.mkdir(parents=True)
    for name in SYNTHESIS_FILES:
        if (fixture / name).is_file():
            shutil.copy2(fixture / name, synthesis / name)
    rendered = render_job(job, db, str(root), None)
    if rendered.get("status") != "complete":
        raise RuntimeError(f"rendering the saved report failed: {rendered}")
    return {"artifact_root": str(root), "db": db, "job_id": job, "mode": run.get("mode")}


def deselect_args() -> list[str]:
    return [arg for node in NEEDS_FULL_JOB for arg in ("--deselect", node)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_save = sub.add_parser("save", help="Keep a passing e2e-llm run as the replay fixture.")
    p_save.add_argument("--from", dest="source", type=Path, required=True)
    p_save.add_argument("--to", dest="dest", type=Path, default=DEFAULT_FIXTURE)
    p_prep = sub.add_parser("prepare", help="Lay the fixture out as a job and render it.")
    p_prep.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    p_prep.add_argument("--root", type=Path, required=True)
    sub.add_parser("deselect", help="Print the pytest --deselect args, one per line.")
    args = parser.parse_args(argv)
    if args.command == "save":
        print(json.dumps(save(args.source, args.dest)))
    elif args.command == "prepare":
        info = prepare(args.fixture, args.root)
        print(f"{info['db']} {info['job_id']} {info['mode']}")
    else:
        print("\n".join(deselect_args()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
