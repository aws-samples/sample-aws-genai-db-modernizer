# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Whether a tool schema compiles under Bedrock strict tool use (#744).

A role with ``RoleSpec.strict_tools`` sends ``strict: true`` on every tool in its
call (``src/shared/models.py``). Bedrock compiles each strict schema into a
constrained-decoding grammar and rejects, with a 400, any schema outside the
subset it supports. A rejected schema fails every call of the role, so the tests
check each strict role's schemas with :func:`strict_schema_violations` before a
change ships.

The subset is the one Bedrock enforced when measured on Sonnet 5.5 through
Converse (2026-11-06), which is wider than the Amazon Bedrock structured-output
guide states in two places:

* ``minLength`` and ``maxLength`` are accepted (the guide lists them as
  unsupported);
* a request may carry up to 24 optional properties (the guide says 12); 25 is
  rejected ("too many optional parameters").

Rejected, as the guide says: ``oneOf`` (and the other combinators but ``anyOf`` and
``allOf``), numeric bounds (``minimum``, ``maximum``, ``exclusiveMinimum``,
``exclusiveMaximum``, ``multipleOf``), ``maxItems``, ``minItems`` other than 0 or
1, ``additionalProperties`` other than ``false``, more than 16 union-typed
properties (a type array or ``anyOf``), recursive or external ``$ref``, and
``pattern`` with lookaround or backreferences.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: Most optional properties one strict request may carry, across every object
#: (measured: 24 compiles, 25 is rejected).
MAX_OPTIONAL_PROPERTIES = 24
#: Most properties typed as a union (a type array or ``anyOf``) one request may carry.
MAX_UNION_PROPERTIES = 16

#: Keywords Bedrock strict mode rejects wherever they appear in a schema node.
UNSUPPORTED_KEYWORDS = frozenset(
    {
        "oneOf",
        "not",
        "if",
        "then",
        "else",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "maxItems",
        "uniqueItems",
        "contains",
        "minContains",
        "maxContains",
        "prefixItems",
        "patternProperties",
        "propertyNames",
        "minProperties",
        "maxProperties",
        "dependentRequired",
        "dependentSchemas",
        "unevaluatedProperties",
        "unevaluatedItems",
    }
)

#: String formats strict mode supports.
SUPPORTED_FORMATS = frozenset(
    {"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"}
)

_UNSUPPORTED_REGEX = re.compile(r"\(\?<?[=!]|\\[1-9]|\\k<")


def strict_schema_violations(schema: Mapping[str, Any]) -> list[str]:
    """Every reason Bedrock strict mode would reject ``schema``, or ``[]`` when it compiles.

    ``schema`` is the tool input schema as sent, after Strands has added
    ``additionalProperties: false`` to each ``type: "object"`` node (it skips a
    nullable object, ``type: ["object", "null"]``). Each reason names the JSON
    path of the node that breaks the subset.
    """
    defs: Mapping[str, Any] = {
        **(schema.get("definitions") or {}),
        **(schema.get("$defs") or {}),
    }
    found: list[str] = []
    counts = {"optional": 0, "union": 0}
    _walk(schema, "$", defs, (), found, counts)
    if counts["optional"] > MAX_OPTIONAL_PROPERTIES:
        found.append(
            f"$: {counts['optional']} optional properties (at most {MAX_OPTIONAL_PROPERTIES})"
        )
    if counts["union"] > MAX_UNION_PROPERTIES:
        found.append(
            f"$: {counts['union']} union-typed properties (at most {MAX_UNION_PROPERTIES})"
        )
    return found


def _walk(
    node: Any,
    path: str,
    defs: Mapping[str, Any],
    refs: tuple[str, ...],
    found: list[str],
    counts: dict[str, int],
) -> None:
    if not isinstance(node, Mapping):
        return
    for keyword in sorted(UNSUPPORTED_KEYWORDS & node.keys()):
        found.append(f"{path}: '{keyword}' is not supported")
    ref = node.get("$ref")
    if isinstance(ref, str):
        name = ref.rsplit("/", 1)[-1]
        if not ref.startswith("#/"):
            found.append(f"{path}: external $ref {ref!r}")
        elif name in refs:
            found.append(f"{path}: recursive $ref {ref!r}")
        elif name not in defs:
            found.append(f"{path}: unresolved $ref {ref!r}")
        else:
            # Expanded at each use: the grammar is compiled from the expanded schema.
            _walk(defs[name], path, defs, (*refs, name), found, counts)
    min_items = node.get("minItems")
    if min_items is not None and min_items not in (0, 1):
        found.append(f"{path}: minItems {min_items} (only 0 or 1)")
    if "format" in node and node["format"] not in SUPPORTED_FORMATS:
        found.append(f"{path}: format {node['format']!r} is not supported")
    pattern = node.get("pattern")
    if isinstance(pattern, str) and _UNSUPPORTED_REGEX.search(pattern):
        found.append(f"{path}: pattern uses lookaround or a backreference")
    enum = node.get("enum")
    if isinstance(enum, list) and any(
        not (value is None or isinstance(value, str | int | float | bool)) for value in enum
    ):
        found.append(f"{path}: enum values must be strings, numbers, booleans or null")
    is_object = node.get("type") == "object" or "properties" in node
    if is_object and node.get("additionalProperties", None) is not False:
        found.append(f"{path}: additionalProperties must be false")
    properties = node.get("properties")
    if isinstance(properties, Mapping):
        required = set(node.get("required") or [])
        for name, sub in properties.items():
            if name not in required:
                counts["optional"] += 1
            if isinstance(sub, Mapping) and (isinstance(sub.get("type"), list) or "anyOf" in sub):
                counts["union"] += 1
            _walk(sub, f"{path}.{name}", defs, refs, found, counts)
    items = node.get("items")
    if isinstance(items, Mapping):
        _walk(items, f"{path}[]", defs, refs, found, counts)
    elif isinstance(items, list):
        found.append(f"{path}: tuple 'items' is not supported")
    for keyword in ("anyOf", "allOf"):
        for index, sub in enumerate(node.get(keyword) or []):
            _walk(sub, f"{path}.{keyword}[{index}]", defs, refs, found, counts)
