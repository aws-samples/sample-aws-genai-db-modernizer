"""A query reality check moves onto an engine that cannot serve it must be
reported honestly, without overstating or duplicating the gap (#335).

Reality check can redirect a query away from a specialist engine (e.g. off
OpenSearch for lacking cross-index joins) onto another committed engine that
is itself incapable of serving it (e.g. DynamoDB, which has no server-side
multi-table join or aggregation support either). The root cause is a routing
gap -- ``src/agents/referee/capability_registry.py``'s hard-capability check
has no "complex_joins"/"aggregation" detector, so the reality check's
serviceability gate (``src/agents/referee/reality_check.py``) never sees that
requirement and lets the move through silently. #338
(``feat/engines-earn-their-place``) fixes that at the source; this module does
not decide routing, so what remains here is a narrow invariant guard, not the
fix: in case a gap like #335's slips through again, raise the severity of
whatever risk already names the query to HIGH and advise keeping it on the
source-compatible Aurora engine (which always runs the source SQL as-is)
rather than stating it already is there -- the assignment still names the
engine it was moved to, not Aurora -- instead of describing the gap as a
routine, maybe-just-not-designed-yet coverage gap, and without ever adding a
second, parallel risk for the same id.

Fixture mirrors the real wordpress e2e evidence that surfaced #335 (job
3f327329): four ``wp_terms``/multi-join queries an OpenSearch anti-pattern
flagged for lacking cross-index joins, reattributed to DynamoDB; three are
served by DynamoDB access patterns, one -- the same ``SUM``-with-join query
from #336's evidence -- is DynamoDB's own ``unsupported_patterns`` entry.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_risk_assessment

# Real query_text from the wordpress e2e evidence (job 3f327329): three
# cross-index multi-join queries OpenSearch cannot serve, now on DynamoDB
# (which serves them via an access pattern).
_SERVED_ON_DYNAMODB = {
    "25e3c7550c3775ca1f60248bfbaefc90e027c17f4c54084860dccd6776fb8a16": (  # pragma: allowlist secret
        "SELECT DISTINCTROW `t` . `term_id` FROM `wp_terms` AS `t` INNER JOIN "
        "`wp_term_taxonomy` AS `tt` ON `t` . `term_id` = `tt` . `term_id` INNER "
        "JOIN `wp_term_relationships` AS `tr` ON `tr` . `term_taxonomy_id` = "
        "`tt` . `term_taxonomy_id` WHERE `tt` . `taxonomy` IN (...) AND "
        "`tr` . `object_id` IN (...)"
    ),
    "d5ae7abb4fb5271f15a4193c025bb6c2bd745ff7e0806d140f89069876cd7ee7": (  # pragma: allowlist secret
        "SELECT DISTINCTROW `t` . `term_id` FROM `wp_terms` AS `t` INNER JOIN "
        "`wp_term_taxonomy` AS `tt` ON `t` . `term_id` = `tt` . `term_id` INNER "
        "JOIN `wp_term_relationships` AS `tr` ON `tr` . `term_taxonomy_id` = "
        "`tt` . `term_taxonomy_id` WHERE `tt` . `taxonomy` IN (...) AND "
        "`tr` . `object_id` IN (...) ORDER BY `t` . `name` ASC"
    ),
    "f37cfe518f83669067b5e9c57f68ced43ba9175a37b7b64f1122a20a46d6bf2e": (  # pragma: allowlist secret
        "SELECT DISTINCTROW `t` . `term_id` , `tr` . `object_id` FROM `wp_terms` "
        "AS `t` INNER JOIN `wp_term_taxonomy` AS `tt` ON `t` . `term_id` = "
        "`tt` . `term_id` INNER JOIN `wp_term_relationships` AS `tr` ON "
        "`tr` . `term_taxonomy_id` = `tt` . `term_taxonomy_id` WHERE "
        "`tt` . `taxonomy` IN (...) AND `tr` . `object_id` IN (...) ORDER BY "
        "`t` . `name` ASC"
    ),
}

# The one query DynamoDB's own schema design lists as unsupported (the same
# SUM-with-join query from #336's evidence): reality check moved it here, and
# it still has no serving engine other than Aurora.
_UNSERVED_QUERY_ID = "a355b7403b1d39d90f3ff59ece2a00e0225bbbc7c3a9d3d755d3f0e8d001e077"
_UNSERVED_SQL = (
    "SELECT `order_items` . `order_item_name` AS `order_item_name` , "
    "SUM ( `order_item_meta_discount_amount` . `meta_value` ) AS `discount_amount` , "
    "`posts` . `post_date` AS `post_date` FROM `wp_posts` AS `posts` "
    "INNER JOIN `wp_woocommerce_order_items` AS `order_items` "
    "ON `posts` . `ID` = `order_items` . `order_id` "
    "GROUP BY `order_items` . `order_item_id`"
)

_ALL_FOUR_IDS = [*_SERVED_ON_DYNAMODB, _UNSERVED_QUERY_ID]

_OPENSEARCH_OWN_QUERY_ID = "68b75de2bd1bba434ddd42e76804623d41efa87a7a210df795c0459e548d083"


def _data() -> SynthesisData:
    queries = (
        [
            {"query_id": qid, "query_text": sql, "tables_accessed": ["wordpress.wp_terms"]}
            for qid, sql in _SERVED_ON_DYNAMODB.items()
        ]
        + [
            {
                "query_id": _UNSERVED_QUERY_ID,
                "query_text": _UNSERVED_SQL,
                "tables_accessed": [
                    "wordpress.wp_posts",
                    "wordpress.wp_woocommerce_order_items",
                ],
            }
        ]
        + [
            {
                "query_id": _OPENSEARCH_OWN_QUERY_ID,
                "query_text": "SELECT * FROM wp_posts WHERE post_content LIKE '%foo%'",
                "tables_accessed": ["wordpress.wp_posts"],
            }
        ]
    )
    opensearch_analysis = {
        "workload_analysis": {
            "anti_patterns_detected": [
                {
                    "anti_pattern_type": "cross-index-joins",
                    "description": (
                        "Queries joining multiple tables (join_count >= 2). OpenSearch "
                        "does not support cross-index joins natively."
                    ),
                    "query_ids": _ALL_FOUR_IDS,
                    "table_ids": ["wordpress.wp_terms", "wordpress.wp_posts"],
                    "severity_weight": 0.8,
                    "recommendation": "Move multi-table joins to a relational or key-value engine.",
                }
            ]
        }
    }
    dynamodb_schema = {
        "table_definitions": [{"table_name": "Terms", "source_tables": ["wordpress.wp_terms"]}],
        "access_patterns": [
            {
                "pattern_id": "DDB-AP-1",
                "query_ids": list(_SERVED_ON_DYNAMODB),
                "in_scope": True,
            }
        ],
        "unsupported_patterns": [
            {
                "query_ids": [_UNSERVED_QUERY_ID],
                "pattern_type": "aggregation",
                "recommendation": (
                    "Multi-table SUM with GROUP BY: DynamoDB cannot compute this "
                    "server-side. Pre-compute on write or move to an analytics store."
                ),
            }
        ],
    }
    data = SynthesisData(
        job_id="j",
        database_name="wordpress",
        collector={"queries": {"query_patterns": queries}},
        engines={
            "opensearch": EngineArtifacts(
                "opensearch", analysis=opensearch_analysis, schema_design={}
            ),
            "dynamodb": EngineArtifacts("dynamodb", analysis={}, schema_design=dynamodb_schema),
        },
        assignment={
            "query_assignments": [
                {"query_id": qid, "assigned_engine": "dynamodb", "in_scope": True}
                for qid in _ALL_FOUR_IDS
            ]
            + [
                {
                    "query_id": _OPENSEARCH_OWN_QUERY_ID,
                    "assigned_engine": "opensearch",
                    "in_scope": True,
                }
            ]
        },
    )
    # Before Reality Check: all four were on OpenSearch (where the anti-pattern
    # flagged them); only the fifth, OpenSearch's own query, never moved.
    data.pre_reality_check_assignment = {
        "query_assignments": [
            {"query_id": qid, "assigned_engine": "opensearch"} for qid in _ALL_FOUR_IDS
        ]
        + [{"query_id": _OPENSEARCH_OWN_QUERY_ID, "assigned_engine": "opensearch"}]
    }
    return data


class TestMovedOntoUnsupportedIsRaisedNotDuplicated:
    def test_the_existing_risk_naming_the_query_is_raised_to_high(self) -> None:
        """The anti-pattern reattribution risk already names all four ids
        (including the unserved one) -- that risk is raised to HIGH (it already
        was, here) and annotated, not shadowed by a second entry."""
        result = build_risk_assessment(_data())
        risk = next(r for r in result["risks"] if _UNSERVED_QUERY_ID in r["query_ids"])
        assert risk["severity"] == "HIGH"
        assert "keep" in risk["description"].lower()
        assert (
            "aurora mysql" in risk["description"].lower() or "aurora" in risk["description"].lower()
        )

    def test_no_new_parallel_risk_is_created_for_the_same_query(self) -> None:
        """The id already sits in two pre-existing risks here -- the anti-pattern
        reattribution risk (all four ids) and its own per-pattern unsupported-
        pattern risk -- that pre-existing double-counting is unrelated to this
        guard. What the guard must never do is add a THIRD, parallel entry on
        top of those two."""
        result = build_risk_assessment(_data())
        risks_naming_it = [r for r in result["risks"] if _UNSERVED_QUERY_ID in r["query_ids"]]
        assert len(risks_naming_it) == 2
        assert len(result["risks"]) == 2
        assert sum(1 for r in risks_naming_it if "keep" in r["description"].lower()) == 1

    def test_mitigation_names_aurora_runs_the_source_sql(self) -> None:
        result = build_risk_assessment(_data())
        risk = next(r for r in result["risks"] if _UNSERVED_QUERY_ID in r["query_ids"])
        assert "runs the source SQL as-is" in risk["mitigation"]

    def test_served_queries_severity_is_unaffected(self) -> None:
        """The 3 served ids share the SAME risk entry as the unserved one here
        (one anti-pattern, one risk) -- raising severity for the gap does not
        invent a false claim about them; it is accurate for the whole risk,
        which already covered all four."""
        result = build_risk_assessment(_data())
        risk = next(r for r in result["risks"] if _UNSERVED_QUERY_ID in r["query_ids"])
        assert set(risk["query_ids"]) == set(_ALL_FOUR_IDS)

    def test_overall_risk_level_is_high_not_critical(self) -> None:
        result = build_risk_assessment(_data())
        assert result["overall_risk_level"] == "HIGH"

    def test_aurora_assigned_queries_are_never_flagged(self) -> None:
        """Aurora runs the source SQL as-is (#221): a query already assigned to
        Aurora is never 'moved onto an unsupported engine'."""
        data = _data()
        for qa in data.assignment["query_assignments"]:
            qa["assigned_engine"] = "aurora_mysql"
        data.engines["aurora_mysql"] = EngineArtifacts(
            "aurora_mysql", analysis={}, schema_design={"access_patterns": []}
        )
        result = build_risk_assessment(data)
        assert not any("keep" in r["description"].lower() for r in result["risks"])

    def test_a_query_never_flagged_by_any_anti_pattern_is_not_moved(self) -> None:
        """Only a query some anti-pattern attributes to a different engine than
        its current one counts as 'moved' -- a query naturally assigned to an
        engine whose schema design happens to list it unsupported, with no
        anti-pattern elsewhere naming it, is a routine unsupported-pattern risk,
        not a 'reality check moved it here' one (narrowed scope, review finding
        1: only a query reality check actually moved is in scope)."""
        data = _data()
        # q-orders-style case: unsupported on dynamodb, never named by any
        # anti-pattern on another engine.
        data.engines["dynamodb"].schema_design["unsupported_patterns"].append(
            {
                "query_ids": ["never-moved-id"],
                "pattern_type": "aggregation",
                "recommendation": "Needs an aggregate dynamodb cannot compute.",
            }
        )
        data.assignment["query_assignments"].append(
            {"query_id": "never-moved-id", "assigned_engine": "dynamodb", "in_scope": True}
        )
        result = build_risk_assessment(data)
        risk = next(r for r in result["risks"] if "never-moved-id" in r["query_ids"])
        assert risk["severity"] == "MEDIUM"
        assert "keep" not in risk["description"].lower()

    def test_without_an_assignment_the_guard_is_a_no_op(self) -> None:
        data = _data()
        data.assignment = None
        result = build_risk_assessment(data)
        assert not any("keep" in r["description"].lower() for r in result["risks"])


class TestOnlyTheFirstOpenRiskNamingTheQueryIsAnnotated:
    """When a query id already sits in more than one open risk, the guard
    annotates only the first one it finds (list order) -- never both, and
    never picks a different one at random. Tested directly against the
    private helper, so which risk is "first" is unambiguous and does not
    depend on build_risk_assessment's own risk-ordering internals."""

    def test_second_risk_naming_the_query_is_left_untouched(self) -> None:
        from src.agents.referee.synthesis_report import _raise_moved_onto_unsupported_risks

        data = SynthesisData(
            job_id="j",
            database_name="wordpress",
            assignment={
                "query_assignments": [
                    {"query_id": "qid", "assigned_engine": "dynamodb", "in_scope": True}
                ]
            },
            pre_reality_check_assignment={
                "query_assignments": [{"query_id": "qid", "assigned_engine": "origin"}]
            },
        )
        unsupported_by = {"dynamodb": {"qid"}}
        risk_tables: dict[str, set[str]] = {}
        query_text: dict[str, str] = {}
        first_risk: dict = {
            "risk_id": "RISK-001",
            "severity": "MEDIUM",
            "description": "[dynamodb] first risk naming qid",
            "mitigation": "",
            "query_ids": ["qid"],
        }
        second_risk: dict = {
            "risk_id": "RISK-002",
            "severity": "MEDIUM",
            "description": "[dynamodb] second risk also naming qid",
            "mitigation": "",
            "query_ids": ["qid"],
        }
        risks = [first_risk, second_risk]
        _raise_moved_onto_unsupported_risks(0, data, unsupported_by, risks, risk_tables, query_text)

        assert first_risk["severity"] == "HIGH"
        assert "keep" in first_risk["description"].lower()
        assert second_risk["severity"] == "MEDIUM"
        assert second_risk["description"] == "[dynamodb] second risk also naming qid"
        assert len(risks) == 2  # no new, third risk added


class TestItThemWordingIsPerTargetRisk:
    """Two queries moved onto the same engine, each sitting in its own
    separate pre-existing risk, must each read "it" (singular) -- not "them"
    (review finding: the pronoun must come from this risk's own overlap with
    the moved set, not the full moved set for the engine)."""

    def test_each_single_query_risk_says_it_not_them(self) -> None:
        from src.agents.referee.synthesis_report import _raise_moved_onto_unsupported_risks

        data = SynthesisData(
            job_id="j",
            database_name="wordpress",
            assignment={
                "query_assignments": [
                    {"query_id": "q1", "assigned_engine": "dynamodb", "in_scope": True},
                    {"query_id": "q2", "assigned_engine": "dynamodb", "in_scope": True},
                ]
            },
            pre_reality_check_assignment={
                "query_assignments": [
                    {"query_id": "q1", "assigned_engine": "origin"},
                    {"query_id": "q2", "assigned_engine": "origin"},
                ]
            },
        )
        unsupported_by = {"dynamodb": {"q1", "q2"}}
        risk_tables: dict[str, set[str]] = {}
        query_text: dict[str, str] = {}
        risk_for_q1: dict = {
            "risk_id": "RISK-001",
            "severity": "MEDIUM",
            "description": "[dynamodb] q1's own risk",
            "mitigation": "",
            "query_ids": ["q1"],
        }
        risk_for_q2: dict = {
            "risk_id": "RISK-002",
            "severity": "MEDIUM",
            "description": "[dynamodb] q2's own risk",
            "mitigation": "",
            "query_ids": ["q2"],
        }
        risks = [risk_for_q1, risk_for_q2]
        _raise_moved_onto_unsupported_risks(0, data, unsupported_by, risks, risk_tables, query_text)

        for risk in (risk_for_q1, risk_for_q2):
            assert risk["severity"] == "HIGH"
            assert " it " in risk["description"].lower() or risk["description"].lower().endswith(
                "it."
            )
            assert "them" not in risk["description"].lower()


class TestFallbackWhenNoExistingRiskNamesTheId:
    """The fallback new-risk path should not be reachable through
    ``build_risk_assessment`` (every unsupported id already gets the
    per-pattern ``unsupported_patterns`` risk above, by construction) -- it
    exists purely so a future refactor can never silently drop the gap. Tested
    directly against the private helper, the one exception to this module's
    "test through build_risk_assessment" convention, precisely because the
    integration path cannot exercise it."""

    def test_new_risk_uses_sql_excerpt_and_display_name(self) -> None:
        from src.agents.referee.synthesis_report import _raise_moved_onto_unsupported_risks

        data = SynthesisData(
            job_id="j",
            database_name="wordpress",
            assignment={
                "query_assignments": [
                    {"query_id": "qid", "assigned_engine": "dynamodb", "in_scope": True}
                ]
            },
            pre_reality_check_assignment={
                "query_assignments": [{"query_id": "qid", "assigned_engine": "origin"}]
            },
        )
        unsupported_by = {"dynamodb": {"qid"}}
        risk_tables = {"qid": {"wordpress.t"}}
        # Backticks must not survive into the excerpt (_sql_excerpt strips them)
        # -- distinguishes this from a hand-rolled excerpt.
        query_text = {"qid": "SELECT `t`.`id` FROM `t` GROUP BY `t`.`id`"}
        risks: list[dict] = []  # no existing risk names "qid" -- forces the fallback
        new_risk_id = _raise_moved_onto_unsupported_risks(
            0, data, unsupported_by, risks, risk_tables, query_text
        )
        assert new_risk_id == 1
        assert len(risks) == 1
        risk = risks[0]
        assert risk["severity"] == "HIGH"
        assert risk["query_ids"] == ["qid"]
        assert "`" not in risk["description"]
        assert "runs the source SQL as-is" in risk["mitigation"]
