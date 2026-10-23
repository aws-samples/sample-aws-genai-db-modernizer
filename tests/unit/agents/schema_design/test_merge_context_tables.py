"""The merge honours the split's table ownership and refuses dangling references (#1243).

``--split`` makes each source table primary in one group (its owner) and gives the
other groups that touch it ``context_tables``. A context group may define a
placeholder for a context table: a single-entity table named after the source table,
keyed by the source primary key, with only the GSIs and access patterns it needs.
The merge reads the manifest's ownership: it points every placeholder at the owner's
table for those rows, and keeps the owner's key schema there (another group's key
becomes a GSI), whatever key serves the most patterns. An access pattern or
hot-partition entry naming a table or GSI the merged design does not define fails
validation. No model is called.
"""

from __future__ import annotations

import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

from src.agents.schema_design.dynamodb_merge import dangling_references
from src.agents.schema_design.group_merger import (
    merge_group_drafts,
    merge_schema_groups,
    table_owners,
)
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import run_pipeline
from tests.shared_runs import cached_run
from tests.unit.agents.schema_design.valid_designs import (
    ddb_attr,
    ddb_design,
    ddb_key,
    ddb_pattern,
    ddb_table,
)

POSTS = "wordpress.wp_posts"
UNIQUE = {"wp_posts": [frozenset({"id"})]}


def _gsi(name: str, pk: str, sk: str | None = None) -> dict:
    return {
        "gsi_name": name,
        "partition_key": [ddb_key(pk)],
        "sort_key": [ddb_key(sk)] if sk else None,
        "projection": "ALL",
        "item_count": 10,
        "item_size_bytes": 100,
    }


def _owner_draft() -> dict:
    table = ddb_table("Posts", POSTS, pk="post_id", attributes=[ddb_attr("post_id", POSTS, "ID")])
    return ddb_design([table], [ddb_pattern("DDB-AP-1", "Posts", POSTS, "q1")])


# ---------------------------------------------------------------------------
# Dangling references (the review's reproduction)
# ---------------------------------------------------------------------------


def test_a_pattern_on_a_gsi_no_table_defines_fails_the_merge() -> None:
    """Owner draft + a context draft with no table definition whose pattern uses an
    undefined GSI: the merge used to say ``validation_passed: true``."""
    context = ddb_design(
        [],
        [
            ddb_pattern(
                "DDB-AP-1", "Posts", POSTS, "q2", gsi_name="PostsByAuthor", operation="Query"
            )
        ],
    )
    merged = merge_group_drafts([_owner_draft(), context], "dynamodb", group_indices=[0, 1])
    assert merged["validation_passed"] is False
    failures = [f for f in merged["validation_failures"] if "PostsByAuthor" in f]
    assert failures and "no GSI" in failures[0] and "Posts" in failures[0]


def test_a_pattern_on_a_table_no_draft_defines_fails_the_merge() -> None:
    context = ddb_design([], [ddb_pattern("DDB-AP-1", "Nowhere", POSTS, "q2")])
    merged = merge_group_drafts([_owner_draft(), context], "dynamodb", group_indices=[0, 1])
    assert merged["validation_passed"] is False
    assert any("Nowhere" in f and "no table" in f for f in merged["validation_failures"])


def test_a_hot_partition_entry_on_an_undefined_gsi_is_dangling() -> None:
    design = {
        "table_definitions": [{"table_name": "Posts", "gsis": [{"gsi_name": "ByAuthor"}]}],
        "access_patterns": [{"pattern_id": "DDB-AP-1", "table_name": "Posts"}],
        "hot_partition_analysis": [{"table_name": "Posts", "gsi_name": "ByDate"}],
    }
    found = dangling_references(design)
    assert len(found) == 1 and "ByDate" in found[0]


def test_defined_references_are_not_dangling() -> None:
    design = {
        "table_definitions": [{"table_name": "Posts", "gsis": [{"gsi_name": "ByAuthor"}]}],
        "access_patterns": [
            {"pattern_id": "DDB-AP-1", "table_name": "Posts"},
            {"pattern_id": "DDB-AP-2", "table_name": "Posts", "gsi_name": "ByAuthor"},
        ],
        "hot_partition_analysis": [{"table_name": "Posts", "gsi_name": None}],
    }
    assert dangling_references(design) == []


