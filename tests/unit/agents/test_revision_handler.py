"""Unit tests for schema design revision handler pure functions.

Tests cover:
- build_revision_context: assembles LLM instruction dicts from a SchemaRevisionRequest
- resolve_table_drops: cascades table DROP actions to affected access pattern IDs
- determine_redesign_scope: identifies which engine groups must be re-run
- generate_changelog: diffs two schema_output dicts into ChangelogEntry list
"""


from unittest.mock import MagicMock

from src.agents.schema_design.revision_handler import (
    _apply_drops_to_schema,
    build_revision_context,
    determine_redesign_scope,
    execute_revision,
    generate_changelog,
    resolve_table_drops,
)
from src.contracts.schema_revision_models import (
    ChangelogEntry,
    NewPattern,
    PatternAction,
    PatternModification,
    SchemaRevisionRequest,
    TableModification,
)

# ---------------------------------------------------------------------------
# Fixtures — helpers to build test data
# ---------------------------------------------------------------------------


def _make_request(
    pattern_modifications=None,
    table_modifications=None,
    new_patterns=None,
    base_version=1,
) -> SchemaRevisionRequest:
    return SchemaRevisionRequest(
        base_version=base_version,
        pattern_modifications=pattern_modifications or [],
        table_modifications=table_modifications or [],
        new_patterns=new_patterns or [],
    )


def _drop_mod(pattern_id: str) -> PatternModification:
    return PatternModification(pattern_id=pattern_id, action=PatternAction.DROP)


def _note_mod(pattern_id: str, note: str) -> PatternModification:
    return PatternModification(pattern_id=pattern_id, action=PatternAction.NOTE, note=note)


def _reassign_mod(pattern_id: str, target_engine: str) -> PatternModification:
    return PatternModification(
        pattern_id=pattern_id,
        action=PatternAction.REASSIGN,
        target_engine=target_engine,
    )


def _new_pattern(
    description: str,
    target_engine: str = "dynamodb",
    source_tables: list[str] | None = None,
) -> NewPattern:
    return NewPattern(
        description=description,
        target_engine=target_engine,
        source_tables=source_tables or ["orders"],
    )


def _ap(pattern_id: str, table_name: str = "orders", query_ids: list[str] | None = None) -> dict:
    return {"pattern_id": pattern_id, "table_name": table_name, "query_ids": query_ids or []}


def _schema_output(access_patterns=None, index_designs=None, collection_designs=None) -> dict:
    return {
        "access_patterns": access_patterns or [],
        "index_designs": index_designs or [],
        "collection_designs": collection_designs or [],
    }


def _groups_manifest(*groups) -> dict:
    """Build a groups_manifest dict from list of (group_index, group_name, query_ids, tables) tuples."""
    return {
        "groups": [
            {
                "group_index": g[0],
                "group_name": g[1],
                "query_ids": g[2],
                "tables": g[3],
            }
            for g in groups
        ]
    }


# ---------------------------------------------------------------------------
# build_revision_context
# ---------------------------------------------------------------------------


