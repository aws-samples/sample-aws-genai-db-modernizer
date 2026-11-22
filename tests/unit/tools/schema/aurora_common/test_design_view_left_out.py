# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The design view lists the source objects the draft leaves out, and the indexes it
renames (#2416).

``AuroraDesignBase.left_out`` holds each object the draft does not create: a name SQL
cannot quote, a table's second index of one name, two tables of one key, a table of a
system schema, a foreign key the design cannot point anywhere. ``renamed`` holds each
index Aurora PostgreSQL creates under another name. The model sees both, with every name
cleaned like the app-layer note, so it can re-add a left-out index under a new name
through ``add_indexes``. The merge still refuses a re-added name that clashes.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import pytest

from src.contracts.schema_design_input import (
    AgentCollectorInput,
    AgentColumn,
    AgentForeignKey,
    AgentIndex,
    AgentTable,
)
from src.shared.sql_identifiers import has_unprintable
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta
from src.tools.schema.aurora_common.design_view import build_design_view, render_request

PG, MY = "aurora_postgresql", "aurora_mysql"
BELL, ZWSP, LSEP = "\u0007", "\u200b", "\u2028"


def _collector() -> AgentCollectorInput:
    return AgentCollectorInput.model_validate(
        {
            "contract_version": "3.0.0",
            "job_id": "j",
            "source_database_name": "app",
            "source_database_engine": "mysql",
            "collection_timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "tables": [],
            "queries": {"query_patterns": []},
        }
    )


def _table(
    name: str,
    *,
    schema: str | None = None,
    columns: tuple[str, ...] = ("a", "b"),
    indexes: list[AgentIndex] | None = None,
    primary_key: list[str] | None = None,
    foreign_keys: list[AgentForeignKey] | None = None,
) -> AgentTable:
    return AgentTable(
        table_id=f"{schema or 'app'}.{name}",
        table_name=name,
        schema_name=schema,
        row_count=10,
        columns=[AgentColumn(column_name=c, data_type="int", nullable=True) for c in columns],
        indexes=indexes,
        primary_key=primary_key,
        foreign_keys=foreign_keys,
    )


def _index(name: str, columns: list[str], unique: bool = False) -> AgentIndex:
    return AgentIndex(index_name=name, columns=columns, is_unique=unique)


def _fk(name: str, table: str, schema: str | None = None) -> AgentForeignKey:
    return AgentForeignKey(constraint_name=name, columns=["a"], referenced_table=table,
                           referenced_schema=schema, referenced_columns=["a"])  # fmt: skip


def _base(engine: str, tables: list[AgentTable], source: str = "mysql") -> AuroraDesignBase:
    return AuroraDesignBase(engine=engine, job_id="j", source_database="app",
                            source_engine=source, tables=tables)  # fmt: skip


def _view(base: AuroraDesignBase) -> dict:
    return build_design_view(base, _collector())


# One case per kind of left-out object the base records: (engine, source, tables, the
# kind and feature of the entry the view lists).
CASES = {
    "unsafe table": (MY, "mysql", [_table(f"t{BELL}x")], ("table", "unsafe_name")),
    "unsafe column": (MY, "mysql", [_table("t", columns=("a", f"b{ZWSP}c"))],
                      ("column", "unsafe_name")),
    "unsafe primary key": (MY, "mysql",
                           [_table("t", columns=("a", f"b{ZWSP}c"), primary_key=[f"b{ZWSP}c"])],
                           ("primary key", "unsafe_name")),
    "unsafe index": (PG, "postgresql", [_table("t", indexes=[_index(f"ix{LSEP}a", ["a"])])],
                     ("index", "unsafe_name")),
    "duplicate index": (MY, "mysql",
                        [_table("t", indexes=[_index("ix", ["a"]), _index("ix", ["b"])])],
                        ("index", "duplicate_index_name")),
    "unsafe foreign key": (MY, "mysql",
                           [_table("t", foreign_keys=[_fk(f"fk{BELL}", "u")]), _table("u")],
                           ("foreign key", "unsafe_name")),
    "colliding table": (MY, "mysql", [_table("Orders"), _table("orders"), _table("t")],
                        ("table", "colliding_table_name")),
    "system schema": (MY, "mysql", [_table("t", schema="app"), _table("user", schema="mysql")],
                      ("table", "system_schema")),
    "other schema reference": (MY, "mysql",
                               [_table("t", schema="app", foreign_keys=[_fk("fk", "u", "crm")]),
                                _table("u", schema="app")],
                               ("foreign key", "ambiguous_reference")),
    "ambiguous reference": (PG, "postgresql",
                            [_table("t", schema="a", foreign_keys=[_fk("fk", "ref")]),
                             _table("ref", schema="b"), _table("ref", schema="c")],
                            ("foreign key", "ambiguous_reference")),
}  # fmt: skip


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
def test_the_view_lists_each_left_out_kind(case: str) -> None:
    engine, source, tables, (kind, feature) = CASES[case]
    base = _base(engine, tables, source)
    assert any(o["kind"] == kind and o.get("feature", "unsafe_name") == feature
               for o in base.left_out), "the case builds the object it names"  # fmt: skip
    view = _view(base)
    assert [(e["kind"], e["feature"]) for e in view["left_out"]] == [
        (o["kind"], o.get("feature", "unsafe_name")) for o in base.left_out
    ]
    assert (kind, feature) in {(e["kind"], e["feature"]) for e in view["left_out"]}
    for entry in view["left_out"]:
        index_keys = {"columns", "unique"} if entry["kind"] == "index" else set()
        assert set(entry) == {"table", "kind", "name", "reason", "feature", *index_keys}


def test_each_left_out_name_is_cleaned_like_the_app_layer_note() -> None:
    tables = [_table("t", columns=("a", f"b{ZWSP}c"),
                     indexes=[_index("ix", ["a"]), _index(f"ix{LSEP}{LSEP}a", ["a"])]),
              _table(f"bad{BELL}\nname")]  # fmt: skip
    view = _view(_base(PG, tables, "postgresql"))
    assert [(e["table"], e["kind"], e["name"]) for e in view["left_out"]] == [
        ("t", "column", "b c"),
        ("t", "index", "ix a"),
        ("bad name", "table", "bad name"),
    ]
    for entry in view["left_out"]:
        assert not any(has_unprintable(str(v)) for v in entry.values())
    text = render_request({"design_view": view}, paged=False)
    assert not any(ch in text for ch in (BELL, ZWSP, LSEP))


def test_a_duplicate_index_entry_names_the_index_the_table_keeps() -> None:
    tables = [_table("t", indexes=[_index("ix", ["a"]), _index("IX", ["b"])])]
    [entry] = _view(_base(MY, tables))["left_out"]
    assert entry == {
        "table": "t",
        "kind": "index",
        "name": "IX",
        "feature": "duplicate_index_name",
        "columns": ["b"],
        "unique": False,
        "reason": "the table already has an index named ix, so this one was not created; "
        "create it under another name if the queries need it",
    }


@pytest.mark.passes_on_main
def test_a_draft_with_nothing_left_out_has_no_left_out_section() -> None:
    view = _view(_base(MY, [_table("t", indexes=[_index("ix", ["a"])])]))
    assert "left_out" not in view and "renamed_indexes" not in view


def test_the_view_lists_each_renamed_index_with_its_new_name() -> None:
    tables = [_table("postmeta", indexes=[_index("meta_key", ["a"])]),
              _table("usermeta", indexes=[_index("meta_key", ["a"])])]  # fmt: skip
    base = _base(PG, tables)
    view = _view(base)
    assert view["renamed_indexes"] == [
        {
            "table": "usermeta",
            "name": "meta_key",
            "new_name": "usermeta_a_idx",
            "reason": base.renamed[0]["reason"],
        }
    ]
    assert "usermeta_a_idx (a)" in json.dumps(view["tables"][1]["indexes"])


def _duplicate_base(engine: str) -> AuroraDesignBase:
    source = "postgresql" if engine == PG else "mysql"
    tables = [_table("t", indexes=[_index("ix", ["a"]), _index("ix", ["b"])]), _table("u")]
    return _base(engine, tables, source)


def _add(table: str, name: str) -> dict:
    return {"delta_version": "1.0",
            "tables": [{"table_name": table,
                        "add_indexes": [{"index_name": name, "columns": ["b"]}]}]}  # fmt: skip


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", [PG, MY])
def test_a_left_out_index_readded_under_a_new_name_merges(engine: str) -> None:
    result = merge_design_delta(_duplicate_base(engine), _add("t", "ix_b"), strict=False)
    assert result.errors == []
    assert result.output is not None
    created = re.findall(r"^CREATE INDEX (\S+) ON (\S+)", result.output["generated_ddl"], re.M)
    assert [(n.strip('"`'), t.strip('"`')) for n, t in created] == [("ix", "t"), ("ix_b", "t")]


@pytest.mark.passes_on_main
@pytest.mark.parametrize("engine", [PG, MY])
def test_a_left_out_index_readded_under_its_clashing_name_is_refused(engine: str) -> None:
    result = merge_design_delta(_duplicate_base(engine), _add("t", "ix"), strict=False)
    assert result.errors == ["tables[0].add_indexes[0]: index 'ix' already exists on 't'"]


@pytest.mark.passes_on_main
def test_on_postgresql_a_readded_name_another_table_has_is_refused() -> None:
    result = merge_design_delta(_duplicate_base(PG), _add("u", "ix"), strict=False)
    assert result.errors == [
        "tables[0].add_indexes[0]: index name 'ix' is already used in the schema by "
        "table 't' (PostgreSQL index names are unique within a schema); choose another name"
    ]


# ---------------------------------------------------------------------------
# #2575: a left-out index's columns and uniqueness, and a cap on every cleaned text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", [PG, MY])
def test_a_left_out_index_entry_carries_its_columns_and_uniqueness(engine: str) -> None:
    """``ix`` and ``IX`` on different columns: the entry says which columns the dropped one
    covered, so the model can re-add exactly that index."""
    source = "postgresql" if engine == PG else "mysql"
    tables = [_table("t", indexes=[_index("ix", ["a"]), _index("ix", ["b", "a"], unique=True)])]
    [entry] = _view(_base(engine, tables, source))["left_out"]
    assert entry["kind"] == "index"
    assert entry["columns"] == ["b", "a"]
    assert entry["unique"] is True


def test_a_left_out_index_with_an_unsafe_column_lists_it_cleaned() -> None:
    tables = [_table("t", columns=("a", f"b{ZWSP}c"), indexes=[_index("ix", ["a", f"b{ZWSP}c"])])]
    view = _view(_base(PG, tables, "postgresql"))
    [entry] = [e for e in view["left_out"] if e["kind"] == "index"]
    assert entry["columns"] == ["a", "b c"]
    assert entry["unique"] is False


@pytest.mark.passes_on_main
def test_other_left_out_kinds_carry_no_columns() -> None:
    view = _view(_base(MY, [_table("t", columns=("a", f"b{ZWSP}c"))]))
    [entry] = view["left_out"]
    assert entry["kind"] == "column"
    assert "columns" not in entry and "unique" not in entry


LONG = 20_000


def test_each_cleaned_text_is_capped_with_a_visible_marker() -> None:
    """A 20,000-character table name is cut to a fixed length in every field that repeats
    it, and the cut shows."""
    from src.tools.schema.aurora_common.design_view import TEXT_LIMIT, TRUNCATED

    long_table = "t" * LONG
    long_index = "i" * LONG
    tables = [_table(long_table, columns=("a", "c" * LONG),
                     indexes=[_index("ix", ["a"]), _index("ix", ["c" * LONG]),
                              _index(f"{long_index}{BELL}", ["a"])]),
              _table(f"{long_table}{BELL}")]  # fmt: skip
    view = _view(_base(MY, tables))
    assert view["left_out"], "the case leaves objects out"
    texts = [str(e[k]) for e in view["left_out"] for k in ("table", "name", "reason")]
    texts += [c for e in view["left_out"] for c in e.get("columns", [])]
    assert any(t.endswith(TRUNCATED) for t in texts)
    for text in texts:
        assert len(text) <= TEXT_LIMIT + len(TRUNCATED)
        assert len(text) <= TEXT_LIMIT or text.endswith(TRUNCATED)


def test_a_renamed_index_text_is_capped_too() -> None:
    from src.tools.schema.aurora_common.design_view import TEXT_LIMIT, TRUNCATED

    long_name = "m" * 70  # PostgreSQL keeps 63 bytes; the view still sees the collected name
    tables = [_table("postmeta", indexes=[_index("meta_key", ["a"])]),
              _table("u" * LONG, indexes=[_index("meta_key", ["a"])]),
              _table("other", indexes=[_index(long_name, ["a"])])]  # fmt: skip
    view = _view(_base(PG, tables, "postgresql"))
    [entry] = [e for e in view["renamed_indexes"] if e["name"] == "meta_key"]
    for key in ("table", "new_name", "reason"):
        assert len(entry[key]) <= TEXT_LIMIT + len(TRUNCATED)
    assert entry["table"].endswith(TRUNCATED)


def test_the_cap_is_applied_after_cleaning() -> None:
    """A name whose run of control characters collapses to one space is short once cleaned,
    so it is not cut."""
    from src.tools.schema.aurora_common.design_view import TEXT_LIMIT, TRUNCATED

    name = "a" + BELL * (TEXT_LIMIT * 3) + "b"
    view = _view(_base(MY, [_table("t", indexes=[_index("ix", ["a"]), _index(name, ["a"])])]))
    [entry] = view["left_out"]
    assert entry["name"] == "a b"
    assert not entry["name"].endswith(TRUNCATED)


def test_a_short_text_is_left_as_cleaned() -> None:
    from src.tools.schema.aurora_common.design_view import TEXT_LIMIT, TRUNCATED

    name = "n" * TEXT_LIMIT
    view = _view(
        _base(MY, [_table("t", indexes=[_index("ix", ["a"]), _index(f"{name}{BELL}", ["a"])])])
    )
    [entry] = view["left_out"]
    assert entry["name"] == f"{name}"
    assert TRUNCATED not in entry["name"]


def test_the_mysql_skill_says_those_entries_before_the_left_out_sentence() -> None:
    """ "Those entries are the only source of index DDL" points at the add, modify and
    remove entries, so the left-out sentence comes after it (#2575)."""
    from pathlib import Path

    text = " ".join(Path("src/skills/aurora_mysql-data-modeling.md").read_text().split())
    those = text.index("Those entries are the only source of index DDL")
    left_out = text.index("A left-out index (`left_out`, `kind` `index`)")
    remove = text.index("`remove_indexes` (names)")
    assert remove < those < left_out
