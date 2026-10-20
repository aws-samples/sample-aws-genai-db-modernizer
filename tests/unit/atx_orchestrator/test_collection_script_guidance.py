"""Every AWS Transform surface names a collection script for every source engine (#741).

The project ships one read-only collection script per supported source engine
(``scripts/collect-<engine>.sql``). The orchestrator prompt, the welcome message, the
negotiation prompt and the upload panel used to name only PostgreSQL and MySQL, so a
SQL Server or Oracle customer got no guidance on the AWS Transform path.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.atx_orchestrator.orchestrator import SYSTEM_PROMPT
from src.atx_orchestrator.startup import WELCOME_MESSAGE
from src.atx_orchestrator.tools import COLLECTION_UPLOAD_DESCRIPTION

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = sorted(p.name for p in (REPO / "scripts").glob("collect-*.sql"))


def _negotiation_prompt() -> str:
    """``pipeline/atx_deploy.py``'s published greeting, read without importing the
    deploy script (it is not part of the ``src`` package)."""
    tree = ast.parse((REPO / "pipeline" / "atx_deploy.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "ORCHESTRATOR_NEGOTIATION_PROMPT"
            for t in node.targets
        ):
            value: str = ast.literal_eval(node.value)
            return value
    raise AssertionError("ORCHESTRATOR_NEGOTIATION_PROMPT not found")


SURFACES = {
    "orchestrator-prompt": SYSTEM_PROMPT,
    "welcome-message": WELCOME_MESSAGE,
    "negotiation-prompt": _negotiation_prompt(),
    "upload-panel": COLLECTION_UPLOAD_DESCRIPTION,
    # The two docs pages that list the collection scripts, kept in step with the prompt.
    "docs-use-claude-code": (REPO / "docs" / "use-claude-code.md").read_text(),
    "docs-faq": (REPO / "docs" / "faq.md").read_text(),
}

# The client each script's own Usage header runs it with.
CLIENT_COMMANDS = {
    "collect-postgresql.sql": "psql ",
    "collect-mysql.sql": "mysql ",
    "collect-sqlserver.sql": "sqlcmd ",
    "collect-oracle.sql": "sqlplus ",
}


def test_the_four_engine_scripts_ship() -> None:
    assert SCRIPTS == sorted(CLIENT_COMMANDS)


@pytest.mark.parametrize("surface", sorted(SURFACES))
@pytest.mark.parametrize("script", SCRIPTS)
def test_every_surface_names_every_script(surface: str, script: str) -> None:
    assert script in SURFACES[surface]


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_orchestrator_prompt_gives_each_scripts_client_command(script: str) -> None:
    line = next(
        (ln for ln in SYSTEM_PROMPT.splitlines() if f"scripts/{script}" in ln and "<" in ln),
        "",
    )
    assert line.strip().startswith(CLIENT_COMMANDS[script]), line


@pytest.mark.parametrize("surface", ["orchestrator-prompt", "docs-use-claude-code"])
def test_the_sql_server_password_stays_off_the_command_line(surface: str) -> None:
    # sqlcmd reads SQLCMDPASSWORD, so no command line carries the password (#1282 review).
    text = SURFACES[surface]
    assert "SQLCMDPASSWORD" in text
    sqlcmd = [ln for ln in text.splitlines() if ln.strip().startswith("sqlcmd ")]
    assert sqlcmd and not any(" -P " in ln for ln in sqlcmd), sqlcmd


def test_the_welcome_message_names_view_server_state() -> None:
    # SQL Server's query section needs a server permission, not only SELECT.
    assert "VIEW SERVER STATE" in WELCOME_MESSAGE
    assert "only need SELECT access" not in WELCOME_MESSAGE


@pytest.mark.parametrize("surface", ["orchestrator-prompt", "docs-use-claude-code"])
def test_db2_is_named_as_having_no_collection_script_yet(surface: str) -> None:
    assert "Db2 has no collection script yet" in " ".join(SURFACES[surface].split())
