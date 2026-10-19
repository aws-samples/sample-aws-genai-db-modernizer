"""One definition of each fact the #493 catalog review found twice, and no dead copies.

``TargetDatabase`` existed in both analysis contracts with different
membership and member names; a Redis analysis agent shipped that no pipeline
path called; and a table of "lightweight alternatives" (with its own
"< 5 queries" threshold and a saga-only DynamoDB transaction text) had no
caller left. These tests keep each one defined once, or gone.
"""

from __future__ import annotations

import importlib.util

from src.agents.referee import capability_registry
from src.contracts import analysis_input, analysis_output
from src.shared.engine_names import ENGINE_DISPLAY_NAMES


def test_analysis_input_and_output_share_one_target_database_enum() -> None:
    assert analysis_input.TargetDatabase is analysis_output.TargetDatabase


def test_every_catalog_engine_is_a_target_database_value() -> None:
    assert set(ENGINE_DISPLAY_NAMES) <= {t.value for t in analysis_output.TargetDatabase}


def test_the_unreachable_redis_analysis_agent_is_gone() -> None:
    """ElastiCache analysis runs through ``elasticache_analysis_agent``; no second entry point."""
    assert importlib.util.find_spec("src.agents.analysis.redis_analysis_agent") is None


def test_the_uncalled_lightweight_alternatives_table_is_gone() -> None:
    assert not hasattr(capability_registry, "LIGHTWEIGHT_ALTERNATIVES")
    assert not hasattr(capability_registry, "suggest_lightweight_alternative")


def _callers(name: str) -> set[str]:
    """The ``src`` modules that call ``name`` (by AST, so a docstring never counts)."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    found = set()
    for path in (root / "src").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                called = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if called == name:
                    found.add(path.relative_to(root).as_posix())
    return found


def test_group_by_query_pattern_is_the_only_statement_grouping() -> None:
    """#1146: one grouping of queries into query patterns. Only
    ``src.tools.sql.query_pattern_groups`` reads a whole statement's signature; every
    other reader groups through it, and routing's key anchors read the scope signature
    only (ADR-031 Amendment 11)."""
    assert _callers("query_signature") == {"src/tools/sql/query_pattern_groups.py"}
    assert _callers("scope_signature") <= {
        "src/tools/sql/access_signature.py",
        "src/agents/referee/access_pattern.py",
    }
    assert _callers("group_by_query_pattern") <= {
        "src/tools/sql/query_pattern_groups.py",
        "src/agents/referee/synthesis_handler.py",
        "src/agents/schema_design/group_splitter.py",
    }