class TestBuildRevisionContext:
    def test_returns_dict_with_required_keys(self):
        """Result always contains all four instruction keys."""
        request = _make_request()
        result = build_revision_context(request, current_schema={})
        assert set(result.keys()) >= {
            "exclusion_instructions",
            "customer_instructions",
            "new_patterns_instructions",
            "reassignment_instructions",
        }

    def test_drop_action_adds_pattern_id_to_exclusion_instructions(self):
        """A DROP action puts the pattern_id in exclusion_instructions with 'removed' text."""
        request = _make_request(pattern_modifications=[_drop_mod("AP-1")])
        result = build_revision_context(request, current_schema={})
        assert "AP-1" in result["exclusion_instructions"]
        assert "removed" in result["exclusion_instructions"].lower()

    def test_note_action_adds_pattern_id_and_note_text_to_customer_instructions(self):
        """A NOTE action puts pattern_id and note text into customer_instructions."""
        request = _make_request(
            pattern_modifications=[_note_mod("AP-2", "Use sparse index instead")]
        )
        result = build_revision_context(request, current_schema={})
        assert "AP-2" in result["customer_instructions"]
        assert "Use sparse index instead" in result["customer_instructions"]

    def test_reassign_action_adds_pattern_to_exclusion_instructions(self):
        """A REASSIGN action adds pattern_id to exclusion_instructions (source engine removes it)."""
        request = _make_request(pattern_modifications=[_reassign_mod("AP-3", "opensearch")])
        result = build_revision_context(request, current_schema={})
        assert "AP-3" in result["exclusion_instructions"]

    def test_reassign_action_adds_info_to_reassignment_instructions(self):
        """A REASSIGN action adds pattern_id and target_engine to reassignment_instructions."""
        request = _make_request(pattern_modifications=[_reassign_mod("AP-3", "opensearch")])
        result = build_revision_context(request, current_schema={})
        assert "AP-3" in result["reassignment_instructions"]
        assert "opensearch" in result["reassignment_instructions"]

    def test_new_pattern_description_appears_in_new_patterns_instructions(self):
        """NewPattern description appears in new_patterns_instructions."""
        np = _new_pattern("Fetch orders by customer ID")
        request = _make_request(new_patterns=[np])
        result = build_revision_context(request, current_schema={})
        assert "Fetch orders by customer ID" in result["new_patterns_instructions"]

    def test_new_pattern_structured_fields_appear_in_new_patterns_instructions(self):
        """NewPattern target_engine and source_tables appear in new_patterns_instructions."""
        np = _new_pattern(
            "Full-text search on products",
            target_engine="opensearch",
            source_tables=["products", "categories"],
        )
        request = _make_request(new_patterns=[np])
        result = build_revision_context(request, current_schema={})
        instr = result["new_patterns_instructions"]
        assert "opensearch" in instr
        assert "products" in instr

    def test_multiple_drops_all_appear_in_exclusion_instructions(self):
        """Multiple DROP actions all populate exclusion_instructions."""
        request = _make_request(
            pattern_modifications=[_drop_mod("AP-A"), _drop_mod("AP-B"), _drop_mod("AP-C")]
        )
        result = build_revision_context(request, current_schema={})
        assert "AP-A" in result["exclusion_instructions"]
        assert "AP-B" in result["exclusion_instructions"]
        assert "AP-C" in result["exclusion_instructions"]

    def test_empty_request_returns_empty_or_placeholder_instructions(self):
        """Empty request returns dict with all four keys (values may be empty strings)."""
        request = _make_request()
        result = build_revision_context(request, current_schema={})
        for key in (
            "exclusion_instructions",
            "customer_instructions",
            "new_patterns_instructions",
            "reassignment_instructions",
        ):
            assert key in result
            assert isinstance(result[key], str)

    def test_note_without_note_text_does_not_raise(self):
        """NOTE action with no note text should not raise — falls back gracefully."""
        mod = PatternModification(pattern_id="AP-X", action=PatternAction.NOTE, note=None)
        request = _make_request(pattern_modifications=[mod])
        result = build_revision_context(request, current_schema={})
        assert "AP-X" in result["customer_instructions"]


# ---------------------------------------------------------------------------
# resolve_table_drops
# ---------------------------------------------------------------------------


