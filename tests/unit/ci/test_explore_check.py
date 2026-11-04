# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``ci/llm/run.py check-explore``: the e2e-llm ``/explore`` step's deterministic check (#307).

After the headless ``/modernize`` run, ``ci/e2e-llm.sh`` asks ``/explore`` one fixed
question and runs the catalog entry that answers it itself (``graph_query.py run``). The
check passes when the session ran that entry on the job and its final answer names every
figure of the expected rows. No model call here: the transcripts are written by hand.
"""

from __future__ import annotations

import json
import re
import subprocess  # nosec B404 -- runs this repo's own script with fixed argv
import sys
from pathlib import Path

import pytest

from ci.llm import run

REPO = Path(__file__).resolve().parents[3]
JOB = "abc12345"
EXPECTED = {
    "status": "ok",
    "entry": "engine_distribution",
    "db": "wordpress",
    "job_id": JOB,
    "params": {},
    "columns": ["engine", "queries"],
    "rows": [{"engine": "aurora_mysql", "queries": 68}, {"engine": "dynamodb", "queries": 39}],
    "row_count": 2,
    "truncated": False,
}
RUN_COMMAND = f"uv run python scripts/graph_query.py run engine_distribution --job-id {JOB}"


def _records(
    answer: str,
    *,
    commands: tuple[str, ...] = ("uv run python scripts/graph_query.py list", RUN_COMMAND),
    is_error: bool = False,
    denials: list[dict] | None = None,
) -> list[dict]:
    records: list[dict] = [{"type": "system", "subtype": "init", "session_id": "s"}]
    for i, command in enumerate(commands):
        records.append(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": f"tu-{i}",
                            "name": "Bash",
                            "input": {"command": command},
                        }
                    ]
                },
            }
        )
        records.append(
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": f"tu-{i}", "content": "{}"}]
                },
            }
        )
    records.append(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": answer}]}}
    )
    records.append(
        {
            "type": "result",
            "is_error": is_error,
            "num_turns": 4,
            "total_cost_usd": 0.12,
            "permission_denials": denials or [],
            "result": answer,
        }
    )
    return records


GOOD = "Aurora MySQL gets 68 queries and DynamoDB gets 39 (from `engine_distribution`)."


def _check(records: list[dict], expected: dict | None = None, **kw) -> dict:
    options = {"name_columns": ["engine"], "count_column": "queries", "ref": "2", **kw}
    return run.check_explore(
        records,
        EXPECTED if expected is None else expected,
        entry=kw.pop("entry", "engine_distribution"),
        job_id=JOB,
        **{k: v for k, v in options.items() if k != "entry"},
    )


def test_an_answer_with_every_figure_from_the_entry_passes() -> None:
    out = _check(_records(GOOD))
    assert out["pass"] is True, out
    assert out["ran_entry"] is True and out["missing_figures"] == []
    assert out["cost_usd"] == 0.12 and out["num_turns"] == 4


@pytest.mark.parametrize(
    "answer",
    [
        "aurora_mysql: 68 queries. dynamodb: 39 queries.",
        "Amazon Aurora MySQL gets 68 queries, Amazon DynamoDB 39.",
        "| Engine | Queries |\n|---|---:|\n| Aurora MySQL | 68 |\n| DynamoDB | 39 |",
        "68 queries go to Aurora MySQL and 39 to DynamoDB.",
    ],
)
def test_ids_display_names_tables_and_either_order_pass(answer: str) -> None:
    assert _check(_records(answer))["pass"] is True, answer


def test_swapped_counts_fail() -> None:
    out = _check(_records("Aurora MySQL gets 39 queries and DynamoDB gets 68."))
    assert out["pass"] is False
    assert out["missing_figures"] == ["aurora_mysql: 68", "dynamodb: 39"]
    table = "| Aurora MySQL | 39 |\n| DynamoDB | 68 |"
    assert _check(_records(table))["pass"] is False


def test_both_figures_in_one_clause_with_both_engines_do_not_count() -> None:
    # Nothing ties a count to an engine here.
    out = _check(_records("Aurora MySQL and DynamoDB: 68 / 39 queries"))
    assert out["pass"] is False


def test_the_entry_ref_is_never_read_as_a_figure() -> None:
    expected = {**EXPECTED, "rows": [{"engine": "aurora_mysql", "queries": 2}]}
    for answer in (
        "Entry 2 (`engine_distribution`): every query goes to Aurora MySQL.",
        "Aurora MySQL gets the queries (catalog ref 2).",
        "Aurora MySQL gets the queries (2).",
    ):
        out = _check(_records(answer), expected)
        assert out["pass"] is False, answer
    assert _check(_records("Aurora MySQL gets 2 queries (entry 2)."), expected)["pass"] is True


def test_an_engine_name_is_matched_whole() -> None:
    # "Aurora" is not Aurora MySQL, and Aurora PostgreSQL is another engine.
    expected = {**EXPECTED, "rows": [{"engine": "aurora_mysql", "queries": 68}]}
    for answer in ("Aurora PostgreSQL gets 68 queries.", "Aurora gets 68 queries."):
        assert _check(_records(answer), expected)["pass"] is False, answer


def test_a_wrong_figure_fails_and_names_it() -> None:
    out = _check(_records("Aurora MySQL gets 68 queries and DynamoDB gets 40."))
    assert out["pass"] is False
    assert out["missing_figures"] == ["dynamodb: 39"]


def test_a_figure_inside_a_larger_number_does_not_count() -> None:
    out = _check(_records("Aurora MySQL gets 168 queries and DynamoDB gets 39."))
    assert out["missing_figures"] == ["aurora_mysql: 68"]


def test_a_missing_engine_fails() -> None:
    out = _check(_records("Aurora MySQL gets 68 queries; another engine gets 39."))
    assert out["pass"] is False
    assert out["missing_figures"] == ["dynamodb: 39"]


def test_thousands_separators_are_accepted() -> None:
    expected = {**EXPECTED, "rows": [{"engine": "dynamodb", "queries": 1234}]}
    assert _check(_records("DynamoDB gets 1,234 queries."), expected)["pass"] is True


# The e2e-llm question: a parameter, answered by the entry's one row for the engine. Its
# target_counts are per design object (a table, a GSI), not the question's figure.
DEST_EXPECTED = {
    "status": "ok",
    "entry": "engine_destinations",
    "params": {"engine": "dynamodb"},
    "rows": [
        {
            "destination": "dynamodb",
            "source_tables": ["wordpress.wp_posts", "wordpress.wp_options"],
            "access_patterns": ["DDB-AP-12", "DDB-AP-30"],
            "targets": ["dynamodb:table:wp_posts@v2", "dynamodb:table:wp_options@v2"],
            "fallbacks": None,
            "target_counts": [
                {"id": "dynamodb:table:wp_posts@v2", "queries": 13, "cache_queries": 0},
                {"id": "dynamodb:table:wp_options@v2", "queries": 5, "cache_queries": 0},
            ],
            "queries": 40,
            "cache_queries": 0,
        },
    ],
}  # fmt: skip
DEST_RUN = f"uv run python scripts/graph_query.py run engine_destinations --job-id {JOB} --param engine=dynamodb"


def _dest(answer: str, commands: tuple[str, ...] = (DEST_RUN,)) -> dict:
    return run.check_explore(
        _records(answer, commands=commands),
        DEST_EXPECTED,
        entry="engine_destinations",
        job_id=JOB,
        params={"engine": "dynamodb"},
        name_columns=["destination", "source_tables"],
        count_column="queries",
        rows="top",
        ref="B",
    )


@pytest.mark.parametrize(
    "answer",
    [
        "DynamoDB serves 40 queries, from wp_posts and wp_options among others.",
        "The `dynamodb` destination gets 40 queries.",
    ],
)
@pytest.mark.passes_on_main
def test_the_engines_row_with_its_count_passes(answer: str) -> None:
    assert _dest(answer)["pass"] is True, answer


@pytest.mark.parametrize(
    "answer",
    [
        # What a model answered when asked for the most-queried source table: the
        # per-design-object count, not the engine's.
        "`wp_posts` sends the most queries to DynamoDB: 13, followed by wp_options with 5.",
        "DynamoDB serves 13 queries through its wp_posts table.",
        "DynamoDB serves 39 queries.",
    ],
)
@pytest.mark.passes_on_main
def test_an_answer_citing_only_per_target_counts_or_a_wrong_total_fails(answer: str) -> None:
    out = _dest(answer)
    assert out["pass"] is False, answer
    assert out["missing_figures"] == ["dynamodb: 40"]


def test_the_run_must_bind_the_parameter() -> None:
    no_param = DEST_RUN.replace(" --param engine=dynamodb", "")
    other = DEST_RUN.replace("engine=dynamodb", "engine=aurora_mysql")
    for command in (no_param, other):
        assert _dest("wp_posts: 8 queries.", (command,))["ran_entry"] is False, command


def test_any_argument_order_the_cli_accepts_counts_as_running_the_entry() -> None:
    reordered = (
        "uv run python scripts/graph_query.py run --param engine=dynamodb "
        f"--job-id={JOB} engine_destinations --format json"
    )
    assert _dest("wp_posts: 8 queries.", (reordered,))["ran_entry"] is True


def test_an_answer_without_running_the_entry_on_the_job_fails() -> None:
    # The right figures, but not from the catalog: invented, or from another job.
    other_job = RUN_COMMAND.replace(JOB, "otherjob")
    for commands in ((), ("uv run python scripts/graph_query.py list",), (other_job,)):
        out = _check(_records(GOOD, commands=commands))
        assert out["pass"] is False and out["ran_entry"] is False, commands
        assert "graph_query.py run engine_distribution" in out["error"]


def test_another_entry_with_a_matching_prefix_does_not_count() -> None:
    commands = (RUN_COMMAND.replace("engine_distribution", "engine_distribution_x"),)
    assert _check(_records(GOOD, commands=commands))["ran_entry"] is False


def test_an_error_result_fails() -> None:
    out = _check(_records(GOOD, is_error=True))
    assert out["pass"] is False and "is_error" in out["error"]


def test_no_result_line_fails() -> None:
    records = [r for r in _records(GOOD) if r["type"] != "result"]
    out = _check(records)
    assert out["pass"] is False and "no result line" in out["error"]


def test_a_denied_graph_query_call_fails_as_a_broken_allowlist() -> None:
    denial = {"tool_name": "Bash", "tool_input": {"command": RUN_COMMAND}}
    out = _check(_records(GOOD, denials=[denial]))
    assert out["pass"] is False and "allowlist" in out["error"]
    assert out["denials"] == 1


def test_an_expected_result_with_no_rows_cannot_be_checked() -> None:
    out = _check(_records(GOOD), {**EXPECTED, "rows": [], "row_count": 0})
    assert out["pass"] is False and "no rows" in out["error"]
    out = _check(_records(GOOD), {"status": "error", "message": "no job"})
    assert out["pass"] is False and "no job" in out["error"]


@pytest.mark.passes_on_main
def test_the_cli_writes_the_verdict_and_exits_by_it(tmp_path: Path) -> None:
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(DEST_EXPECTED))
    for answer, code in (("DynamoDB serves 40 queries.", 0), ("Entry B: wp_posts.", 1)):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            "\n".join(json.dumps(r) for r in _records(answer, commands=(DEST_RUN,)))
        )
        proc = subprocess.run(  # nosec B603 -- fixed argv
            [
                sys.executable, "ci/llm/run.py", "check-explore", str(transcript),
                "--expected", str(expected), "--entry", "engine_destinations", "--job-id", JOB,
                "--param", "engine=dynamodb", "--names", "destination,source_tables",
                "--count", "queries", "--rows", "top",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=60,
        )  # fmt: skip
        assert proc.returncode == code, proc.stderr
        assert json.loads(proc.stdout)["pass"] is (code == 0)


# ---------------------------------------------------------------------------
# results.json
# ---------------------------------------------------------------------------


def _results(tmp_path: Path, explore: Path | None) -> dict:
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps({"job_id": JOB, "db": "wordpress", "mode": "chat", "cost_usd": 0.42})
    )
    junit = tmp_path / "junit.xml"
    junit.write_text(
        "<testsuite>"
        + "".join(
            f'<testcase classname="tests.e2e.{m}" name="t"/>'
            for m in ("test_pipeline", "test_report_engineering", "test_report_pdf")
        )
        + "</testsuite>"
    )
    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps({"pass": True, "mean": 5.0, "scores": {}}))
    return run.build_results_row(
        mode="chat",
        fixture="wordpress",
        transcript_summary_path=summary,
        pytest_junit_paths=[junit],
        judge_path=judge,
        git_sha="abc",
        explore_path=explore,
    )


@pytest.mark.parametrize("passed", [True, False])
def test_results_record_the_explore_check_and_require_it_when_it_ran(
    tmp_path: Path, passed: bool
) -> None:
    explore = tmp_path / "explore.json"
    explore.write_text(
        json.dumps({"pass": passed, "cost_usd": 0.2, "entry": "engine_distribution"})
    )
    row = _results(tmp_path, explore)
    assert row["checks"]["explore"] is passed
    assert row["explore"]["cost_usd"] == 0.2
    assert row["total_cost_usd"] == pytest.approx(0.62)  # the run's 0.42 plus the session's
    assert row["pass"] is passed


def test_results_fail_when_the_explore_step_ran_but_wrote_nothing(tmp_path: Path) -> None:
    row = _results(tmp_path, tmp_path / "missing.json")
    assert row["checks"]["explore"] is None and row["pass"] is False


def test_results_without_the_explore_step_leave_it_null_and_unrequired(tmp_path: Path) -> None:
    row = _results(tmp_path, None)
    assert row["checks"]["explore"] is None and row["explore"] is None
    assert row["pass"] is True and row["total_cost_usd"] == 0.42


# ---------------------------------------------------------------------------
# ci/e2e-llm.sh wiring
# ---------------------------------------------------------------------------


def _script() -> str:
    return (REPO / "ci" / "e2e-llm.sh").read_text()


def test_e2e_llm_runs_the_explore_step_with_the_same_permissions() -> None:
    script = _script()
    assert '-p "/explore --job-id $JOB $EXPLORE_QUESTION" "${EXPLORE_FLAGS[@]}"' in script
    assert 'for flag in "${CLAUDE_FLAGS[@]}"; do' in script
    assert "EXPLORE_CMD=(env MODERNIZER_CI_SANDBOX=1)" in script
    assert 'graph_query.py run "$EXPLORE_ENTRY" --job-id "$JOB" --db "$DB"' in script
    assert "ci/llm/run.py check-explore" in script
    assert '--explore "$OUT/explore.json"' in script


def test_the_explore_question_needs_a_parameter_and_is_not_the_catalogs_wording() -> None:
    from src.graph import catalog

    script = _script()
    entry = re.search(r'EXPLORE_ENTRY="(\w+)"', script).group(1)
    param = re.search(r'EXPLORE_PARAM="(\w+)=(\w+)"', script)
    question = re.search(r'EXPLORE_QUESTION="([^"]+)"', script).group(1)
    known = catalog.get_entry(entry)
    assert [p.name for p in known.params] == [param.group(1)]
    assert question != known.question
    assert "--names destination,source_tables --count queries --rows top" in script


def test_the_explore_question_asks_for_the_engines_count_the_entry_answers() -> None:
    """The entry returns one row for the engine (its queries and source tables), so the
    question asks for that count; a question about the most-queried table was answered
    from the entry's per-object `target_counts` instead (#1826)."""
    question = re.search(r'EXPLORE_QUESTION="([^"]+)"', _script()).group(1)
    assert question == "How many queries does DynamoDB serve, and from which source tables?"
    assert "most" not in question.lower()


