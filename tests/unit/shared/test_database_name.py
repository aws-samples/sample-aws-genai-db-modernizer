"""src.shared.database_name: the one database_name validator every entry
point that builds an artifact path shares (#420). #418 added it only on the
AWS Transform path (src/atx_orchestrator/subagents/base.py); this is now the
single source of truth it, the scripts/run_*.py CLI scripts, and the local
API's assessment request models all call."""

from __future__ import annotations

import pytest

from src.shared.database_name import (
    DATABASE_NAME_ERROR,
    validate_database_name,
)


@pytest.mark.parametrize(
    "name",
    ["wordpress", "Demo_DB-42", "_-", "a-", "a", "a" * 128, "  wordpress  ", "abc\n", "\tabc\t"],
)
def test_accepts_valid_identifiers(name: str) -> None:
    assert validate_database_name(name) == name.strip()


def test_strips_trailing_newline_and_returns_the_stripped_value() -> None:
    """A trailing newline (e.g. from a shell `$(...)` or a file read) must not
    smuggle extra bytes into the artifact path -- the caller gets back the
    stripped value, not the original."""
    result = validate_database_name("abc\n")
    assert result == "abc"
    assert "\n" not in result


@pytest.mark.parametrize(
    "name",
    [
        "",
        "../demo",
        "..",
        ".",
        "a/b",
        "a\\b",
        "a.b",
        "-lead",
        "--db",
        "-",
        "café",
        "demo database",
        "a,b",
        "a;bad",
        "a" * 129,
        "../../etc/passwd",
        "/etc/passwd",
    ],
)
def test_rejects_invalid_identifiers(name: str) -> None:
    with pytest.raises(ValueError, match="database_name"):
        validate_database_name(name)


@pytest.mark.parametrize("name", [None, True, 123, [], {}])
def test_rejects_non_string_values(name: object) -> None:
    with pytest.raises(ValueError, match="database_name"):
        validate_database_name(name)


def test_error_message_names_the_accepted_shape() -> None:
    assert "1 to 128" in DATABASE_NAME_ERROR
    assert "letters, digits, underscores or hyphens" in DATABASE_NAME_ERROR


def test_error_message_never_echoes_the_rejected_value() -> None:
    try:
        validate_database_name("../attacker-controlled-value")
    except ValueError as exc:
        assert "attacker-controlled-value" not in str(exc)
    else:
        pytest.fail("expected a ValueError")