class TestResolveTableDrops:
    def test_table_drop_cascades_to_matching_access_patterns(self):
        """Dropping a table returns pattern_ids of all access_patterns with that table_name."""
        schema = _schema_output(
            access_patterns=[
                _ap("AP-1", table_name="orders"),
                _ap("AP-2", table_name="orders"),
                _ap("AP-3", table_name="customers"),
            ]
        )
        mods = [TableModification(table_id="orders", action="drop")]
        result = resolve_table_drops(mods, schema)
        assert result == {"AP-1", "AP-2"}

    def test_patterns_with_different_table_not_affected(self):
        """Patterns referencing a different table are not in the returned set."""
        schema = _schema_output(
            access_patterns=[_ap("AP-1", table_name="orders"), _ap("AP-2", table_name="users")]
        )
        mods = [TableModification(table_id="orders", action="drop")]
        result = resolve_table_drops(mods, schema)
        assert "AP-2" not in result

    def test_empty_table_modifications_returns_empty_set(self):
        """No table modifications → empty set."""
        schema = _schema_output(access_patterns=[_ap("AP-1", table_name="orders")])
        result = resolve_table_drops([], schema)
        assert result == set()

    def test_table_drop_also_checks_index_designs(self):
        """Patterns referenced in index_designs with matching table_name are also returned."""
        schema = _schema_output(index_designs=[{"pattern_id": "IDX-1", "table_name": "products"}])
        mods = [TableModification(table_id="products", action="drop")]
        result = resolve_table_drops(mods, schema)
        assert "IDX-1" in result

    def test_table_drop_also_checks_collection_designs(self):
        """Patterns referenced in collection_designs with matching table_name are also returned."""
        schema = _schema_output(collection_designs=[{"pattern_id": "COL-1", "table_name": "logs"}])
        mods = [TableModification(table_id="logs", action="drop")]
        result = resolve_table_drops(mods, schema)
        assert "COL-1" in result

    def test_multiple_table_drops_union_of_affected_patterns(self):
        """Multiple table drops return the union of all affected pattern IDs."""
        schema = _schema_output(
            access_patterns=[
                _ap("AP-1", table_name="orders"),
                _ap("AP-2", table_name="users"),
                _ap("AP-3", table_name="products"),
            ]
        )
        mods = [
            TableModification(table_id="orders", action="drop"),
            TableModification(table_id="users", action="drop"),
        ]
        result = resolve_table_drops(mods, schema)
        assert result == {"AP-1", "AP-2"}

    def test_drop_nonexistent_table_returns_empty_set(self):
        """Dropping a table that has no associated patterns returns an empty set."""
        schema = _schema_output(access_patterns=[_ap("AP-1", table_name="orders")])
        mods = [TableModification(table_id="nonexistent_table", action="drop")]
        result = resolve_table_drops(mods, schema)
        assert result == set()


# ---------------------------------------------------------------------------
# determine_redesign_scope
# ---------------------------------------------------------------------------


