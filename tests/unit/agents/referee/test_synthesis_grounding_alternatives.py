"""An eliminated engine listed as an alternative of a recommend cue is recommended (#253).

The grounding rule looks for the nearest cue in the three words before an engine. In
"Keep in the relational database or OpenSearch" those words are "relational database
or", so the recommend cue "in" -- which governs both alternatives -- was missed and the
risk kept recommending OpenSearch after the reality check eliminated it. The cue window
now extends past a coordinated list ("X or", "X, Y, and") within the same clause, and a
mitigation loses just the eliminated alternative when that leaves a sound sentence.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_grounding import ground_risks, recommends_engine

# RISK-006 of the wordpress UI-mode run: an ElastiCache workaround written while
# OpenSearch was still a candidate.
RISK_006 = (
    "Keep in the relational database or OpenSearch; cache only the resulting action_id "
    "list keyed by a hash of the parameters."
)
ELIM = {"opensearch": "aurora_mysql"}


def _risk(mitigation: str, description: str = "[elasticache] unsupported pattern: Join.") -> dict:
    return {
        "risk_id": "RISK-006",
        "risk_type": "MIGRATION_COMPLEXITY",
        "severity": "MEDIUM",
        "description": description,
        "mitigation": mitigation,
        "query_ids": ["q1"],
    }


@pytest.mark.parametrize(
    "sentence",
    [
        RISK_006,
        "Keep in the relational database or in OpenSearch.",
        "Keep it in Aurora MySQL and OpenSearch.",
        "Serve it with Aurora MySQL, ElastiCache, or OpenSearch.",
        "Use DynamoDB or OpenSearch for the keyword search.",
        "Move these queries to the relational database or Amazon OpenSearch Service.",
        "Keep in OpenSearch or the relational database.",
    ],
)
def test_alternative_target_of_a_recommend_cue_is_recommended(sentence: str) -> None:
    assert recommends_engine(sentence, "opensearch")


@pytest.mark.parametrize(
    "sentence",
    [
        "Consolidating DynamoDB and OpenSearch into Aurora MySQL loses relevance ranking.",
        "Without Redis or OpenSearch, ranking uses MySQL FULLTEXT natural-language mode.",
        "Accept simpler ranking instead of Redis or OpenSearch scoring.",
        "Queries moved from DynamoDB and OpenSearch need a covering index.",
        "Compare to the latency between DynamoDB and OpenSearch before cutover.",
        "Store results in Aurora MySQL; DynamoDB and OpenSearch are not involved.",
        "Data in DynamoDB is denormalized and OpenSearch is not needed.",
    ],
)
def test_trade_off_or_unrelated_mention_is_not_a_recommendation(sentence: str) -> None:
    assert not recommends_engine(sentence, "opensearch")


class TestMitigationLosesOnlyTheEliminatedAlternative:
    def test_risk_006(self) -> None:
        out = ground_risks([_risk(RISK_006)], ELIM)[0]
        assert out["mitigation"] == (
            "Keep in the relational database; cache only the resulting action_id list keyed "
            "by a hash of the parameters."
        )
        assert "OpenSearch" in out["grounding_note"]

    @pytest.mark.parametrize(
        ("mitigation", "expected"),
        [
            (
                "Keep in the relational database or in OpenSearch.",
                "Keep in the relational database.",
            ),
            (
                "Serve it with Aurora MySQL, ElastiCache, or OpenSearch.",
                "Serve it with Aurora MySQL or ElastiCache.",
            ),
            (
                "Use DynamoDB or OpenSearch for the keyword search.",
                "Use DynamoDB for the keyword search.",
            ),
            (
                "Move these queries to Aurora MySQL or Amazon OpenSearch Service.",
                "Move these queries to Aurora MySQL.",
            ),
        ],
    )
    def test_near_variants(self, mitigation: str, expected: str) -> None:
        assert ground_risks([_risk(mitigation)], ELIM)[0]["mitigation"] == expected

    def test_engine_first_alternative_drops_the_sentence(self) -> None:
        out = ground_risks([_risk("Add an index. Keep in OpenSearch or the database.")], ELIM)
        assert out[0]["mitigation"] == "Add an index."

    def test_sole_target_still_drops_the_sentence(self) -> None:
        out = ground_risks([_risk("Add an index. Stream the rows to OpenSearch.")], ELIM)
        assert out[0]["mitigation"] == "Add an index."

    def test_trade_off_mitigation_is_untouched(self) -> None:
        risk = _risk("Accept simpler ranking instead of Redis or OpenSearch scoring.")
        assert ground_risks([risk], ELIM) == [risk]

    def test_description_keeps_the_sentence_and_gets_a_note(self) -> None:
        description = f"[elasticache] unsupported pattern: Join. {RISK_006}"
        out = ground_risks([_risk("Cache the id list.", description)], ELIM)[0]
        assert out["description"].startswith(description)
        assert out["description"].endswith(
            "(OpenSearch is not part of the target architecture; its queries run on "
            "Aurora MySQL.)"
        )


# Review of #253: "and"/", and" joining two clauses is not a list of alternatives.
_CLAUSE_KEEP_CASES = [
    ("Store the rows in Aurora MySQL and OpenSearch is no longer required.", "opensearch"),
    ("Store the data in Aurora MySQL and the OpenSearch domain is removed.", "opensearch"),
    ("Paginate in the application, and OpenSearch is out of scope.", "opensearch"),
    ("Keep the join in Aurora MySQL and avoid OpenSearch.", "opensearch"),
    (
        "Unbounded scan of wp_options in the relational database and ElastiCache is not a fit.",
        "elasticache",
    ),
    ("Previously the plan routed this to DynamoDB or OpenSearch.", "opensearch"),
    ("Keep the rows in Aurora MySQL; OpenSearch is not used.", "opensearch"),
    ("Index the rows in Aurora MySQL; OpenSearch was dropped.", "opensearch"),
]


@pytest.mark.parametrize(("sentence", "engine"), _CLAUSE_KEEP_CASES)
def test_clause_joined_mention_is_not_a_recommendation(sentence: str, engine: str) -> None:
    assert not recommends_engine(sentence, engine)


def test_cue_window_does_not_cross_a_semicolon() -> None:
    assert not recommends_engine("Keep the rows in Aurora; OpenSearch.", "opensearch")


@pytest.mark.parametrize(("sentence", "engine"), _CLAUSE_KEEP_CASES)
def test_clause_joined_mention_does_not_resolve_an_open_risk(sentence: str, engine: str) -> None:
    """``recommends_engine`` also decides anti-pattern resolution (#221): advice that only
    mentions the engine the queries moved to must not resolve the risk."""
    from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
    from src.agents.referee.synthesis_report import build_risk_assessment

    data = SynthesisData(job_id="j", database_name="db")
    data.engines["aurora_mysql"] = EngineArtifacts(
        "aurora_mysql",
        analysis={
            "workload_analysis": {
                "anti_patterns_detected": [
                    {
                        "anti_pattern_type": "hot-key",
                        "description": "Hot key reads.",
                        "recommendation": sentence,
                        "query_ids": ["q1"],
                        "table_ids": ["t1"],
                        "severity_weight": 0.9,
                    }
                ]
            }
        },
        schema_design={},
    )
    data.engines[engine] = EngineArtifacts(engine, analysis={}, schema_design={})
    data.assignment = {
        "query_assignments": [
            {"query_id": "q1", "assigned_engine": engine},
            {"query_id": "q2", "assigned_engine": "aurora_mysql"},
        ]
    }
    out = build_risk_assessment(data)
    assert out["resolved_risks"] == []
    risk = next(r for r in out["risks"] if r.get("reattributed_from") == "aurora_mysql")
    assert risk["severity"] == "HIGH"


@pytest.mark.parametrize(
    ("mitigation", "expected"),
    [
        (
            "Serve it with Aurora MySQL, ElastiCache or OpenSearch.",
            "Serve it with Aurora MySQL or ElastiCache.",
        ),
        (
            "Keep it in Aurora MySQL, ElastiCache and OpenSearch.",
            "Keep it in Aurora MySQL and ElastiCache.",
        ),
        (
            "For small tables, keep it in Aurora MySQL or OpenSearch.",
            "For small tables, keep it in Aurora MySQL.",
        ),
        (
            "For small tables, keep it in Aurora MySQL, ElastiCache, or OpenSearch.",
            "For small tables, keep it in Aurora MySQL or ElastiCache.",
        ),
    ],
)
def test_list_tail_grammar(mitigation: str, expected: str) -> None:
    assert ground_risks([_risk(mitigation)], ELIM)[0]["mitigation"] == expected


def test_as_complement_is_not_pruned() -> None:
    # "and OpenSearch as the search tier" is not a trailing alternative; drop the sentence.
    out = ground_risks(
        [_risk("Add an index. Use Aurora MySQL and OpenSearch as the search tier.")], ELIM
    )
    assert out[0]["mitigation"] == "Add an index."
