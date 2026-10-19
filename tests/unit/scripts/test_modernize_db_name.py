"""/modernize derives a valid database name from any collection file name (#509).

``/modernize`` takes no database name: the scripts derive one from the collector
file name (#1096) and the command passes it on as ``--db``. Since #420 a database name must match
``DATABASE_NAME_PATTERN``, so a file such as ``my.app-collection.json`` used to
produce ``my.app`` and the run failed at ``init``. The command must state the
rewrite rule, and every example it gives must follow that rule and pass the
validator.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.shared.collection_database_name import database_name_from_collection_file
from src.shared.database_name import validate_database_name

REPO_ROOT = Path(__file__).resolve().parents[3]
MODERNIZE = REPO_ROOT / ".claude" / "commands" / "modernize.md"

ALLOWED_CLASS = "[A-Za-z0-9_-]"  # the rule's character class (the pattern also bars a leading -)
EXAMPLE = re.compile(r"`([^`]+\.json)` → `([^`]*)`")


def _rule_paragraph() -> str:
    text = MODERNIZE.read_text(encoding="utf-8")
    paragraphs = [p for p in text.split("\n\n") if "With `--db-from-file-name`" in p]
    assert len(paragraphs) == 1, "expected one paragraph stating how the name is derived"
    return paragraphs[0]


def derive(file_name: str) -> str:
    """The rule the command states; the scripts apply it (#1096)."""
    return database_name_from_collection_file(file_name)


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("wordpress-collection.json", "wordpress"),
        ("my.app-collection.json", "my_app"),
        ("sales data.json", "sales_data"),
        ("café.json", "caf_"),
        ("-collection.json", "database"),
        ("-lead.json", "_lead"),
        ("a" * 200 + ".json", "a" * 128),
    ],
)
def test_reference_rule_always_yields_a_valid_name(file_name: str, expected: str) -> None:
    assert derive(file_name) == expected
    assert validate_database_name(expected) == expected


def test_command_states_the_rewrite_rule() -> None:
    paragraph = _rule_paragraph()
    assert ALLOWED_CLASS in paragraph, f"the rule must name the allowed set {ALLOWED_CLASS}"
    assert "with `_`" in paragraph
    assert "128" in paragraph
    assert "`database`" in paragraph, "the rule must say what to use when nothing is left"


def test_command_examples_follow_the_rule_and_validate() -> None:
    examples = EXAMPLE.findall(_rule_paragraph())
    assert any(
        re.search(r"[^A-Za-z0-9_-]", f.removesuffix(".json")) for f, _ in examples
    ), "at least one example must show a rewritten character"
    for file_name, name in examples:
        assert name == derive(file_name), f"{file_name} -> {name}"
        validate_database_name(name)


def test_ui_mode_names_the_database_name_notice_as_an_exception() -> None:
    """--mode ui limits chat to a fixed list; the rename notice must be on it."""
    text = MODERNIZE.read_text(encoding="utf-8")
    ui_rule = next(line for line in text.splitlines() if "`--mode ui` is UI-first" in line)
    assert "database-name notice" in ui_rule
    assert "`--mode ui` included" in _rule_paragraph()
