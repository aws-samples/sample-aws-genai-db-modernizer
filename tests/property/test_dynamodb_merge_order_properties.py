"""The DynamoDB group-draft merge does not depend on draft order (issue #809).

DynamoDB schema design runs in parallel groups, and the merge reconciles what
the groups designed for the same rows. The groups are numbered by the
splitter, so the merged design must be a function of the drafts' content
alone: the same drafts in any order give byte-identical output, and
renumbering the groups changes nothing but the group labels in messages.
No model is called anywhere here.
"""

from __future__ import annotations

import copy
import json

from hypothesis import given
from hypothesis import strategies as st

from src.agents.schema_design.group_merger import merge_failures, merge_group_drafts

USERS = "forum.users"
TOPICS = "forum.topics"
POSTS = "forum.posts"
LINKS = "forum.topic_links"
TAGS = "forum.topic_tags"
SOLVED = "forum.solved_topics"
FIELDS = "forum.user_custom_fields"
BADGES = "forum.badges"
STATS = "forum.user_stats"

UNIQUE = {"users": [frozenset({"id"})], "badges": [frozenset({"id"})]}
COLUMN_TYPES = {"users": {"id": "N"}, "topics": {"id": "N"}}


def _key(name: str, type_: str = "N") -> dict:
    return {"attribute_name": name, "attribute_type": type_}


def _attr(name: str, source: str, column: str | None = None, type_: str = "N") -> dict:
    return {"name": name, "type": type_, "source_table": source, "source_column": column or name}


def _gsi(name: str, pk: str, projection: str = "ALL") -> dict:
    return {
        "gsi_name": name,
        "partition_key": [_key(pk, "S")],
        "sort_key": None,
        "projection": projection,
        "item_count": 10,
        "item_size_bytes": 100,
    }


def _single(name: str, source: str, pk: str, attrs: list[dict], gsis=(), sk: str | None = None):
    return {
        "table_name": name,
        "aggregate_pattern": "separate",
        "source_tables": [source],
        "partition_key": _key(pk),
        "sort_key": _key(sk) if sk else None,
        "attributes": attrs,
        "gsis": list(gsis),
        "item_count": 10,
        "item_size_bytes": 100,
    }


def _entity(entity_type: str, source: str, pk: str, sk: str, *attrs: dict) -> dict:
    return {
        "entity_type": entity_type,
        "source_table": source,
        "pk_template": pk,
        "sk_template": sk,
        "attributes": list(attrs) or [_attr("id", source)],
    }


def _collection(name: str, pk: str, sk: str, *entities: dict) -> dict:
    return {
        "table_name": name,
        "aggregate_pattern": "item_collection",
        "source_tables": list(dict.fromkeys(e["source_table"] for e in entities)),
        "partition_key": _key(pk),
        "sort_key": _key(sk, "S"),
        "entities": list(entities),
        "gsis": [],
        "item_count": 10,
        "item_size_bytes": 100,
    }


def _ap(n: int, query: str, table: str, operation: str, condition: str, gsi=None, source=USERS):
    return {
        "pattern_id": f"DDB-AP-{n}",
        "pattern_group": "g",
        "query_ids": [query],
        "source_tables": [source],
        "description": f"{operation} for {query}",
        "operation": operation,
        "table_name": table,
        "gsi_name": gsi,
        "key_condition": condition,
        "design_rps": 1.0,
        "item_size_bytes": 100,
        "strongly_consistent": False,
    }


def _draft(tables: list[dict], aps: list[dict], hot: list[dict] | None = None) -> dict:
    return {
        "contract_version": "1.0",
        "target_engine": "dynamodb",
        "table_definitions": tables,
        "access_patterns": aps,
        "hot_partition_analysis": hot or [],
        "trade_offs": [],
        "migration_notes": [],
        "unsupported_patterns": [],
        "validation_passed": True,
        "validation_failures": [],
    }


def _topic_entities(link: str, tag: str, solved: str) -> list[dict]:
    return [
        _entity(solved, SOLVED, "{topic_id}", "SOLVED", _attr("topic_id", SOLVED)),
        _entity(
            link, LINKS, "{topic_id}", "LINK#{id}", _attr("topic_id", LINKS), _attr("id", LINKS)
        ),
        _entity(
            tag, TAGS, "{topic_id}", "TAG#{tag_id}", _attr("topic_id", TAGS), _attr("tag_id", TAGS)
        ),
    ]


def _custom_fields(sk: str) -> dict:
    return _collection(
        "user_items",
        "user_id",
        "sk",
        _entity("USER_STAT", STATS, "{user_id}", "STATS", _attr("user_id", STATS)),
        _entity(
            "CUSTOM_FIELD",
            FIELDS,
            "{user_id}",
            sk,
            _attr("user_id", FIELDS),
            _attr("id", FIELDS),
            _attr("name", FIELDS, type_="S"),
        ),
    )