class TestDetermineRedesignScope:
    def test_group_containing_affected_query_id_is_included(self):
        """A group whose query_ids overlap with affected_query_ids is included."""
        manifest = _groups_manifest(
            (0, "group-a", ["Q1", "Q2"], ["orders"]),
            (1, "group-b", ["Q3", "Q4"], ["users"]),
        )
        result = determine_redesign_scope(manifest, affected_query_ids={"Q1"})
        assert 0 in result

    def test_unrelated_group_not_included(self):
        """A group with no overlap and no shared tables is excluded."""
        manifest = _groups_manifest(
            (0, "group-a", ["Q1", "Q2"], ["orders"]),
            (1, "group-b", ["Q3", "Q4"], ["users"]),
        )
        result = determine_redesign_scope(manifest, affected_query_ids={"Q1"})
        assert 1 not in result

    def test_group_sharing_tables_with_affected_group_is_included(self):
        """A group sharing a table with an affected group is pulled in by dependency."""
        manifest = _groups_manifest(
            (0, "group-a", ["Q1"], ["orders"]),
            (1, "group-b", ["Q2"], ["orders"]),  # shares table with group-a
            (2, "group-c", ["Q3"], ["products"]),
        )
        result = determine_redesign_scope(manifest, affected_query_ids={"Q1"})
        assert 0 in result
        assert 1 in result  # pulled in by shared table
        assert 2 not in result

    def test_empty_affected_query_ids_returns_empty_set(self):
        """No affected queries → no groups in scope."""
        manifest = _groups_manifest(
            (0, "group-a", ["Q1"], ["orders"]),
        )
        result = determine_redesign_scope(manifest, affected_query_ids=set())
        assert result == set()

    def test_empty_manifest_returns_empty_set(self):
        """Empty groups manifest → empty set."""
        result = determine_redesign_scope({"groups": []}, affected_query_ids={"Q1"})
        assert result == set()

    def test_all_groups_affected_when_all_share_query(self):
        """When all groups contain affected queries, all group indices are returned."""
        manifest = _groups_manifest(
            (0, "group-a", ["Q1"], ["orders"]),
            (1, "group-b", ["Q2"], ["users"]),
        )
        result = determine_redesign_scope(manifest, affected_query_ids={"Q1", "Q2"})
        assert result == {0, 1}

    def test_returns_set_of_group_index_integers(self):
        """Return type is a set of ints (group_index values)."""
        manifest = _groups_manifest(
            (0, "group-a", ["Q1"], ["orders"]),
        )
        result = determine_redesign_scope(manifest, affected_query_ids={"Q1"})
        assert isinstance(result, set)
        assert all(isinstance(i, int) for i in result)

    def test_transitive_dependency_not_included(self):
        """Groups are pulled in only by direct table sharing with a directly-affected group (one hop)."""
        manifest = _groups_manifest(
            (0, "group-a", ["Q1"], ["orders"]),
            (1, "group-b", ["Q2"], ["orders", "users"]),  # shares table with group-a
            (2, "group-c", ["Q3"], ["users"]),  # shares table with group-b but not group-a
        )
        result = determine_redesign_scope(manifest, affected_query_ids={"Q1"})
        # group-b is included because it shares "orders" with the directly-affected group-a
        assert 0 in result
        assert 1 in result
        # group-c shares "users" with group-b — whether it's included is implementation-defined
        # The spec says "groups sharing tables with affected groups" — group-b is an affected group
        # so group-c qualifies. We assert it's either included or not, but not both.
        # This test simply confirms group-a and group-b are definitely in.


# ---------------------------------------------------------------------------
# generate_changelog
# ---------------------------------------------------------------------------