# ---------------------------------------------------------------------------
# Ownership: placeholders fold onto the owner's table, the owner's key is kept
# ---------------------------------------------------------------------------


def _placeholder_draft() -> dict:
    """Group 1 reads wp_posts as context: a placeholder named after the source table,
    keyed by the source primary key, carrying only the GSI its pattern needs."""
    table = ddb_table(
        "wp_posts",
        POSTS,
        pk="ID",
        attributes=[ddb_attr("ID", POSTS), ddb_attr("post_author", POSTS)],
        gsis=[_gsi("PostsByAuthor", "post_author")],
    )
    pattern = ddb_pattern(
        "DDB-AP-1", "wp_posts", POSTS, "q2", gsi_name="PostsByAuthor", operation="Query"
    )
    return ddb_design([table], [pattern])


@pytest.mark.passes_on_main  # one placeholder already folds by its rows; the rule relies on it
def test_a_placeholder_folds_onto_the_owners_table() -> None:
    merged = merge_group_drafts(
        [_owner_draft(), _placeholder_draft()],
        "dynamodb",
        group_indices=[0, 1],
        unique_keys=UNIQUE,
        owners={"wp_posts": 0},
    )
    tables = {t["table_name"]: t for t in merged["table_definitions"]}
    assert list(tables) == ["Posts"]
    assert tables["Posts"]["partition_key"]["attribute_name"] == "post_id"
    assert [g["gsi_name"] for g in tables["Posts"]["gsis"]] == ["PostsByAuthor"]
    by_query = {ap["query_ids"][0]: ap for ap in merged["access_patterns"]}
    assert by_query["q2"]["table_name"] == "Posts"
    assert by_query["q2"]["gsi_name"] == "PostsByAuthor"
    assert merged["validation_passed"] is True, merged["validation_failures"]


def test_two_placeholders_never_outvote_the_owner() -> None:
    """Two context groups name the table and its key after the source table: by votes
    their names win; with the split's ownership the owner's table name and key do."""
    second = _placeholder_draft()
    second["access_patterns"][0]["query_ids"] = ["q3"]
    drafts = [_owner_draft(), _placeholder_draft(), second]
    without = merge_group_drafts(drafts, "dynamodb", group_indices=[0, 1, 2], unique_keys=UNIQUE)
    assert [
        (t["table_name"], t["partition_key"]["attribute_name"])
        for t in without["table_definitions"]
    ] == [("wp_posts", "ID")]
    merged = merge_group_drafts(
        drafts, "dynamodb", group_indices=[0, 1, 2], unique_keys=UNIQUE, owners={"wp_posts": 0}
    )
    assert [
        (t["table_name"], t["partition_key"]["attribute_name"]) for t in merged["table_definitions"]
    ] == [("Posts", "post_id")]
    assert {ap["table_name"] for ap in merged["access_patterns"]} == {"Posts"}
    assert merged["validation_passed"] is True, merged["validation_failures"]


def _rival_draft(group_patterns: int) -> dict:
    """A context group that keyed Posts by (post_author, ID) and has more patterns on it."""
    table = ddb_table(
        "Posts",
        POSTS,
        pk="post_author",
        sort_key=ddb_key("post_id"),
        attributes=[ddb_attr("post_author", POSTS), ddb_attr("post_id", POSTS, "ID")],
    )
    patterns = [
        ddb_pattern(f"DDB-AP-{i + 1}", "Posts", POSTS, f"r{i}", operation="Query")
        for i in range(group_patterns)
    ]
    return ddb_design([table], patterns)


def test_the_owners_key_is_kept_even_when_another_key_serves_more_patterns() -> None:
    drafts = [_owner_draft(), _rival_draft(3)]
    without = merge_group_drafts(drafts, "dynamodb", group_indices=[0, 1], unique_keys=UNIQUE)
    posts = next(t for t in without["table_definitions"] if t["table_name"] == "Posts")
    assert posts["partition_key"]["attribute_name"] == "post_author"  # most patterns win

    merged = merge_group_drafts(
        drafts, "dynamodb", group_indices=[0, 1], unique_keys=UNIQUE, owners={"wp_posts": 0}
    )
    posts = next(t for t in merged["table_definitions"] if t["table_name"] == "Posts")
    assert posts["partition_key"]["attribute_name"] == "post_id"
    assert posts.get("sort_key") is None
    rival = [ap for ap in merged["access_patterns"] if ap["query_ids"][0].startswith("r")]
    assert rival and all(ap.get("gsi_name") for ap in rival)
    gsis = {g["gsi_name"] for g in posts["gsis"]}
    assert {ap["gsi_name"] for ap in rival} <= gsis
    assert merged["validation_passed"] is True, merged["validation_failures"]


