"""An unresolved DynamoDB cross-group design conflict is an open risk (#426).

``dynamodb_merge.independent_homes`` records a source table two design groups each
gave its own entity as a review trade-off (``dynamodb_merge.merge_overlaps``) instead of
consolidating it -- that trade-off landed in ``report.json``'s ``trade_offs`` list, but
``risk_assessment.risks`` never read it, so a report could say "review this conflict
before migration" in its trade-offs while the risk register (and ``overall_risk_level``)
stayed empty/LOW. ``build_risk_assessment`` now turns every open overlap trade-off into
one MEDIUM risk naming the tables and the access patterns that actually read the shared
source table; once the trade-off is gone (consolidated, or covered by a real trade-off),
so is the risk.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_risk_assessment
from src.agents.schema_design.dynamodb_merge import OVERLAP_PREFIX
from src.agents.schema_design.group_merger import merge_group_drafts

_SOURCE_TABLES = ["wp_posts", "wp_postmeta", "wp_termmeta"]


def _schema(trade_offs: list[dict], access_patterns: list[dict] | None = None) -> dict:
    return {
        "table_definitions": [
            {"table_name": "PostsTable", "partition_key": {"attribute_name": "pk"}},
            {"table_name": "PostsAltTable", "partition_key": {"attribute_name": "pk"}},
        ],
        "trade_offs": trade_offs,
        "access_patterns": access_patterns or [],
    }


def _overlap_trade_off() -> dict:
    return {
        "description": (
            f"{OVERLAP_PREFIX}{', '.join(_SOURCE_TABLES)} modelled independently by "
            "design groups in PostsTable (group 0; PK pk), PostsAltTable (group 1; "
            "PK pk); choose one write path or keep copies in sync -- review before "
            "migration."
        ),
        "impact": (
            f"Until this is reviewed, rows of {', '.join(_SOURCE_TABLES)} have 2 "
            "DynamoDB homes (PostsTable, PostsAltTable): a migration must either "
            "write one of them and drop the others, or write all of them and keep "
            "them in sync."
        ),
        "source_tables": _SOURCE_TABLES,
        "target_tables": ["PostsTable", "PostsAltTable"],
        "query_ids": [],
        "engine": "dynamodb",
    }


def _data(schema: dict) -> SynthesisData:
    data = SynthesisData(job_id="j", database_name="wordpress")
    data.collector = {"database_schema": {"tables": []}, "queries": {"query_patterns": []}}
    data.engines["dynamodb"] = EngineArtifacts("dynamodb", analysis={}, schema_design=schema)
    return data


def test_unresolved_overlap_becomes_a_medium_risk() -> None:
    access_patterns = [
        {"table_name": "PostsTable", "source_tables": ["wp_posts"], "query_ids": ["q1"]},
        {"table_name": "PostsAltTable", "source_tables": ["wp_posts"], "query_ids": ["q2"]},
        # Unrelated entity sharing the same target table (an item collection holding
        # other data too) -- its query must not be attributed to the conflict.
        {"table_name": "PostsTable", "source_tables": ["wp_users"], "query_ids": ["q-unrelated"]},
    ]
    schema = _schema([_overlap_trade_off()], access_patterns)
    risks = build_risk_assessment(_data(schema))["risks"]

    conflict_risks = [r for r in risks if r["risk_type"] == "DATA_CONSISTENCY"]
    assert len(conflict_risks) == 1
    risk = conflict_risks[0]

    assert risk["severity"] == "MEDIUM"
    assert risk["description"].startswith("[dynamodb]")
    assert OVERLAP_PREFIX not in risk["description"]
    for table in _SOURCE_TABLES:
        assert table in risk["description"]
    assert "PostsTable" in risk["description"] and "PostsAltTable" in risk["description"]
    assert sorted(risk["affected_tables"]) == sorted(_SOURCE_TABLES)
    assert risk["query_ids"] == ["q1", "q2"]
    assert "q-unrelated" not in risk["query_ids"]
    assert risk["mitigation"]
    assert "consolidate" in risk["mitigation"].lower()


def test_unresolved_overlap_raises_overall_risk_level() -> None:
    schema = _schema([_overlap_trade_off()])
    result = build_risk_assessment(_data(schema))
    assert result["overall_risk_level"] == "MEDIUM"


def test_resolved_conflict_has_no_trade_off_and_no_risk() -> None:
    # Once consolidated (or otherwise covered), the merge no longer emits the
    # overlap trade-off at all -- nothing for the risk builder to see.
    schema = _schema([])
    risks = build_risk_assessment(_data(schema))["risks"]
    assert not any(r["risk_type"] == "DATA_CONSISTENCY" for r in risks)


def test_other_trade_offs_do_not_become_risks() -> None:
    ordinary_trade_off = {
        "description": "Group designs PostsTable (group 0) and Posts2 (group 1) both "
        "model wp_posts with the same keys; the merge combines them into PostsTable.",
        "impact": "wp_posts has one DynamoDB home, PostsTable.",
        "source_tables": ["wp_posts"],
        "target_tables": ["PostsTable"],
        "query_ids": [],
        "engine": "dynamodb",
    }
    schema = _schema([ordinary_trade_off])
    risks = build_risk_assessment(_data(schema))["risks"]
    assert not any(r["risk_type"] == "DATA_CONSISTENCY" for r in risks)


# ---------------------------------------------------------------------------
# Built from the real DynamoDB merge (not a hand-built schema), so a change to
# how ``dynamodb_merge.independent_homes`` builds the review trade-off breaks
# this test, not just a hand-rolled fixture that happens to match today's shape.
# Same shapes as ``TestDynamoDBOverlappingDesigns._posts_drafts`` in
# tests/unit/test_group_merger.py.
# ---------------------------------------------------------------------------


def _key(name: str, type_: str = "S") -> dict:
    return {"attribute_name": name, "attribute_type": type_}


def _attr(name: str, source_table: str, column: str | None = None) -> dict:
    return {
        "name": name,
        "type": "S",
        "source_table": source_table,
        "source_column": column or name,
    }


def _entity(entity_type: str, source_table: str, pk: str, sk: str, *attrs: str) -> dict:
    return {
        "entity_type": entity_type,
        "source_table": source_table,
        "pk_template": pk,
        "sk_template": sk,
        "attributes": [_attr(a, source_table) for a in attrs or ("id",)],
    }


def _collection(name: str, pk: str, sk: str, *entities: dict, gsis=(), items=10) -> dict:
    sources = list(dict.fromkeys(e["source_table"] for e in entities))
    return {
        "table_name": name,
        "aggregate_pattern": "item_collection",
        "source_tables": sources,
        "partition_key": _key(pk, "N"),
        "sort_key": _key(sk),
        "entities": list(entities),
        "gsis": list(gsis),
        "item_count": items,
        "item_size_bytes": 100,
    }


def _single(name: str, source_table: str, pk: str, sk: str | None = None, items=10) -> dict:
    return {
        "table_name": name,
        "aggregate_pattern": "separate",
        "source_tables": [source_table],
        "partition_key": _key(pk, "N"),
        "sort_key": _key(sk) if sk else None,
        "attributes": [_attr(pk, source_table)],
        "gsis": [],
        "item_count": items,
        "item_size_bytes": 200,
    }


def _ap(pattern_id: str, table: str, *sources: str, gsi: str | None = None) -> dict:
    return {
        "pattern_id": pattern_id,
        "table_name": table,
        "gsi_name": gsi,
        "source_tables": list(sources),
        "query_ids": [f"q-{pattern_id}-{table}"],
    }


def _ddb(tables: list[dict], aps: list[dict], trade_offs: list[dict] | None = None) -> dict:
    return {
        "table_definitions": tables,
        "access_patterns": aps,
        "hot_partition_analysis": [{"table_name": t["table_name"]} for t in tables],
        "trade_offs": trade_offs or [],
        "validation_passed": True,
        "validation_failures": [],
    }


def _posts_drafts() -> list[dict]:
    """``WpPosts`` (group 0, item collection) and ``wp_posts`` (group 1, single-entity)
    both give ``wp_posts`` its own entity -- different shapes, so the merge cannot
    consolidate them and records the overlap instead (#426's trigger).
    """
    posts = _entity("POST", "wp_posts", "{ID}", "POST")
    g0 = _ddb(
        [_collection("WpPosts", "post_id", "record_key", posts)],
        [
            _ap("DDB-AP-4", "WpPosts", "wp_posts"),
            # An unrelated entity in the same item collection: its query must not be
            # attributed to the wp_posts overlap.
            _ap("DDB-AP-5", "WpPosts", "wp_users"),
        ],
    )
    g1 = _ddb([_single("wp_posts", "wp_posts", "id")], [_ap("DDB-AP-2", "wp_posts", "wp_posts")])
    return [g0, g1]


def test_real_merge_overlap_becomes_a_risk_with_only_its_own_queries() -> None:
    merged = merge_group_drafts(_posts_drafts(), "dynamodb")
    assert [t["table_name"] for t in merged["table_definitions"]] == ["WpPosts", "wp_posts"]

    data = _data(merged)
    risks = build_risk_assessment(data)["risks"]
    conflict_risks = [r for r in risks if r["risk_type"] == "DATA_CONSISTENCY"]
    assert len(conflict_risks) == 1
    risk = conflict_risks[0]

    assert risk["severity"] == "MEDIUM"
    assert risk["affected_tables"] == ["wp_posts"]
    # Only the two patterns that actually read wp_posts, not DDB-AP-5 (wp_users)
    # which merely shares WpPosts as its target table.
    assert sorted(risk["query_ids"]) == ["q-DDB-AP-2-wp_posts", "q-DDB-AP-4-WpPosts"]
