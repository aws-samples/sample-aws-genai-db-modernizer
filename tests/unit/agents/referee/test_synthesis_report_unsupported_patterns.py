"""build_risk_assessment must read whichever unsupported-pattern fields the
engine's schema-design contract actually carries (#210).

Before this fix, the unsupported-patterns loop in
``src.agents.referee.synthesis_report.build_risk_assessment`` read only
``pattern_type`` and ``recommendation`` -- fields only the dynamodb contract
has. DocumentDB and ElastiCache (``reason``/``workaround`` instead) produced
risks with the literal text ``"[elasticache] unknown: "`` and
``mitigation: None``. A separate, dedicated test module from
``test_synthesis_llm_seam.py`` so this doesn't collide with concurrent work
on that shared file.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_risk_assessment

# The real ElastiCache unsupported_patterns from the wordpress e2e evidence run
# that surfaced #210 (schema_designs.elasticache.unsupported_patterns in
# tests/unit/report/fixtures/wordpress_report.json).
_ELASTICACHE_UNSUPPORTED = [
    {
        "source_query_ids": ["59163c184972d1ec4ad95106a5fc20c95d19dd06d071f956bf50e0c51ce12bdf"],
        "reason": (
            "LEFT JOIN with multiple LIKE predicates on hook, args and extended_args "
            "combined with claim_id and status filters cannot be expressed as key "
            "lookups; Redis has no substring search."
        ),
        "workaround": "Keep this query against MySQL; do not migrate it to ElastiCache.",
    },
    {
        "source_query_ids": ["797c033f24c616611d9d000bcef94d12e698d48aac1ea09e84c71805d1d47935"],
        "reason": (
            "JOIN of wp_users and wp_usermeta with LIKE on meta_value (capabilities) "
            "ordered by display_name is an ad-hoc pattern match not supported by Redis."
        ),
        "workaround": "Maintain a role->user_ids set per role, updated on capability change.",
    },
    {
        "source_query_ids": [
            "b065d0f32c1e6491039cf7a3ed2b48bed98783293749e636139aa1883fe6eb11",
            "3a5af258b286c55a86defe9dc509fee3e199c02019eb8faa10ce2d072ee1427a",
        ],
        "reason": (
            "SHOW FULL FIELDS and SET SESSION are relational metadata and "
            "session-configuration statements with no Redis equivalent."
        ),
        "workaround": "Keep these as MySQL-only administrative statements.",
    },
]

_DYNAMODB_UNSUPPORTED = [
    {
        "query_ids": ["254f282ce4da5837c67df5f5d405e1ad3c405cee2c14420378bd8dfd0d86787"],
        "pattern_type": "aggregation",
        "recommendation": "COUNT(*) on wp_postmeta: run a Query with Select=COUNT.",
    }
]


def _data(unsupported_by_engine: dict[str, list[dict]]) -> SynthesisData:
    data = SynthesisData(job_id="job-1", database_name="wordpress")
    data.triage = {"selected_agents": [{"agent_type": e} for e in unsupported_by_engine]}
    for engine, patterns in unsupported_by_engine.items():
        data.engines[engine] = EngineArtifacts(
            engine=engine,
            analysis={},
            schema_design={"unsupported_patterns": patterns},
        )
    return data


class TestUnsupportedPatternRisksCarryRealText:
    def test_elasticache_risks_have_the_reason_as_description(self) -> None:
        out = build_risk_assessment(_data({"elasticache": _ELASTICACHE_UNSUPPORTED}))
        assert len(out["risks"]) == 3
        for risk in out["risks"]:
            assert "unknown:" not in risk["description"].lower()
            assert risk["description"].strip() != "[elasticache]"

        descriptions = " ".join(r["description"] for r in out["risks"])
        assert "LEFT JOIN" in descriptions
        assert "ad-hoc pattern match" in descriptions
        assert "SHOW FULL FIELDS" in descriptions

    def test_elasticache_risks_carry_a_mitigation(self) -> None:
        out = build_risk_assessment(_data({"elasticache": _ELASTICACHE_UNSUPPORTED}))
        for risk in out["risks"]:
            assert risk["mitigation"]
        mitigations = " ".join(r["mitigation"] for r in out["risks"])
        assert "Keep this query against MySQL" in mitigations

    def test_dynamodb_risk_still_uses_pattern_type_and_recommendation(self) -> None:
        """The one shape that was never broken keeps working unchanged."""
        out = build_risk_assessment(_data({"dynamodb": _DYNAMODB_UNSUPPORTED}))
        assert len(out["risks"]) == 1
        risk = out["risks"][0]
        assert "aggregation" in risk["description"]
        assert "COUNT(*) on wp_postmeta" in risk["description"]
        assert risk["mitigation"] == "COUNT(*) on wp_postmeta: run a Query with Select=COUNT."

    def test_mixed_engines_all_twelve_risks_have_real_text(self) -> None:
        """The full evidence shape: dynamodb (5) + elasticache (3) unsupported
        patterns. All eight resulting risks must carry real explanatory text."""
        dynamodb_five = _DYNAMODB_UNSUPPORTED * 5
        out = build_risk_assessment(
            _data({"dynamodb": dynamodb_five, "elasticache": _ELASTICACHE_UNSUPPORTED})
        )
        assert len(out["risks"]) == 8
        for risk in out["risks"]:
            _, _, body = risk["description"].partition("] ")
            assert body.strip(), f"risk {risk['risk_id']} has no body: {risk['description']!r}"
