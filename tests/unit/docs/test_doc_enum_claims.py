# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lists that the docs state outright match their source of truth (#1162).

Only explicit lists are checked, and each claim is one regular expression whose first
group is the list as the page writes it:

* Engine names and ids against the engine catalog (``src/catalog/engines/*.toml``, read
  through ``src.shared.catalog`` and ``src.shared.engine_names``).
* The load-test engines against ``src/agents/load_test/<engine>/``.
* The ``--llm-mode`` values against the ``choices`` of the scripts that take the flag
  (read from the AST, nothing is run).

A page that drops or invents an engine, or still names a retired mode, fails with the
page, the claim and the difference.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.shared import catalog
from src.shared.engine_names import ENGINE_DISPLAY_NAMES

REPO = Path(__file__).resolve().parents[3]

ACTIVE = catalog.engine_ids(status="active")
FUTURE = catalog.engine_ids(status="future")
LEGACY = catalog.engine_ids(status="legacy")


def _display(ids: frozenset[str]) -> frozenset[str]:
    return frozenset(ENGINE_DISPLAY_NAMES[i] for i in ids)


def _names_in(text: str) -> frozenset[str]:
    """Engine display names a list names; the longest match wins ("Aurora MySQL", not "Aurora")."""
    found = set()
    for name in sorted(ENGINE_DISPLAY_NAMES.values(), key=len, reverse=True):
        if re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text):
            found.add(name)
            text = text.replace(name, " ")
    return frozenset(found)


def _ids_in(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"`([a-z_]+)`", text))


def _load_test_engines() -> frozenset[str]:
    root = REPO / "src" / "agents" / "load_test"
    dirs = {p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")}
    return frozenset(ENGINE_DISPLAY_NAMES[d] for d in dirs)


@dataclass(frozen=True)
class Claim:
    page: str
    pattern: str
    expected: frozenset[str]
    reader: str = "names"  # "names": display names in the list; "ids": backticked engine ids

    @property
    def label(self) -> str:
        return f"{self.page}: /{self.pattern[:40]}/"


CLAIMS = [
    Claim(
        "docs/architecture/high-level-design.md",
        r"\*\*Target engines:\*\* ([^.]*)\.",
        _display(ACTIVE),
    ),
    Claim(
        "docs/architecture/high-level-design.md",
        r"\. ([A-Za-z ]+) are catalog placeholders",
        _display(FUTURE),
    ),
    Claim(
        "docs/architecture/high-level-design.md",
        r"Analysis: one per selected engine \(([^)]*)\)",
        _display(ACTIVE),
    ),
    Claim(
        "docs/architecture/high-level-design.md",
        r"One deterministic analysis per selected engine \(([^)]*)\)",
        _display(ACTIVE),
    ),
    Claim(
        "docs/architecture/high-level-design.md",
        r"One designer per assigned engine: ([^.]*)\.",
        _display(ACTIVE),
    ),
    Claim(
        "docs/architecture/high-level-design.md",
        r"Load Testing: [^(]*\(([^)]*)\)",
        _load_test_engines(),
    ),
    Claim(
        "docs/guides/load-testing-new-engine.md",
        r"target engine\. ([A-Za-z, ]+) are implemented",
        _load_test_engines(),
    ),
    Claim(
        "docs/use-claude-code.md",
        r"The ids are the active engine ids \(([^)]*)\)",
        ACTIVE,
        "ids",
    ),
    Claim(
        "docs/use-claude-code.md",
        r"the legacy (`\w+`)",
        LEGACY,
        "ids",
    ),
    Claim(
        "docs/use-claude-code.md",
        r"a planned engine \(([^)]*)\)",
        FUTURE,
        "ids",
    ),
    Claim(
        "README.md",
        r"\*\*Targets:\*\* ([^\n]*)",
        _display(ACTIVE),
    ),
    Claim(
        "CONTRIBUTING.md",
        r"Workload analysis \(([^)]*)\)",
        _display(ACTIVE),
    ),
    Claim(
        "docs/guides/context-graph-query-cookbook.md",
        r"Change `dynamodb` to ([^.]*)\.",
        ACTIVE - {"dynamodb"},
        "ids",
    ),
]


def _read(claim: Claim) -> frozenset[str]:
    text = (REPO / claim.page).read_text(encoding="utf-8")
    match = re.search(claim.pattern, text, re.DOTALL if claim.reader == "names" else 0)
    assert match, f"{claim.label}: the sentence is gone; update the claim or the page"
    return _names_in(match.group(1)) if claim.reader == "names" else _ids_in(match.group(1))


@pytest.mark.parametrize("claim", CLAIMS, ids=lambda c: c.label)
def test_a_page_that_lists_engines_lists_the_catalogs(claim: Claim):
    got = _read(claim)
    assert got == claim.expected, (
        f"{claim.label}: the page lists {sorted(got)}, the source of truth has "
        f"{sorted(claim.expected)} (missing {sorted(claim.expected - got)}, "
        f"extra {sorted(got - claim.expected)})"
    )


@pytest.mark.passes_on_main
def test_a_dropped_engine_fails_the_check():
    """The reader finds a list that lacks an engine (the claim would fail on it)."""
    assert _names_in("DynamoDB, DocumentDB and Aurora MySQL") != _display(ACTIVE)
    assert _names_in("Aurora PostgreSQL and Aurora MySQL") == frozenset(
        {"Aurora PostgreSQL", "Aurora MySQL"}
    )


@pytest.mark.passes_on_main
def test_every_engine_name_the_pages_use_is_a_display_name_of_the_catalog():
    assert _display(ACTIVE | FUTURE | LEGACY) == frozenset(ENGINE_DISPLAY_NAMES.values())


# ---- --llm-mode ----


def _llm_mode_choices(
    scripts: Path = REPO / "scripts",
) -> dict[str, tuple[frozenset[str], str | None]]:
    """script -> (choices, default) for every ``--llm-mode`` add_argument under scripts/."""
    found: dict[str, tuple[frozenset[str], str | None]] = {}
    for path in sorted(scripts.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "--llm-mode"
            ):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            choices = kw.get("choices")
            assert isinstance(choices, (ast.List, ast.Tuple)) and all(
                isinstance(c, ast.Constant) for c in choices.elts
            ), (
                f"{path.name}: --llm-mode `choices` must be a literal list of strings so this "
                "check can read it; if you moved it to a constant, teach the reader"
            )
            default = kw.get("default")
            default_value = (
                str(default.value)
                if isinstance(default, ast.Constant) and isinstance(default.value, str)
                else None
            )
            found[path.name] = (
                frozenset(ast.literal_eval(c) for c in choices.elts),
                default_value,
            )
    return found


MODES = _llm_mode_choices()
ALL_MODES = frozenset().union(*(c for c, _ in MODES.values()))


@pytest.mark.passes_on_main
def test_the_llm_mode_scripts_were_found():
    assert "run_assessment.py" in MODES and len(MODES) >= 4, sorted(MODES)


@pytest.mark.passes_on_main
def test_agents_md_modes_table_lists_the_modes_the_scripts_accept():
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    section = text.split("### The three LLM modes", 1)[1].split("\n###", 1)[0].split("\n## ", 1)[0]
    rows = re.findall(r"^\| `(\w+)` \|", section, re.MULTILINE)
    assert (
        frozenset(rows) == ALL_MODES
    ), f"AGENTS.md lists {sorted(rows)}; the scripts accept {sorted(ALL_MODES)}"
    assert len(ALL_MODES) == 3, "the heading says 'three LLM modes'; update it"


@pytest.mark.passes_on_main
def test_agents_md_names_the_default_mode_of_run_assessment():
    text = (REPO / "AGENTS.md").read_text(encoding="utf-8")
    default = MODES["run_assessment.py"][1]
    assert re.search(
        rf"\| `{default}` \|[^|]*\(the default for `run_assessment.py`\)", text
    ), f"run_assessment.py defaults to {default!r}; AGENTS.md's modes table must say so"


@pytest.mark.passes_on_main
def test_the_add_skill_command_lists_the_modes_a_phase_script_supports():
    text = (REPO / ".claude/commands/add-modernizer-skill.md").read_text(encoding="utf-8")
    match = re.search(r"`--llm-mode` \(([a-z/]+)\)", text)
    assert match
    assert frozenset(match.group(1).split("/")) == ALL_MODES


@pytest.mark.passes_on_main
def test_no_script_accepts_a_mode_the_docs_do_not_know():
    for script, (choices, _) in MODES.items():
        assert choices <= ALL_MODES, script


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "choices",
    ["choices=MODES,", "choices=[*BASE, 'x'],", ""],
    ids=["constant", "starred", "missing"],
)
def test_a_llm_mode_without_literal_choices_fails_instead_of_being_skipped(tmp_path, choices):
    (tmp_path / "run_x.py").write_text(
        "import argparse\np = argparse.ArgumentParser()\n"
        f"p.add_argument('--llm-mode', default='none', {choices})\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="literal list"):
        _llm_mode_choices(tmp_path)


@pytest.mark.passes_on_main
def test_a_literal_llm_mode_choices_list_is_read(tmp_path):
    (tmp_path / "run_x.py").write_text(
        "import argparse\np = argparse.ArgumentParser()\n"
        "p.add_argument('--llm-mode', default='none', choices=['none', 'bedrock'])\n",
        encoding="utf-8",
    )
    assert _llm_mode_choices(tmp_path) == {"run_x.py": (frozenset({"none", "bedrock"}), "none")}
