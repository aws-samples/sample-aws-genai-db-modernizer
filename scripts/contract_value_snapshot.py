#!/usr/bin/env python3
"""Published value-set snapshot (#975, ADR-008 Decision 3).

``scripts/contract_schema_snapshot.py`` (#574) catches a field-shape change
that did not move ``contract_version``. It cannot see a value renamed or
removed *inside* a field that is a plain ``str`` (the capability id
``inverted_index`` became ``text_pattern_match`` in #493 with "no schema
change, version unchanged"), nor an enum or id list that no versioned model
field spells out. This module snapshots those published value sets so the
same bump rule applies to them:

- every ``Enum`` class and module-level ``Literal`` alias defined in
  ``src/contracts/*.py`` (reason codes, flag reasons, phases, ...),
- the engine ids of the engine catalog,
- the capability ids a triage ``query_capabilities`` entry can carry,
- the engine prefixes of the access-pattern ids (``DDB-AP-1`` -> ``DDB``),
  read from the four ``*_model_output`` contracts' ``pattern_id`` field text.

Each set records the version of the contract module that publishes it. That
version is the highest ``contract_version`` default of a versioned model in
the module, else the newest entry of the module docstring's "Version History"
(``VERSION_SOURCE_OVERRIDES`` names the module whose version applies when the
module has neither). Adding a value needs that version to change (MINOR or
more); removing or renaming one needs its MAJOR number to increase.

Regenerate deliberately and review the printed diff::

    uv run python scripts/contract_value_snapshot.py --write

``tests/contract/test_value_set_snapshot.py`` runs the comparison on the real
contracts; ``tests/unit/scripts/test_contract_value_snapshot.py`` runs it on
synthetic cases.
"""

from __future__ import annotations

import argparse
import enum
import importlib
import json
import re
import sys
import typing
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.contract_schema_snapshot import (  # noqa: E402
    CONTRACTS_DIR,
    major_version,
    versioned_contract_models,
)

SNAPSHOT_PATH = REPO / "tests" / "contract" / "value_set_snapshot.json"
REGENERATE_COMMAND = "uv run python scripts/contract_value_snapshot.py --write"

# module -> module whose version governs it, for contract modules that declare
# neither a versioned model nor a Version History of their own.
VERSION_SOURCE_OVERRIDES: dict[str, str] = {
    # The query trace is embedded in the synthesis output; its docstring says so.
    "query_trace": "synthesis_output",
}

_HISTORY_RE = re.compile(r"^-\s+(\d+)\.(\d+)\s+\(", re.MULTILINE)
_PATTERN_ID_PREFIX_RE = re.compile(r"'([A-Z]+)-AP-\d+'")


def _module(stem: str) -> Any:
    # stem comes from the repo's own src/contracts/*.py file list, not input.
    # nosemgrep: python.lang.security.audit.non-literal-import.non-literal-import
    return importlib.import_module(f"src.contracts.{stem}")


def module_version(stem: str) -> str | None:
    """The version of contract module ``stem``, or ``None`` if it has none."""
    versions = [
        model.model_fields["contract_version"].default
        for key, model in versioned_contract_models()
        if key.split(".", 1)[0] == stem
    ]
    if versions:
        return str(max(versions, key=lambda v: tuple(int(p) for p in v.split("."))))
    entries = [(int(a), int(b)) for a, b in _HISTORY_RE.findall(_module(stem).__doc__ or "")]
    if entries:
        major, minor = max(entries)
        return f"{major}.{minor}"
    if stem in VERSION_SOURCE_OVERRIDES:
        return module_version(VERSION_SOURCE_OVERRIDES[stem])
    return None


