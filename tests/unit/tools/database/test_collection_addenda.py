# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A collection script may print a second object after the collection (#2128).

``collect-mysql.sql`` measures LOB lengths last, after the whole collection is printed, so that
a timeout or a lock held by another session costs the sizes and never the collection. The sizes
arrive as ``{"lob_sizes": [...]}``; the ingest merges them into ``columns`` and reads a file
without them, or with a cut one, as "not measured".
"""

from __future__ import annotations

import json

import pytest

from src.assessment.loading import CollectionLoadError, _parse
from src.tools.database.offline_collection_check import OfflineCollectionError, read_collection_text

COLLECTION = {
    "collection_version": "1.0",
    "columns": [
        {"table_name": "posts", "column_name": "id", "ordinal_position": 1, "data_type": "int"},
        {
            "table_name": "posts",
            "column_name": "body",
            "ordinal_position": 2,
            "data_type": "longtext",
        },
        {
            "table_name": "posts",
            "column_name": "raw",
            "ordinal_position": 3,
            "data_type": "mediumblob",
        },
    ],
    "tables": [{"table_name": "posts"}],
    "queries": [],
}


def _file(second: object | None = None, *, header: str = "", cut: bool = False) -> str:
    text = header + json.dumps(COLLECTION) + "\n"
    if second is not None:
        body = second if isinstance(second, str) else json.dumps(second)
        text += body[: len(body) // 2] if cut else body + "\n"
    return text


SIZES = {
    "lob_sizes": [
        {"table_name": "posts", "ordinal_position": 2, "max_value_bytes": 1_866_112},
        {"table_name": "posts", "ordinal_position": 3, "max_value_bytes": 0},
    ]
}


def _lengths(data: dict) -> list[object]:
    return [c.get("max_value_bytes") for c in data["columns"]]


def test_the_second_object_is_merged_into_the_columns_by_table_and_position():
    data = read_collection_text(_file(SIZES), source="c.json")
    assert _lengths(data) == [None, 1_866_112, 0]  # an empty column is 0, not unmeasured


@pytest.mark.passes_on_main  # behaviour main already has: this guards it
def test_a_file_without_the_second_object_loads_with_every_size_unmeasured():
    # What a timeout leaves: the whole collection and no second object.
    data = read_collection_text(_file(), source="c.json")
    assert _lengths(data) == [None, None, None]
    assert len(data["columns"]) == 3 and data["tables"] == [{"table_name": "posts"}]


def test_a_cut_second_object_is_dropped_not_fatal():
    data = read_collection_text(_file(SIZES, cut=True), source="c.json")
    assert _lengths(data) == [None, None, None]
    from src.tools.database.collection_text import extract_json_objects

    assert extract_json_objects(_file(SIZES, cut=True))[1] == []


def test_an_unclosed_trailing_object_that_is_not_the_lob_sizes_is_refused():
    """Only the measurement's own object may be cut short; anything else is data that would be
    silently dropped (the rule since a second object was first refused)."""
    for cut in ('{"columns": [{"table_name": "t"', '{"queries": [', "{", '{"lob": [1,'):
        with pytest.raises(OfflineCollectionError, match="more than one JSON object"):
            read_collection_text(_file(cut), source="c.json")
        with pytest.raises(CollectionLoadError, match="more than one JSON object"):
            _parse(_file(cut), "c.json")
    # Whitespace before the key is still the sizes object.
    assert _lengths(read_collection_text(_file('{ \n "lob_sizes": [{"table_na'), source="c")) == [
        None,
        None,
        None,
    ]


def test_a_client_header_and_trailing_chatter_around_both_objects_are_ignored():
    text = "collection_output\n" + _file(SIZES, header="") + "(1 rows affected)\n"
    assert _lengths(read_collection_text(text, source="c.json")) == [None, 1_866_112, 0]


def test_an_entry_for_no_column_or_with_a_bad_size_changes_nothing():
    bad = {
        "lob_sizes": [
            {"table_name": "nope", "ordinal_position": 2, "max_value_bytes": 5},
            {"table_name": "posts", "ordinal_position": 2, "max_value_bytes": -1},
            {"table_name": "posts", "ordinal_position": 3, "max_value_bytes": "7"},
            "junk",
        ]
    }
    assert _lengths(read_collection_text(_file(bad), source="c.json")) == [None, None, None]


@pytest.mark.passes_on_main  # behaviour main already has: this guards it
def test_a_second_object_that_is_not_a_known_addendum_is_still_refused():
    for other in ({"columns": []}, {"lob_sizes": [], "extra": 1}, {}):
        with pytest.raises(OfflineCollectionError, match="more than one JSON object"):
            read_collection_text(_file(other), source="c.json")
    with pytest.raises(OfflineCollectionError, match="not valid JSON"):
        read_collection_text(_file('{"lob_sizes": [1,]}'), source="c.json")


@pytest.mark.passes_on_main  # behaviour main already has: this guards it
def test_the_first_object_must_still_be_whole():
    with pytest.raises(OfflineCollectionError, match="looks cut short"):
        read_collection_text(json.dumps(COLLECTION)[:40], source="c.json")


def test_the_assessment_loader_merges_it_too():
    data = _parse(_file(SIZES), "c.json")
    assert _lengths(data) == [None, 1_866_112, 0]
    assert _lengths(_parse(_file(), "c.json")) == [None, None, None]
    with pytest.raises(CollectionLoadError, match="more than one JSON object"):
        _parse(_file({"columns": []}), "c.json")


def test_extract_json_objects_returns_each_complete_later_object():
    from src.tools.database.collection_text import extract_json_objects

    first, later = extract_json_objects('x {"a": "}{"} y {"b": 1}\n{"c": 2}')
    assert json.loads(first) == {"a": "}{"}
    assert [json.loads(o) for o in later] == [{"b": 1}, {"c": 2}]
