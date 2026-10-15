"""ADR-031 Decision 9 guard: Neptune and Keyspaces stay placeholders (#640).

A placeholder engine is never a routing candidate and routing output is unchanged by its
presence. Neptune and Keyspaces are *named* in several places (the engine enums, the
display names, triage's deferred list, a chart colour) so the UI, the contracts and the
reports can talk about them, but nothing may route a query to them, score them, analyze
them or design a schema for them.

Decision 9 is held by layers, each tested below:

1. ``src.shared.engine_names.PLACEHOLDER_ENGINES``: the resolver, the validator and the
   reality check drop or reject these engines even if an analysis output with a score for
   one exists or triage selects it.
2. Triage defers them (``DEFERRED``) and never dispatches an analysis agent for them.
3. The analysis and schema-design handlers write empty "not implemented" placeholders.
4. A scan of ``src/`` fails when a new module (or a new use in a pinned module) names one.

5. The engine catalog (``src/catalog/engines/*.toml``, #492) marks them ``status = "future"``,
   and ``PLACEHOLDER_ENGINES`` is derived from that, so there is one list, not two. Their
   display names and colours live in those files, which the ``.py`` scan here never reads.
6. Every pattern file (``src/catalog/patterns/*.toml``, #495) carries an explicit
   ``kind = "future"``, ``supported = false`` entry for each of them, so the decision is
   written down, and no constant built from the pattern catalog (penalties, capability
   sets, analysis catalogs) names either one. The comparison table labels them "future
   enhancement".

To make Neptune or Keyspaces a real engine: change ADR-031 Decision 9 (or supersede it)
first, then update this file in the same PR.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from src.agents.analysis.handler import run_analysis
from src.agents.referee import triage as triage_module
from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.assignment_validator import AssignmentValidator
from src.agents.referee.cache_overlay import CACHE_OVERLAY_ENGINES
from src.agents.referee.reality_check import run_reality_check
from src.agents.referee.triage import DEFERRED, TriageSignal, _select_agents
from src.agents.schema_design.handler import run_schema_design
from src.shared import catalog
from src.shared.catalog.patterns import (
    anti_pattern_penalties,
    capability_redesign_penalties,
    capability_signal_engines,
    patterns,
)
from src.shared.engine_names import PLACEHOLDER_ENGINES, display_engine
from src.storage.local_store import LocalArtifactStore

PLACEHOLDERS = frozenset({"neptune", "keyspaces"})
REPO_ROOT = Path(__file__).resolve().parents[4]
SRC = REPO_ROOT / "src"
BASELINES = REPO_ROOT / "tests" / "e2e" / "baselines"

# Every Python module under src/ that names a placeholder engine, with the exact number of
# string literals (case-insensitive) and enum names/attributes it may contain, and why. The
# counts are pinned, so a new ``"neptune"`` in triage.py or the resolver fails too, not just
# a new module. Changing a count means a reviewer looks at the new use.
#   path: (string literals, enum names or attribute accesses, reason)
NAMING_ALLOWLIST: dict[str, tuple[int, int, str]] = {
    "src/contracts/analysis_output.py": (2, 2, "TargetDatabase enum members"),
    "src/contracts/referee_output.py": (2, 2, "TargetEngine enum members"),
    "src/agents/entrypoint.py": (2, 0, "agent types the entrypoint accepts"),
    "src/agents/referee/triage.py": (
        8,
        0,
        "DEFERRED (2), the junction_tables / high_fk_density / self_referential_fk "
        "targets (3) and the write_heavy / time_series / time_range_filters targets (3)",
    ),
    "src/agents/referee/assignment_resolver.py": (
        1,
        0,
        "SIGNAL_ENGINE_OVERRIDES graph_traversal; filtered out by PLACEHOLDER_ENGINES",
    ),
    "src/agents/referee/reality_check_summary.py": (4, 0, "engine-name patterns for summary prose"),
}

_NAMES = {"neptune", "keyspaces"}

# The triage signals whose targets include a placeholder (checked against triage.py below).
PLACEHOLDER_SIGNALS: dict[str, list[str]] = {
    "junction_tables": ["neptune", "dynamodb"],
    "high_fk_density": ["neptune"],
    "self_referential_fk": ["neptune", "dynamodb"],
    "write_heavy": ["dynamodb", "keyspaces", "documentdb"],
    "time_series": ["dynamodb", "keyspaces", "documentdb", "opensearch"],
    "time_range_filters": ["dynamodb", "keyspaces", "opensearch", "documentdb"],
}


def _counts() -> dict[str, tuple[int, int]]:
    found: dict[str, tuple[int, int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        # Docstrings are prose, not routing data.
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        literals = names = 0
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.lower() in _NAMES
                and id(node) not in docstrings
            ):
                literals += 1
            elif isinstance(node, ast.Attribute) and node.attr.lower() in _NAMES:
                names += 1  # TargetDatabase.KEYSPACES.value and the like
            elif isinstance(node, ast.Name) and node.id.lower() in _NAMES:
                names += 1
        if literals or names:
            found[path.relative_to(REPO_ROOT).as_posix()] = (literals, names)
    return found


def _str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _str_list(node: ast.AST) -> list[str] | None:
    if not isinstance(node, ast.List):
        return None
    return [v for v in (_str(e) for e in node.elts) if v is not None]


def _triage_signals_targeting_a_placeholder() -> dict[str, list[str]]:
    """``{signal: targets}`` for every signal triage.py builds that targets a placeholder."""
    tree = ast.parse(Path(triage_module.__file__).read_text())
    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name: str | None = None
        targets: list[str] | None = None
        if len(node.args) > 1:  # _signal("name", [targets], ...)
            name, targets = _str(node.args[0]), _str_list(node.args[1])
        for kw in node.keywords:  # TriageSignal / _build_signal(signal=..., targets=[...])
            if kw.arg == "signal":
                name = _str(kw.value)
            if kw.arg == "targets":
                targets = _str_list(kw.value)
        if name and targets and set(targets) & PLACEHOLDERS:
            out[name] = targets
    return out


def _make_collector(query_ids):
    return {
        "job_id": "test",
        "database_schema": {"tables": [{"table_id": "db.users", "table_name": "users"}]},
        "queries": {
            "query_patterns": [
                {
                    "query_id": qid,
                    "query_text": "SELECT ...",
                    "query_type": "SELECT",
                    "tables_accessed": ["db.users"],
                    "join_count": 0,
                    "has_joins": False,
                    "has_aggregations": False,
                    "filter_tables": [],
                    "calls_per_second": 1.0,
                    "rows_returned_avg": 10,
                }
                for qid in query_ids
            ]
        },
    }


def _make_triage(engines, signals=None):
    return {"selected_agents": [{"agent_type": e} for e in engines], "signals": signals or []}


def _make_analysis(table_ids, confidence):
    return {
        "table_recommendations": [
            {"table_id": t, "confidence_score": confidence} for t in table_ids
        ],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }


EMPTY_PLACEHOLDER_ANALYSIS = {
    "table_recommendations": [],
    "workload_analysis": {"patterns_detected": []},
}


class TestPlaceholdersAreOnlyNamed:
    def test_the_set_is_the_two_engines(self):
        assert PLACEHOLDER_ENGINES == PLACEHOLDERS

    def test_the_set_is_the_catalogs_future_engines(self):
        future = {e.id: e for e in catalog.engines() if e.status == "future"}
        assert set(future) == PLACEHOLDERS
        assert PLACEHOLDER_ENGINES == catalog.engine_ids(status="future")
        # A future engine is in no active role set, so nothing that routes reads it.
        assert not PLACEHOLDERS & catalog.engine_ids(status=catalog.ACTIVE)

    def test_only_the_pinned_modules_name_a_placeholder_engine(self):
        found = _counts()
        unexpected = sorted(set(found) - set(NAMING_ALLOWLIST))
        assert not unexpected, (
            "These modules now name Neptune or Keyspaces (a literal, or an enum access such as "
            "TargetDatabase.KEYSPACES). If this makes either engine a routing candidate, change "
            "ADR-031 Decision 9 first; if it is only a label, add the module to NAMING_ALLOWLIST "
            f"with the reason: {unexpected}"
        )
        drift = {
            path: {"found": found.get(path), "pinned": pinned[:2]}
            for path, pinned in NAMING_ALLOWLIST.items()
            if found.get(path) != pinned[:2]
        }
        assert not drift, f"A pinned module gained or lost a use of a placeholder engine: {drift}"

    def test_the_triage_signals_that_target_a_placeholder_are_the_known_ones(self):
        assert _triage_signals_targeting_a_placeholder() == PLACEHOLDER_SIGNALS

    def test_every_pattern_file_writes_them_down_as_not_modelled(self):
        for entry in patterns():
            for engine in PLACEHOLDERS:
                support = entry.engines.get(engine)
                assert support is not None, f"{entry.id}.toml has no [engines.{engine}]"
                assert support.kind == "future" and not support.supported, (entry.id, engine)
                assert support.catalog is None and support.score is None, (entry.id, engine)

    def test_no_constant_built_from_the_pattern_catalog_names_them(self):
        engines_named = set().union(*capability_signal_engines().values())
        engines_named |= {e for m in capability_redesign_penalties().values() for e in m}
        assert not engines_named & PLACEHOLDERS
        assert not set(anti_pattern_penalties()) & PLACEHOLDERS

    def test_the_comparison_table_labels_them_future_enhancements(self):
        text = (REPO_ROOT / "docs" / "architecture" / "rule-catalog.md").read_text()
        for name in ("Neptune", "Keyspaces"):
            assert f"| {name} | `{name.lower()}` | future enhancement |" in text
            assert f" {name} (future) |" in text

    def test_they_are_not_cache_overlay_engines(self):
        assert not PLACEHOLDERS & set(CACHE_OVERLAY_ENGINES)

    def test_display_names_exist_so_reports_can_name_them(self):
        assert display_engine("neptune") == "Neptune"
        assert display_engine("keyspaces") == "Keyspaces"


class TestTriageNeverSelectsThem:
    def test_the_deferred_set_is_exactly_the_placeholders(self):
        assert DEFERRED == PLACEHOLDERS

    def test_no_dispatchable_analysis_agent_is_a_placeholder(self):
        assert not PLACEHOLDERS & set(triage_module.ANALYSIS_AGENTS)
        assert triage_module.BASELINE not in PLACEHOLDERS

    @pytest.mark.parametrize("signal", sorted(PLACEHOLDER_SIGNALS))
    def test_a_real_signal_targeting_a_placeholder_defers_it(self, signal):
        targets = PLACEHOLDER_SIGNALS[signal]
        result = _select_agents(
            [TriageSignal(signal=signal, targets=targets, evidence="e", query_ids=["q1"])],
            aurora_agents=["aurora_mysql"],
        )
        placeholders = set(targets) & PLACEHOLDERS
        assert placeholders <= set(result.deferred)
        assert not placeholders & set(result.selected)
        assert not placeholders & set(result.skipped)

    def test_all_the_real_signals_together_select_no_placeholder(self):
        signals = [
            TriageSignal(signal=name, targets=targets, evidence="e")
            for name, targets in PLACEHOLDER_SIGNALS.items()
        ]
        result = _select_agents(signals, aurora_agents=["aurora_postgresql"])
        assert not PLACEHOLDERS & set(result.selected)
        assert PLACEHOLDERS <= set(result.deferred)


def _resolve(selected, analysis, signals=None):
    collector = _make_collector(["q1", "q2"])
    return (
        AssignmentResolver().resolve(_make_triage(selected, signals), analysis, collector),
        collector,
    )


def _owners(assignment):
    return {qa.query_id: qa.assigned_engine for qa in assignment.query_assignments}


def _graph_signal():
    return [
        {
            "signal": "graph_traversal",
            "targets": ["neptune"],
            "query_ids": ["q1", "q2"],
            "evidence": "recursive joins",
        }
    ]


BASE_ANALYSIS = {
    "aurora_mysql": _make_analysis(["db.users"], 60),
    "dynamodb": _make_analysis(["db.users"], 50),
}


class TestNoRoutingEntryPointTreatsThemAsCandidates:
    @pytest.mark.parametrize("engine", sorted(PLACEHOLDERS))
    def test_an_unselected_scored_analysis_output_wins_nothing(self, engine):
        analysis = {**BASE_ANALYSIS, engine: _make_analysis(["db.users"], 95)}
        assignment, _ = _resolve(["aurora_mysql", "dynamodb"], analysis)
        assert not set(_owners(assignment).values()) & PLACEHOLDERS
        assert not re.search(r"neptune|keyspaces", assignment.model_dump_json(), re.IGNORECASE)

    @pytest.mark.parametrize("engine", sorted(PLACEHOLDERS))
    def test_a_scored_output_for_a_selected_placeholder_wins_nothing(self, engine):
        analysis = {**BASE_ANALYSIS, engine: _make_analysis(["db.users"], 95)}
        assignment, _ = _resolve(["aurora_mysql", "dynamodb", engine], analysis)
        assert not set(_owners(assignment).values()) & PLACEHOLDERS

    def test_triage_selecting_neptune_with_graph_traversal_does_not_pin_a_query(self):
        analysis = {**BASE_ANALYSIS, "neptune": EMPTY_PLACEHOLDER_ANALYSIS}
        assignment, _ = _resolve(
            ["aurora_mysql", "dynamodb", "neptune"], analysis, signals=_graph_signal()
        )
        assert not set(_owners(assignment).values()) & PLACEHOLDERS

    def test_routing_is_identical_with_and_without_the_placeholders(self):
        without, _ = _resolve(["aurora_mysql", "dynamodb"], BASE_ANALYSIS)
        with_them, _ = _resolve(
            ["aurora_mysql", "dynamodb", "neptune", "keyspaces"],
            {
                **BASE_ANALYSIS,
                "neptune": _make_analysis(["db.users"], 95),
                "keyspaces": _make_analysis(["db.users"], 95),
            },
            signals=_graph_signal(),
        )
        assert _owners(with_them) == _owners(without)

    @pytest.mark.parametrize("engine", sorted(PLACEHOLDERS))
    def test_the_validator_rejects_a_placeholder_owner_even_when_it_has_an_analysis(self, engine):
        analysis = {**BASE_ANALYSIS, engine: _make_analysis(["db.users"], 95)}
        assignment, collector = _resolve(["aurora_mysql", "dynamodb"], BASE_ANALYSIS)
        assignment.query_assignments[0].assigned_engine = engine
        result = AssignmentValidator().validate(assignment, collector, analysis)
        assert not result.valid
        assert any(engine in error and "placeholder" in error for error in result.errors)

    def test_the_reality_check_never_moves_a_query_onto_a_placeholder(self):
        analysis = {
            **BASE_ANALYSIS,
            "neptune": _make_analysis(["db.users"], 99),
            "keyspaces": _make_analysis(["db.users"], 99),
        }
        triage = _make_triage(["aurora_mysql", "dynamodb", "neptune", "keyspaces"])
        collector = _make_collector(["q1", "q2"])
        assignment = AssignmentResolver().resolve(
            _make_triage(["aurora_mysql", "dynamodb"]), BASE_ANALYSIS, collector
        )
        result = run_reality_check(
            assignment.model_dump(mode="json"), triage, analysis, collector, source_engine="mysql"
        )
        assert not re.search(r"neptune|keyspaces", json.dumps(result, default=str), re.IGNORECASE)


class TestNoAnalysisOrSchemaDesign:
    @pytest.fixture
    def store(self, tmp_path):
        store = LocalArtifactStore(str(tmp_path))
        store.write_json(
            "mydb/job-001/collector/output.json",
            {
                "contract_version": "3.0",
                "database_schema": {
                    "tables": [
                        {
                            "table_id": "mydb.users",
                            "table_name": "users",
                            "row_count": 100,
                            "columns": [
                                {"column_name": "id", "data_type": "int", "nullable": False}
                            ],
                        }
                    ]
                },
                "queries": {"query_patterns": []},
            },
        )
        return store

    @pytest.mark.parametrize("engine", sorted(PLACEHOLDERS))
    def test_analysis_is_empty(self, store, engine):
        run_analysis("job-001", "mydb", engine, store)
        out = store.read_json(f"mydb/job-001/analysis-{engine}/analysis.json")
        assert out["table_recommendations"] == []
        assert out["workload_analysis"]["patterns_detected"] == []
        assert out["cost_estimate"]["monthly_cost_usd"] == 0

    @pytest.mark.parametrize("engine", sorted(PLACEHOLDERS))
    def test_schema_design_is_not_implemented(self, store, engine):
        store.write_json(
            f"mydb/job-001/analysis-{engine}/analysis.json", {"contract_version": "2.1"}
        )
        run_schema_design("job-001", "mydb", engine, store)
        out = store.read_json(f"mydb/job-001/schema-{engine}/schema_output.json")
        assert out["status"] == "not_implemented"
        assert set(out) <= {"target_type", "status", "timestamp"}


class TestCommittedRoutingBaselinesNameNoPlaceholder:
    """The committed per-query snapshot and totals cover every bundled sample."""

    @pytest.mark.parametrize("name", ["routing_snapshot.json", "routing_baseline.json"])
    def test_baseline(self, name):
        text = (BASELINES / name).read_text()
        assert not re.search(r"neptune|keyspaces", text, re.IGNORECASE)
        # The files are real data, not an empty stub that would pass vacuously.
        assert json.loads(text)