def test_the_explore_step_runs_after_the_browser_tests() -> None:
    script = _script()
    assert script.index("step ui-tests") < script.index("step explore-expected")
    assert script.index("step report-tests") < script.index("step explore run_before_deadline")
    assert script.index("step explore run_before_deadline") < script.index("step results")


def _flag_loop() -> str:
    script = _script()
    start = script.index("    EXPLORE_FLAGS=()")
    return script[start : script.index("    done\n", start) + len("    done\n")]


@pytest.mark.parametrize(
    ("env", "turns", "budget"),
    [
        ({}, "15", "2"),
        ({"E2E_LLM_EXPLORE_MAX_TURNS": "5", "E2E_LLM_EXPLORE_MAX_BUDGET_USD": "1"}, "5", "1"),
    ],
)
def test_the_explore_session_gets_its_own_turn_and_budget_caps(env, turns, budget) -> None:
    snippet = (
        "CLAUDE_FLAGS=(--output-format stream-json --max-turns 200 --max-budget-usd 50 --verbose)\n"
        + _flag_loop()
        + 'printf "%s\\n" "${EXPLORE_FLAGS[@]}"\n'
    )
    proc = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["bash", "-c", snippet], capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", **env},
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == [
        "--output-format", "stream-json", "--max-turns", turns, "--max-budget-usd", budget,
        "--verbose",
    ]  # fmt: skip


def test_e2e_llm_dry_run_and_scopes_without_dynamodb_skip_explore() -> None:
    script = _script()
    assert "E2E_LLM_EXPLORE_TRANSCRIPT" in script
    assert "dry run: no E2E_LLM_EXPLORE_TRANSCRIPT, skipping the /explore step" in script
    assert "the engine scope leaves out DynamoDB: skipping the /explore step" in script
