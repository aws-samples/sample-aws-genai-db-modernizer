# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The current catalog loader reads the catalog files every recorded release shipped (#1647).

``tests/contract/fixtures/catalog_releases/<tag>/`` holds a release's
``src/catalog/**/*.toml`` files exactly as the tag shipped them, as raw TOML (recorded with
``uv run python scripts/catalog_format_snapshot.py --record-files <tag>``, and by the
release step's ``--write-released <tag>``; never edited by hand). They are committed rather
than read with ``git show <tag>:...`` because not every CI checkout carries the release
tags; as raw files, git stores a file unchanged between releases once.

Each release's files are loaded with every loader in ``src/shared/catalog/`` (and the
model price reader, ``scripts/usage_cost.py``). "Same meaning" is checked per file:
every key and value the file sets reaches the loaded model unchanged (dumped by alias), so
no key is dropped, renamed or reinterpreted. A key the
current loader knows that an older file lacks must take a default: that is why the older
file loads at all, and a loader that made such a key required fails here.

v0.4.0-beta.2's catalog files are byte-identical to the catalog this test was written
against, so no current key is missing from them yet.

The gates, routing and triage-signal loaders also check the names they cite against the
live engine and pattern files (``engine_ids()``, ``patterns()``); those cross-checks read
the current catalog, not the release's.

Model price rows are compared only for the models ``src/shared/models.MODEL_IDS`` names
today, since the reader looks a row up by model ID: a row an older release kept for a
model since retired is not read, so it is not compared.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from scripts.catalog_format_snapshot import RELEASE_FILES_DIR
from scripts.usage_cost import load_prices
from src.shared import models
from src.shared.catalog.access_classes import load_access_class_table
from src.shared.catalog.engines import load_engine_catalog
from src.shared.catalog.gaps import load_aurora_versions, load_gap_rules
from src.shared.catalog.patterns import load_pattern_catalog
from src.shared.catalog.prices import load_price_table
from src.shared.catalog.routing import load_gates_file, load_routing_file, load_triage_signals_file
from src.shared.catalog.write_intents import load_carried_write_settings, load_write_intents

RELEASES = sorted(p.name for p in RELEASE_FILES_DIR.iterdir() if p.is_dir())

#: Single-file kinds read by one loader that returns the file's model.
SINGLE_FILES: dict[str, Callable[[Path], BaseModel]] = {
    "access_classes.toml": load_access_class_table,
    "carried_writes.toml": load_carried_write_settings,
    "gates.toml": load_gates_file,
    "routing.toml": load_routing_file,
    "triage_signals.toml": load_triage_signals_file,
}


def _json(value: Any) -> Any:
    """``value`` as JSON reads it back (TOML dates become ISO strings)."""
    return json.loads(json.dumps(value, default=lambda o: o.isoformat()))


def meaning_changes(raw: Any, loaded: Any, path: str = "") -> list[str]:
    """Every key or value of ``raw`` that ``loaded`` drops or changes.

    Keys ``loaded`` adds (defaults, merged list prices) are not changes."""
    if isinstance(raw, dict):
        if not isinstance(loaded, dict):
            return [f"{path or '<file>'}: a table became {type(loaded).__name__}"]
        out: list[str] = []
        for key, value in raw.items():
            if key not in loaded:
                out.append(f"{path}.{key}: dropped by the loader")
            else:
                out += meaning_changes(value, loaded[key], f"{path}.{key}")
        return out
    if isinstance(raw, list):
        if not isinstance(loaded, list) or len(loaded) != len(raw):
            return [f"{path}: array {raw!r} loaded as {loaded!r}"]
        return [
            c
            for i, pair in enumerate(zip(raw, loaded, strict=True))
            for c in meaning_changes(*pair, f"{path}[{i}]")
        ]
    if raw != loaded or isinstance(raw, bool) is not isinstance(loaded, bool):
        return [f"{path}: {raw!r} loaded as {loaded!r}"]
    return []


def _dump(model: BaseModel) -> Any:
    return _json(model.model_dump(mode="json", by_alias=True))


def _raw(root: Path, rel: str) -> Any:
    return _json(tomllib.loads((root / rel).read_text(encoding="utf-8")))


