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
