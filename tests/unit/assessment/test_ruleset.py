"""``ruleset_hash`` names the rules a result came from (#1182)."""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this interpreter on a fixed import script
import sys
from pathlib import Path

import pytest

from src.assessment import ruleset

REPO = Path(__file__).resolve().parents[3]


def _covered() -> set[str]:
    return {p.relative_to(ruleset._SRC).as_posix() for p in ruleset.ruleset_files()}


def test_hash_covers_catalog_scorer_analyses_and_parser() -> None:
    files = _covered()
    assert "catalog/routing.toml" in files
    assert any(f.startswith("catalog/patterns/") for f in files)
    for module in (
        "agents/referee/query_scoring.py",
        "agents/referee/tie_break.py",
        "agents/referee/assignment_resolver.py",
        "agents/referee/reality_check_rules.py",
        # What the scorer and the analyses import from outside the referee.
        "tools/analysis/scoring.py",
        "shared/engine_capabilities.py",
        "shared/capability_labels.py",
        "tools/database/offline_parser.py",
        "tools/sql/facade.py",
    ):
        assert module in files, module
    # Not routing: the report renderers, the UI, the API and the public module itself.
    assert not any(f.startswith(("report/", "assessment/", "ui/", "api/")) for f in files)


def test_every_module_the_phases_import_is_hashed() -> None:
    """Import every root in a fresh interpreter: each ``src`` module that loads is in
    the hashed set, so a module the scorer or an analysis starts importing can never
    change scores without changing the hash."""
    roots = ["src." + r.removesuffix(".py").replace("/", ".") for r in ruleset.RULESET_ROOTS]
    script = (
        "import importlib, json, sys\n"
        f"for name in {roots!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps(sorted(m.__file__ for n, m in list(sys.modules.items())\n"
        "    if (n == 'src' or n.startswith('src.')) and getattr(m, '__file__', None))))\n"
    )
    out = subprocess.run(  # nosec B603 -- this interpreter, a fixed script
        [sys.executable, "-c", script], cwd=REPO, capture_output=True, text=True, check=True
    )
    loaded = {
        Path(f).resolve().relative_to(ruleset._SRC).as_posix() for f in json.loads(out.stdout)
    }
    assert loaded
    assert loaded - _covered() == set()


def test_hash_is_stable() -> None:
    ruleset.ruleset_hash.cache_clear()
    first = ruleset.ruleset_hash()
    ruleset.ruleset_hash.cache_clear()
    assert ruleset.ruleset_hash() == first
    assert first.startswith("sha256:")


def _tree(root: Path) -> Path:
    src = root / "src"
    (src / "catalog").mkdir(parents=True)
    (src / "pkg").mkdir()
    (src / "__init__.py").write_text("")
    (src / "pkg" / "__init__.py").write_text("")
    (src / "catalog" / "routing.toml").write_text("threshold = 15\n")
    (src / "pkg" / "scorer.py").write_text("def f():\n    from src.pkg import formula\n")
    (src / "pkg" / "formula.py").write_text("WEIGHT = 1\n")
    (src / "pkg" / "unrelated.py").write_text("X = 1\n")
    return src


def _hash() -> str:
    ruleset.ruleset_hash.cache_clear()
    try:
        return ruleset.ruleset_hash()
    finally:
        ruleset.ruleset_hash.cache_clear()


def test_hash_follows_imports_and_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _tree(tmp_path)
    monkeypatch.setattr(ruleset, "_SRC", src)
    monkeypatch.setattr(ruleset, "RULESET_ROOTS", ("pkg/scorer.py",))
    # A module imported inside a function is covered; one nothing imports is not.
    assert _covered() == {
        "__init__.py",
        "catalog/routing.toml",
        "pkg/__init__.py",
        "pkg/formula.py",
        "pkg/scorer.py",
    }
    hashes = [_hash()]
    (src / "pkg" / "formula.py").write_text("WEIGHT = 2\n")
    hashes.append(_hash())
    (src / "catalog" / "routing.toml").write_text("threshold = 16\n")
    hashes.append(_hash())
    (src / "pkg" / "unrelated.py").write_text("X = 2\n")
    hashes.append(_hash())
    monkeypatch.setattr(ruleset, "RULESET_LIBRARIES", ())
    hashes.append(_hash())
    assert len(set(hashes)) == 4
    assert hashes[2] == hashes[3]


def test_a_missing_root_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ruleset, "_SRC", _tree(tmp_path))
    monkeypatch.setattr(ruleset, "RULESET_ROOTS", ("pkg/gone.py",))
    with pytest.raises(FileNotFoundError, match="pkg/gone.py"):
        ruleset.ruleset_files()