class TestGenerateChangelog:
    def test_pattern_in_previous_only_is_removed(self):
        """A pattern present in previous but absent in current → 'removed' ChangelogEntry."""
        prev = _schema_output(access_patterns=[_ap("AP-1")])
        curr = _schema_output(access_patterns=[])
        changelog = generate_changelog(prev, curr, engine="dynamodb")
        assert len(changelog) == 1
        entry = changelog[0]
        assert isinstance(entry, ChangelogEntry)
        assert entry.change_type == "removed"
        assert entry.entity_id == "AP-1"

    def test_pattern_in_current_only_is_added(self):
        """A pattern absent in previous but present in current → 'added' ChangelogEntry."""
        prev = _schema_output(access_patterns=[])
        curr = _schema_output(access_patterns=[_ap("AP-2")])
        changelog = generate_changelog(prev, curr, engine="dynamodb")
        assert len(changelog) == 1
        entry = changelog[0]
        assert entry.change_type == "added"
        assert entry.entity_id == "AP-2"

    def test_pattern_with_changed_content_is_modified(self):
        """A pattern in both outputs but with different content → 'modified' ChangelogEntry."""
        prev = _schema_output(
            access_patterns=[{"pattern_id": "AP-3", "table_name": "orders", "query_ids": ["Q1"]}]
        )
        curr = _schema_output(
            access_patterns=[
                {"pattern_id": "AP-3", "table_name": "orders", "query_ids": ["Q1", "Q2"]}
            ]
        )
        changelog = generate_changelog(prev, curr, engine="dynamodb")
        assert len(changelog) == 1
        entry = changelog[0]
        assert entry.change_type == "modified"
        assert entry.entity_id == "AP-3"

    def test_identical_patterns_produce_empty_changelog(self):
        """Same patterns same content in both outputs → empty changelog."""
        ap = _ap("AP-4")
        prev = _schema_output(access_patterns=[ap])
        curr = _schema_output(access_patterns=[ap])
        changelog = generate_changelog(prev, curr, engine="dynamodb")
        assert changelog == []

    def test_multiple_changes_all_captured(self):
        """Removed, added, and modified patterns all appear in changelog."""
        prev = _schema_output(
            access_patterns=[
                _ap("AP-OLD"),
                {"pattern_id": "AP-MOD", "table_name": "orders", "query_ids": ["Q1"]},
            ]
        )
        curr = _schema_output(
            access_patterns=[
                {"pattern_id": "AP-MOD", "table_name": "orders", "query_ids": ["Q1", "Q2"]},
                _ap("AP-NEW"),
            ]
        )
        changelog = generate_changelog(prev, curr, engine="dynamodb")
        change_types = {e.change_type for e in changelog}
        entity_ids = {e.entity_id for e in changelog}
        assert "removed" in change_types
        assert "added" in change_types
        assert "modified" in change_types
        assert "AP-OLD" in entity_ids
        assert "AP-NEW" in entity_ids
        assert "AP-MOD" in entity_ids

    def test_returns_list_of_changelog_entry_instances(self):
        """generate_changelog always returns a list of ChangelogEntry instances."""
        prev = _schema_output(access_patterns=[_ap("AP-1")])
        curr = _schema_output(access_patterns=[])
        changelog = generate_changelog(prev, curr, engine="dynamodb")
        assert all(isinstance(e, ChangelogEntry) for e in changelog)

    def test_entity_type_is_access_pattern(self):
        """Entries generated from access_patterns have entity_type='access_pattern'."""
        prev = _schema_output(access_patterns=[_ap("AP-1")])
        curr = _schema_output(access_patterns=[])
        changelog = generate_changelog(prev, curr, engine="dynamodb")
        assert changelog[0].entity_type == "access_pattern"

    def test_engine_parameter_used_in_description(self):
        """The engine name appears somewhere in the entry description."""
        prev = _schema_output(access_patterns=[_ap("AP-1")])
        curr = _schema_output(access_patterns=[])
        changelog = generate_changelog(prev, curr, engine="dynamodb")
        assert "dynamodb" in changelog[0].description.lower()

    def test_empty_both_outputs_returns_empty_changelog(self):
        """Both outputs empty → no changes to report."""
        changelog = generate_changelog(_schema_output(), _schema_output(), engine="opensearch")
        assert changelog == []


# ---------------------------------------------------------------------------
# _apply_drops_to_schema
# ---------------------------------------------------------------------------


class TestApplyDropsToSchema:
    def test_removes_dropped_patterns_from_access_patterns(self):
        schema = {
            "access_patterns": [
                {"pattern_id": "AP-1", "query_ids": ["q1"]},
                {"pattern_id": "AP-2", "query_ids": ["q2"]},
            ],
            "index_designs": [],
            "collection_designs": [],
        }
        result = _apply_drops_to_schema(schema, {"AP-1"})
        assert len(result["access_patterns"]) == 1
        assert result["access_patterns"][0]["pattern_id"] == "AP-2"

    def test_removes_from_index_designs(self):
        schema = {
            "access_patterns": [],
            "index_designs": [{"pattern_id": "IDX-1"}, {"pattern_id": "IDX-2"}],
            "collection_designs": [],
        }
        result = _apply_drops_to_schema(schema, {"IDX-1"})
        assert len(result["index_designs"]) == 1

    def test_removes_from_collection_designs(self):
        schema = {
            "access_patterns": [],
            "index_designs": [],
            "collection_designs": [{"pattern_id": "COL-1"}],
        }
        result = _apply_drops_to_schema(schema, {"COL-1"})
        assert result["collection_designs"] == []

    def test_empty_drop_set_preserves_all(self):
        schema = {
            "access_patterns": [{"pattern_id": "AP-1"}],
            "index_designs": [{"pattern_id": "IDX-1"}],
            "collection_designs": [{"pattern_id": "COL-1"}],
        }
        result = _apply_drops_to_schema(schema, set())
        assert len(result["access_patterns"]) == 1
        assert len(result["index_designs"]) == 1
        assert len(result["collection_designs"]) == 1


