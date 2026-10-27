"""The library demo (``docs/examples/library/demo_assess.py``) keeps working.

It runs the demo as a reader would, on the WordPress sample, and checks each printed
section against the result ``assess()`` returns for the same request. With
``--llm-mode none`` (the default) nothing is patched: no model is called. The
``--llm-mode bedrock`` runs patch the model entry points with the shared stand-ins
(``tests/assess_models.py``): tests never call a model.
"""

from __future__ import annotations

import importlib.util
import re
from collections import Counter
from functools import cache
from types import ModuleType
from typing import Any

import pytest

from src.assessment import CONTRACT_VERSION, AssessmentResult, assess
from src.shared import model_usage
from src.shared.report_facts import COST_SCOPE_LABEL
from tests.assess_models import DESIGNER, fake_designer, models_patched
from tests.unit.assessment.conftest import REPO, collection
from tests.unit.assessment.test_post_schema_routing import _FlaggingDesigner

DEMO = REPO / "docs" / "examples" / "library" / "demo_assess.py"
DB = "wordpress"
SCOPE = ["dynamodb", "elasticache"]
#: A priced model id (``src/catalog/model_prices.toml``) the fake designer reports usage for.
PRICED_MODEL = "us.anthropic.claude-opus-5-5"
FAKE_USAGE = {"inputTokens": 1000, "outputTokens": 200}


