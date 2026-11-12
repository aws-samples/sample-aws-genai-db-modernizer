# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No text the modernizer writes says "in sync" (#2167, the cutover rule of #2107).

The deliverables never use "in sync": a model that paraphrases a deterministic "copy CDC
keeps in sync" writes readiness wording the cutover guard fails. A CDC copy is "kept
current by CDC" from its owner (``KEPT_IN_SYNC_LABEL``), in every deliverable and the UI.

Scanned: every string literal in ``src/`` and ``scripts/`` Python (docstrings and comments
are not output), the skills and the commands and agents a model reads, the rule catalog,
and the UI's locale and source files. The only "in sync" left is a quoted mention in the
prompts' rule ("never write \\"in sync\\""), allowed in the skills and agents only (never in a
Python string or the UI), and four exact (file, string) pins: RDS's own apply status
``in-sync`` and the two preflight messages that quote it, and the pre-#2167 replica risk
wording the facts layer recognises.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

# "in sync", "in-sync", "kept in sync", "keeps ... in sync", any case.
IN_SYNC = re.compile(r"\bin[\s-]+sync\b", re.I)
# A quoted mention of the banned words, in the rule that bans them: allowed only in the
# prompts that carry that rule (``RULE_FILES``), never in a Python string or the UI.
MENTION = re.compile(r'"in sync"')
RULE_FILES = ("src/skills/", ".claude/agents/")
# Pinned (file, the exact string): RDS's parameter group apply status, a value the step 1
# preflight reads and names, and the wording a report written before #2167 stored, which
# the facts layer recognises so that report is stated in today's words.
PINNED = {
    ("src/shared/report_facts.py", "a copy kept in sync from "),
    ("src/tools/data_movement/dms_preflight.py", "in-sync"),
    (
        "src/tools/data_movement/dms_preflight.py",
        "RDS is applying the values; wait until the status is in-sync, then run the preflight "
        "again.",
    ),
    (
        "src/tools/data_movement/dms_preflight.py",
        "RDS reported no apply status; wait until it reports in-sync, then run the preflight "
        "again.",
    ),
}


