"""Unit tests for ``extract_json_object`` (#396).

One tolerant reader for collection JSON, used by every call site that reads
raw DB-client output before ``json.loads``. Covers each row of the issue's
table (BOM, header line, header + dashes separator, trailing row-count
chatter, and combinations), plus "no JSON at all".
"""

from __future__ import annotations

import json

import pytest

from src.tools.database.collection_text import extract_json_object

PAYLOAD = '{"a": 1, "b": [1, 2, 3]}'
PAYLOAD_OBJ = {"a": 1, "b": [1, 2, 3]}


def _loads(text: str) -> dict:
    result: dict = json.loads(extract_json_object(text, source="t"))
    return result


def test_plain_json_object() -> None:
    assert _loads(PAYLOAD) == PAYLOAD_OBJ


def test_collection_output_header_line() -> None:
    assert _loads(f"collection_output\n{PAYLOAD}") == PAYLOAD_OBJ


def test_utf8_bom_before_object() -> None:
    assert _loads(f"﻿{PAYLOAD}") == PAYLOAD_OBJ


def test_bom_plus_header_line() -> None:
    assert _loads(f"﻿collection_output\n{PAYLOAD}") == PAYLOAD_OBJ


def test_header_plus_dashes_separator() -> None:
    # sqlcmd writes a header, then a dashes separator line, unless -y 0 is
    # used alone.
    text = f"collection_output\n{'-' * 20}\n{PAYLOAD}"
    assert _loads(text) == PAYLOAD_OBJ


def test_trailing_rows_affected() -> None:
    assert _loads(f"{PAYLOAD}\n(1 rows affected)") == PAYLOAD_OBJ


def test_trailing_rows_affected_no_leading_number_word() -> None:
    assert _loads(f"{PAYLOAD}\nRows affected: 1") == PAYLOAD_OBJ


def test_trailing_blank_lines() -> None:
    assert _loads(f"{PAYLOAD}\n\n\n") == PAYLOAD_OBJ


def test_header_json_and_trailing_rows_affected() -> None:
    text = f"collection_output\n{PAYLOAD}\n(1 rows affected)"
    assert _loads(text) == PAYLOAD_OBJ


def test_header_dashes_json_and_trailing_rows_affected() -> None:
    text = f"collection_output\n{'-' * 20}\n{PAYLOAD}\n(1 rows affected)"
    assert _loads(text) == PAYLOAD_OBJ


def test_braces_inside_string_values_do_not_confuse_matching() -> None:
    payload = '{"query": "SELECT * FROM t WHERE x = \'{foo}\'", "n": 1}'
    assert _loads(payload) == {"query": "SELECT * FROM t WHERE x = '{foo}'", "n": 1}


def test_escaped_quote_inside_string_does_not_end_it_early() -> None:
    payload = '{"text": "a \\"quoted\\" value with a } brace"}'
    assert _loads(payload) == {"text": 'a "quoted" value with a } brace'}


def test_no_json_at_all_raises_value_error_naming_source() -> None:
    with pytest.raises(ValueError, match="my-source"):
        extract_json_object("just some text, no braces here", source="my-source")


def test_empty_text_raises_value_error() -> None:
    with pytest.raises(ValueError, match="empty-source"):
        extract_json_object("", source="empty-source")


def test_unbalanced_braces_raise_value_error_naming_source() -> None:
    with pytest.raises(ValueError, match="unbalanced-source"):
        extract_json_object('{"a": 1', source="unbalanced-source")


def test_default_source_label_is_usable_without_one() -> None:
    # Callers that don't pass `source` still get a clear error.
    with pytest.raises(ValueError):
        extract_json_object("no braces")


def test_a_second_json_object_is_an_error_not_silently_dropped() -> None:
    with pytest.raises(ValueError, match="more than one JSON object"):
        extract_json_object('{"a": 1}\n{"b": 2}', source="two.json")


def test_a_top_level_array_is_an_error_not_its_first_element() -> None:
    with pytest.raises(ValueError, match="more than one JSON object"):
        extract_json_object('[{"a": 1}, {"b": 2}]', source="array.json")