@cache
def demo() -> ModuleType:
    spec = importlib.util.spec_from_file_location("demo_assess", DEMO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_demo(capsys: pytest.CaptureFixture[str], *argv: str) -> dict[int, list[str]]:
    """The demo's output, by section number (0: the lines before section 1)."""
    assert demo().main(list(argv)) == 0
    sections: dict[int, list[str]] = {0: []}
    current = 0
    for raw in capsys.readouterr().out.splitlines():
        heading = re.match(r"^(\d+)\. ", raw)
        if heading:
            current = int(heading.group(1))
            sections[current] = []
        elif raw.strip():
            sections[current].append(raw.strip())
    return sections


@cache
def _expected(run_through: str, scope: tuple[str, ...] | None) -> AssessmentResult:
    return assess(
        {
            "database_name": DB,
            "collection": collection(DB),
            "options": {
                "run_through": run_through,
                "target_engines": list(scope) if scope else None,
            },
        }
    )


def text(lines: list[str]) -> str:
    return "\n".join(lines)


def check_loading(out: dict[int, list[str]]) -> None:
    raw = collection(DB)
    assert out[1][0] == (
        f"docs/examples/wordpress/wordpress.zip: {len(raw['tables'])} tables, "
        f"{len(raw['queries'])} statements"
    )
    assert "raises CollectionLoadError: the text: no JSON object found" in out[1][1]


def check_analysis(out: dict[int, list[str]], scope: tuple[str, ...] | None) -> None:
    scored = _expected("analysis", scope)
    assert (
        out[2][0]
        == f"label analysis_only: {scored.summary.statement_count} statements scored, 0 placed"
    )
    best = max(scored.statements, key=lambda s: len(s.fit))
    fit = ", ".join(f"{e} {n}" for e, n in sorted(best.fit.items()))
    assert out[2][1] == f"statement {best.query_id[:12]} ({best.query_type}): fit {fit}"
    assert out[2][2] == "A repeat run gives a byte-identical result: yes"
    # Contract 1.2 (#1603): the parsed text and the best engine's fit breakdown.
    assert out[2][3].startswith("query_text: ")
    assert out[2][3].removeprefix("query_text: ").rstrip(" .") in (best.query_text or "")
    engine = max(sorted(best.fit), key=lambda e: best.fit[e])
    entry = best.fit_breakdown[engine]
    assert out[2][4].startswith(f"fit_breakdown on {engine}: {entry.method}; ")
    penalties = ", ".join(f"{p.id} -{p.points}" for p in entry.penalties) or "none"
    assert out[2][4].endswith(f"base {entry.base}; penalties {penalties}; fit {best.fit[engine]}")
    for line, table in zip(out[2][5:], entry.tables, strict=False):
        assert line.startswith(f"{table.table_id}: ") and line.endswith(f"score {table.score}")


def check_placement(out: dict[int, list[str]], placed: AssessmentResult) -> None:
    summary = placed.summary
    per_engine = ", ".join(f"{e} {n}" for e, n in summary.statements_per_engine.items())
    assert out[3][0] == (
        f"label recommendation, assignment v{summary.assignment_version}, "
        f"{summary.statement_count} statements: {per_engine}"
    )
    body = text(out[3])
    for engine in summary.engines:
        assert re.search(
            rf"^{engine.engine} +{engine.role} +{engine.statement_count}\b", body, re.M
        ), engine.engine
    for move in summary.consolidations:
        assert f"consolidated: {move.from_engine} -> {move.to_engine} ({move.query_count})" in body
        assert (
            f"consolidated: {move.from_engine} -> {move.to_engine} ({move.query_count}), "
            f"{move.action}"
        ) in body
    kinds = Counter(g.kind for g in placed.groups)
    assert (
        f"groups that move together: {', '.join(f'{n} {k}' for k, n in sorted(kinds.items()))}"
        in body
    )
    assert f"tables with an owner: {sum(1 for t in summary.tables if t.owner)}" in body
    assert f"table facts: {len(summary.table_facts)} tables, 0 foreign keys" in body


def check_reasons(out: dict[int, list[str]], placed: AssessmentResult) -> None:
    codes = Counter(s.reason_code for s in placed.statements if s.reason_code)
    assert out[4][0] == "reason codes: " + ", ".join(f"{c} {n}" for c, n in codes.most_common())
    examples = out[4][1:]
    assert 1 <= len(examples) <= 3
    by_id = {s.query_id[:12]: s for s in placed.statements}
    for example in examples:
        statement = by_id[example.split()[0]]
        assert f"{statement.engine} [{statement.reason_code}]" in example


def check_scope(out: dict[int, list[str]], placed: AssessmentResult) -> None:
    scope = placed.options.target_engines
    assert out[5][0] == (
        f"scope: {', '.join(scope)}"
        if scope
        else "scope: none (every engine the workload warrants)"
    )
    assert out[5][1] == f"unservable statements: {placed.summary.unservable_count}"
    unservable = [s for s in placed.statements if s.reason_code == "unservable"]
    if unservable:
        assert out[5][2].startswith(f"e.g. {unservable[0].query_id[:12]}: No engine in the")
    else:
        assert len(out[5]) == 2


def check_json(out: dict[int, list[str]]) -> None:
    assert out[10][0].endswith(
        f"contract {CONTRACT_VERSION}: valid against "
        "docs/contracts/schemas/assess_result.v1.schema.json"
    )


# --------------------------------------------------------------------------- no model


@pytest.mark.parametrize("scope", [None, tuple(SCOPE)], ids=["every-engine", "scoped"])
def test_the_deterministic_demo_states_each_sections_facts(
    capsys: pytest.CaptureFixture[str], scope: tuple[str, ...] | None
) -> None:
    argv = ["--target-databases", ",".join(scope)] if scope else []
    out = run_demo(capsys, *argv)
    placed = _expected("reality_check", scope)

    assert out[0][0].startswith("assess() library demo: wordpress, --llm-mode none")
    check_loading(out)
    check_analysis(out, scope)
    check_placement(out, placed)
    check_reasons(out, placed)
    check_scope(out, placed)
    if scope:
        assert placed.summary.unservable_count > 0
        assert "aurora_mysql" not in text(out[3])
    else:
        assert "relational engine: aurora_mysql" in out[3]
    # The model phases say what they need, and the library's refusal is shown.
    assert "it needs a model: with deterministic=True assess() refuses it up front:" in out[6]
    assert out[6][1].startswith("run_through='schema_design' needs a model")
    assert out[7] == ["runs after schema design (needs a model); nothing to show at this stage"]
    assert out[8] == ["the report (and its cost figures) comes from synthesis, which needs a model"]
    assert out[9] == ["no model call, so no usage record (--llm-mode none)"]
    check_json(out)


def test_the_demo_stops_at_analysis_when_asked(capsys: pytest.CaptureFixture[str]) -> None:
    out = run_demo(capsys, "--run-through", "analysis")
    check_analysis(out, None)
    assert 3 not in out and 4 not in out
    check_scope(out, _expected("analysis", None))
    check_json(out)


def test_progress_lines_go_to_stderr_only_when_asked(capsys: pytest.CaptureFixture[str]) -> None:
    import logging

    from src.assessment.runner import PROGRESS_LOGGER

    progress = logging.getLogger(PROGRESS_LOGGER)
    handlers, level = list(progress.handlers), progress.level
    assert demo().main(["--run-through", "analysis", "--progress"]) == 0
    captured = capsys.readouterr()
    assert "   | [triage] Starting triage for wordpress" in captured.err.splitlines()
    assert "[triage]" not in captured.out
    # The demo removes the handler it added and restores the level it set.
    assert progress.handlers == handlers
    assert progress.level == level

    assert demo().main(["--run-through", "analysis"]) == 0
    assert "[triage]" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--target-databases", "aurora"], "the engine id 'aurora' is legacy"),
        (["--target-databases", ","], "target_engines is empty: name at least one engine"),
        (["--run-through", "synthesis"], "--run-through synthesis needs a model"),
    ],
)
def test_a_refused_option_stops_before_anything_runs(
    capsys: pytest.CaptureFixture[str], argv: list[str], message: str
) -> None:
    with pytest.raises(SystemExit) as stopped:
        demo().main(argv)
    assert stopped.value.code == 2
    captured = capsys.readouterr()
    assert message in captured.err
    assert captured.out == ""


# --------------------------------------------------------------------------- model phases


def _designer_with_usage(inner: Any) -> Any:
    def design(**kw: Any) -> Any:
        model_usage.record_usage(PRICED_MODEL, FAKE_USAGE)
        return inner(**kw)

    return design


