# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The offline collection's input check runs before any transform (#131).

Three inputs used to fail badly: a file that is not JSON raised a bare
``JSONDecodeError`` naming no file; an entry missing a key the transform reads
raised a ``KeyError`` from inside the transform; and a JSON document of another
shape parsed into an empty collection with no error at all. The check rejects
each with one :class:`OfflineCollectionError` naming the file and the problem,
and records an absent optional section instead of passing it off as empty.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.tools.database.offline_collection_check import (
    OfflineCollectionError,
    read_collection_text,
    validate_offline_collection,
)
from src.tools.database.offline_parser import fetch_offline_json, parse_offline_collection
from tests.unit.assessment.conftest import SAMPLES, sample_collection

SOURCE = "shop-collection.json"


def _collection() -> dict[str, Any]:
    """A small raw collection in the collection scripts' format."""
    return {
        "collection_version": "1.0",
        "metadata": {"database_name": "shop", "version": "8.0.36"},
        "tables": [{"table_name": "orders", "row_count": 10}],
        "columns": [{"table_name": "orders", "column_name": "id", "data_type": "int"}],
        "indexes": [
            {"table_name": "orders", "index_name": "PRIMARY", "non_unique": 0, "column_name": "id"}
        ],
        "foreign_keys": [],
        "primary_keys": [{"table_name": "orders", "column_name": "id"}],
        "views": [],
        "procedures": [],
        "triggers": [],
        "queries": [{"digest": "d1", "query_text": "SELECT * FROM orders WHERE id = ?"}],
        "global_stats": {},
    }


# -- Mode 1: not JSON ---------------------------------------------------------------


def test_a_truncated_file_names_the_file_and_the_stage() -> None:
    text = 'collection_output\n{"tables": [\n  {"table_name": "orders"},\n  {"table_name": '

    with pytest.raises(OfflineCollectionError) as err:
        read_collection_text(text, source=SOURCE)

    message = str(err.value)
    assert SOURCE in message
    assert "collect" in message
    assert "not valid JSON" in message


def test_invalid_json_reports_the_line_of_the_file_not_of_the_object() -> None:
    # The header line comes first, so the bad line is the file's third line.
    text = 'collection_output\n{"tables": [],\n "queries": [] "x": 1}\n'

    with pytest.raises(OfflineCollectionError) as err:
        read_collection_text(text, source=SOURCE)

    assert "at line 3" in str(err.value)
    assert isinstance(err.value, ValueError)


def test_a_file_with_no_json_object_is_refused_by_name() -> None:
    with pytest.raises(OfflineCollectionError) as err:
        read_collection_text("ERROR 1045 (28000): Access denied\n", source=SOURCE)

    assert SOURCE in str(err.value)
    assert "no JSON object" in str(err.value)


def test_valid_text_reads_as_the_object() -> None:
    text = "collection_output\n" + json.dumps(_collection()) + "\n(1 row affected)\n"

    assert read_collection_text(text, source=SOURCE) == _collection()


def test_the_s3_reader_names_the_object_when_it_is_not_json() -> None:
    from unittest.mock import MagicMock, patch

    body = b'{"tables": [ {"table_name": "orders"} '
    s3 = MagicMock()
    s3.head_object.return_value = {"ContentLength": len(body)}
    s3.get_object.return_value = {"Body": MagicMock(read=lambda: body)}
    with (
        patch("src.tools.database.offline_parser.boto3.client", return_value=s3),
        pytest.raises(OfflineCollectionError) as err,
    ):
        fetch_offline_json("bucket", "shop/job/uploads/collector-output.json")

    assert "s3://bucket/shop/job/uploads/collector-output.json" in str(err.value)


# -- Mode 2: an entry misses a key the transform reads ------------------------------


def test_an_index_without_table_name_is_refused_before_the_transform() -> None:
    raw = _collection()
    del raw["indexes"][0]["table_name"]

    with pytest.raises(OfflineCollectionError) as err:
        parse_offline_collection(raw, source=SOURCE)

    message = str(err.value)
    assert SOURCE in message
    assert "indexes[0]" in message
    assert "table_name" in message
    assert not isinstance(err.value, KeyError)


def test_one_error_lists_every_bad_entry_with_its_section_and_position() -> None:
    raw = _collection()
    raw["indexes"].append({"index_name": "ix_a", "non_unique": 1})  # no table_name
    raw["indexes"].append({"table_name": "orders", "index_name": "ix_b"})  # no non_unique
    raw["foreign_keys"] = [{"table_name": "orders", "column_name": "customer_id"}]
    raw["tables"].append({"row_count": 3})
    raw["primary_keys"].append("orders.id")  # not an object

    with pytest.raises(OfflineCollectionError) as err:
        validate_offline_collection(raw, source=SOURCE)

    message = str(err.value)
    for where in ("indexes[1]", "indexes[2]", "foreign_keys[0]", "tables[1]", "primary_keys[1]"):
        assert where in message, where
    assert "non_unique" in message
    assert "constraint_name" in message and "referenced_table_name" in message
    assert "indexes[0]" not in message  # the good entry is not listed
    assert "5 entries" in message


