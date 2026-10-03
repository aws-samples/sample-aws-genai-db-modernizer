"""Architectural patterns follow the final assignment (#202 review, root cause A).

The reality check detected its patterns before the LLM corrections, the re-run Aurora
absorption, the sanity sweep and the customer-override restore, so the written output
could recommend a pattern around an engine that ended with no in-scope query (the
wordpress run: CQRS/Materialized View/Polyglot around OpenSearch after OpenSearch was
absorbed into Aurora MySQL). Both the reality check and synthesis now recompute them.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from src.agents.referee.reality_check import refresh_patterns_and_recommendations
from src.agents.referee.reality_check_handler import (
    apply_reality_check_llm_output,
    write_reality_check_result,
)
from src.agents.referee.synthesis_grounding import recompute_reality_check_patterns
from src.storage.local_store import LocalArtifactStore


def _qas(**counts: int) -> list[dict]:
    return [
        {"query_id": f"{engine}-{i}", "assigned_engine": engine, "in_scope": True}
        for engine, n in counts.items()
        for i in range(n)
    ]


# The wordpress evidence: stale patterns around OpenSearch, final assignment
# dynamodb 63 / elasticache 34 / aurora_mysql 10 (OpenSearch absorbed into Aurora MySQL).
_STALE_PATTERNS = [
    {
        "name": "Command Query Responsibility Segregation (CQRS)",
        "description": "Separate writes from reads.",
        "applies_to": {"write_engine": "dynamodb", "read_engines": ["elasticache", "opensearch"]},
    },
    {
        "name": "Materialized View Pattern",
        "description": "Read-only projection.",
        "applies_to": {"source_engine": "dynamodb", "view_engine": "opensearch"},
    },
    {
        "name": "Polyglot Persistence",
        "description": "Per bounded context.",
        "applies_to": {"engines": ["dynamodb", "elasticache", "opensearch"]},
    },
]
_CONSOLIDATION_LINE = (
    "Consolidated 3 queries from opensearch → aurora_mysql: Aurora absorption. Saves ~$450/mo."
)
_EVIDENCE_SUMMARY = {
    "consolidations": [
        {"from_engine": "opensearch", "to_engine": "aurora_mysql", "query_count": 3}
    ],
    "architectural_patterns": _STALE_PATTERNS,
    "recommendations": [
        _CONSOLIDATION_LINE,
        "Recommended pattern: Command Query Responsibility Segregation (CQRS). Use dynamodb "
        "for all write operations and elasticache, opensearch for specialized reads.",
        "Recommended pattern: Materialized View Pattern. dynamodb is the source of truth; "
        "opensearch maintains a search-optimized projection.",
    ],
}
_EVIDENCE_ASSIGNMENT = _qas(dynamodb=63, elasticache=34, aurora_mysql=10)


def _engines_in(patterns: list[dict]) -> set[str]:
    out: set[str] = set()
    for p in patterns:
        for v in p["applies_to"].values():
            out.update(v if isinstance(v, list) else [v])
    return out


class TestSynthesisRecompute:
    def test_evidence_shape_lists_active_engines_only(self) -> None:
        out = recompute_reality_check_patterns(_EVIDENCE_SUMMARY, _EVIDENCE_ASSIGNMENT)
        by_name = {p["name"]: p["applies_to"] for p in out["architectural_patterns"]}
        assert set(by_name["Polyglot Persistence"]["engines"]) == {
            "dynamodb",
            "elasticache",
            "aurora_mysql",
        }
        assert by_name["Command Query Responsibility Segregation (CQRS)"] == {
            "write_engine": "dynamodb",
            "read_engines": ["elasticache"],
        }
        assert "Materialized View Pattern" not in by_name
        assert "opensearch" not in _engines_in(out["architectural_patterns"])

    def test_recommendation_lines_regenerated_history_kept(self) -> None:
        out = recompute_reality_check_patterns(_EVIDENCE_SUMMARY, _EVIDENCE_ASSIGNMENT)
        assert out["recommendations"][0] == _CONSOLIDATION_LINE
        pattern_lines = [r for r in out["recommendations"] if r.startswith("Recommended pattern")]
        assert pattern_lines and not any("opensearch" in r for r in pattern_lines)

    def test_no_pattern_targets_its_own_source(self) -> None:
        """Event-Driven Sync used to list its source among its targets after patching."""
        out = recompute_reality_check_patterns(
            {"architectural_patterns": [], "recommendations": []},
            _qas(aurora_mysql=10, elasticache=4),
        )
        for p in out["architectural_patterns"]:
            applies = p["applies_to"]
            src = applies.get("source_engine") or applies.get("write_engine")
            targets = applies.get("target_engines") or applies.get("read_engines") or []
            assert src not in targets, p

    def test_out_of_scope_queries_do_not_make_an_engine_active(self) -> None:
        qas = _qas(dynamodb=5, elasticache=2) + [
            {"query_id": "os-1", "assigned_engine": "opensearch", "in_scope": False}
        ]
        out = recompute_reality_check_patterns(_EVIDENCE_SUMMARY, qas)
        assert "opensearch" not in _engines_in(out["architectural_patterns"])

    def test_no_assignment_leaves_summary_alone(self) -> None:
        assert recompute_reality_check_patterns(_EVIDENCE_SUMMARY, []) is _EVIDENCE_SUMMARY


def _rc_result(revised: list[dict]) -> dict:
    return {
        "revised_assignments": revised,
        "consolidations": [
            {
                "from_engine": "opensearch",
                "to_engine": "aurora_mysql",
                "query_count": 3,
                "reason": "Aurora absorption",
                "saved_cost_estimate": 450,
                "action": "full",
                "queries_retained": [],
                "retention_reason": None,
            }
        ],
        "architectural_patterns": json.loads(json.dumps(_STALE_PATTERNS)),
        "recommendations": ["stale"],
        "unique_value_assessment": {},
        "executive_summary": None,
        "before_distribution": {"dynamodb": 5, "elasticache": 2, "opensearch": 3},
        "after_distribution": {},
        "lightweight_recommendations": [],
        "assignment": {"query_assignments": revised},
    }


class TestRealityCheckRefresh:
    def test_refresh_uses_the_final_assignment(self) -> None:
        result = _rc_result(_qas(dynamodb=5, elasticache=2, aurora_mysql=3))
        refresh_patterns_and_recommendations(result)
        assert "opensearch" not in _engines_in(result["architectural_patterns"])
        assert result["recommendations"][0].startswith("Consolidated 3 queries from opensearch")

    def test_llm_corrections_recompute_patterns(self) -> None:
        result = _rc_result(_qas(dynamodb=5, elasticache=2, aurora_mysql=3))
        result["after_distribution"] = {"dynamodb": 5, "elasticache": 2, "aurora_mysql": 3}
        corrected = _qas(dynamodb=5, elasticache=2, aurora_mysql=3)
        with patch(
            "src.agents.referee.reality_check_handler.apply_corrections",
            return_value=(corrected, result["consolidations"]),
        ):
            apply_reality_check_llm_output(result, {"consolidation_corrections": [{"q": 1}]})
        assert "opensearch" not in _engines_in(result["architectural_patterns"])
        assert not any("opensearch for specialized reads" in r for r in result["recommendations"])

    def test_written_output_matches_the_assignment_it_ships_with(self, tmp_path) -> None:
        """The sweep/override restore run after detection; the write recomputes."""
        store = LocalArtifactStore(base_dir=str(tmp_path))
        result = _rc_result(_qas(dynamodb=5, elasticache=2, aurora_mysql=3))
        write_reality_check_result(store, "job-1", "mydb", result, 1, write_revision=False)
        out = store.read_json("mydb/job-1/reality-check/output.json")
        assert "opensearch" not in _engines_in(out["architectural_patterns"])
        assert not any(
            r.startswith("Recommended pattern") and "opensearch" in r
            for r in out["recommendations"]
        )