def test_the_owners_key_is_kept_whatever_the_group_numbering() -> None:
    """Ownership is by group index, not draft position: owner group 7 listed second."""
    merged = merge_group_drafts(
        [_rival_draft(3), _owner_draft()],
        "dynamodb",
        group_indices=[2, 7],
        unique_keys=UNIQUE,
        owners={"wp_posts": 7},
    )
    posts = next(t for t in merged["table_definitions"] if t["table_name"] == "Posts")
    assert posts["partition_key"]["attribute_name"] == "post_id"


def test_table_owners_reads_the_manifest() -> None:
    manifest = {
        "contract_version": "1.2",
        "groups": [
            {"group_index": 0, "primary_tables": ["wordpress.wp_posts", "wordpress.wp_users"]},
            {"group_index": 1, "primary_tables": [], "context_tables": ["wordpress.wp_posts"]},
        ],
    }
    assert table_owners(manifest) == {"wp_posts": 0, "wp_users": 0}
    assert table_owners({"contract_version": "1.2", "groups": []}) == {}


@pytest.mark.parametrize("version", [None, "1.0", "1.1"])
def test_a_manifest_written_before_ownership_has_no_owners(version: str | None) -> None:
    """Before 1.2 a table was primary in every chunk of its cluster: no group owns it."""
    manifest: dict[str, Any] = {
        "groups": [
            {"group_index": 0, "primary_tables": ["wordpress.wp_posts"]},
            {"group_index": 1, "primary_tables": ["wordpress.wp_posts", "wordpress.wp_users"]},
        ]
    }
    if version:
        manifest["contract_version"] = version
    assert table_owners(manifest) == {}


def test_a_table_primary_in_two_groups_has_no_owner() -> None:
    manifest = {
        "contract_version": "1.2",
        "groups": [
            {"group_index": 0, "primary_tables": ["wordpress.wp_posts", "wordpress.wp_users"]},
            {"group_index": 1, "primary_tables": ["wordpress.wp_posts"]},
        ],
    }
    assert table_owners(manifest) == {"wp_users": 0}


def _write_job(tmp_path: Path, manifest: dict, drafts: list[dict]) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    base = "wordpress/j/schema-dynamodb/v1"
    store.write_json(f"{base}/groups_manifest.json", manifest)
    for n, draft in enumerate(drafts):
        store.write_json(f"{base}/schema_draft_group_{n}.json", draft)
    return store


def test_an_old_manifest_merges_exactly_as_main_did(tmp_path: Path) -> None:
    """A job split before 1.2 (wp_posts primary in both groups), re-merged: the design is
    the one main's merge makes (the key serving the most patterns, the owner's own key
    as a GSI), not the first group's key."""
    manifest = {
        "job_id": "j",
        "database_name": "wordpress",
        "target_engine": "dynamodb",
        "total_queries": 4,
        "total_groups": 2,
        "groups": [
            {
                "group_index": n,
                "group_name": f"wp_posts_part{n + 1}",
                "primary_tables": [POSTS],
                "query_count": 1 + 2 * n,
                "table_count": 1,
                "input_file": f"input_group_{n}.json",
            }
            for n in range(2)
        ],
    }
    drafts = [_owner_draft(), _rival_draft(3)]
    store = _write_job(tmp_path, manifest, drafts)
    merged = merge_schema_groups("j", "wordpress", "dynamodb", store, schema_version=1)
    expected = merge_group_drafts(drafts, "dynamodb", group_indices=[0, 1])
    assert merged["table_definitions"] == expected["table_definitions"]
    assert merged["access_patterns"] == expected["access_patterns"]
    assert merged["validation_passed"] is expected["validation_passed"] is True
    posts = next(t for t in merged["table_definitions"] if t["table_name"] == "Posts")
    assert posts["partition_key"]["attribute_name"] == "post_author"


# ---------------------------------------------------------------------------
# Owners that keep the rows in an item collection, or only copy them
# ---------------------------------------------------------------------------

META = "wordpress.wp_postmeta"
USERS = "wordpress.wp_users"
OWNS_POSTS = {"wp_posts": 0, "wp_postmeta": 0, "wp_users": 0}


