#!/usr/bin/env python3
"""Contract schema snapshot (#574).

``src/contracts/`` has versioned contracts twice without bumping
``contract_version`` -- once with the field added and the default left
alone, once with the model's own "Version history" docstring updated but
the default forgotten. ``scripts/validate_contracts.py`` and
``tests/contract/`` check that a contract *instance* validates; neither
checks that the *shape* and the *version* ever moved together.

This module is the harness for that check, the same shape as
``tests/e2e/routing_snapshot.py``'s per-query routing snapshot:

- ``all_contract_version_models()`` finds every Pydantic model defined
  directly in a ``src/contracts/*.py`` module that declares its own
  ``contract_version`` field, versioned or not. ``versioned_contract_models()``
  narrows that to the ones with a literal default -- the versioned contracts
  CONTRIBUTING.md's "Contract Changes" bump rule applies to.
  ``unallowlisted_unversioned_contracts()`` is the complement: any model
  with a required, no-default ``contract_version`` that is *not* one of the
  three in ``EXCLUDED_NO_DEFAULT`` (each with its own reason) -- a new one of
  those means a contract was added without picking a starting version.
- ``canonical_schema()`` turns ``BaseModel.model_json_schema()`` into a
  cosmetic-free, fully ``$ref``-resolved JSON Schema. Pydantic emits a
  nested model's ``$ref`` either alone or together with sibling keys (most
  often a field-level ``description``), and, in older JSON Schema drafts, as
  a single-entry ``allOf: [{"$ref": ...}]`` wrapper when a sibling needs
  attaching -- ``_resolve()`` recognizes all three shapes, inlines the
  referenced ``$defs`` entry, and merges in the siblings (after cosmetic
  keys are dropped from them too), so a nested model's own fields are never
  silently skipped. A ``$ref`` cycle resolves to a ``{"$cycle": name}``
  marker instead of recursing forever (not currently possible in these
  contracts, but cheap to guard).
- ``field_summary()`` converts a model's resolved schema into a *structural*
  tree (``_structural_node()``): each node is tagged with a ``kind``
  (``object``/``array``/``enum``/``const``/``anyOf``/``oneOf``/``scalar``)
  and keeps only what can change its shape -- nested ``properties`` (each
  with its own ``required`` flag and node), ``items``, ``enum`` values, and
  numeric/string/length constraints (``minimum``/``maximum``/
  ``exclusiveMinimum``/``exclusiveMaximum``/``minLength``/``maxLength``/
  ``pattern``/``minItems``/``maxItems``/``multipleOf``). This tree *is* what
  gets hashed (``schema_hash()``) and diffed -- there is only one
  representation, so the hash and the diff can never disagree about what
  changed.
- ``diff_fields()``/``diff_schema()`` walk two ``field_summary()`` trees in
  lockstep and report every change at the deepest path where it actually
  happened (e.g. ``agent_metadata.retries`` added, not "``agent_metadata``
  changed"), each tagged ``"major"`` or ``"minor"`` per CONTRIBUTING.md's
  "Contract Changes" rule: a field removed, a type changed, an optional
  field made required, an enum value removed, or a tightened constraint is
  MAJOR; an additive optional field, a new enum value, a required field
  relaxed to optional, or a loosened constraint is MINOR. One exception (#1006):
  a top-level ``contract_version`` adopting exactly ``VERSION_PATTERN`` (from an
  unpatterned string, or from a ``Literal`` whose value matches it) is MINOR, since
  every published version already matches it. ``overall_bump()``
  reduces a change list to the one bump CONTRIBUTING.md requires overall.
- ``evaluate_snapshot()`` is the full comparison between a committed and a
  freshly built snapshot (new/removed contracts, insufficient bumps, a
  version that moved without a regenerated snapshot) -- the single thing
  both ``tests/contract/test_schema_snapshot.py`` (the real contracts) and
  ``tests/unit/scripts/test_contract_schema_snapshot.py`` (synthetic models,
  one per case) exercise, so neither can drift from what ``--write``
  actually checks.

The public ``assess()`` contract (``src/assessment/models.py``, #1184) is versioned on its
own and promised to callers, so it is compared with the shape the last release shipped
rather than with main: ``build_public_snapshot()`` records ``PUBLIC_CONTRACTS`` with the
same ``field_summary()``, and ``evaluate_against_released()`` applies the same
classification against ``tests/contract/assess_released_snapshot.json`` (a breaking
change needs a MAJOR move of ``CONTRACT_VERSION``; once a release is recorded, an additive
one needs a MINOR move). The release step records the shipped shape with
``--write-released <tag>``.

Regenerate the committed snapshot deliberately, review it like the routing
snapshot (``git diff`` on the file -- it is ``indent=2``, one field change
per line)::

    uv run python scripts/contract_schema_snapshot.py --write

A pull request declares its bump with ``scripts/bump_contract.py`` and leaves this
file at main's until ``scripts/resolve_contract_versions.py`` assigns the number and
regenerates it after the last rebase (ADR-008 Decision 8, #1158), so two concurrent
contract-changing pull requests no longer both rewrite it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import re
import subprocess  # nosec B404 -- git and this interpreter, fixed arguments, no shell
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel

REPO = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = REPO / "src" / "contracts"
SNAPSHOT_PATH = REPO / "tests" / "contract" / "schema_snapshot.json"
REGENERATE_COMMAND = "uv run python scripts/contract_schema_snapshot.py --write"
RELEASED_SNAPSHOT_PATH = REPO / "tests" / "contract" / "assess_released_snapshot.json"
WRITE_RELEASED_COMMAND = "uv run python scripts/contract_schema_snapshot.py --write-released <tag>"

#: The public contract's models (key -> ``module:Class``), compared with the shape the
#: last release shipped (``evaluate_against_released``), not with main.
PUBLIC_CONTRACTS: dict[str, str] = {
    "assessment.AssessmentRequest": "src.assessment.models:AssessmentRequest",
    "assessment.AssessmentResult": "src.assessment.models:AssessmentResult",
}

# Keys pydantic puts in generated JSON Schema that describe documentation or
# are known to vary cosmetically between pydantic versions, never the shape
# of the data itself.
_COSMETIC_KEYS = {"title", "description", "default", "examples"}

# model.module_key -> why its contract_version field is required with no
# literal default (so it is excluded from versioned_contract_models()).
# Reviewed deliberately: any OTHER model with a required contract_version
# field is a contract added without picking a starting version, and
# unallowlisted_unversioned_contracts() below fails the test for it.
EXCLUDED_NO_DEFAULT: dict[str, str] = {
    "schema_design_input.AgentCollectorInput": (
        "Projection of CollectorOutputContract for schema design -- its "
        "contract_version must mirror whichever version the collector "
        "output actually produced, not carry its own default."
    ),
    "schema_design_input.AgentAnalysisInput": (
        "Projection of AnalysisOutputContract for schema design -- same "
        "reason as AgentCollectorInput above."
    ),
    "decision_trace_base.DecisionTraceBase": (
        "Shared core of the engines' decision-trace contracts (#1187); each "
        "engine's trace model sets its own contract_version default."
    ),
    "schema_design_output.SchemaDesignOutputBase": (
        "Shared envelope for engine-specific schema design outputs; no "
        "engine-specific contract currently subclasses it, so there is no "
        "concrete version to pin."
    ),
}


def all_contract_version_models() -> list[tuple[str, type[BaseModel]]]:
    """Every ``(module.ClassName, model)`` in ``src/contracts/*.py`` whose
    model declares its own ``contract_version`` field, default or not,
    sorted by key."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    found: list[tuple[str, type[BaseModel]]] = []
    for path in sorted(CONTRACTS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module_name = f"src.contracts.{path.stem}"
        # module_name comes from the repo's own src/contracts/*.py file list, not input.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        module = importlib.import_module(module_name)
        for name, obj in vars(module).items():
            if not isinstance(obj, type) or not issubclass(obj, BaseModel) or obj is BaseModel:
                continue
            if obj.__module__ != module_name:
                continue  # re-exported/imported here, not defined here -- avoid double-counting
            if "contract_version" not in obj.model_fields:
                continue
            found.append((f"{path.stem}.{name}", obj))
    found.sort(key=lambda item: item[0])
    return found


def versioned_contract_models() -> list[tuple[str, type[BaseModel]]]:
    """``all_contract_version_models()`` narrowed to models whose
    ``contract_version`` has a literal default -- the ones CONTRIBUTING.md's
    bump rule applies to, and what ``build_snapshot()`` snapshots."""
    return [
        (key, model)
        for key, model in all_contract_version_models()
        if not model.model_fields["contract_version"].is_required()
    ]


def unallowlisted_unversioned_contracts() -> list[str]:
    """Keys with a required, no-default ``contract_version`` that are not in
    ``EXCLUDED_NO_DEFAULT`` -- should always be empty; see module docstring."""
    return [
        key
        for key, model in all_contract_version_models()
        if model.model_fields["contract_version"].is_required() and key not in EXCLUDED_NO_DEFAULT
    ]


def stale_allowlist_entries() -> list[str]:
    """``EXCLUDED_NO_DEFAULT`` keys that no longer describe a required,
    no-default ``contract_version`` model (it was given a default, renamed,
    or removed) -- keeps the allowlist from silently going stale."""
    current = {
        key
        for key, model in all_contract_version_models()
        if model.model_fields["contract_version"].is_required()
    }
    return sorted(set(EXCLUDED_NO_DEFAULT) - current)


def _unwrap_allof_ref(node: dict[str, Any]) -> dict[str, Any]:
    """Pydantic (and older JSON Schema drafts generally) sometimes wraps a
    single ``$ref`` in ``allOf`` to attach a sibling keyword (e.g. a
    field-level description) to a referenced submodel, since draft-07
    disallows siblings next to ``$ref`` directly. Unwrap that pattern into
    one dict with ``$ref`` plus any siblings -- the same shape pydantic's
    newer sibling-key form already produces -- so ``_resolve()`` only needs
    to handle one case.
    """
    all_of = node.get("allOf")
    if (
        isinstance(all_of, list)
        and len(all_of) == 1
        and isinstance(all_of[0], dict)
        and "$ref" in all_of[0]
    ):
        merged = dict(all_of[0])
        for key, value in node.items():
            if key != "allOf":
                merged[key] = value
        return merged
    return node


def _resolve(node: Any, defs: dict[str, Any], seen: frozenset[str]) -> Any:
    """Recursively resolve every ``$ref`` -- alone, with siblings, or
    ``allOf``-wrapped -- against ``defs``, and drop cosmetic keys. See the
    module docstring for why all three shapes matter.
    """
    if isinstance(node, dict):
        node = _unwrap_allof_ref(node)
        if "$ref" in node:
            ref_name = node["$ref"].rsplit("/", 1)[-1]
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            if ref_name in seen:
                resolved = cast(dict[str, Any], {"$cycle": ref_name})
            else:
                resolved = _resolve(defs.get(ref_name, {}), defs, seen | {ref_name})
            resolved_siblings = _resolve(siblings, defs, seen)
            # Siblings win on key collision -- they're specified at the use
            # site, closer to what this exact field actually allows.
            return {**resolved, **resolved_siblings}
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _COSMETIC_KEYS or key == "$defs":
                continue
            out[key] = _resolve(value, defs, seen)
        if isinstance(out.get("required"), list):
            out["required"] = sorted(out["required"])
        if isinstance(out.get("enum"), list):
            out["enum"] = sorted(out["enum"], key=repr)
        for union_key in ("anyOf", "oneOf"):
            if isinstance(out.get(union_key), list):
                out[union_key] = sorted(out[union_key], key=lambda n: json.dumps(n, sort_keys=True))
        return out
    if isinstance(node, list):
        return [_resolve(v, defs, seen) for v in node]
    return node


def canonical_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A cosmetic-free, fully ``$ref``-resolved JSON Schema for ``model``."""
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    # The top-level model schema is always a JSON object schema (never bare
    # `{"$ref": ...}`), so this is always a dict; the cast only tells mypy
    # what `_resolve`'s `Any` return already guarantees at runtime.
    return cast(dict[str, Any], _resolve(schema, defs, frozenset()))


# Numeric/string/array constraint keywords pydantic emits in JSON Schema for
# Field(ge=, le=, gt=, lt=, min_length=, max_length=, pattern=, ...).
_CONSTRAINT_KEYS = (
    "minimum",
    "exclusiveMinimum",
    "maximum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "multipleOf",
)


def _structural_node(node: dict[str, Any]) -> dict[str, Any]:
    """A compact structural node for one *resolved* (post ``canonical_schema``)
    schema node -- see module docstring. Every nested object/array/union is
    itself a structural node, so ``diff_schema()`` can recurse to the exact
    path a change happened at.
    """
    if "$cycle" in node:
        return {"kind": "cycle", "name": node["$cycle"]}
    if "const" in node:
        return {"kind": "const", "value": node["const"]}
    if isinstance(node.get("enum"), list):
        return {"kind": "enum", "values": list(node["enum"])}
    for union_key in ("anyOf", "oneOf"):
        if isinstance(node.get(union_key), list):
            return {"kind": union_key, "members": [_structural_node(m) for m in node[union_key]]}
    kind = node.get("type")
    if kind == "array":
        result: dict[str, Any] = {"kind": "array", "items": _structural_node(node.get("items", {}))}
        for key in ("minItems", "maxItems"):
            if key in node:
                result[key] = node[key]
        return result
    if kind == "object":
        props = node.get("properties")
        if props:
            required = set(node.get("required", []))
            return {
                "kind": "object",
                "properties": {
                    name: {"required": name in required, "schema": _structural_node(props[name])}
                    for name in sorted(props)
                },
            }
        additional = node.get("additionalProperties")
        if isinstance(additional, dict):
            return {"kind": "object", "additional": _structural_node(additional)}
        return {"kind": "object"}
    result = {"kind": "scalar", "type": kind if kind is not None else "any"}
    for key in _CONSTRAINT_KEYS:
        if key in node:
            result[key] = node[key]
    return result


def field_summary(model: type[BaseModel]) -> dict[str, Any]:
    """``{field_name: {"required": bool, "schema": <structural node>}}`` for
    every top-level property of ``model`` -- the single representation that
    both ``schema_hash()`` hashes and ``diff_fields()`` diffs."""
    canonical = canonical_schema(model)
    props = canonical.get("properties", {})
    required = set(canonical.get("required", []))
    return {
        name: {"required": name in required, "schema": _structural_node(props[name])}
        for name in sorted(props)
    }


def schema_hash(fields: dict[str, Any]) -> str:
    """A stable hash of a ``field_summary()`` result."""
    blob = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_snapshot() -> dict[str, Any]:
    """``{model_key: {"version", "schema_hash", "fields"}}`` for every
    versioned contract model, freshly computed from ``src/contracts/``."""
    snapshot: dict[str, Any] = {}
    for key, model in versioned_contract_models():
        fields = field_summary(model)
        snapshot[key] = {
            "version": model.model_fields["contract_version"].default,
            "schema_hash": schema_hash(fields),
            "fields": fields,
        }
    return snapshot


_VERSION_MAJOR_RE = re.compile(r"^(\d+)\.")


def major_version(version: str) -> int:
    """The MAJOR component of a ``MAJOR.MINOR`` contract version string."""
    match = _VERSION_MAJOR_RE.match(version)
    if not match:
        raise ValueError(f"not a MAJOR.MINOR contract version: {version!r}")
    return int(match.group(1))


# A single reported change: (path, "major"|"minor", human description).
Change = tuple[str, str, str]

# Constraint direction: for these keys, a *larger* value tightens (raises the
# floor); for the rest, a *smaller* value tightens (lowers the ceiling).
_TIGHTENS_WHEN_INCREASED = {"minimum", "exclusiveMinimum", "minLength", "minItems"}


def _diff_constraints(before: dict[str, Any], after: dict[str, Any], path: str) -> list[Change]:
    """Per-constraint-key diff of two structural nodes at ``path``: a
    constraint added or tightened is MAJOR (it can reject previously-valid
    data), one removed or loosened is MINOR. ``pattern`` has no numeric
    direction, so any change to it (other than removal) is treated as MAJOR.
    """
    changes: list[Change] = []
    for key in _CONSTRAINT_KEYS:
        before_value, after_value = before.get(key), after.get(key)
        if before_value == after_value:
            continue
        if key == "pattern":
            if after_value is None:
                changes.append((path, "minor", "pattern constraint removed"))
            elif before_value is None:
                changes.append((path, "major", f"pattern constraint added: {after_value!r}"))
            else:
                changes.append(
                    (path, "major", f"pattern changed ({before_value!r} -> {after_value!r})")
                )
            continue
        if after_value is None:
            changes.append((path, "minor", f"{key} constraint removed (was {before_value!r})"))
            continue
        if before_value is None:
            changes.append((path, "major", f"{key} constraint added: {after_value!r}"))
            continue
        tightened = (
            after_value > before_value
            if key in _TIGHTENS_WHEN_INCREASED
            else after_value < before_value
        )
        bump = "major" if tightened else "minor"
        state = "tightened" if tightened else "loosened"
        changes.append((path, bump, f"{key} {before_value!r} -> {after_value!r} ({state})"))
    return changes


def _diff_enum(before: list[Any], after: list[Any], path: str) -> list[Change]:
    before_set, after_set = set(before), set(after)
    changes: list[Change] = []
    for value in sorted(after_set - before_set, key=repr):
        changes.append((path, "minor", f"enum value added: {value!r}"))
    for value in sorted(before_set - after_set, key=repr):
        changes.append((path, "major", f"enum value removed: {value!r}"))
    return changes


def _diff_union(
    before_members: list[dict[str, Any]], after_members: list[dict[str, Any]], path: str
) -> list[Change]:
    """Union (``anyOf``/``oneOf``) members matched by their own ``kind`` --
    the common case here is ``Optional[X]``, a 2-member union of X's kind
    and ``null``. A member kind that disappears narrows what's accepted
    (MAJOR); one that appears widens it (MINOR); a kind present in both is
    diffed recursively at the same path.
    """
    before_by_kind = {m["kind"]: m for m in before_members}
    after_by_kind = {m["kind"]: m for m in after_members}
    changes: list[Change] = []
    for kind in sorted(set(before_by_kind) | set(after_by_kind)):
        before_member, after_member = before_by_kind.get(kind), after_by_kind.get(kind)
        if before_member is None:
            changes.append((path, "minor", f"union gained member kind {kind!r}"))
        elif after_member is None:
            changes.append((path, "major", f"union lost member kind {kind!r}"))
        else:
            changes.extend(diff_schema(before_member, after_member, path))
    return changes


# The one contract_version declaration (ADR-008 Decision 1, #1006).
VERSION_PATTERN = r"^\d+\.\d+$"


def _version_pattern_adopted(before: dict[str, Any], after: dict[str, Any]) -> bool:
    """A top-level ``contract_version`` that gained exactly ``VERSION_PATTERN``, from an
    unpatterned string or from a ``Literal`` whose value already matches it. Every
    published version is MAJOR.MINOR, so no file ever written is refused: MINOR (#1006)."""
    if not (
        after.get("kind") == "scalar"
        and after.get("type") == "string"
        and after.get("pattern") == VERSION_PATTERN
        and set(after) == {"kind", "type", "pattern"}
    ):
        return False
    if before.get("kind") == "scalar":
        return before.get("type") == "string" and set(before) == {"kind", "type"}
    if before.get("kind") == "const":
        value = before.get("value")
        return isinstance(value, str) and re.match(VERSION_PATTERN, value) is not None
    return False


def diff_schema(before: dict[str, Any], after: dict[str, Any], path: str) -> list[Change]:
    """Path-level diff between two ``_structural_node()`` outputs at ``path``."""
    if path == "contract_version" and _version_pattern_adopted(before, after):
        return [(path, "minor", f"the version pattern {VERSION_PATTERN!r} adopted (#1006)")]
    if before.get("kind") != after.get("kind"):
        return [(path, "major", f"type changed ({before.get('kind')} -> {after.get('kind')})")]
    kind = before["kind"]
    if kind == "object":
        before_additional, after_additional = before.get("additional"), after.get("additional")
        if before_additional is not None or after_additional is not None:
            if before_additional is None or after_additional is None:
                return [(path, "major", "additionalProperties shape changed")]
            return diff_schema(before_additional, after_additional, f"{path}[*]")
        return diff_fields(before.get("properties", {}), after.get("properties", {}), path)
    if kind == "array":
        changes = diff_schema(before.get("items", {}), after.get("items", {}), f"{path}[]")
        changes.extend(_diff_constraints(before, after, path))
        return changes
    if kind == "enum":
        return _diff_enum(before["values"], after["values"], path)
    if kind == "const":
        if before["value"] != after["value"]:
            return [(path, "major", f"const changed ({before['value']!r} -> {after['value']!r})")]
        return []
    if kind in ("anyOf", "oneOf"):
        return _diff_union(before["members"], after["members"], path)
    if kind == "cycle":
        if before["name"] != after["name"]:
            return [
                (path, "major", f"cycle target changed ({before['name']!r} -> {after['name']!r})")
            ]
        return []
    # scalar
    changes = []
    if before.get("type") != after.get("type"):
        changes.append(
            (path, "major", f"type changed ({before.get('type')} -> {after.get('type')})")
        )
    changes.extend(_diff_constraints(before, after, path))
    return changes


def diff_fields(before: dict[str, Any], after: dict[str, Any], path: str = "") -> list[Change]:
    """Path-level diff between two ``field_summary()``-shaped property maps
    (also used, at a non-root ``path``, to diff two nested ``object`` nodes'
    ``properties``)."""
    changes: list[Change] = []
    for name in sorted(set(before) | set(after)):
        child_path = f"{path}.{name}" if path else name
        before_field, after_field = before.get(name), after.get(name)
        if before_field is None:
            assert after_field is not None  # nosec B101 -- name came from the union
            state = "required" if after_field["required"] else "optional"
            bump = "major" if after_field["required"] else "minor"
            changes.append((child_path, bump, f"added ({state})"))
            continue
        if after_field is None:
            changes.append((child_path, "major", "removed"))
            continue
        if before_field["required"] != after_field["required"]:
            became_required = after_field["required"]
            bump = "major" if became_required else "minor"
            direction = "optional->required" if became_required else "required->optional"
            changes.append((child_path, bump, direction))
        changes.extend(diff_schema(before_field["schema"], after_field["schema"], child_path))
    return changes


_KIND_RANK = {"none": 0, "minor": 1, "major": 2}


def overall_bump(changes: list[Change]) -> str:
    """``"major"``, ``"minor"`` or ``"none"`` -- the bump CONTRIBUTING.md's
    Contract Changes rule requires overall, given every per-path change."""
    if any(bump == "major" for _, bump, _ in changes):
        return "major"
    if changes:
        return "minor"
    return "none"


def _format_changes(changes: list[Change]) -> str:
    return "; ".join(f"{path}: {description} ({bump})" for path, bump, description in changes)


def evaluate_snapshot(
    committed: dict[str, Any], actual: dict[str, Any], pending: Mapping[str, str] | None = None
) -> list[str]:
    """Every problem between a committed snapshot and a freshly built one,
    one string per contract -- shared by ``tests/contract/test_schema_snapshot.py``
    (the real contracts) and ``tests/unit/scripts/test_contract_schema_snapshot.py``
    (synthetic models, one per case), so neither can drift from what
    ``--write`` and the real test actually check. See module docstring for
    the bump rules.

    ``pending`` maps a contract to the bump kind a pending file declares for it
    (``scripts/contract_bumps.py``, ADR-008 Decision 8). A shape change that a
    pending bump covers is accepted with the version and the snapshot left at
    main's; ``scripts/resolve_contract_versions.py`` writes both after the last
    rebase. A pending kind smaller than the change requires is a problem. The version
    a pending contract's committed entry records is not compared (#1393): the resolver
    writes it, and it goes stale whenever another pull request's bump of the same
    contract lands first; ``resolve_contract_versions.py --check --against`` checks
    that the branch left the version at main's.
    """
    pending = pending or {}
    problems: list[str] = []
    for key in sorted(set(committed) | set(actual)):
        before, after = committed.get(key), actual.get(key)
        if before is None:
            problems.append(
                f"{key}: new contract, not in the committed snapshot. Run: {REGENERATE_COMMAND}"
            )
            continue
        if after is None:
            problems.append(
                f"{key}: contract removed from src/contracts/ but still in the committed "
                f"snapshot. Run: {REGENERATE_COMMAND}"
            )
            continue
        declared = pending.get(key)
        if declared is not None:
            before = {**before, "version": after["version"]}
        if before == after:
            continue

        changes = diff_fields(before["fields"], after["fields"])
        bump = overall_bump(changes)
        if changes:
            change_text = _format_changes(changes)
        else:
            # The field tree is unchanged but the hash moved: the shape changed at a
            # level diff_fields() doesn't surface. Should not happen given
            # field_summary() is exactly what's hashed, but floor it at MINOR
            # rather than claim nothing happened.
            bump = "minor"
            change_text = "(schema changed without a field-level diff -- investigate)"

        if before["schema_hash"] == after["schema_hash"] and before["version"] != after["version"]:
            # Version-only move (the hash is on field_summary(), which doesn't include
            # contract_version's own default): still must land with a regenerated,
            # committed snapshot.
            problems.append(
                f"{key}: contract_version changed ({before['version']!r} -> "
                f"{after['version']!r}) but the committed snapshot wasn't regenerated to "
                f"match. Run: {REGENERATE_COMMAND}"
            )
            continue

        if declared is not None:
            if _KIND_RANK[declared] >= _KIND_RANK[bump]:
                continue
            problems.append(
                f"{key}: schema changed ({change_text}), which needs a {bump.upper()} bump, but "
                f"the pending bump declares only {declared.upper()}. Run: "
                f"uv run python scripts/bump_contract.py {key} {bump} --reason ..."
            )
            continue

        if before["version"] == after["version"]:
            problems.append(
                f"{key}: schema changed ({change_text}) but contract_version is still "
                f"{after['version']!r}. CONTRIBUTING.md's Contract Changes section requires "
                f"a {bump.upper()} version bump here. Declare it with: uv run python "
                f"scripts/bump_contract.py {key} {bump} --reason ... (the number is assigned "
                f"after your last rebase, ADR-008 Decision 8)"
            )
            continue

        if bump == "major" and major_version(after["version"]) <= major_version(before["version"]):
            problems.append(
                f"{key}: {change_text} requires a MAJOR version bump (contract_version's "
                f"leading number must increase past {major_version(before['version'])}), but "
                f"it only moved to {after['version']!r}. See CONTRIBUTING.md's Contract "
                f"Changes section."
            )
            continue

        # Version moved by a sufficient bump, but the committed snapshot's hash/fields
        # still don't match a fresh rebuild -- it simply wasn't regenerated after the change.
        problems.append(
            f"{key}: contract_version moved {before['version']!r} -> {after['version']!r} "
            f"({change_text}) but the committed snapshot wasn't regenerated to match. "
            f"Run: {REGENERATE_COMMAND}"
        )
    return problems


def evaluate_against_base(
    base: dict[str, Any], actual: dict[str, Any], pending: Mapping[str, str] | None = None
) -> list[str]:
    """Every shape change since ``base`` (main's committed snapshot) that did not move
    the version enough and has no pending bump that covers it (ADR-008 Decisions 3
    and 8, #1158).

    ``evaluate_snapshot`` compares a fresh build with the branch's own committed
    snapshot, so a branch that changes a contract, forgets to declare the bump and
    regenerates the snapshot passes it. This compares with main's snapshot instead:
    the ``validate-contracts`` job and ``ci/pre_merge.sh`` run it against the merge
    base. A contract new or removed since ``base`` is left to ``evaluate_snapshot``.
    """
    pending = pending or {}
    problems: list[str] = []
    for key in sorted(set(base) & set(actual)):
        before, after = base[key], actual[key]
        changes = diff_fields(before["fields"], after["fields"])
        if not changes:
            continue
        bump = overall_bump(changes)
        detail = _format_changes(changes)
        if before["version"] == after["version"]:
            declared = pending.get(key)
            if declared is not None and _KIND_RANK[declared] >= _KIND_RANK[bump]:
                continue
            problems.append(
                f"{key}: schema changed since main ({detail}) but contract_version is still "
                f"{after['version']!r} and no pending bump covers it"
                + (f" (the pending bump declares only {declared.upper()})" if declared else "")
                + f". Run: uv run python scripts/bump_contract.py {key} {bump} --reason ..."
            )
        elif bump == "major" and major_version(after["version"]) <= major_version(
            before["version"]
        ):
            problems.append(
                f"{key}: schema changed since main ({detail}), which needs a MAJOR bump, but "
                f"contract_version only moved {before['version']!r} -> {after['version']!r}."
            )
    return problems


def public_contract_models(root: Path | None = None) -> list[tuple[str, type[BaseModel]]]:
    """``(key, model)`` for every ``PUBLIC_CONTRACTS`` entry, in key order.

    ``root`` (a source tree other than this one, such as a release tag's) is put first
    on ``sys.path``, and every model must come from it; only ``--print-public`` in a
    fresh process passes it, since ``src`` is imported once per process."""
    base = REPO if root is None else root.resolve()
    if root is not None:
        sys.path.insert(0, str(base))
    elif str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    found: list[tuple[str, type[BaseModel]]] = []
    for key in sorted(PUBLIC_CONTRACTS):
        module_name, _, class_name = PUBLIC_CONTRACTS[key].partition(":")
        # module_name comes from the PUBLIC_CONTRACTS constant above, not input.
        # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
        module = importlib.import_module(module_name)
        if root is not None and not Path(module.__file__ or "").resolve().is_relative_to(base):
            raise RuntimeError(f"{module_name} was imported from {module.__file__}, not {base}")
        found.append((key, getattr(module, class_name)))
    return found


def build_public_snapshot(
    models: Mapping[str, type[BaseModel]] | None = None,
) -> dict[str, Any]:
    """``{key: {"version", "schema_hash", "fields"}}`` for the public contract (or the
    given models): the same entry ``build_snapshot()`` writes for an internal contract."""
    chosen = dict(public_contract_models()) if models is None else dict(models)
    snapshot: dict[str, Any] = {}
    for key in sorted(chosen):
        model = chosen[key]
        fields = field_summary(model)
        snapshot[key] = {
            "version": model.model_fields["contract_version"].default,
            "schema_hash": schema_hash(fields),
            "fields": fields,
        }
    return snapshot


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def evaluate_against_released(released: Mapping[str, Any], actual: dict[str, Any]) -> list[str]:
    """Every public-contract change since the last release that its version does not
    cover (#1184).

    ``released`` is ``tests/contract/assess_released_snapshot.json``: ``{"released": tag
    or None, "contracts": {key: entry}}``. A change ``diff_fields`` classifies as MAJOR
    (a field removed or renamed, a type changed, an enum value removed or changed, a
    field made required) needs the MAJOR number to rise. Once a release is recorded
    (``released`` is a tag), a MINOR change needs the version to rise; before the first
    release, additions keep the draft's version (docs/contracts/assess.md). The
    ``contract_version`` field itself is a ``Literal`` of the version, so its own move is
    the bump, never a change.
    """
    tag = released.get("released")
    before_all: dict[str, Any] = released.get("contracts", {})
    problems: list[str] = []
    for key in sorted(set(before_all) | set(actual)):
        before, after = before_all.get(key), actual.get(key)
        if after is None:
            problems.append(
                f"{key}: public contract removed since the last release (a MAJOR change); "
                "callers built on it"
            )
            continue
        if before is None:
            problems.append(
                f"{key}: public contract not in the released snapshot. Record it with: "
                f"{WRITE_RELEASED_COMMAND}"
            )
            continue
        old, new = before["version"], after["version"]
        if _version_tuple(new) < _version_tuple(old):
            problems.append(f"{key}: contract_version went back ({old!r} -> {new!r})")
            continue
        changes = [
            c for c in diff_fields(before["fields"], after["fields"]) if c[0] != "contract_version"
        ]
        bump = overall_bump(changes)
        since = f"since {tag}" if tag else "since the recorded draft"
        if bump == "major" and major_version(new) <= major_version(old):
            problems.append(
                f"{key}: {_format_changes(changes)} {since} breaks callers, which needs a "
                f"MAJOR bump of CONTRACT_VERSION (src/assessment/models.py), but it is "
                f"{new!r} (was {old!r}). Keep the field, or bump the MAJOR version and add "
                "the next schema file (docs/contracts/schemas/README.md)."
            )
        elif bump == "minor" and tag and new == old:
            problems.append(
                f"{key}: {_format_changes(changes)} {since} needs a MINOR bump of "
                f"CONTRACT_VERSION (src/assessment/models.py), but it is still {new!r}."
            )
    return problems


def _git(repo: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess[Any]:
    # Fixed git subcommands; refs and paths are separate arguments, never a shell.
    return subprocess.run(  # nosec B603 B607
        ["git", "-C", str(repo), *args], capture_output=True, text=text, check=False
    )


def tag_exists(tag: str, repo: Path = REPO) -> bool:
    """Whether ``tag`` is a tag in ``repo`` (an exact ``refs/tags/`` name)."""
    return (
        _git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}").returncode
        == 0
    )


def public_snapshot_at(ref: str, repo: Path = REPO) -> dict[str, Any]:
    """``build_public_snapshot()`` of the models as ``ref`` has them, not this tree's.

    ``src/`` at ``ref`` is extracted to a temporary directory (``git archive``, no
    worktree or ref changes) and a fresh process imports the models from there and
    prints their snapshot with this script's own classification."""
    archive = _git(repo, "archive", "--format=tar", ref, "src", text=False)
    if archive.returncode != 0:
        raise RuntimeError(f"cannot read src/ at {ref}: {archive.stderr.decode().strip()}")
    with tempfile.TemporaryDirectory(prefix="public-contract-") as root:
        # The data filter below rejects absolute paths, ".." and links outside root, and
        # the archive is this repository's own src/ at ref.
        # nosemgrep: trailofbits.python.tarfile-extractall-traversal.tarfile-extractall-traversal
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(root, filter="data")
        done = subprocess.run(  # nosec B603 -- this interpreter and this script, fixed flags
            [sys.executable, str(Path(__file__).resolve()), "--print-public", "--root", root],
            capture_output=True,
            text=True,
            check=False,
        )
    if done.returncode != 0:
        raise RuntimeError(f"cannot build the public contract at {ref}: {done.stderr.strip()}")
    loaded: dict[str, Any] = json.loads(done.stdout)
    return loaded


def released_document(tag: str | None, repo: Path = REPO) -> dict[str, Any]:
    """The released snapshot for ``tag``: the public contract's shape at that tag, read
    from the tag itself (so running it on a later main never records main's shape). With
    no tag, this tree's shape as the unreleased draft."""
    if tag is not None and not tag_exists(tag, repo):
        raise SystemExit(
            f"contract_schema_snapshot: {tag!r} is not a tag here; create or fetch the "
            "release tag first (git fetch --tags)"
        )
    return {
        "about": (
            "The public assess() contract's shape at the last release (#1184). Compared by "
            "tests/contract/test_assess_released_contract.py and, against the base branch's "
            "copy, by scripts/resolve_contract_versions.py --check --against. Recorded by the "
            "release step, never by hand: " + WRITE_RELEASED_COMMAND
        ),
        "released": tag,
        "contracts": build_public_snapshot() if tag is None else public_snapshot_at(tag, repo),
    }


def write_released(
    tag: str | None, repo: Path = REPO, path: Path = RELEASED_SNAPSHOT_PATH
) -> dict[str, Any]:
    """Write ``path``: the public contract's shape as ``tag`` ships it."""
    document = released_document(tag, repo)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return document


def check_released_against(
    base: str,
    repo: Path = REPO,
    path: Path = RELEASED_SNAPSHOT_PATH,
    actual: dict[str, Any] | None = None,
) -> list[str]:
    """``evaluate_against_released`` with the base branch's copy of the released snapshot,
    so a pull request cannot pass by re-recording the baseline it is checked against.

    A branch copy that differs from ``base``'s is used only when it records an existing
    tag with exactly that tag's shape (the release step); otherwise that is a problem and
    the base copy is used. With no copy at ``base`` (the file's first commit), the
    branch's copy is used."""
    shown = _git(repo, "show", f"{base}:{path.relative_to(repo).as_posix()}")
    base_doc = json.loads(shown.stdout) if shown.returncode == 0 else None
    branch_doc = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    problems: list[str] = []
    effective = branch_doc if base_doc is None else base_doc
    if base_doc is not None and branch_doc is not None and branch_doc != base_doc:
        tag = branch_doc.get("released")
        name = path.relative_to(repo).as_posix()
        if not tag or not tag_exists(tag, repo):
            problems.append(
                f"{name} changed since {base} without recording an existing release tag "
                f"(released: {tag!r}); only the release step changes it: {WRITE_RELEASED_COMMAND}"
            )
        elif branch_doc.get("contracts") != public_snapshot_at(tag, repo):
            problems.append(
                f"{name} records {tag} with a shape that is not {tag}'s; record it with: "
                f"uv run python scripts/contract_schema_snapshot.py --write-released {tag}"
            )
        else:
            effective = branch_doc
    if effective is None:
        return problems
    current = build_public_snapshot() if actual is None else actual
    return problems + evaluate_against_released(effective, current)


def write_snapshot() -> dict[str, Any]:
    """Build and commit-write the snapshot, returning what was written."""
    snapshot = build_snapshot()
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    return snapshot


def _print_diff(previous: dict[str, Any], actual: dict[str, Any]) -> None:
    """Print, per contract, what changed -- so ``--write``'s own output is
    the thing a reviewer checks, the same convention as the routing
    snapshot's printed diff."""
    changed_keys = sorted(
        k for k in set(previous) | set(actual) if previous.get(k) != actual.get(k)
    )
    if not changed_keys:
        print("no contract schema changes")
        return
    for key in changed_keys:
        before, after = previous.get(key), actual.get(key)
        if before is None:
            assert after is not None  # nosec B101 -- key came from the union, one side is set
            print(f"{key}: new contract (version {after['version']!r})")
            continue
        if after is None:
            print(f"{key}: removed")
            continue
        changes = diff_fields(before["fields"], after["fields"])
        bump = overall_bump(changes)
        detail = _format_changes(changes) if changes else "no field-level change"
        print(
            f"{key}: version {before['version']!r} -> {after['version']!r} ({bump} bump); {detail}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write", action="store_true", help="regenerate tests/contract/schema_snapshot.json"
    )
    parser.add_argument(
        "--write-released",
        metavar="TAG",
        help=(
            "record the public assess() contract's shape at release TAG (read from the tag) "
            "in tests/contract/assess_released_snapshot.json (the release step)"
        ),
    )
    parser.add_argument("--print-public", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.print_public:
        models = dict(public_contract_models(args.root))
        print(json.dumps(build_public_snapshot(models), sort_keys=True))
        return 0
    if args.write_released is not None:
        tag = args.write_released.strip() or None
        document = write_released(tag)
        versions = {key: entry["version"] for key, entry in document["contracts"].items()}
        print(
            json.dumps(
                {
                    "recorded": RELEASED_SNAPSHOT_PATH.relative_to(REPO).as_posix(),
                    "released": tag,
                    "versions": versions,
                }
            )
        )
        return 0
    if not args.write:
        print(json.dumps(build_snapshot(), indent=2, sort_keys=True))
        return 0
    previous = json.loads(SNAPSHOT_PATH.read_text()) if SNAPSHOT_PATH.exists() else {}
    actual = write_snapshot()
    _print_diff(previous, actual)
    print(f"wrote {SNAPSHOT_PATH.relative_to(REPO)} ({len(actual)} contracts)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
