"""A key anchor is also the key of other access patterns on the table (#1061).

The #728 key-column rule (``access_pattern.TableKeys.key_columns``) counts a primary
key, a foreign key and an index's leading column. The maintainer's direction on #915
adds "the key of other access patterns on the table": a column that at least two
distinct access patterns look the table up by (each pins it to a value and pins none of
the table's own keys; copies of one access pattern count once, ``TestDistinctLookups``,
#1104) is a key too, unless it is a date, time or true/false column or the statistics
show it low-cardinality. It is part of the same definition, so a read and a write by
that column are anchored alike: the read is a key-scoped 1:N read, the write a
key-scoped multi-item write whose trace says the column must become a partition key
or a GSI's.

The last tests guard the "one definition" rule: no module but ``access_pattern`` builds
a table's keys, and the write-shape module reads them only through ``TableKeys``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.agents.referee.access_pattern import (
    AD_HOC_FILTER,
    KEY_SCOPED,
    WORKLOAD_KEY_LOOKUPS,
    SchemaKeys,
    classify_query,
)
from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.transaction_shape import KEY_SCOPED as WRITE_KEY_SCOPED
from src.agents.referee.transaction_shape import UNBOUNDED, transaction_shape

_REPO = Path(__file__).resolve().parents[4]
_ENGINE = "postgresql"


def _table(name: str, columns: dict[str, str], **extra: object) -> dict:
    return {
        "table_id": f"app.{name}",
        "table_name": name,
        "schema_name": "app",
        "row_count": 50_000,
        "size_mb": 10.0,
        "primary_key": ["id"],
        "columns": [{"column_name": c, "data_type": t} for c, t in columns.items()],
        "indexes": [],
        "foreign_keys": [],
        **extra,
    }


CLAIMS = _table(
    "claims",
    {
        "id": "bigint",
        "user_id": "integer",
        "topic_id": "integer",
        "flagged": "boolean",
        "claimed_at": "timestamp",
        "status": "integer",
    },
    indexes=[{"index_name": "ix_topic", "columns": ["topic_id"], "is_unique": False}],
)
NOTES = _table("notes", {"id": "bigint", "user_id": "integer", "body": "text"})


def _query(qid: str, text: str, query_type: str = "SELECT", table: str = "claims") -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": query_type,
        "tables_accessed": [f"app.{table}"],
        "calls_per_second": 1.0,
    }


def _collector(*queries: dict, tables: tuple[dict, ...] = (CLAIMS, NOTES)) -> dict:
    return {
        "metadata": {"source_database": {"engine": _ENGINE, "database_name": "app"}},
        "database_schema": {"tables": list(tables)},
        "queries": {"query_patterns": list(queries)},
    }


DELETE_BY_USER = _query("del", "DELETE FROM claims WHERE user_id = $1", "DELETE")
READ_BY_USER = _query("read", "SELECT id, topic_id FROM claims WHERE user_id = $1")


def _keys(*queries: dict, tables: tuple[dict, ...] = (CLAIMS, NOTES)) -> SchemaKeys:
    return SchemaKeys.from_collector(_collector(*queries, tables=tables))


def _write_shape(query: dict, *workload: dict):
    collector = _collector(query, *workload)
    shape = transaction_shape(
        query, SourceScope.from_collector(collector), SchemaKeys.from_collector(collector)
    )
    assert shape is not None
    return shape


class TestTheKeyOfOtherAccessPatterns:
    def test_a_column_other_queries_look_the_table_up_by_is_a_key(self) -> None:
        keys = _keys(DELETE_BY_USER, READ_BY_USER).tables["app.claims"]
        assert "user_id" in keys.key_columns
        assert keys.workload_keys == frozenset({"user_id"})
        assert keys.lookup_queries("user_id") == ("del", "read")
        assert keys.lookup_queries("topic_id") == ()

    def test_the_read_and_the_write_are_anchored_alike(self) -> None:
        keys = _keys(DELETE_BY_USER, READ_BY_USER)
        read = classify_query(READ_BY_USER, keys, _ENGINE)
        assert read.name == KEY_SCOPED
        assert "other queries look the table up by it" in read.reason
        write = _write_shape(DELETE_BY_USER, READ_BY_USER)
        assert write.shape == WRITE_KEY_SCOPED
        assert write.schema_note == (
            "claims.user_id must become the partition key of an item collection "
            "(or a GSI's) so this write reads its items by key"
        )

    def test_one_query_alone_is_not_another_access_pattern(self) -> None:
        assert WORKLOAD_KEY_LOOKUPS == 2
        keys = _keys(DELETE_BY_USER)
        assert "user_id" not in keys.tables["app.claims"].key_columns
        assert classify_query(DELETE_BY_USER, keys, _ENGINE).name == AD_HOC_FILTER
        assert _write_shape(DELETE_BY_USER).shape == UNBOUNDED

    def test_a_filter_on_rows_a_key_already_addresses_is_no_lookup_by_it(self) -> None:
        """``topic_id`` (an index's leading column) anchors the read; ``user_id`` only
        filters that item collection, so it is not that access pattern's key."""
        residual = _query("res", "SELECT id FROM claims WHERE topic_id = $1 AND user_id = $2")
        assert "user_id" not in _keys(DELETE_BY_USER, residual).tables["app.claims"].key_columns

    def test_a_lookup_of_another_table_does_not_count(self) -> None:
        other = _query("notes", "SELECT body FROM notes WHERE user_id = $1", table="notes")
        keys = _keys(DELETE_BY_USER, other)
        assert "user_id" not in keys.tables["app.claims"].key_columns
        assert "user_id" not in keys.tables["app.notes"].key_columns

    def test_a_join_scope_is_no_lookup_of_one_table(self) -> None:
        join = _query(
            "join",
            "SELECT c.id FROM claims c JOIN notes n ON n.id = c.id WHERE c.user_id = $1",
        )
        assert "user_id" not in _keys(DELETE_BY_USER, join).tables["app.claims"].key_columns

    @pytest.mark.parametrize(
        "column", ["flagged", "claimed_at"], ids=["true/false", "point in time"]
    )
    def test_a_true_false_or_temporal_column_is_never_a_key(self, column: str) -> None:
        first = _query("a", f"SELECT id FROM claims WHERE {column} = $1")
        second = _query("b", f"UPDATE claims SET status = $1 WHERE {column} = $2", "UPDATE")
        assert column not in _keys(first, second).tables["app.claims"].key_columns

    def test_statistics_showing_low_cardinality_exclude_it(self) -> None:
        low = {
            **CLAIMS,
            "columns": [
                *(c for c in CLAIMS["columns"] if c["column_name"] != "status"),
                {"column_name": "status", "data_type": "integer", "cardinality": 4},
            ],
        }
        first = _query("a", "SELECT id FROM claims WHERE status = $1")
        second = _query("b", "DELETE FROM claims WHERE status = $1", "DELETE")
        assert (
            "status"
            not in _keys(first, second, tables=(low, NOTES)).tables["app.claims"].key_columns
        )
        assert "status" in _keys(first, second).tables["app.claims"].key_columns

    def test_no_workload_leaves_the_schema_keys_unchanged(self) -> None:
        keys = _keys().tables["app.claims"]
        assert keys.key_columns == frozenset({"id", "topic_id"})
        assert keys.workload_keys == frozenset()


# ---------------------------------------------------------------------------
# The count is of distinct lookups, not collected queries (#1104 review)
# ---------------------------------------------------------------------------

GROUPS = _table(
    "groups",
    {"id": "bigint", "category_id": "integer", "permission": "integer", "group_id": "integer"},
)
CATEGORIES = _table("categories", {"id": "bigint", "name": "text"})


def _permission_read(qid: str, outer: str, in_list: str, tail: str) -> dict:
    """The discourse ``category_groups.permission_type`` statements: a key read (or its
    count) of ``categories`` whose only lookup of ``groups`` is a permission subquery."""
    return _query(
        qid,
        f"SELECT {outer} FROM categories WHERE categories.id IN ("
        f"SELECT category_id FROM groups WHERE permission IN ({in_list}) "
        f"AND group_id = $9) AND categories.id {tail}",
        table="categories",
    )


ONE_STATEMENT_THREE_WAYS = (
    _permission_read("one", "$1 AS one", "$5", "!= $8 LIMIT $10"),
    _permission_read("two", "$1 AS one", "$5, $6", "= $8 LIMIT $10"),
    _permission_read("count", "COUNT(*)", "$4", "= $8"),
)


class TestDistinctLookups:
    def _keys(self, *queries: dict):
        return _keys(*queries, tables=(CLAIMS, GROUPS, CATEGORIES))

    def test_two_reads_differing_only_in_in_list_length_are_one_lookup(self) -> None:
        """Discourse's two ``category_groups.permission_type`` item reads differ only in
        ``IN ($5)`` vs ``IN ($5, $6)`` (and the outer key test): one access pattern of
        ``groups``, so no key on their own."""
        reads = ONE_STATEMENT_THREE_WAYS[:2]
        keys = self._keys(*reads).tables["app.groups"]
        assert "permission" not in keys.key_columns
        assert keys.workload_keys == frozenset()

    def test_the_count_of_the_same_lookup_is_a_second_access_pattern(self) -> None:
        """Maintainer decision on #1104: a ``COUNT(*)`` is another access pattern than the
        item read on the same predicate, so the read and its count make the key (the
        discourse ``category_groups.permission_type`` case stays a key)."""
        keys = self._keys(*ONE_STATEMENT_THREE_WAYS).tables["app.groups"]
        assert "permission" in keys.workload_keys
        assert keys.lookup_queries("permission") == ("count", "one", "two")

    def test_reads_differing_only_in_the_columns_they_return_are_one_lookup(self) -> None:
        narrow = _query("narrow", "SELECT id FROM claims WHERE user_id = $1")
        wide = _query("wide", "SELECT id, topic_id, status FROM claims WHERE user_id = $1")
        assert "user_id" not in self._keys(narrow, wide).tables["app.claims"].key_columns

    def test_the_same_statement_collected_twice_counts_once(self) -> None:
        again = {**READ_BY_USER, "query_id": "read-again"}
        keys = self._keys(READ_BY_USER, again).tables["app.claims"]
        assert "user_id" not in keys.key_columns
        assert classify_query(READ_BY_USER, self._keys(READ_BY_USER, again), _ENGINE).name == (
            AD_HOC_FILTER
        )

    def test_a_different_lookup_by_the_same_column_still_makes_a_key(self) -> None:
        """Two genuinely different statements (another predicate, an order and a limit)
        are two lookups; the copies of the first are kept as its evidence."""
        again = {**READ_BY_USER, "query_id": "read-again"}
        latest = _query(
            "latest", "SELECT id FROM claims WHERE user_id = $1 ORDER BY claimed_at DESC LIMIT $2"
        )
        keys = self._keys(READ_BY_USER, again, latest).tables["app.claims"]
        assert keys.workload_keys == frozenset({"user_id"})
        assert keys.lookup_queries("user_id") == ("latest", "read", "read-again")

    def test_a_different_statement_kind_is_a_different_lookup(self) -> None:
        """The DELETE and the SELECT pin the same column with the same predicate: the
        statement kind tells them apart (the round-1 example still holds)."""
        assert (
            "user_id" in self._keys(DELETE_BY_USER, READ_BY_USER).tables["app.claims"].key_columns
        )

    def test_the_query_classified_is_one_of_its_keys_lookups(self) -> None:
        """At the threshold exactly (two distinct lookups), the read is key scoped: it is
        one of the two, so one other lookup is enough."""
        keys = self._keys(DELETE_BY_USER, READ_BY_USER)
        assert len({"del", "read"}) == WORKLOAD_KEY_LOOKUPS
        assert classify_query(READ_BY_USER, keys, _ENGINE).name == KEY_SCOPED

    def test_copies_never_add_up_to_a_second_lookup(self) -> None:
        copies = [{**READ_BY_USER, "query_id": f"copy-{n}"} for n in range(5)]
        assert "user_id" not in self._keys(*copies).tables["app.claims"].key_columns

    def test_no_queries_make_no_workload_key(self) -> None:
        for keys in self._keys().tables.values():
            assert keys.workload_keys == frozenset()
            assert keys.workload_lookups == ()
        bare = {**_collector(tables=(CLAIMS,)), "queries": {}}
        assert SchemaKeys.from_collector(bare).tables["app.claims"].workload_keys == frozenset()


# ---------------------------------------------------------------------------
# One definition (#1061: the rejected alternative is a second, write-only rule)
# ---------------------------------------------------------------------------

_DEFINITION = "src/agents/referee/access_pattern.py"


def _python_files() -> list[Path]:
    return sorted((_REPO / "src").rglob("*.py"))


def _calls(tree: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call)]