def _drafts() -> list[dict]:
    """Seven groups whose shared tables exercise every reconciliation rule.

    - users: one key under two names (``user_id``/``id``), then a second key
      schema (``email``) that becomes a GSI, a GSI name two groups use for
      different keys;
    - topics: one item collection designed by three groups under different
      names and entity labels, a single-entity posts table folding into it;
    - user_items: three groups agree on a custom-field sort key, one disagrees
      (a real conflict that must be reported once, whatever the order);
    - badges: the same rows under two table names with the same keys;
    - colliding pattern IDs (every group numbers from DDB-AP-1).
    """
    user_attrs = [_attr("user_id", USERS, "id"), _attr("email", USERS, type_="S")]
    g0 = _draft(
        [
            _single("users", USERS, "user_id", user_attrs, [_gsi("UsersByLogin", "email")]),
            _collection(
                "topics",
                "topic_id",
                "sk",
                _entity("TOPIC", TOPICS, "{id}", "TOPIC", _attr("id", TOPICS)),
                _entity(
                    "POST",
                    POSTS,
                    "{topic_id}",
                    "POST#{id}",
                    _attr("topic_id", POSTS),
                    _attr("id", POSTS),
                ),
                *_topic_entities("TOPIC_LINK", "TOPIC_TAG", "SOLVED_TOPIC"),
            ),
        ],
        [
            _ap(1, "q-users-1", "users", "GetItem", "PK=user_id"),
            _ap(2, "q-users-2", "users", "Query", "email = :e", gsi="UsersByLogin"),
            _ap(3, "q-topics-1", "topics", "Query", "PK=topic_id", source=TOPICS),
        ],
        [
            {
                "table_name": "users",
                "gsi_name": None,
                "operation": "read",
                "rcu_or_wcu_per_second": 10.0,
                "partition_limit": 3000,
                "utilization_pct": 0.3,
                "at_risk": False,
                "contributing_patterns": ["DDB-AP-1"],
                "mitigation": None,
            }
        ],
    )
    g1 = _draft(
        [
            _single(
                "users",
                USERS,
                "id",
                [_attr("id", USERS), _attr("username", USERS, type_="S")],
                [_gsi("UsersByLogin", "username")],
            ),
            _collection("topic_items", "topic_id", "sk", *_topic_entities("LINK", "TAG", "SOLVED")),
        ],
        [
            _ap(1, "q-users-3", "users", "GetItem", "PK=id"),
            _ap(2, "q-users-4", "users", "Query", "username = :u", gsi="UsersByLogin"),
            _ap(
                3, "q-topics-2", "topic_items", "Query", "PK=topic_id AND begins_with(sk, 'LINK#')"
            ),
        ],
    )
    g2 = _draft(
        [
            _collection(
                "topic_items",
                "topic_id",
                "record_key",
                _entity("SOLVED", SOLVED, "{topic_id}", "SOLVED", _attr("topic_id", SOLVED)),
                _entity(
                    "TOPIC_TAG",
                    TAGS,
                    "{topic_id}",
                    "TAG#{tag_id}",
                    _attr("topic_id", TAGS),
                    _attr("tag_id", TAGS),
                ),
            ),
            _single(
                "users",
                USERS,
                "email",
                [_attr("email", USERS, type_="S"), _attr("user_id", USERS, "id")],
            ),
        ],
        [
            _ap(1, "q-topics-3", "topic_items", "Query", "PK=topic_id", source=TAGS),
            _ap(2, "q-users-5", "users", "GetItem", "PK=email"),
        ],
    )
    g2["table_definitions"][1]["partition_key"] = _key("email", "S")
    g3 = _draft(
        [_custom_fields("CF#{id}")],
        [_ap(1, "q-fields-1", "user_items", "Query", "PK=user_id", source=FIELDS)],
    )
    g4 = _draft(
        [_custom_fields("CF#{name}")],
        [_ap(1, "q-fields-2", "user_items", "Query", "PK=user_id", source=FIELDS)],
    )
    g5 = _draft(
        [
            _custom_fields("CF#{id}"),
            _single(
                "posts",
                POSTS,
                "topic_id",
                [_attr("topic_id", POSTS), _attr("id", POSTS), _attr("raw", POSTS, type_="S")],
                sk="id",
            ),
            _single(
                "badges", BADGES, "id", [_attr("id", BADGES), _attr("name", BADGES, type_="S")]
            ),
        ],
        [
            _ap(1, "q-fields-3", "user_items", "Query", "PK=user_id", source=FIELDS),
            _ap(2, "q-posts-1", "posts", "Query", "PK=topic_id", source=POSTS),
            _ap(3, "q-badges-1", "badges", "GetItem", "PK=id", source=BADGES),
        ],
    )
    g6 = _draft(
        [
            _custom_fields("CF#{id}"),
            _single(
                "forum_badges",
                BADGES,
                "id",
                [_attr("id", BADGES), _attr("icon", BADGES, type_="S")],
            ),
        ],
        [
            _ap(1, "q-fields-4", "user_items", "Query", "PK=user_id", source=FIELDS),
            _ap(2, "q-badges-2", "forum_badges", "GetItem", "PK=id", source=BADGES),
        ],
    )
    return [g0, g1, g2, g3, g4, g5, g6]


