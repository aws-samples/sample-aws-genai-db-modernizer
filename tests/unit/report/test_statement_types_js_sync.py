# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``STATEMENT_TYPES`` in ``src/ui/src/utils/accessPatterns.js`` covers every
collector ``QueryType`` (#2480 review).

The results page's "Operation type" filter types a query no access pattern serves by
its source statement. A statement type the JS map leaves out reads as "other", so a
new collector type would drop silently to "other". This test parses the JS object
literal and checks it against the contract enum.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.contracts.collector_output import QueryType

JS_PATH = Path(__file__).resolve().parents[3] / "src" / "ui" / "src" / "utils" / "accessPatterns.js"

_ENTRY_RE = re.compile(r"(\w+):\s*'(\w+)'")


def _statement_types(js_text: str) -> dict[str, str]:
    start = js_text.index("const STATEMENT_TYPES = {")
    end = js_text.index("};", start)
    return dict(_ENTRY_RE.findall(js_text[start:end]))


def test_every_collector_query_type_has_an_operation_type() -> None:
    types = _statement_types(JS_PATH.read_text(encoding="utf-8"))
    assert types, "failed to parse STATEMENT_TYPES from accessPatterns.js"
    assert {q.value for q in QueryType} <= set(types)


def test_writes_and_other_are_typed_as_the_collector_means_them() -> None:
    types = _statement_types(JS_PATH.read_text(encoding="utf-8"))
    assert types["SELECT"] == "read"
    assert types["MERGE"] == "write"
    assert types["OTHER"] == "other"
    assert [k for k, v in types.items() if v == "read"] == ["SELECT"]