def _per_file(root: Path, kind: str, entries: Any) -> list[str]:
    seen = {f"{kind}/{e.id}.toml" for e in entries}
    shipped = {p.relative_to(root).as_posix() for p in (root / kind).glob("*.toml")}
    problems = [f"{rel}: not loaded" for rel in sorted(shipped - seen)]
    for entry in entries:
        rel = f"{kind}/{entry.id}.toml"
        problems += [f"{rel}{c}" for c in meaning_changes(_raw(root, rel), _dump(entry))]
    return problems


def load_and_compare(root: Path) -> list[str]:
    """Load the catalog under ``root`` with the current loaders; every meaning change."""
    problems: list[str] = []
    problems += _per_file(
        root, "engines", load_engine_catalog(root / "engines", root / "prices.toml")
    )
    problems += _per_file(root, "patterns", load_pattern_catalog(root / "patterns"))
    versions = load_aurora_versions(root / "aurora_versions.toml")
    loaded_versions = {"engines": {k: _dump(v) for k, v in versions.items()}}
    problems += [
        f"aurora_versions.toml{c}"
        for c in meaning_changes(_raw(root, "aurora_versions.toml"), loaded_versions)
    ]
    problems += _per_file(root, "gaps", load_gap_rules(root / "gaps", versions))
    problems += _per_file(root, "write_intents", load_write_intents(root / "write_intents"))
    for rel, loader in SINGLE_FILES.items():
        problems += [
            f"{rel}{c}" for c in meaning_changes(_raw(root, rel), _dump(loader(root / rel)))
        ]
    # The price table moves its offer sections under ``sections``.
    raw_prices = _raw(root, "prices.toml")
    meta = {
        k: raw_prices.pop(k) for k in ("region", "retrieved_on", "published") if k in raw_prices
    }
    problems += [
        f"prices.toml{c}"
        for c in meaning_changes(
            {**meta, "sections": raw_prices}, _dump(load_price_table(root / "prices.toml"))
        )
    ]
    # Model prices are read by model ID: each one's row is the file's [<name>.<profile>].
    raw_models = _raw(root, "model_prices.toml")
    loaded = load_prices(root / "model_prices.toml")
    for name, model_id in models.MODEL_IDS.items():
        base = model_id.partition(".")[2]
        for profile, row in (raw_models.get(name) or {}).items():
            problems += [
                f"model_prices.toml.{name}.{profile}{c}"
                for c in meaning_changes(row, _json(loaded.get(f"{profile}.{base}")))
            ]
    return problems


def test_a_release_is_recorded() -> None:
    assert "v0.4.0-beta.2" in RELEASES


@pytest.mark.parametrize("tag", RELEASES)
def test_a_release_s_catalog_loads_with_the_same_meaning(tag: str) -> None:
    root = RELEASE_FILES_DIR / tag
    every_file = {p.relative_to(root).as_posix() for p in root.rglob("*.toml")}
    covered = {*SINGLE_FILES, "aurora_versions.toml", "prices.toml", "model_prices.toml"}
    covered |= {
        f for f in every_file if f.split("/")[0] in {"engines", "patterns", "gaps", "write_intents"}
    }
    assert every_file <= covered, f"{tag}: no loader reads {sorted(every_file - covered)}"
    problems = load_and_compare(root)
    assert not problems, f"{tag}'s catalog files load differently now:\n" + "\n".join(problems)


def test_a_dropped_or_changed_value_is_a_meaning_change() -> None:
    raw = {"a": 1, "b": {"c": [1, 2], "d": True}, "e": "x"}
    assert meaning_changes(raw, {**raw, "extra": 0}) == []
    assert meaning_changes(raw, {"a": 1, "b": {"c": [1, 2], "d": True}}) == [
        ".e: dropped by the loader"
    ]
    assert meaning_changes(raw, {**raw, "b": {"c": [1, 3], "d": True}}) == [
        ".b.c[1]: 2 loaded as 3"
    ]
    assert meaning_changes(raw, {**raw, "b": {"c": [1, 2], "d": 1}}) == [".b.d: True loaded as 1"]
    assert meaning_changes(raw, {**raw, "b": "flat"}) == [".b: a table became str"]