def test_a_long_list_of_bad_entries_is_capped_but_counted() -> None:
    raw = _collection()
    raw["columns"] = [{"column_name": f"c{i}"} for i in range(500)]

    with pytest.raises(OfflineCollectionError) as err:
        validate_offline_collection(raw, source=SOURCE)

    message = str(err.value)
    assert "500 entries" in message
    assert "columns[0]" in message
    assert "columns[499]" not in message
    assert "more" in message


# -- Mode 3: valid JSON of another shape --------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        [{"table_name": "orders"}],
        "a collection",
        {"name": "shop", "rows": []},
        {},
    ],
    ids=["a-list", "a-string", "another-document", "an-empty-object"],
)
def test_a_document_that_is_not_a_collection_is_refused(document: Any) -> None:
    with pytest.raises(OfflineCollectionError) as err:
        parse_offline_collection(document, source=SOURCE)

    message = str(err.value)
    assert SOURCE in message
    assert "tables" in message and "queries" in message


def test_queries_as_an_object_is_refused_for_its_shape() -> None:
    """``queries`` an object (a collector output's), not a raw collection's list."""
    document = {"metadata": {}, "tables": [], "queries": {"query_patterns": []}}

    with pytest.raises(OfflineCollectionError) as err:
        validate_offline_collection(document, source=SOURCE)

    message = str(err.value)
    assert "'queries'" in message and "list" in message


@pytest.mark.parametrize(
    ("section", "value", "expected"),
    [
        ("indexes", {"orders": []}, "list"),
        ("tables", "orders", "list"),
        ("global_stats", [1, 2], "object"),
        ("os_stats", "a string", "object"),
        ("metadata", "not json", "object"),
    ],
)
def test_a_section_of_the_wrong_type_says_what_was_expected(
    section: str, value: Any, expected: str
) -> None:
    raw = _collection()
    raw[section] = value

    with pytest.raises(OfflineCollectionError) as err:
        validate_offline_collection(raw, source=SOURCE)

    message = str(err.value)
    assert f"'{section}'" in message
    assert expected in message