def _name(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


_ANCHOR_FIELDS = {"key_columns", "workload_keys"}


def _key_anchor_offenders(source: str) -> list[int]:
    """Lines that build a ``TableKeys`` or assign one of its anchor fields.

    A bare ``key_columns=`` keyword is not an offence: other concepts use the name
    (the data movement load's hub topic, #1442). An assignment to a ``.key_columns``
    or ``.workload_keys`` attribute counts only in a module that names ``TableKeys``.
    """
    tree = ast.parse(source)
    lines = [c.lineno for c in _calls(tree) if _name(c) == "TableKeys"]
    names_table_keys = any(
        (isinstance(n, ast.Name) and n.id == "TableKeys")
        or (isinstance(n, ast.alias) and n.name == "TableKeys")
        for n in ast.walk(tree)
    )
    if names_table_keys:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            else:
                continue
            lines += [
                t.lineno
                for t in targets
                if isinstance(t, ast.Attribute) and t.attr in _ANCHOR_FIELDS
            ]
    return sorted(lines)


@pytest.mark.passes_on_main  # src/ has no second definition on main either
def test_only_the_access_pattern_module_builds_a_tables_keys() -> None:
    """``TableKeys`` is built, and its ``key_columns``/``workload_keys`` set, in one
    module: every other reader takes them from ``SchemaKeys.from_collector``."""
    offenders = []
    for path in _python_files():
        rel = path.relative_to(_REPO).as_posix()
        if rel == _DEFINITION:
            continue
        offenders += [
            f"{rel}:{line}" for line in _key_anchor_offenders(path.read_text(encoding="utf-8"))
        ]
    assert not offenders, f"a second key-anchor definition: {offenders}"


class TestTheKeyAnchorGuardIsNarrow:
    def test_a_new_table_keys_call_elsewhere_is_flagged(self) -> None:
        assert _key_anchor_offenders("x = TableKeys(key_columns=frozenset())\n") == [1]
        assert _key_anchor_offenders("x = mod.TableKeys()\n") == [1]

    def test_assigning_an_anchor_field_of_a_table_keys_is_flagged(self) -> None:
        source = (
            "from src.agents.referee.access_pattern import TableKeys\n"
            "def f(keys: TableKeys):\n"
            "    keys.workload_keys = frozenset()\n"
        )
        assert _key_anchor_offenders(source) == [3]

    def test_an_unrelated_dataclass_with_a_key_columns_field_passes(self) -> None:
        source = (
            "from dataclasses import dataclass\n"
            "@dataclass\n"
            "class HubTopic:\n"
            "    key_columns: tuple[str, ...]\n"
            "t = HubTopic(key_columns=('id',))\n"
            "u = other(workload_keys=1)\n"
        )
        assert _key_anchor_offenders(source) == []

    def test_an_attribute_assignment_in_a_module_without_table_keys_passes(self) -> None:
        assert _key_anchor_offenders("self.key_columns = ('id',)\n") == []


def test_the_write_shape_reads_keys_only_through_table_keys() -> None:
    """The write shape (#915, #1038) has no anchor rule of its own: it never reads a
    table's raw primary key, foreign keys or indexes from the collector."""
    path = _REPO / "src/agents/referee/transaction_shape.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    raw = {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant)
        and n.value in {"primary_key", "foreign_keys", "indexes", "database_schema"}
    }
    assert not raw, f"transaction_shape reads raw schema keys: {sorted(raw)}"