def contract_value_sets() -> dict[str, list[str]]:
    """``{"<module>.<Name>": sorted values}`` for every Enum class and module-level
    Literal alias defined in ``src/contracts/*.py``."""
    found: dict[str, list[str]] = {}
    for path in sorted(CONTRACTS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        module = _module(path.stem)
        for name, obj in vars(module).items():
            if isinstance(obj, type) and issubclass(obj, enum.Enum):
                if obj.__module__ == module.__name__:
                    found[f"{path.stem}.{name}"] = sorted(str(m.value) for m in obj)
            elif typing.get_origin(obj) is typing.Literal:
                found[f"{path.stem}.{name}"] = sorted(str(v) for v in typing.get_args(obj))
    return found


def _engine_ids() -> list[str]:
    from src.shared import catalog

    return sorted(catalog.engine_ids())


def _capability_ids() -> list[str]:
    from src.agents.referee.capability_registry import CAPABILITY_FACTS, ENGINE_CAPABILITIES

    ids = set(CAPABILITY_FACTS)
    for capabilities in ENGINE_CAPABILITIES.values():
        ids |= capabilities
    return sorted(ids)


def _access_pattern_prefixes(stem: str) -> Callable[[], list[str]]:
    def load() -> list[str]:
        for obj in vars(_module(stem)).values():
            fields = getattr(obj, "model_fields", None)
            if isinstance(obj, type) and fields and "pattern_id" in fields:
                text = fields["pattern_id"].description or ""
                found = _PATTERN_ID_PREFIX_RE.findall(text)
                if found:
                    return sorted(set(found))
        return []

    return load


# key -> (module whose version governs it, loader). Values no contract class
# spells out, so no schema snapshot sees them.
REGISTERED_VALUE_SETS: dict[str, tuple[str, Callable[[], list[str]]]] = {
    "engine_ids": ("assignment_models", _engine_ids),
    "capability_ids": ("triage_output", _capability_ids),
    "access_pattern_id_prefixes.dynamodb": (
        "dynamodb_model_output",
        _access_pattern_prefixes("dynamodb_model_output"),
    ),
    "access_pattern_id_prefixes.documentdb": (
        "documentdb_model_output",
        _access_pattern_prefixes("documentdb_model_output"),
    ),
    "access_pattern_id_prefixes.elasticache": (
        "elasticache_model_output",
        _access_pattern_prefixes("elasticache_model_output"),
    ),
    "access_pattern_id_prefixes.opensearch": (
        "opensearch_model_output",
        _access_pattern_prefixes("opensearch_model_output"),
    ),
}


def build_snapshot() -> dict[str, Any]:
    """``{set_key: {"module": ..., "version": ..., "values": [...]}}`` computed
    now from the contracts and registries."""
    snapshot: dict[str, Any] = {}
    for key, values in contract_value_sets().items():
        stem = key.split(".", 1)[0]
        snapshot[key] = {"module": stem, "version": module_version(stem), "values": values}
    for key, (stem, loader) in REGISTERED_VALUE_SETS.items():
        snapshot[key] = {"module": stem, "version": module_version(stem), "values": loader()}
    return snapshot


def unversioned_value_sets(snapshot: dict[str, Any]) -> list[str]:
    """Keys of sets whose governing module has no version to bump."""
    return sorted(k for k, v in snapshot.items() if v["version"] is None)


def evaluate_snapshot(
    committed: dict[str, Any],
    actual: dict[str, Any],
    pending_modules: Mapping[str, str] | None = None,
) -> list[str]:
    """Every problem between the committed and a fresh snapshot, one string per set.

    ``pending_modules`` maps a contract module to the strongest bump kind its pending
    files declare (ADR-008 Decision 8): a value change that kind covers is accepted
    with the version left at main's until ``scripts/resolve_contract_versions.py``.
    """
    pending_modules = pending_modules or {}
    problems: list[str] = []
    for key in sorted(set(committed) | set(actual)):
        before, after = committed.get(key), actual.get(key)
        if before is None:
            problems.append(
                f"{key}: new value set, not in the committed snapshot. Run: {REGENERATE_COMMAND}"
            )
            continue
        if after is None:
            problems.append(
                f"{key}: value set removed (or its values moved out of src/contracts/) but "
                f"still in the committed snapshot. Removing a published value set is a "
                f"MAJOR change of {before['module']}. Run: {REGENERATE_COMMAND}"
            )
            continue
        if before == after:
            continue
        removed = sorted(set(before["values"]) - set(after["values"]))
        added = sorted(set(after["values"]) - set(before["values"]))
        if not removed and not added:
            problems.append(
                f"{key}: the module version moved ({before['version']!r} -> "
                f"{after['version']!r}) but the committed snapshot wasn't regenerated. "
                f"Run: {REGENERATE_COMMAND}"
            )
            continue
        bump = "major" if removed else "minor"
        detail = "; ".join(
            [f"removed or renamed {removed!r} (major)"] * bool(removed)
            + [f"added {added!r} (minor)"] * bool(added)
        )
        module, old, new = after["module"], before["version"], after["version"]
        if old is None or new is None:
            problems.append(f"{key}: {detail}, but {module} has no version to bump.")
        elif old == new and module in pending_modules:
            declared = pending_modules[module]
            if declared != "major" and bump == "major":
                problems.append(
                    f"{key}: published values changed ({detail}), which needs a MAJOR bump "
                    f"of {module}, but its pending bump declares only {declared.upper()}."
                )
        elif old == new:
            problems.append(
                f"{key}: published values changed ({detail}) but the version of {module} is "
                f"still {new!r}. ADR-008 Decision 3 requires a {bump.upper()} bump. Declare it "
                f"with: uv run python scripts/bump_contract.py {module} {bump} --reason ... "
                f"(ADR-008 Decision 8)"
            )
        elif bump == "major" and major_version(new) <= major_version(old):
            problems.append(
                f"{key}: {detail} requires a MAJOR bump of {module} (its leading number "
                f"must increase past {major_version(old)}), but it only moved to {new!r}."
            )
        else:
            problems.append(
                f"{key}: the version of {module} moved {old!r} -> {new!r} ({detail}) but the "
                f"committed snapshot wasn't regenerated. Run: {REGENERATE_COMMAND}"
            )
    return problems


def evaluate_against_base(
    base: dict[str, Any],
    actual: dict[str, Any],
    pending_modules: Mapping[str, str] | None = None,
) -> list[str]:
    """Every published value change since ``base`` (main's committed snapshot) whose
    module version did not move enough and that no pending bump covers (#1158); the
    counterpart of ``contract_schema_snapshot.evaluate_against_base``."""
    pending_modules = pending_modules or {}
    problems: list[str] = []
    for key in sorted(set(base) & set(actual)):
        before, after = base[key], actual[key]
        removed = sorted(set(before["values"]) - set(after["values"]))
        added = sorted(set(after["values"]) - set(before["values"]))
        if not removed and not added:
            continue
        bump = "major" if removed else "minor"
        module, old, new = after["module"], before["version"], after["version"]
        if old is None or new is None:
            continue  # evaluate_snapshot reports a module with no version
        if old == new:
            declared = pending_modules.get(module)
            if declared == "major" or (declared == "minor" and bump == "minor"):
                continue
            problems.append(
                f"{key}: published values changed since main (removed {removed!r}, added "
                f"{added!r}) but the version of {module} is still {new!r} and no pending "
                f"bump covers it. Run: uv run python scripts/bump_contract.py {module} {bump} "
                "--reason ..."
            )
        elif bump == "major" and major_version(new) <= major_version(old):
            problems.append(
                f"{key}: removed {removed!r} since main, which needs a MAJOR bump of {module}, "
                f"but it only moved {old!r} -> {new!r}."
            )
    return problems


def write_snapshot() -> dict[str, Any]:
    snapshot = build_snapshot()
    SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help=f"regenerate {SNAPSHOT_PATH.name}")
    args = parser.parse_args(argv)
    if not args.write:
        print(json.dumps(build_snapshot(), indent=2, sort_keys=True))
        return 0
    previous = json.loads(SNAPSHOT_PATH.read_text()) if SNAPSHOT_PATH.exists() else {}
    actual = write_snapshot()
    for key in sorted(set(previous) | set(actual)):
        if previous.get(key) != actual.get(key):
            print(f"{key}: changed")
    print(f"wrote {SNAPSHOT_PATH.relative_to(REPO)} ({len(actual)} value sets)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
