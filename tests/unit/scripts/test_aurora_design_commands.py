"""Tripwires for the external-mode Aurora design commands (issue #273).

A headless run on a large schema failed because the command asked the model to
transcribe the whole deterministic draft (every column and the full DDL) from a
4 MB request. The commands must ask for a delta only, describe the compact
design view, and steer large-file reading to Read offset/limit and Grep.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.contracts.aurora_design_delta import AuroraDesignDeltaContract

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMANDS = {
    "aurora_postgresql": REPO_ROOT / ".claude" / "commands" / "design-schema-aurora-postgresql.md",
    "aurora_mysql": REPO_ROOT / ".claude" / "commands" / "design-schema-aurora-mysql.md",
}
SKILLS = {
    "aurora_postgresql": REPO_ROOT / "src" / "skills" / "aurora_postgresql-data-modeling.md",
    "aurora_mysql": REPO_ROOT / "src" / "skills" / "aurora_mysql-data-modeling.md",
}

# Instructions that made the model copy the draft (the #273 failure).
_TRANSCRIBE_PHRASES = (
    "reproduce its `aurora_type`",
    "Start `generated_ddl` from",
    "Copy `primary_key`, `indexes`, and `foreign_keys`",
    "Reproduce those types",
    "Return a complete `Aurora",
    "draft.residuals",
    "draft.full_ddl",
)


@pytest.mark.parametrize("engine", sorted(COMMANDS))
def test_command_asks_for_a_delta_only(engine):
    text = COMMANDS[engine].read_text()
    assert '"delta_version": "1.0"' in text
    assert "only a delta" in text
    assert "AuroraDesignDeltaContract" in text
    assert "Never copy tables, columns or DDL" in text
    for phrase in _TRANSCRIBE_PHRASES:
        assert phrase not in text, f"{COMMANDS[engine].name} still says {phrase!r}"


@pytest.mark.parametrize("engine", sorted(COMMANDS))
def test_command_describes_the_compact_view(engine):
    text = COMMANDS[engine].read_text()
    for key in ("design_view", "residual_types", "hot_queries", "type_rules"):
        assert key in text, key
    assert f"--engine {engine} --finalize" in text


@pytest.mark.parametrize("engine", sorted(COMMANDS))
def test_command_reads_large_requests_with_read_offset_limit_and_grep(engine):
    text = COMMANDS[engine].read_text()
    assert "`offset` and `limit`" in text
    assert "use Grep" in text
    assert "Never read it with `cat`, `sed`, `jq` or a\n        script." in text


@pytest.mark.parametrize("engine", sorted(COMMANDS))
def test_command_delta_fields_exist_in_the_contract(engine):
    """Every delta field the command names is a real contract field."""
    text = COMMANDS[engine].read_text()
    top = set(AuroraDesignDeltaContract.model_fields)
    for name in ("type_rules", "optimizations", "app_layer_notes", "trade_offs", "tables"):
        assert name in top and f"`{name}" in text
    for field in ("add_indexes", "modify_indexes", "remove_indexes", "column_types"):
        assert f"tables[].{field}" in text


@pytest.mark.parametrize("engine", sorted(SKILLS))
def test_designer_skill_returns_a_delta(engine):
    text = SKILLS[engine].read_text()
    assert "Return only an `AuroraDesignDeltaContract`" in text
    for phrase in _TRANSCRIBE_PHRASES:
        assert phrase not in text, f"{SKILLS[engine].name} still says {phrase!r}"