def test_misnamed_sections_are_recorded_as_absent_with_the_unknown_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``index`` for ``indexes``: the tables still load, the absence is recorded."""
    raw = _collection()
    raw["index"] = raw.pop("indexes")

    with caplog.at_level(logging.WARNING, logger="src.tools.database.offline_collection_check"):
        parsed = parse_offline_collection(raw, source=SOURCE)

    assert parsed["absent_sections"] == ["indexes"]
    assert parsed["tables"][0]["indexes"] == []
    warning = " ".join(r.getMessage() for r in caplog.records)
    assert SOURCE in warning and "indexes" in warning and "'index'" in warning


# -- Absent optional sections -------------------------------------------------------


@pytest.mark.parametrize(
    "section",
    ["metadata", "columns", "indexes", "foreign_keys", "primary_keys", "views", "procedures",
     "triggers", "global_stats"],
)  # fmt: skip
def test_an_absent_optional_section_is_empty_and_recorded(section: str) -> None:
    raw = _collection()
    del raw[section]

    parsed = parse_offline_collection(raw, source=SOURCE)

    assert parsed["absent_sections"] == [section]
    assert len(parsed["tables"]) == 1
    assert len(parsed["queries"]) == 1


@pytest.mark.parametrize(("section", "kept"), [("queries", "tables"), ("tables", "queries")])
def test_tables_without_queries_or_the_reverse_loads_and_is_recorded(
    section: str, kept: str
) -> None:
    """Only a document with neither is refused: a schema-only collection still loads."""
    raw = _collection()
    del raw[section]

    parsed = parse_offline_collection(raw, source=SOURCE)

    assert parsed["absent_sections"] == [section]
    assert len(parsed[kept]) == 1


@pytest.mark.parametrize("section", ["indexes", "foreign_keys", "global_stats", "tables"])
def test_a_null_section_reads_as_absent(section: str) -> None:
    """A ``null`` section (``"indexes": null``) is absent, not a crash in the transform."""
    raw = _collection()
    raw[section] = None

    parsed = parse_offline_collection(raw, source=SOURCE)

    assert parsed["absent_sections"] == [section]


def test_the_engine_only_sections_are_not_reported_absent() -> None:
    """``io_stats``, ``wait_events`` and ``os_stats`` come from SQL Server and Oracle only."""
    parsed = parse_offline_collection(_collection(), source=SOURCE)

    assert "absent_sections" not in parsed


def test_an_empty_but_well_formed_collection_is_legal() -> None:
    """A source with no tables and no statements is accepted, explicitly."""
    raw = {key: ([] if isinstance(v, list) else v) for key, v in _collection().items()}

    parsed = parse_offline_collection(raw, source=SOURCE)

    assert parsed["tables"] == [] and parsed["queries"] == []
    assert "absent_sections" not in parsed


def test_the_check_keeps_the_collection_as_given() -> None:
    raw = _collection()
    before = copy.deepcopy(raw)

    validate_offline_collection(raw, source=SOURCE)

    assert raw == before


def test_extra_keys_on_entries_and_at_the_top_are_accepted() -> None:
    """Not a stricter schema: only the keys the transform reads are required."""
    raw = _collection()
    raw["collected_at"] = "2026-01-01T00:00:00Z"
    raw["indexes"][0]["cardinality"] = 10
    raw["procedures"] = [{"routine_name": "p", "routine_type": "PROCEDURE"}]  # no definition
    raw["views"] = [{"view_name": "v"}]

    assert validate_offline_collection(raw, source=SOURCE) == ()


def test_metadata_as_a_json_string_is_accepted() -> None:
    raw = _collection()
    raw["metadata"] = json.dumps(raw["metadata"])

    assert validate_offline_collection(raw, source=SOURCE) == ()


# -- The bundled samples ------------------------------------------------------------


@pytest.mark.parametrize("name", SAMPLES)
def test_every_sample_passes_the_check_with_nothing_absent(name: str) -> None:
    raw = sample_collection(name)

    assert validate_offline_collection(raw, source=f"{name}.zip") == ()


@pytest.mark.parametrize("name", SAMPLES)
def test_every_sample_builds_the_same_collector_output_through_the_check(name: str) -> None:
    """The check adds nothing to the parsed collection of a well-formed sample."""
    parsed = parse_offline_collection(sample_collection(name), database_name=name, source=name)

    assert "absent_sections" not in parsed
    output = build_offline_collector_output(parsed, job_id="j", database_name=name, s3_key="k")
    assert output["database_schema"]["tables"]
    assert output["queries"]["query_patterns"]


def test_a_null_entry_is_called_null() -> None:
    raw = _collection()
    raw["triggers"] = [None]

    with pytest.raises(OfflineCollectionError) as err:
        validate_offline_collection(raw, source=SOURCE)

    assert "triggers[0] is null, not an object" in str(err.value)


# -- Nothing main loads is refused (#1755 review) -----------------------------------


def _loads(raw: dict[str, Any], name: str = "wordpress") -> dict[str, Any]:
    parsed = parse_offline_collection(raw, database_name=name, source=SOURCE)
    output: dict[str, Any] = build_offline_collector_output(
        parsed, job_id="j", database_name=name, s3_key="k"
    )
    return output


@pytest.mark.parametrize(
    ("section", "value", "absent"),
    [
        ("views", {}, True),
        ("procedures", {}, True),
        ("triggers", {}, True),
        ("indexes", "", True),
        ("foreign_keys", {}, True),
        ("wait_events", {}, False),
        ("wait_events", [1, "x", None], False),
        ("io_stats", [1, 2], False),
        ("io_stats", "a string", False),
        ("os_stats", [], False),
        ("os_stats", "", False),
        ("metadata", [{"database_name": "wordpress"}], True),
        ("metadata", 5, True),
        ("metadata", False, True),
    ],
)
def test_a_value_the_transform_reads_as_empty_or_passes_on_still_loads(
    section: str, value: Any, absent: bool
) -> None:
    """Each loads on main to a valid collector output; it keeps loading, an empty
    value of another type reported as an absent section."""
    raw = sample_collection("wordpress")
    raw[section] = value

    parsed = parse_offline_collection(raw, database_name="wordpress", source=SOURCE)

    assert (section in parsed.get("absent_sections", [])) is absent
    output = build_offline_collector_output(
        parsed, job_id="j", database_name="wordpress", s3_key="k"
    )
    assert len(output["database_schema"]["tables"]) == 50


def _discourse() -> dict[str, Any]:
    return sample_collection("discourse")


def test_a_column_of_a_table_not_collected_needs_no_column_name() -> None:
    """The builder reads ``column_name`` only for a column of a collected table."""
    raw = _discourse()
    raw["columns"].append({"table_name": "not_a_collected_table", "data_type": "int"})

    assert len(_loads(raw, "discourse")["database_schema"]["tables"]) == 311

    raw["columns"].append({"table_name": raw["tables"][0]["table_name"], "data_type": "int"})
    with pytest.raises(OfflineCollectionError) as err:
        validate_offline_collection(raw, source=SOURCE)
    assert f"columns[{len(raw['columns']) - 1}] lacks 'column_name'" in str(err.value)


def test_only_an_index_s_first_row_needs_non_unique() -> None:
    raw = _discourse()
    multi = next(
        (i["table_name"], i["index_name"])
        for n, i in enumerate(raw["indexes"])
        if any(
            (j["table_name"], j["index_name"]) == (i["table_name"], i["index_name"])
            for j in raw["indexes"][n + 1 :]
        )
    )
    rows = [n for n, i in enumerate(raw["indexes"]) if (i["table_name"], i["index_name"]) == multi]
    for n in rows[1:]:
        del raw["indexes"][n]["non_unique"]

    assert _loads(raw, "discourse")["database_schema"]["tables"]

    del raw["indexes"][rows[0]]["non_unique"]
    with pytest.raises(OfflineCollectionError) as err:
        validate_offline_collection(raw, source=SOURCE)
    assert f"indexes[{rows[0]}] lacks 'non_unique'" in str(err.value)
    assert f"indexes[{rows[1]}]" not in str(err.value)


def test_only_a_constraint_s_first_row_needs_referenced_table_name() -> None:
    raw = _discourse()
    first = raw["foreign_keys"][0]
    extra = {**first, "column_name": "extra_column", "referenced_column_name": "id"}
    del extra["referenced_table_name"]
    raw["foreign_keys"].insert(1, extra)

    assert _loads(raw, "discourse")["database_schema"]["tables"]

    del raw["foreign_keys"][0]["referenced_table_name"]
    with pytest.raises(OfflineCollectionError) as err:
        validate_offline_collection(raw, source=SOURCE)
    assert "foreign_keys[0] lacks 'referenced_table_name'" in str(err.value)
    assert "foreign_keys[1]" not in str(err.value)


def test_an_in_memory_collection_main_accepts_still_passes_assess() -> None:
    """``assess()`` keeps running what it ran before: empty sections of another type,
    engine-only sections of any type, a metadata list, and Oracle's sentinels."""
    from src.assessment import assess

    raw = sample_collection("wordpress")
    raw.update(views={}, triggers={}, indexes="", foreign_keys={}, io_stats="x")
    raw["queries"].append({"_sentinel": True})

    result = assess(
        {"database_name": "wordpress", "collection": raw, "options": {"run_through": "analysis"}}
    )

    assert result.summary is not None


