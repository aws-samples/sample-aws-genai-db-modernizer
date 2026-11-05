# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine check loader's rules for the MySQL checks (#1936).

The loader refuses, at load time, a check or a routine kind the rule's source engine does
not have, and a misspelled MySQL ``[match]``. Who reads the catalog is
``test_routine_check_readers.py``'s guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.shared.catalog.engines import CatalogError
from src.shared.catalog.routine_checks import load_routine_check_rules

_HEAD = (
    'id = "x-rule"\nfeature = "X"\nsource_engines = {engines}\ntargets = {targets}\n'
    'kinds = {kinds}\nimpact = "needs_rewrite"\nreason = "Why."\nfix_hint = "How."\n'
    'docs = "Manual"\n\n[match]\n{match}\n'
)


def _load(tmp_path: Path, match: str, engine: str = "mysql", kinds: str = '["procedure"]'):
    target = "aurora_postgresql" if engine == "postgresql" else "aurora_mysql"
    (tmp_path / "x-rule.toml").write_text(
        _HEAD.format(engines=f'["{engine}"]', targets=f'["{target}"]', kinds=kinds, match=match),
        encoding="utf-8",
    )
    return load_routine_check_rules(tmp_path)


@pytest.mark.parametrize(
    ("engine", "match", "kinds", "message"),
    [
        ("postgresql", "declare_order = true", '["procedure"]', "applies only to"),
        ("postgresql", "valid_handlers = true", '["procedure"]', "applies only to"),
        ("postgresql", "definer_not_target_user = true", '["procedure"]', "applies only to"),
        ("postgresql", 'statement_starts = ["SET GLOBAL"]', '["procedure"]', "applies only to"),
        ("postgresql", 'sql_modes = ["ANSI"]', '["procedure"]', "applies only to"),
        ("postgresql", "dynamic_sql = true", '["procedure"]', "applies only to"),
        ("mysql", 'languages = ["sql"]', '["procedure"]', "applies only to"),
        ("mysql", "security_definer_owned_by_superuser = true", '["procedure"]', "applies only"),
        ("mysql", 'extensions = "offered_on_target_line"', '["procedure"]', "applies only to"),
        ("postgresql", "parses = true", '["procedure", "event"]', "only MySQL has events"),
    ],
)
def test_a_check_or_kind_its_engine_lacks_fails_at_load_time(
    tmp_path: Path, engine: str, match: str, kinds: str, message: str
) -> None:
    with pytest.raises(CatalogError, match=message):
        _load(tmp_path, match, engine, kinds)


@pytest.mark.parametrize(
    ("match", "message"),
    [
        ('set_scopes = ["GLOBL"]', "the scopes are"),
        (
            'set_scopes = ["GLOBAL"]\nstatement_exceptions = ["INTO OUTFILE S3"]',
            "goes with statement_contains",
        ),
        ('function_calls = ["load_file"]', "upper case"),
        ('function_calls = ["LOAD_FILE", "LOAD_FILE"]', "listed twice"),
        ("dynamic_sql = true\nparses = true", "exactly one of"),
    ],
)
def test_a_misspelled_mysql_match_fails_at_load_time(
    tmp_path: Path, match: str, message: str
) -> None:
    with pytest.raises(CatalogError, match=message):
        _load(tmp_path, match)


@pytest.mark.parametrize(
    ("engine", "match", "kinds"),
    [
        # The "must still load" side: main's loader loads these three too.
        pytest.param(
            "mysql",
            'set_scopes = ["GLOBAL", "PERSIST", "PERSIST_ONLY", "SESSION", "LOCAL"]',
            '["event"]',
            marks=pytest.mark.passes_on_main,
        ),
        pytest.param(
            "mysql", "parses = true", '["procedure", "event"]', marks=pytest.mark.passes_on_main
        ),
        pytest.param(
            "postgresql",
            "parses = true",
            '["function", "procedure", "trigger"]',
            marks=pytest.mark.passes_on_main,
        ),
        ("mysql", "dynamic_sql = true", '["procedure"]'),
    ],
)
def test_a_rule_its_engine_has_loads(tmp_path: Path, engine: str, match: str, kinds: str) -> None:
    (rule,) = _load(tmp_path, match, engine, kinds)
    assert rule.source_engines == (engine,)