def run_with_models(
    capsys: pytest.CaptureFixture[str], designer: Any, *argv: str
) -> dict[int, list[str]]:
    with models_patched() as mocks:
        mocks[DESIGNER].side_effect = _designer_with_usage(designer)
        return run_demo(capsys, "--llm-mode", "bedrock", *argv)


def test_the_bedrock_demo_shows_designs_the_report_and_the_cost(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from scripts.usage_cost import compute, load_prices

    out = run_with_models(capsys, fake_designer)

    assert out[0][0].startswith("assess() library demo: wordpress, --llm-mode bedrock")
    assert "run through synthesis" in out[0][0]
    check_loading(out)
    assert out[3][0].startswith("label designed, assignment v")
    assert out[4][-1] == "moved by the model's consolidation review (capability_flag): 0"
    # The design block: one line per designed engine, each read from `designs`.
    assert out[6][0] == "label designed, designed engines: aurora_mysql, dynamodb, elasticache"
    designs = {line.split()[0]: line for line in out[6][1:]}
    assert list(designs) == ["aurora_mysql", "dynamodb", "elasticache"]
    assert all(" valid: " in line for line in designs.values())
    assert re.search(
        r"dynamodb +valid: \d+ tables?, 1 GSI; e\.g\. \w+: PK \w+", designs["dynamodb"]
    )
    assert "1 key; e.g. wp:options:{option_name}" in designs["elasticache"]
    assert out[7] == [
        "moved after a design flagged them (post_schema_reroute): 0",
        "left terminal (post_schema_terminal_count): 0",
    ]
    assert out[8][0] == "(read from artifacts.synthesis_report, an internal contract)"
    assert re.fullmatch(r"monthly cost of the kept engines: \$[\d,]+\.\d\d", out[8][1])
    assert out[8][2] == f"cost label: {COST_SCOPE_LABEL}"
    # Usage: one recorded call per design; its cost is usage_cost.py's.
    calls = 3
    assert out[9][0] == (
        f"{calls} model calls, {calls * 1000:,} input and {calls * 200:,} output tokens"
    )
    row = {"calls": calls, "inputTokens": calls * 1000, "outputTokens": calls * 200}
    usage = {"by_phase": {"schema_design": {PRICED_MODEL: row}}}
    expected = compute(usage, load_prices())
    assert out[9][1] == f"schema_design: ${expected['total_usd']:.2f}"
    assert out[9][-1] == f"total: ${expected['total_usd']:.2f}"
    check_json(out)


def test_the_bedrock_demo_shows_the_opensearch_design(capsys: pytest.CaptureFixture[str]) -> None:
    """In a scope that keeps OpenSearch, its stable design block entry is shown too."""
    out = run_with_models(
        capsys,
        fake_designer,
        "--run-through",
        "schema_design",
        "--target-databases",
        "dynamodb,opensearch",
    )
    assert out[6][0] == "label designed, designed engines: dynamodb, opensearch"
    designs = {line.split()[0]: line for line in out[6][1:]}
    assert list(designs) == ["dynamodb", "opensearch"]
    assert re.search(
        r"^opensearch +valid: \d+ (index|indexes), \d+ data streams?; e\.g\. \S+: \d+ fields?$",
        designs["opensearch"],
    ), designs["opensearch"]


def test_the_bedrock_demo_shows_a_post_schema_reroute(capsys: pytest.CaptureFixture[str]) -> None:
    designer = _FlaggingDesigner()
    out = run_with_models(capsys, designer, "--run-through", "schema_design")
    assert designer.flagged
    assert out[7][0] == "moved after a design flagged them (post_schema_reroute): 1"
    assert out[7][1] == f"e.g. {designer.flagged[:12]} now on aurora_mysql"
    assert out[7][2] == "left terminal (post_schema_terminal_count): 0"
    assert out[8] == ["the report (and its cost figures) comes from synthesis, which needs a model"]


def test_the_bedrock_demo_shows_a_terminal_statement_under_a_scope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    designer = _FlaggingDesigner()
    out = run_with_models(
        capsys, designer, "--run-through", "schema_design", "--target-databases", ",".join(SCOPE)
    )
    assert designer.flagged
    assert out[5][0] == "scope: dynamodb, elasticache"
    assert out[7][0] == "moved after a design flagged them (post_schema_reroute): 0"
    assert out[7][1] == "left terminal (post_schema_terminal_count): 1"
    assert out[7][2].startswith(
        f"e.g. {designer.flagged[:12]} stays on dynamodb (missing_feature, no_kept_engine): "
    )


def test_the_demo_is_linked_from_the_docs() -> None:
    assert "docs/examples/library" in (REPO / "docs" / "other-ways-to-run-it.md").read_text()
    assert "../examples/library/" in (REPO / "docs" / "contracts" / "assess.md").read_text()
    assert "demo_assess.py" in (DEMO.parent / "README.md").read_text()