# -- No customer text in the messages -----------------------------------------------


def test_a_refusal_never_repeats_a_document_key() -> None:
    document = {"jane.doe@example.com": 1, "x" * 300: 2, "index": [], "tabels": []}

    with pytest.raises(OfflineCollectionError) as err:
        validate_offline_collection(document, source=SOURCE)

    message = str(err.value)
    assert "jane.doe" not in message and "x" * 40 not in message
    assert "4 top-level keys" in message
    assert "'index' (for 'indexes'?)" in message and "'tabels' (for 'tables'?)" in message


def test_the_absent_sections_warning_never_repeats_a_document_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = _collection()
    del raw["indexes"]
    raw["jane.doe@example.com"] = "secret"

    with caplog.at_level(logging.WARNING, logger="src.tools.database.offline_collection_check"):
        parse_offline_collection(raw, source=SOURCE)

    warning = " ".join(r.getMessage() for r in caplog.records)
    assert "indexes" in warning and "1 top-level key" in warning
    assert "jane.doe" not in warning and "secret" not in warning


# -- Oracle sentinels and control characters ----------------------------------------


def test_a_sentinel_at_the_end_of_queries_is_not_a_statement() -> None:
    """On main a trailing sentinel was counted as a statement: 108 for wordpress's 107."""
    raw = sample_collection("wordpress")
    raw["queries"].append({"_sentinel": True})

    assert len(_loads(raw)["queries"]["query_patterns"]) == 107


def test_a_sentinel_anywhere_else_is_kept() -> None:
    raw = _collection()
    raw["views"] = [{"_sentinel": True}, {"view_name": "v"}]

    parsed = parse_offline_collection(raw, source=SOURCE)

    assert len(parsed["views"]) == 2


def test_an_oracle_spool_reads_with_its_sentinels_and_control_characters() -> None:
    raw = _collection()
    raw["metadata"] = {"database_name": "shop", "version": "Oracle Database 19c"}
    for section in ("tables", "columns", "queries", "triggers"):
        raw[section].append({"_sentinel": True})
    # Oracle 19c leaves control characters in string values unescaped.
    text = json.dumps(raw).replace("SELECT *", "SELECT\t*").replace('"d1"', '"d\x011"')
    assert "\t" in text and "\x01" in text

    data = read_collection_text("collection_output\n" + text, source=SOURCE)

    assert data["triggers"] == []
    parsed = parse_offline_collection(data, source=SOURCE)
    assert [t["table_name"] for t in parsed["tables"]] == ["orders"]
    assert len(parsed["queries"]) == 1
    assert "absent_sections" not in parsed
