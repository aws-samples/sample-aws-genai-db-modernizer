# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The splitter copy must agree with the one in ci/llm/run.py (#683)."""

from __future__ import annotations

import pytest

from ci.llm.run import _split_compound_command
from scripts.hooks.hook_common import has_unquoted, split_compound_command

COMMANDS = [
    "uv run python scripts/run_assessment.py --file x",
    "a | b",
    "a && b || c; d & e",
    "a 2>&1",
    "a &> out",
    "a $(b c) `d`",
    "echo 'a | b; c' \"d && e\"",
    "a\nb",
    'echo "unbalanced',
    "a \\| b",
    "(cd x; ls)",
    "",
]


@pytest.mark.parametrize("command", COMMANDS)
def test_splitter_matches_ci_llm_run(command: str) -> None:
    assert split_compound_command(command) == _split_compound_command(command)


def test_has_unquoted() -> None:
    assert has_unquoted("cat <<EOF", "<<")
    assert not has_unquoted("echo '<<' \"<<\"", "<<")
    assert not has_unquoted("echo \\<<", "<<")


def test_mask_quoted() -> None:
    from scripts.hooks.hook_common import mask_quoted

    assert mask_quoted("a '>' \"<\" b > c") == "a 'x' \"x\" b > c"
    assert mask_quoted("a \\> b") == "a xx b"
    assert mask_quoted('echo "a \\" > b" > c') == 'echo "xxxxxxxx" > c'


# The search a discourse run's DocumentDB review sent: Claude Code denied it because
# ``\\$lookup`` in double quotes is a backslash and then the ``$lookup`` variable.
DENIED_SEARCH = (
    'uv run python scripts/search_artifacts.py "unsupported_patterns|validation_failures|'
    'trade_offs|in_scope|\\"version\\"|target_version|\\\\$lookup|\\\\$graphLookup|\\\\$facet|'
    'sparse|partialFilter|wildcard|estimated_max_doc_size_kb\\": [0-9]{4}" '
    "artifacts/discourse/24df684d/llm_requests/pe_review_documentdb.json"
)


@pytest.mark.parametrize(
    "command",
    [
        DENIED_SEARCH,
        "a $HOME",
        'a "x|$lookup"',
        'a "${x}"',
        'a "$(cat f)"',
        'a "`cat f`"',
        "a `cat f`",
        'a "$1" "$@"',
        "a '$x'\"$y\"",
        'a "\\\\$x"',  # an escaped backslash, then a live $x
    ],
)
def test_has_shell_expansion(command: str) -> None:
    from scripts.hooks.hook_common import has_shell_expansion

    assert has_shell_expansion(command)


@pytest.mark.parametrize(
    "command",
    [
        "uv run python scripts/search_artifacts.py '\\$lookup|\\$facet' artifacts/x",
        'uv run python scripts/search_artifacts.py "\\"version\\"|in_scope" artifacts/x',
        'uv run python scripts/search_artifacts.py "\\"wave\\": [0-9],? *$|x" artifacts/x',
        'uv run python scripts/search_artifacts.py "\\"\\\\\\$defs\\"" artifacts/x',
        'uv run python scripts/search_artifacts.py "x\\$y" artifacts/x',
        "uv run python scripts/search_artifacts.py '\"a\"|`b`|$(c)' artifacts/x",
        "a \\$x",
        "a $",
        "",
    ],
)
def test_literal_dollars_and_quotes_are_not_expansions(command: str) -> None:
    from scripts.hooks.hook_common import has_shell_expansion

    assert not has_shell_expansion(command)