def _content_draft(post_entity: dict | None = None) -> dict:
    """The owner keeps posts and their meta in one item collection, ``Content``. The
    post entity names its key after ``post_id`` without saying which column it copies,
    so the single-table fold (rule 4) cannot map a placeholder's key onto it."""
    post = post_entity or {
        "entity_type": "POST",
        "source_table": POSTS,
        "pk_template": "{post_id}",
        "sk_template": "POST",
        "attributes": [ddb_attr("post_title", POSTS)],
    }
    meta = {
        "entity_type": "META",
        "source_table": META,
        "pk_template": "{post_id}",
        "sk_template": "META#{meta_id}",
        "attributes": [ddb_attr("post_id", META), ddb_attr("meta_id", META)],
    }
    table = {
        "table_name": "Content",
        "aggregate_pattern": "item_collection",
        "source_tables": [POSTS, META],
        "partition_key": ddb_key("post_id"),
        "sort_key": ddb_key("sk"),
        "entities": [post, meta],
        "gsis": [],
        "item_count": 10,
        "item_size_bytes": 100,
    }
    return ddb_design([table], [ddb_pattern("DDB-AP-1", "Content", POSTS, "q1", operation="Query")])


def test_a_placeholder_folds_into_the_owners_item_collection() -> None:
    merged = merge_group_drafts(
        [_content_draft(), _placeholder_draft()],
        "dynamodb",
        group_indices=[0, 1],
        unique_keys=UNIQUE,
        owners=OWNS_POSTS,
    )
    assert merged["validation_passed"] is True, merged["validation_failures"]
    assert [t["table_name"] for t in merged["table_definitions"]] == ["Content"]
    content = merged["table_definitions"][0]
    # The owner's key is kept; the placeholder's GSI and its key attribute join it.
    assert content["partition_key"]["attribute_name"] == "post_id"
    assert content["sort_key"]["attribute_name"] == "sk"
    assert [g["gsi_name"] for g in content["gsis"]] == ["PostsByAuthor"]
    post = next(e for e in content["entities"] if e["entity_type"] == "POST")
    assert "post_author" in {a["name"] for a in post["attributes"]}
    q2 = next(ap for ap in merged["access_patterns"] if ap["query_ids"] == ["q2"])
    assert (q2["table_name"], q2["gsi_name"]) == ("Content", "PostsByAuthor")


def test_a_placeholder_key_read_the_collection_cannot_serve_fails_and_says_why() -> None:
    """A GetItem on the placeholder's own key (ID): the owner's entity keys posts by
    ``{post_id}`` with no column behind it, so nothing proves the read finds the item."""
    context = _placeholder_draft()
    context["access_patterns"].append(ddb_pattern("DDB-AP-2", "wp_posts", POSTS, "q3"))
    merged = merge_group_drafts(
        [_content_draft(), context],
        "dynamodb",
        group_indices=[0, 1],
        unique_keys=UNIQUE,
        owners=OWNS_POSTS,
    )
    assert merged["validation_passed"] is False
    reasons = [f for f in merged["validation_failures"] if "placeholder" in f]
    assert reasons and "Content" in reasons[0] and "POST" in reasons[0], merged[
        "validation_failures"
    ]


def test_a_placeholder_key_read_the_entity_key_serves_is_folded() -> None:
    """The owner's entity is keyed by the post's own ID (PK and SK templates copy only
    ``ID``): the placeholder's GetItem by ID runs on the collection."""
    post = {
        "entity_type": "POST",
        "source_table": POSTS,
        "pk_template": "{post_id}",
        "sk_template": "POST#{post_id}",
        "attributes": [ddb_attr("post_id", POSTS, "ID"), ddb_attr("post_title", POSTS)],
    }
    context = _placeholder_draft()
    context["access_patterns"].append(ddb_pattern("DDB-AP-2", "wp_posts", POSTS, "q3"))
    merged = merge_group_drafts(
        [_content_draft(post), context],
        "dynamodb",
        group_indices=[0, 1],
        unique_keys=UNIQUE,
        owners=OWNS_POSTS,
    )
    assert merged["validation_passed"] is True, merged["validation_failures"]
    assert [t["table_name"] for t in merged["table_definitions"]] == ["Content"]
    q3 = next(ap for ap in merged["access_patterns"] if ap["query_ids"] == ["q3"])
    assert q3["table_name"] == "Content" and not q3.get("gsi_name")
    assert "POST" in q3["key_condition"]