def _docstrings(tree: ast.AST) -> set[int]:
    """The ids of the string constants that are statements: docstrings, not output."""
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _strings(path: Path) -> list[tuple[int, str]]:
    """Each string literal in a Python file (an f-string with its fields as ``{}``)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    skip = _docstrings(tree)
    inner = {
        id(v) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for v in node.values
    }
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            text = "".join(
                str(v.value) if isinstance(v, ast.Constant) else "{}" for v in node.values
            )
            out.append((node.lineno, text))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
            and id(node) not in inner
        ):
            out.append((node.lineno, node.value))
    return out


def _python_files() -> list[str]:
    files = [*(REPO / "src").rglob("*.py"), *(REPO / "scripts").rglob("*.py")]
    return sorted(
        p.relative_to(REPO).as_posix()
        for p in files
        if "node_modules" not in p.parts and "src/ui" not in p.as_posix()
    )


def _text_files() -> list[str]:
    ui = REPO / "src/ui/src"
    files = [
        *(REPO / "src/skills").glob("*.md"),
        *(REPO / ".claude/commands").glob("*.md"),
        *(REPO / ".claude/agents").glob("*.md"),
        *(REPO / "src/catalog").rglob("*.toml"),
        *(ui / "locales").glob("*.json"),
        *(p for p in ui.rglob("*.js") if "__tests__" not in p.parts),
    ]
    return sorted(p.relative_to(REPO).as_posix() for p in files)


def _in_sync_strings(name: str, strings: list[tuple[int, str]]) -> list[str]:
    mention_ok = name.startswith(RULE_FILES)
    return [
        f"{name}:{line}: {m.group(0)!r} in {text[:90]!r}"
        for line, text in strings
        if (name, text) not in PINNED
        for m in IN_SYNC.finditer(MENTION.sub("", text) if mention_ok else text)
    ]


def test_no_python_string_says_in_sync() -> None:
    names = _python_files()
    assert len(names) > 300  # src/ and scripts/ both found
    found = [f for name in names for f in _in_sync_strings(name, _strings(REPO / name))]
    assert found == [], "\n".join(found)


def test_no_prompt_catalog_or_ui_text_says_in_sync() -> None:
    names = _text_files()
    assert any(n.endswith("locales/en.json") for n in names)
    assert any(n.startswith("src/skills/") for n in names)
    found = [
        f
        for name in names
        for f in _in_sync_strings(
            name, list(enumerate((REPO / name).read_text().splitlines(), start=1))
        )
    ]
    assert found == [], "\n".join(found)


@pytest.mark.passes_on_main
def test_each_pinned_string_is_still_there() -> None:
    for path, pinned in PINNED:
        assert pinned in [text for _, text in _strings(REPO / path)], (path, pinned)


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "source",
    [
        'X = "a copy that CDC keeps in sync from DynamoDB"',
        'X = f"{n} kept in sync from {owner}"',
        'X = ("3 tables kept "\n     "in sync")',
        "X = 'IN SYNC'",
        "X = 'kept-in-sync tables'",
        'X = f"keep the {names} copy in sync via CDC"',
        # The review of #2169: a quoted "in sync" is no exemption in Python.
        "X = 'the tables CDC keeps \"in sync\"'",
        'X = \'Never write "in sync": say "kept current by CDC".\'',
    ],
)
def test_the_guard_catches_each_form(tmp_path: Path, source: str) -> None:
    path = tmp_path / "sample.py"
    path.write_text(source + "\n")
    assert _in_sync_strings("sample.py", _strings(path)) != []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "source",
    [
        '"""A docstring that says kept in sync."""',
        "# a comment that says kept in sync\nX = 1",
        'X = "a copy kept current by CDC from DynamoDB"',
        'X = f"{n} syncs in {m} waves"',
    ],
)
def test_the_guard_lets_through_what_is_not_output(tmp_path: Path, source: str) -> None:
    path = tmp_path / "sample.py"
    path.write_text(source + "\n")
    assert _in_sync_strings("sample.py", _strings(path)) == []


def test_a_risk_stored_before_2167_is_stated_in_todays_words() -> None:
    """A report written before #2167 stored the replica risk as "a copy kept in sync from";
    its facts block, with no ``shares_replica_risk``, gets the flag from the same sentence,
    so the deliverables state the shared risk in today's words."""
    from src.shared.report_facts import replica_risk_text, shares_replica_risk

    today = replica_risk_text("dynamodb", ["aurora_mysql"], 2)
    stored = today.replace("a copy kept current from ", "a copy kept in sync from ")
    assert stored != today
    row = {
        "source_of_truth": "dynamodb",
        "replica_engines": ["aurora_mysql"],
        "replica_read_count": 2,
    }
    assert shares_replica_risk({**row, "risk": today})
    assert shares_replica_risk({**row, "risk": stored})
    assert not shares_replica_risk({**row, "risk": stored.replace("2 reads", "3 reads")})
    assert not shares_replica_risk({**row, "risk": "Older wording of the risk."})


def test_a_quoted_in_sync_in_a_producer_fails(tmp_path: Path) -> None:
    """The review of #2169: ``executive_brief.py`` with its exposure phrase quoting "in sync"
    fails the guard; the quoted mention is allowed only in the prompts' rule."""
    name = "src/shared/executive_brief.py"
    source = (REPO / name).read_text()
    phrase = '"the tables kept current by change data capture (CDC)"'
    assert phrase in source
    planted = tmp_path / "executive_brief.py"
    planted.write_text(source.replace(phrase, "'the tables CDC keeps \"in sync\"'"))
    assert _in_sync_strings(name, _strings(planted)) != []
    assert _in_sync_strings(name, _strings(REPO / name)) == []
    rule = 'Never write "in sync": a copy that CDC keeps current from its owner.'
    assert _in_sync_strings("src/skills/executive-summary.md", [(1, rule)]) == []
    assert _in_sync_strings("src/ui/src/locales/en.json", [(1, rule)]) != []