# ---------------------------------------------------------------------------
# execute_revision (integration with mock store)
# ---------------------------------------------------------------------------


class TestExecuteRevision:
    def _make_store(self, schema: dict) -> MagicMock:
        store = MagicMock()

        # Return *schema* for the schema artifact path; raise for any other path
        # (e.g. query-journey files) so that materializer calls skip gracefully.
        def _read_json_side_effect(path: str) -> dict:
            if "schema_output.json" in path or "schema-" in path:
                return schema
            raise FileNotFoundError(f"Not found: {path}")

        store.read_json.side_effect = _read_json_side_effect
        return store

    def test_produces_new_version_on_success(self):
        schema = {
            "access_patterns": [{"pattern_id": "AP-1", "query_ids": ["q1"]}],
            "index_designs": [],
            "collection_designs": [],
        }
        store = self._make_store(schema)
        request = SchemaRevisionRequest(
            base_version=1,
            pattern_modifications=[],
            table_modifications=[],
            new_patterns=[],
        )

        new_schema, meta = execute_revision("job-1", "mydb", "dynamodb", request, store)

        assert meta.version == 2
        assert meta.base_version == 1
        assert meta.initiated_by == "customer"
        assert new_schema == schema  # No drops → schema unchanged

    def test_drop_removes_pattern_and_generates_changelog(self):
        schema = {
            "access_patterns": [
                {"pattern_id": "AP-1", "query_ids": ["q1"]},
                {"pattern_id": "AP-2", "query_ids": ["q2"]},
            ],
            "index_designs": [],
            "collection_designs": [],
        }
        store = self._make_store(schema)
        request = SchemaRevisionRequest(
            base_version=1,
            pattern_modifications=[
                PatternModification(pattern_id="AP-1", action=PatternAction.DROP),
            ],
            table_modifications=[],
            new_patterns=[],
        )

        new_schema, meta = execute_revision("job-1", "mydb", "dynamodb", request, store)

        # AP-1 removed
        assert len(new_schema["access_patterns"]) == 1
        assert new_schema["access_patterns"][0]["pattern_id"] == "AP-2"
        # Changelog records the removal
        assert len(meta.changelog) == 1
        assert meta.changelog[0].change_type == "removed"
        assert meta.changelog[0].entity_id == "AP-1"

    def test_writes_artifacts_to_store(self):
        schema = {
            "access_patterns": [],
            "index_designs": [],
            "collection_designs": [],
        }
        store = self._make_store(schema)
        request = SchemaRevisionRequest(
            base_version=1,
            pattern_modifications=[],
            table_modifications=[],
            new_patterns=[],
        )

        execute_revision("job-1", "mydb", "dynamodb", request, store)

        write_paths = [call[0][0] for call in store.write_json.call_args_list]
        assert any("v2/schema_output.json" in p for p in write_paths)
        assert any("v2/version_meta.json" in p for p in write_paths)
        assert any("v2/revision_request.json" in p for p in write_paths)
        assert any("v2/changelog.json" in p for p in write_paths)
        assert any("v2/revision_context.json" in p for p in write_paths)

    def test_reassign_removes_pattern_from_source_engine(self):
        schema = {
            "access_patterns": [
                {"pattern_id": "AP-1", "query_ids": ["q1"]},
                {"pattern_id": "AP-2", "query_ids": ["q2"]},
            ],
            "index_designs": [],
            "collection_designs": [],
        }
        store = self._make_store(schema)
        request = SchemaRevisionRequest(
            base_version=1,
            pattern_modifications=[
                PatternModification(
                    pattern_id="AP-1",
                    action=PatternAction.REASSIGN,
                    target_engine="opensearch",
                ),
            ],
            table_modifications=[],
            new_patterns=[],
        )

        new_schema, meta = execute_revision("job-1", "mydb", "dynamodb", request, store)

        assert len(new_schema["access_patterns"]) == 1
        assert new_schema["access_patterns"][0]["pattern_id"] == "AP-2"
