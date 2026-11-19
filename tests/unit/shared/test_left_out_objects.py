# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2484: the objects an Aurora design leaves out, read once from its app-layer notes.

The merge writes one note per object it does not create (``<Object> not created:
<reason>.``). ``left_out_objects`` is the one reader: the facts layer lists them, and the
generated script closes with one ``-- Not created: <name>: <reason>`` comment per object,
each one line whatever the collected name holds.
"""

from __future__ import annotations

import pytest

from src.shared.left_out_objects import (
    is_left_out_note,
    left_out_objects,
    not_created_comment,
    with_not_created_comments,
    without_not_created_comments,
)

COLLIDING = {
    "feature": "colliding_table_name",
    "source_object": "Sales.orders",
    "recommendation": (
        "Table not created: its schema and name match another table of the design once "
        "case and quote marks are ignored, so neither is designed; rename one, or design "
        "them by hand."
    ),
}
AMBIGUOUS = {
    "feature": "ambiguous_reference",
    "source_object": "sales.lines.fk_customer",
    "recommendation": "Foreign key not created: its referenced table customers is in more than one other schema (archive, crm) and not in this table's, so the design cannot tell which one it references; add it by hand once you know.",  # noqa: E501
}
UNSAFE = {
    "feature": "unsafe_name",
    "source_object": "bad\nname",
    "recommendation": "Table not created: its name holds a control character.",
}


def test_each_left_out_note_reads_as_its_object_kind_and_reason() -> None:
    assert left_out_objects([COLLIDING, AMBIGUOUS]) == [
        {
            "name": "Sales.orders",
            "feature": "colliding_table_name",
            "label": "Colliding table name",
            "object": "table",
            "reason": COLLIDING["recommendation"].removeprefix("Table not created: "),
        },
        {
            "name": "sales.lines.fk_customer",
            "feature": "ambiguous_reference",
            "label": "Ambiguous reference",
            "object": "foreign key",
            "reason": AMBIGUOUS["recommendation"].removeprefix("Foreign key not created: "),
        },
    ]


@pytest.mark.parametrize(
    "note",
    [
        {"feature": "trigger", "source_object": "t", "recommendation": "Move it to the app."},
        {"feature": "column_default", "source_object": "a.b", "recommendation": "Set it."},
        {
            "feature": "index_key_prefix",
            "source_object": "a.ix",
            "recommendation": "Index not created: too long.",
        },  # noqa: E501
        {"feature": "unsafe_name", "source_object": "a", "recommendation": "Rename it."},
        {"feature": "unsafe_name", "source_object": "", "recommendation": "Table not created: x."},
        {"feature": "unsafe_name", "source_object": "a", "recommendation": "Table not created: "},
        "Table not created: x.",
        None,
    ],
    ids=["model", "default", "prefix", "no-head", "no-name", "no-reason", "string", "none"],
)
def test_any_other_note_is_not_a_left_out_object(note) -> None:
    assert not is_left_out_note(note)
    assert left_out_objects([note]) == []


def test_a_table_note_written_before_2484_names_the_table_once() -> None:
    old = {**COLLIDING, "source_object": "Sales.orders.Sales.orders"}
    assert left_out_objects([old])[0]["name"] == "Sales.orders"
    index = {**AMBIGUOUS, "source_object": "a.a", "recommendation": "Index not created: x."}
    assert left_out_objects([index])[0]["name"] == "a.a"  # only a table is named twice


@pytest.mark.parametrize("name", ["Sales.sales", "sales.sales", "a.a", "x.x.x"])
def test_a_table_named_as_its_schema_keeps_its_full_name(name: str) -> None:
    """Only the doubled form of a qualified table is halved: a table whose schema and
    name match (``sales.sales``) is already in the single form."""
    note = {**COLLIDING, "source_object": name}
    assert left_out_objects([note])[0]["name"] == name
    doubled = {**COLLIDING, "source_object": f"{name}.{name}"}
    assert left_out_objects([doubled])[0]["name"] == name


def test_the_comment_cleaner_is_the_one_the_ddl_generator_uses() -> None:
    from src.shared.sql_identifiers import comment_text
    from src.tools.schema.aurora_common import ddl_generator

    assert ddl_generator.comment_text is comment_text
    assert comment_text("a\nb\u2028 c") == "a b c"


def test_the_script_carries_the_comments_only_when_it_ends_with_them() -> None:
    from src.shared.left_out_objects import script_has_not_created_comments

    script = "CREATE TABLE a (id INT);"
    notes = [COLLIDING]
    assert script_has_not_created_comments(with_not_created_comments(script, notes), notes)
    assert script_has_not_created_comments(not_created_comment(left_out_objects(notes)[0]), notes)
    assert not script_has_not_created_comments(script, notes)
    assert not script_has_not_created_comments(with_not_created_comments(script, notes), [])
    assert not script_has_not_created_comments("", notes)


@pytest.mark.parametrize(
    "hostile",
    [
        "x\nDROP TABLE users; --",
        "x\r\nDROP TABLE users;",
        "x\u2028DROP TABLE users;",
        "x\u0085DROP TABLE users;",
        "x\x0bDROP TABLE users;",
    ],
    ids=["lf", "crlf", "line-separator", "nel", "vt"],
)
def test_a_hostile_name_cannot_end_its_comment(hostile: str) -> None:
    comment = not_created_comment({"name": hostile, "reason": "why\nDROP TABLE b;"})
    assert comment == (
        f"-- Not created: x DROP TABLE users;{' --' if hostile.endswith('--') else ''}: "
        "why DROP TABLE b;"
    )
    assert len(comment.splitlines()) == 1
    script = with_not_created_comments(
        "CREATE TABLE a (id INT);", [{**UNSAFE, "source_object": hostile}]
    )
    assert [line for line in script.splitlines() if "DROP" in line] == [script.splitlines()[-1]]
    assert script.splitlines()[-1].startswith("-- Not created: ")


def test_the_comments_close_the_script_and_come_off_it() -> None:
    script = 'CREATE TABLE "crm"."items" (\n  "id" int\n);'
    out = with_not_created_comments(script, [COLLIDING, AMBIGUOUS])
    assert out == (
        f"{script}\n\n"
        f"-- Not created: Sales.orders: {COLLIDING['recommendation'][len('Table not created: '):]}\n"  # noqa: E501
        "-- Not created: sales.lines.fk_customer: "
        f"{AMBIGUOUS['recommendation'][len('Foreign key not created: '):]}"
    )
    assert without_not_created_comments(out, [COLLIDING, AMBIGUOUS]) == script
    # Any other ending is left for the saved-design check to refuse.
    assert without_not_created_comments(out + "\nDROP TABLE x;", [COLLIDING]) == (
        out + "\nDROP TABLE x;"
    )


def test_a_script_with_nothing_left_out_is_byte_identical() -> None:
    """No left-out note, no comment: the script is returned as it is."""
    from src.shared import left_out_objects as module

    script = "CREATE TABLE a (id INT);\n\nCREATE INDEX i ON a (id);"
    notes = [{"feature": "trigger", "source_object": "t", "recommendation": "Move it."}]
    assert module.with_not_created_comments(script, notes) == script
    assert module.with_not_created_comments(script, []) == script
    assert module.without_not_created_comments(script, notes) == script