def _users_draft() -> dict:
    """The owner only copies a post column onto its users: it has no home for posts."""
    table = ddb_table(
        "Users",
        USERS,
        pk="user_id",
        source_tables=[USERS, POSTS],
        attributes=[
            ddb_attr("user_id", USERS, "ID"),
            ddb_attr("last_post_title", POSTS, "post_title"),
        ],
    )
    return ddb_design([table], [ddb_pattern("DDB-AP-1", "Users", USERS, "q1")])


def test_a_copy_only_owner_leaves_the_placeholder_as_main_does() -> None:
    drafts = [_users_draft(), _placeholder_draft()]
    merged = merge_group_drafts(
        drafts, "dynamodb", group_indices=[0, 1], unique_keys=UNIQUE, owners=OWNS_POSTS
    )
    expected = merge_group_drafts(drafts, "dynamodb", group_indices=[0, 1], unique_keys=UNIQUE)
    assert merged["validation_passed"] is True, merged["validation_failures"]
    assert merged["table_definitions"] == expected["table_definitions"]
    users = next(t for t in merged["table_definitions"] if t["table_name"] == "Users")
    assert users["partition_key"]["attribute_name"] == "user_id"


# ---------------------------------------------------------------------------
# Deterministic end-to-end check on the samples with context groups
# ---------------------------------------------------------------------------


def _latest_assignment(job: Path) -> int:
    return max(int(p.name[1:]) for p in (job / "assignment").glob("v*") if p.name[1:].isdigit())


def _pk_columns(table: dict) -> list[str]:
    for index in table.get("indexes") or []:
        if index.get("is_primary"):
            return list(index.get("columns") or [])
    return [str((table.get("columns") or [{}])[0].get("column_name") or "id")]


def _rule_drafts(inputs: dict[int, dict], manifest: dict, collections: bool) -> list[dict]:
    """One draft per group, written to the context-table rule: every owned table as a
    single-entity table keyed by its source primary key (or, with ``collections``, as
    an item collection with one entity keyed by it); every context table a query
    reads as a placeholder named after the source table, same key, plus one GSI of the
    group's own; each query one pattern on its primary table."""
    owner = {t: g["group_index"] for g in manifest["groups"] for t in g["primary_tables"]}
    drafts = []
    for n, data in sorted(inputs.items()):
        schema = {t["table_id"]: t for t in data["collector_output"]["database_schema"]["tables"]}
        context = {c["table"] for c in data.get("group_context_tables") or []}
        tables: dict[str, dict] = {}
        patterns: list[dict] = []
        for t in sorted(schema):
            if owner.get(t) != n and t not in context:
                continue
            pk = _pk_columns(schema[t])
            short = t.split(".")[-1]
            name = f"Owned_{short}" if owner.get(t) == n else short
            gsis = [] if owner.get(t) == n else [_gsi(f"{short}_g{n}", f"g{n}_attr")]
            # Each group's GSI is on a column of its own, so no two are the same index.
            attrs = [ddb_attr(c, t) for c in pk] + [ddb_attr(f"g{n}_attr", t, f"g{n}_col")] * bool(
                gsis
            )
            tables[t] = ddb_table(
                name,
                t,
                pk=pk[0],
                sort_key=ddb_key(pk[1]) if len(pk) > 1 else None,
                attributes=attrs,
                gsis=gsis,
            )
            if collections and owner.get(t) == n:
                label = short.upper()
                tables[t] = ddb_table(
                    name,
                    t,
                    pk=pk[0],
                    aggregate_pattern="item_collection",
                    sort_key=ddb_key("sk"),
                    attributes=None,
                    entities=[
                        {
                            "entity_type": label,
                            "source_table": t,
                            "pk_template": "{" + pk[0] + "}",
                            "sk_template": label + "".join("#{" + c + "}" for c in pk[1:]),
                            "attributes": [ddb_attr(c, t) for c in pk],
                        }
                    ],
                )
        for i, q in enumerate(data["collector_output"]["queries"]["query_patterns"]):
            primary = next((t for t in q.get("tables_accessed") or [] if t in tables), None)
            if primary is None:
                continue
            table = tables[primary]
            gsi = table["gsis"][0]["gsi_name"] if table["gsis"] else None
            patterns.append(
                ddb_pattern(
                    f"DDB-AP-{i + 1}",
                    table["table_name"],
                    primary,
                    q["query_id"],
                    gsi_name=gsi,
                    operation="Query" if gsi else "GetItem",
                )
            )
        drafts.append(ddb_design(list(tables.values()), patterns))
    return drafts