def _merge(drafts: list[dict], groups: list[int]) -> tuple[dict, list[str]]:
    notes: list[str] = []
    merged = merge_group_drafts(
        copy.deepcopy(drafts),
        "dynamodb",
        group_indices=groups,
        unique_keys=UNIQUE,
        notes=notes,
        column_types=COLUMN_TYPES,
    )
    return merged, notes


def _output(drafts: list[dict], groups: list[int]) -> str:
    merged, notes = _merge(drafts, groups)
    return json.dumps({"merged": merged, "notes": notes}, sort_keys=True)


def _structure(merged: dict) -> str:
    """The merged design without group labels (they name groups; GSIs are named by keys)."""

    def keys(value: list[dict] | None) -> list[tuple[str, str]]:
        return [(k["attribute_name"], k["attribute_type"]) for k in value or []]

    tables = {}
    gsi_keys: dict[tuple[str, str], object] = {}
    for t in merged["table_definitions"]:
        gsis = []
        for g in t.get("gsis") or []:
            sig = (keys(g["partition_key"]), keys(g.get("sort_key")), g.get("projection"))
            gsi_keys[(t["table_name"], g["gsi_name"])] = sig
            gsis.append(sig)
        tables[t["table_name"]] = {
            "keys": [t.get("partition_key"), t.get("sort_key")],
            "entities": sorted(
                (e["entity_type"], e["source_table"], e["pk_template"], e["sk_template"])
                for e in t.get("entities") or []
            ),
            "attributes": sorted(a["name"] for a in t.get("attributes") or []),
            "gsis": sorted(map(str, gsis)),
        }
    patterns = sorted(
        (
            ap["pattern_id"],
            tuple(ap["query_ids"]),
            ap["table_name"],
            ap["operation"],
            str(gsi_keys.get((ap["table_name"], ap.get("gsi_name"))) if ap.get("gsi_name") else ""),
        )
        for ap in merged["access_patterns"]
    )
    return json.dumps(
        {
            "tables": tables,
            "patterns": patterns,
            "failures": len(merge_failures(merged)),
            "trade_offs": len(merged.get("trade_offs") or []),
        },
        sort_keys=True,
    )


BASELINE = _output(_drafts(), list(range(7)))


@given(st.permutations(range(7)))
def test_any_draft_order_gives_byte_identical_output(order: list[int]) -> None:
    drafts = _drafts()
    assert _output([drafts[i] for i in order], list(order)) == BASELINE


def _numbered(labels: list[int]) -> tuple[list[dict], list[int]]:
    """The drafts as ``--merge`` loads them when the splitter numbered group i ``labels[i]``."""
    pairs = sorted(zip(labels, _drafts(), strict=True), key=lambda p: p[0])
    return [d for _, d in pairs], [g for g, _ in pairs]


@given(st.permutations(range(7)))
def test_group_numbering_changes_only_the_group_labels(labels: list[int]) -> None:
    baseline, _ = _merge(*_numbered(list(range(7))))
    renumbered, _ = _merge(*_numbered(list(labels)))
    assert _structure(renumbered) == _structure(baseline)


def test_the_fixture_reconciles_and_keeps_its_one_real_conflict() -> None:
    """Guard the fixture itself: the rules above are really exercised."""
    merged, notes = _merge(_drafts(), list(range(7)))
    tables = {t["table_name"]: t for t in merged["table_definitions"]}
    [failure] = merge_failures(merged)
    assert "CUSTOM_FIELD" in failure and "group 4" in failure  # the one group that disagrees
    assert "users" in tables and len(tables["users"]["gsis"]) >= 2
    assert (
        sum(
            1
            for t in tables.values()
            if t.get("entities") and any(e["source_table"] == LINKS for e in t["entities"])
        )
        == 1
    )  # the three topic collections are one table
    assert not {"badges", "forum_badges"} <= set(tables)  # one home for the badges rows
    assert notes
