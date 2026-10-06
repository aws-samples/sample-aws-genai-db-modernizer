"""Dedup-only GROUP BY is not a blocking aggregation risk (#336).

DynamoDB schema design's own ``unsupported_patterns`` sometimes flags a
``GROUP BY`` used only to de-duplicate rows (no aggregate function, no
HAVING) as ``pattern_type: "aggregation"``, even though its own
``recommendation`` text says to drop the ``GROUP BY`` and serve the query
with a plain ``Query`` -- a trivial, fully-servable fix, not a real blocking
aggregation. Before this fix, ``build_risk_assessment`` counted every one of
these the same as a genuine multi-table ``SUM``/``GROUP BY`` DynamoDB truly
cannot serve, inflating ``RISK-001``'s "N remaining" count and keeping the
case for Aurora artificially strong.

Fixture mirrors the real wordpress e2e evidence that surfaced #336 (job
3f327329): five ``wp_posts`` queries whose ``GROUP BY wp_posts.ID`` only
collapses duplicate rows from an upstream join, plus one genuine
``SUM``-with-join aggregation (query id
``a355b7403b1d39d90f3ff59ece2a00e0225bbbc7c3a9d3d755d3f0e8d001e077``) that
DynamoDB truly cannot serve and must stay an open risk.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_risk_assessment

# Real query_text from the wordpress e2e evidence (job 3f327329) for the five
# dedup-only GROUP BY queries #336 is about.
_DEDUP_QUERIES = {
    "7b62184001103ac9e6f0c4677064c9cbde2fd4c70e17246b09808d5d98b2dc06": (  # pragma: allowlist secret
        "SELECT `wp_posts` . * FROM `wp_posts` WHERE ? = ? AND `wp_posts` . "
        "`post_name` IN (...) AND ( ? = ? ) AND ( ( `wp_posts` . `post_type` = ? "
        "AND ( `wp_posts` . `post_status` = ? ) ) ) GROUP BY `wp_posts` . `ID` "
        "ORDER BY `wp_posts` . `post_date` DESC"
    ),
    "c186dff756b764e27eee24482997ec9f6bbcae891097580079dacad7b9c3ac30": (  # pragma: allowlist secret
        "SELECT `wp_posts` . `ID` FROM `wp_posts` WHERE ? = ? AND `wp_posts` . "
        "`post_name` IN (...) AND ( ? = ? ) AND `wp_posts` . `post_type` = ? AND "
        "( ( `wp_posts` . `post_status` = ? ) ) GROUP BY `wp_posts` . `ID` ORDER BY "
        "`wp_posts` . `post_date` DESC LIMIT ?, ..."
    ),
    "c674aa12b3665c30c5aefb1c6f96f35b8d71ffe95f6a4d2769632d53db421347": (  # pragma: allowlist secret
        "SELECT `wp_posts` . * FROM `wp_posts` WHERE ? = ? AND `wp_posts` . "
        "`post_name` IN (...) AND ( ? = ? ) AND ( ( `wp_posts` . `post_type` = ? "
        "AND ( `wp_posts` . `post_status` = ? OR `wp_posts` . `post_status` = ? ) ) ) "
        "GROUP BY `wp_posts` . `ID` ORDER BY `wp_posts` . `post_date` DESC"
    ),
    "3d3b308b385cfc54b939406d23a85a0df689c48c05b5aa3ae901d382b5055d00": (  # pragma: allowlist secret
        "SELECT `wp_posts` . `ID` FROM `wp_posts` WHERE ? = ? AND ( ? = ? ) AND "
        "`wp_posts` . `post_type` = ? AND ( ( `wp_posts` . `post_status` = ? ) ) "
        "GROUP BY `wp_posts` . `ID` ORDER BY `wp_posts` . `post_date` DESC LIMIT ?, ..."
    ),
    "620879e270f6234f50eb8c2188653359be6486f86612d1678214adbf786102d8": (  # pragma: allowlist secret
        "SELECT `wp_posts` . `ID` FROM `wp_posts` WHERE ? = ? AND `wp_posts` . "
        "`post_name` IN (...) AND ( ? = ? ) AND `wp_posts` . `post_type` = ? AND "
        "( ( `wp_posts` . `post_status` = ? OR `wp_posts` . `post_status` = ? OR "
        "`wp_posts` . `post_status` = ? OR `wp_posts` . `post_status` = ? ) ) "
        "GROUP BY `wp_posts` . `ID`"
    ),
}

# Real query_text (same job) for the one genuine SUM/join aggregation DynamoDB
# truly cannot serve.
_REAL_AGGREGATION_QUERY_ID = "a355b7403b1d39d90f3ff59ece2a00e0225bbbc7c3a9d3d755d3f0e8d001e077"
_REAL_AGGREGATION_SQL = (
    "SELECT `order_items` . `order_item_name` AS `order_item_name` , "
    "SUM ( `order_item_meta_discount_amount` . `meta_value` ) AS `discount_amount` , "
    "`posts` . `post_date` AS `post_date` FROM `wp_posts` AS `posts` "
    "INNER JOIN `wp_woocommerce_order_items` AS `order_items` "
    "ON `posts` . `ID` = `order_items` . `order_id` "
    "GROUP BY `order_items` . `order_item_id`"
)
# A second genuine multi-table aggregation (synthetic, same shape as the issue's
# second "genuinely unservable" query) so the fixture has the issue's full 7.
_SECOND_REAL_AGGREGATION_QUERY_ID = (
    "d99618ea3724cae0b2ef0dda96a021dda2c50c6b3ba3847b9ccf307ad044ec6"  # pragma: allowlist secret
)
_SECOND_REAL_AGGREGATION_SQL = (
    "SELECT `wp_postmeta` . `post_id` , COUNT ( * ) AS `cnt` FROM `wp_postmeta` "
    "INNER JOIN `wp_posts` ON `wp_posts` . `ID` = `wp_postmeta` . `post_id` "
    "GROUP BY `wp_postmeta` . `post_id` HAVING COUNT ( * ) > ?"
)

_ALL_SEVEN_IDS = [
    *_DEDUP_QUERIES,
    _REAL_AGGREGATION_QUERY_ID,
    _SECOND_REAL_AGGREGATION_QUERY_ID,
]


def _data() -> SynthesisData:
    queries = [
        {"query_id": qid, "query_text": sql, "tables_accessed": ["wordpress.wp_posts"]}
        for qid, sql in _DEDUP_QUERIES.items()
    ] + [
        {
            "query_id": _REAL_AGGREGATION_QUERY_ID,
            "query_text": _REAL_AGGREGATION_SQL,
            "tables_accessed": [
                "wordpress.wp_posts",
                "wordpress.wp_woocommerce_order_items",
            ],
        },
        {
            "query_id": _SECOND_REAL_AGGREGATION_QUERY_ID,
            "query_text": _SECOND_REAL_AGGREGATION_SQL,
            "tables_accessed": ["wordpress.wp_postmeta", "wordpress.wp_posts"],
        },
    ]
    dynamodb_analysis = {
        "workload_analysis": {
            "anti_patterns_detected": [
                {
                    "anti_pattern_type": "complex-aggregation",
                    "description": (
                        "Complex GROUP BY / HAVING / multi-table aggregations. DynamoDB "
                        "doesn't support server-side aggregation — these need to move "
                        "to application layer, pre-computed aggregates, or a separate "
                        "analytics store."
                    ),
                    "query_ids": _ALL_SEVEN_IDS,
                    "table_ids": ["wordpress.wp_posts", "wordpress.wp_woocommerce_order_items"],
                    "severity_weight": 0.8,
                    "recommendation": (
                        "Pre-compute aggregates on write (DynamoDB Streams + Lambda), or "
                        "export to S3/Athena for analytics."
                    ),
                }
            ]
        }
    }
    dynamodb_schema = {
        "table_definitions": [{"table_name": "Posts", "source_tables": ["wordpress.wp_posts"]}],
        "access_patterns": [],
        "unsupported_patterns": [
            {
                "query_ids": list(_DEDUP_QUERIES)[0:3],
                "pattern_type": "aggregation",
                "recommendation": (
                    "GROUP BY wp_posts.ID here is WordPress's defensive de-duplication "
                    "guard, not a true aggregation (no join present). Drop the GROUP BY "
                    "in application code and serve with a Query against Posts by "
                    "post_name/post_type/post_status."
                ),
            },
            {
                "query_ids": list(_DEDUP_QUERIES)[3:5],
                "pattern_type": "aggregation",
                "recommendation": (
                    "GROUP BY wp_posts.ID de-duplicates rows produced by an upstream "
                    "join; a DynamoDB Query against PostsByTypeStatusDate already "
                    "returns unique items per key, so these must be served as a plain "
                    "Query without GROUP BY (any residual de-duplication performed "
                    "client-side)."
                ),
            },
            {
                "query_ids": [_REAL_AGGREGATION_QUERY_ID, _SECOND_REAL_AGGREGATION_QUERY_ID],
                "pattern_type": "aggregation",
                "recommendation": (
                    "Multi-table SUM/COUNT with GROUP BY and HAVING: DynamoDB cannot "
                    "compute this server-side. Pre-compute on write or move to an "
                    "analytics store."
                ),
            },
        ],
    }
    return SynthesisData(
        job_id="j",
        database_name="wordpress",
        collector={"queries": {"query_patterns": queries}},
        engines={
            "dynamodb": EngineArtifacts(
                "dynamodb", analysis=dynamodb_analysis, schema_design=dynamodb_schema
            ),
        },
        assignment={
            "query_assignments": [
                {"query_id": qid, "assigned_engine": "dynamodb", "in_scope": True}
                for qid in _ALL_SEVEN_IDS
            ]
        },
    )


class TestDedupOnlyGroupByIsALowRiskNotAResolvedOne:
    """Review finding 4: dedup-only GROUP BY entries are not "resolved" -- no
    in-scope access pattern actually serves these queries yet. They stay an
    open risk, downgraded to LOW and worded as the application-code fix they
    are (the issue's own alternative), and are never added to ``covered_by``,
    so the anti-pattern's own "N remaining" count is unaffected."""

    def test_anti_pattern_remaining_count_is_unaffected(self) -> None:
        """The dedup fix changes severity/wording of the per-pattern risk, not
        whether the anti-pattern coverage math treats these queries as served."""
        result = build_risk_assessment(_data())
        risk = next(
            r
            for r in result["risks"]
            if "doesn't support server-side aggregation" in r["description"]
        )
        assert "(0% of queries resolved by schema design, 7 remaining)" in risk["description"]

    def test_dedup_only_unsupported_pattern_entries_are_low_not_medium(self) -> None:
        result = build_risk_assessment(_data())
        aggregation_risks = [
            r
            for r in result["risks"]
            if r["risk_type"] == "MIGRATION_COMPLEXITY" and "aggregation" in r["description"]
        ]
        severities = {r["severity"] for r in aggregation_risks}
        assert severities == {"LOW", "MEDIUM"}
        low_ids = {q for r in aggregation_risks if r["severity"] == "LOW" for q in r["query_ids"]}
        medium_ids = {
            q for r in aggregation_risks if r["severity"] == "MEDIUM" for q in r["query_ids"]
        }
        assert low_ids == set(_DEDUP_QUERIES)
        assert medium_ids == {_REAL_AGGREGATION_QUERY_ID, _SECOND_REAL_AGGREGATION_QUERY_ID}

    def test_dedup_only_risk_is_worded_as_an_application_change(self) -> None:
        result = build_risk_assessment(_data())
        risk = next(r for r in result["risks"] if r["severity"] == "LOW")
        assert risk["mitigation"] == (
            "Needs an application change: drop the GROUP BY and serve it as a Query."
        )

    def test_dedup_only_entries_are_never_resolved(self) -> None:
        result = build_risk_assessment(_data())
        dedup_ids = set(_DEDUP_QUERIES)
        assert not [r for r in result["resolved_risks"] if set(r["query_ids"]) & dedup_ids]

    def test_genuine_aggregation_stays_medium_and_is_never_resolved(self) -> None:
        result = build_risk_assessment(_data())
        for r in result["resolved_risks"]:
            assert _REAL_AGGREGATION_QUERY_ID not in r["query_ids"]
            assert _SECOND_REAL_AGGREGATION_QUERY_ID not in r["query_ids"]


class TestDedupDowngradeIsGatedOnTheEntrysOwnCategory:
    """Review finding 3: a non-aggregation unsupported_patterns entry is never
    downgraded just because its SQL happens to also have a dedup-only GROUP
    BY -- the entry's own pattern_type/reason has to say aggregation."""

    def test_a_join_entry_with_a_dedup_only_group_by_query_stays_medium(self) -> None:
        data = _data()
        dedup_id = next(iter(_DEDUP_QUERIES))
        dynamodb_schema = data.engines["dynamodb"].schema_design
        dynamodb_schema["unsupported_patterns"].append(
            {
                "query_ids": [dedup_id],
                "pattern_type": "text_search",
                "recommendation": "Needs OpenSearch for multi-table joins, not aggregation.",
            }
        )
        result = build_risk_assessment(data)
        join_risk = next(r for r in result["risks"] if r["query_ids"] == [dedup_id])
        assert join_risk["severity"] == "MEDIUM"
        assert join_risk["mitigation"] == "Needs OpenSearch for multi-table joins, not aggregation."

    def test_elasticache_like_pattern_entry_with_a_dedup_only_query_stays_medium(self) -> None:
        data = _data()
        dedup_id = next(iter(_DEDUP_QUERIES))
        data.engines["elasticache"] = EngineArtifacts(
            "elasticache",
            analysis={},
            schema_design={
                "unsupported_patterns": [
                    {
                        "source_query_ids": [dedup_id],
                        "reason": "LIKE pattern matching cannot be a key lookup.",
                        "workaround": "Keep this query on the relational engine.",
                    }
                ]
            },
        )
        data.assignment["query_assignments"].append(
            {"query_id": dedup_id, "assigned_engine": "elasticache", "in_scope": True}
        )
        result = build_risk_assessment(data)
        like_risk = next(r for r in result["risks"] if "LIKE pattern matching" in r["description"])
        assert like_risk["severity"] == "MEDIUM"