@pytest.mark.parametrize("collections", [False, True], ids=["single-owners", "collection-owners"])
@pytest.mark.parametrize("sample", ["trailgear", "discourse"])
def test_drafts_written_to_the_rule_merge_onto_their_owners(
    sample: str, collections: bool, tmp_path_factory
) -> None:
    from src.agents.schema_design.handler import run_schema_split

    job_id = f"job-split-{sample}"
    cached = cached_run(
        tmp_path_factory,
        f"pipeline-{sample}-{job_id}",
        lambda d: run_pipeline(sample, d / "artifacts", job_id),
    )
    root = tmp_path_factory.mktemp(f"merge-{sample}-{collections}") / "artifacts"
    shutil.copytree(cached / "artifacts", root)
    store = LocalArtifactStore(base_dir=str(root))
    version = _latest_assignment(root / sample / job_id)
    run_schema_split(job_id, sample, "dynamodb", store, assignment_version=version)
    base = f"{sample}/{job_id}/schema-dynamodb/v{version}"
    manifest = store.read_json(f"{base}/groups_manifest.json")
    assert any(g.get("context_tables") for g in manifest["groups"])
    inputs = {
        g["group_index"]: store.read_json(f"{base}/{g['input_file']}") for g in manifest["groups"]
    }
    for n, draft in zip(sorted(inputs), _rule_drafts(inputs, manifest, collections), strict=True):
        store.write_json(f"{base}/schema_draft_group_{n}.json", draft)

    merged = merge_schema_groups(job_id, sample, "dynamodb", store, schema_version=version)

    assert dangling_references(merged) == []
    names = defaultdict(list)
    for t in merged["table_definitions"]:
        names[t["table_name"]].append(t)
    # Every placeholder folded: only the owners' tables are left, each once.
    assert sorted(names) == sorted(
        f"Owned_{t.split('.')[-1]}" for g in manifest["groups"] for t in g["primary_tables"]
    )
    assert merged["validation_passed"] is True, merged.get("validation_failures")
    # Each context group's GSI lives on its owner's table.
    gsis = {g["gsi_name"] for t in merged["table_definitions"] for g in t.get("gsis") or []}
    for g in manifest["groups"]:
        for t in g.get("context_tables") or []:
            assert f"{t.split('.')[-1]}_g{g['group_index']}" in gsis, (t, g["group_index"])
    json.dumps(merged)  # serialisable


def test_an_owner_key_unsafe_for_a_context_write_fails_the_merge() -> None:
    """The context group writes on its own key with a condition: the owner's key would
    make that condition check another item, so the merge keeps no other key: it fails."""
    table = ddb_table(
        "wp_posts",
        POSTS,
        pk="post_author",
        attributes=[ddb_attr("post_author", POSTS)],
    )
    write = ddb_pattern(
        "DDB-AP-1",
        "wp_posts",
        POSTS,
        "w1",
        operation="PutItem",
        condition_expression="attribute_not_exists(post_author)",
    )
    merged = merge_group_drafts(
        [_owner_draft(), ddb_design([table], [write])],
        "dynamodb",
        group_indices=[0, 1],
        unique_keys=UNIQUE,
        owners={"wp_posts": 0},
    )
    assert merged["validation_passed"] is False
    reasons = [f for f in merged["validation_failures"] if "owner group 0" in f]
    assert reasons and "conditional write" in reasons[0] and "post_author" in reasons[0], merged[
        "validation_failures"
    ]


def test_the_command_and_the_bedrock_prompt_state_the_same_rule() -> None:
    from src.agents.schema_design.group_input import CONTEXT_TABLE_RULE
    from src.tools.schema.dynamodb_schema_agent import context_tables_prompt

    command = Path(__file__).resolve().parents[4] / ".claude/commands/design-schema-dynamodb.md"
    assert f"- Context tables: {CONTEXT_TABLE_RULE}\n" in command.read_text(encoding="utf-8")
    prompt = context_tables_prompt([{"table": "db.t", "owner_group": 3}])
    # The rule is an instruction, before the framed (data) table list.
    assert CONTEXT_TABLE_RULE in prompt
    assert prompt.index(CONTEXT_TABLE_RULE) < prompt.index("db.t")


def _writing_placeholder() -> dict:
    """A placeholder whose group writes the post: a PutItem on the placeholder's key
    with the columns the group sets (post_status among them)."""
    table = ddb_table(
        "wp_posts",
        POSTS,
        pk="ID",
        attributes=[
            ddb_attr("ID", POSTS),
            ddb_attr("post_author", POSTS),
            ddb_attr("post_status", POSTS),
        ],
    )
    write = ddb_pattern("DDB-AP-1", "wp_posts", POSTS, "w1", operation="PutItem")
    return ddb_design([table], [write])


def _post_entity(sk_template: str) -> dict:
    return {
        "entity_type": "POST",
        "source_table": POSTS,
        "pk_template": "{post_id}",
        "sk_template": sk_template,
        "attributes": [ddb_attr("post_id", POSTS, "ID"), ddb_attr("post_title", POSTS)],
    }


def _folded(sk_template: str) -> tuple[dict, dict]:
    merged = merge_group_drafts(
        [_content_draft(_post_entity(sk_template)), _writing_placeholder()],
        "dynamodb",
        group_indices=[0, 1],
        unique_keys=UNIQUE,
        owners=OWNS_POSTS,
    )
    assert merged["validation_passed"] is True, merged["validation_failures"]
    assert [t["table_name"] for t in merged["table_definitions"]] == ["Content"]
    post = next(e for e in merged["table_definitions"][0]["entities"] if e["entity_type"] == "POST")
    return merged, post


def _overwrite_trade_offs(merged: dict) -> list[dict]:
    return [t for t in merged["trade_offs"] if "union of their attributes" in str(t.get("impact"))]


def test_a_folded_placeholder_write_keeps_its_attributes_and_warns_of_overwrites() -> None:
    """The owner's POST entity (SK POST#{post_id}) only this fold can take: the
    placeholder's attributes join the entity, and its PutItem moving onto the owner's
    item carries the overwrite trade-off."""
    merged, post = _folded("POST#{post_id}")
    assert {"post_author", "post_status"} <= {a["name"] for a in post["attributes"]}
    w1 = next(ap for ap in merged["access_patterns"] if ap["query_ids"] == ["w1"])
    assert w1["table_name"] == "Content"
    trade_offs = _overwrite_trade_offs(merged)
    assert len(trade_offs) == 1 and trade_offs[0]["query_ids"] == ["w1"]
    assert trade_offs[0]["target_tables"] == ["Content"]


def test_the_owner_fold_and_rule_4_carry_the_same_attributes_and_trade_off() -> None:
    """The same placeholder folded by rule 4 (static SK POST) and by the owner fold
    (SK POST#{post_id}): the entity gets the same attributes and the same trade-off."""
    by_rule_4, post_4 = _folded("POST")
    by_owner, post_owner = _folded("POST#{post_id}")

    def names(entity: dict) -> set[str]:
        return {a["name"] for a in entity["attributes"]}

    assert names(post_4) == names(post_owner)
    (t4,), (to,) = _overwrite_trade_offs(by_rule_4), _overwrite_trade_offs(by_owner)
    for key in ("impact", "source_tables", "target_tables", "query_ids", "engine"):
        assert t4[key] == to[key], key


def test_a_placeholder_gsi_key_meaning_another_entity_column_fails_and_says_why() -> None:
    """The owner's POST entity uses ``post_author`` for ``post_title``: the placeholder's
    GSI on ``post_author`` (the author column) cannot join it. (SK ``POST#{post_id}``: the
    owner fold takes it, not rule 4.)"""
    post = _post_entity("POST#{post_id}")
    post["attributes"].append(ddb_attr("post_author", POSTS, "post_title"))
    merged = merge_group_drafts(
        [_content_draft(post), _placeholder_draft()],
        "dynamodb",
        group_indices=[0, 1],
        unique_keys=UNIQUE,
        owners=OWNS_POSTS,
    )
    assert merged["validation_passed"] is False
    reasons = [f for f in merged["validation_failures"] if "another column" in f]
    assert reasons and "PostsByAuthor" in reasons[0] and "post_author" in reasons[0], merged[
        "validation_failures"
    ]
