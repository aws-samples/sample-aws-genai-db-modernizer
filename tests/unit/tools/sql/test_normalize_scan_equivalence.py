# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``normalize._scan`` reads its spans from ``sql_spans``, and masks what it masked (#1451).

``_reference_scan`` is the scanner as it was before: its own state machine with its own
copy of the comment, string and identifier rules. The new scan must give the same masks,
paren depth and truncation signals on every dialect over the corpus, every sample query,
every SQL string in the tests and generated token soups.

Two readings changed on purpose, to the shared grammar's: a ``$`` that continues a name
(``a$b$``) opens no dollar quote, and ``#E'..'`` in PostgreSQL is an operator then an
escape string (the old scan took the ``#`` as part of a name). The comparison skips texts
with either, and a test pins the changed readings.

A third changed with #1482: MySQL and MariaDB read ``--`` as a comment only before a space
or a control character (or the end of the text), so ``5--1`` is arithmetic there. The
MySQL-family comparison skips texts with another ``--``, and a test pins the reading.
"""

from __future__ import annotations

import re

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from src.tools.database.offline_parser import parse_offline_collection
from src.tools.sql.normalize import _scan, normalize_digest_text, quoted_mask
from tests.corpus.adversarial_inputs import SQL_ENTRIES
from tests.unit.tools.sql import _reference_scan
from tests.unit.tools.sql.table_refs_snapshot import SAMPLES, load_sample
from tests.unit.tools.sql.test_linear_detectors import _test_strings

# Every test here guards the masks the old scan gave, which main has: they pass there.
_DIALECTS = (
    "mysql",
    "mariadb",
    "MySQL",
    "postgresql",
    "postgres",
    "aurora_postgresql",
    "sqlserver",
    "tsql",
    "mssql",
    "oracle",
    "db2",
    "",
)


def _same(text: str, dialect: str, brackets: bool) -> None:
    new = _scan(text, dialect, brackets)
    old = _reference_scan._scan(text, dialect, brackets)
    where = (dialect, brackets, text[:120])
    assert new.mask == old.mask, where
    assert new.comment_mask == old.comment_mask, where
    assert new.paren_depth == old.paren_depth, where
    assert new.unterminated_quote == old.unterminated_quote, where
    assert new.unterminated_comment == old.unterminated_comment, where


_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})


def _mysql_dash_reading(text: str) -> bool:
    """Whether the text holds a ``--`` MySQL reads as two minus signs (#1482)."""
    return re.search(r"--(?![\x00-\x20\x7f]|\Z)", text) is not None


def _all_dialects(text: str) -> None:
    dashes = _mysql_dash_reading(text)
    for dialect in _DIALECTS:
        if dashes and dialect.lower() in _MYSQL_FAMILY:
            continue
        _same(text, dialect, False)
        # Callers read ``[name]`` as quoted for SQL Server only (the old scan let a MySQL
        # backslash escape inside one, which no caller reaches).
        if dialect in ("sqlserver", "tsql", "mssql"):
            _same(text, dialect, True)


@pytest.fixture(scope="module")
def texts() -> list[str]:
    found = list(_test_strings())
    for name in SAMPLES:
        found += [q["query_text"] for q in parse_offline_collection(load_sample(name))["queries"]]
    for entry in SQL_ENTRIES:
        found += [entry.text, entry.twin]
    return found


@pytest.mark.passes_on_main
def test_the_scan_masks_what_it_masked_on_the_corpus_and_the_samples(texts):
    assert len(texts) > 2_000
    for text in texts:
        if not _changed_reading(text):
            _all_dialects(text)


def _changed_reading(text: str) -> bool:
    """Whether the text holds a form the shared grammar reads differently on purpose."""
    return re.search(r"[\w$]\$|#[eE]'", text) is not None


@pytest.mark.passes_on_main
def test_the_normalized_digests_of_the_samples_are_unchanged(texts):
    # The scan feeds normalize_digest_text; its output must not move for any dialect.
    for text in texts:
        for dialect in ("mysql", "postgresql", "sqlserver", "oracle"):
            if _changed_reading(text) or (dialect == "mysql" and _mysql_dash_reading(text)):
                continue
            assert normalize_digest_text(text, dialect) == _with_reference(text, dialect)


def _with_reference(text: str, dialect: str):
    from src.tools.sql import normalize

    def reference(text: str, dialect: str, brackets: bool = False, settings=None):  # type: ignore[no-untyped-def]
        return _reference_scan._scan(text, dialect, brackets)

    real = normalize._scan
    normalize._scan = reference  # type: ignore[assignment]
    try:
        return normalize.normalize_digest_text(text, dialect)
    finally:
        normalize._scan = real  # type: ignore[assignment]


_TOKENS = [
    "'",
    '"',
    "`",
    "[",
    "]",
    "(",
    ")",
    "--",
    "#",
    "/*",
    "*/",
    "\\",
    "$$",
    "$a$",
    "E'",
    "e'",
    "q'",
    "Q'",
    "nq'",
    "[",
    "{",
    "}",
    "<",
    ">",
    "!",
    "\n",
    " ",
    "x",
    "''",
    '""',
    "SELECT",
    ";",
    "?",
]


@pytest.mark.passes_on_main
@settings(max_examples=2500, deadline=None)
@given(st.lists(st.sampled_from(_TOKENS), max_size=30))
def test_generated_text_is_masked_as_before(tokens):
    text = "".join(tokens)
    assume(not _changed_reading(text))
    _all_dialects(text)


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_mysql_reads_a_dash_pair_before_a_non_space_as_minus_signs(dialect):
    text = "SELECT 5--1 FROM t WHERE a = 'x'"
    assert not any(quoted_mask(text, dialect)[: text.index("'")])
    assert any(_reference_scan._scan(text, dialect).mask[: text.index("'")])  # the old reading
    # Before a space, a tab, a control character or the end of the text it is a comment.
    for tail in (" c", "\tc", "\x01c", ""):
        masked = quoted_mask(f"SELECT 5 --{tail}", dialect)
        assert masked[len("SELECT 5 ") :] == [True] * (2 + len(tail))
    # Before a newline it is an empty comment, and the next line is code.
    assert quoted_mask("SELECT 5 --\nc", dialect)[-4:] == [True, True, True, False]


@pytest.mark.passes_on_main
def test_a_dollar_inside_a_name_opens_no_dollar_quote():
    text = "SELECT a$b$ FROM t WHERE x = 1"
    assert not any(quoted_mask(text, "postgresql"))
    assert any(_reference_scan._scan(text, "postgresql").mask)  # the old reading


@pytest.mark.passes_on_main
def test_a_hash_before_an_escape_string_is_an_operator_in_postgresql():
    text = "SELECT a #E'x' FROM t WHERE b = 1"
    assert quoted_mask(text, "postgresql")[text.index("x")]
    assert not quoted_mask(text, "postgresql")[text.index("FROM")]


@pytest.mark.passes_on_main
def test_oracle_q_quotes_mask_as_before_and_other_dialects_do_not_read_them():
    text = "SELECT 1 FROM t WHERE a = q'[it's]' AND b = 1"
    assert quoted_mask(text, "oracle") == _reference_scan._scan(text, "oracle").mask
    assert quoted_mask(text, "oracle")[text.index("it's")]
    # Elsewhere q is a name and '[it' a string; the apostrophe in "it's" ends it.
    assert quoted_mask(text, "postgresql")[text.index("it's")]
    assert not quoted_mask(text, "postgresql")[text.index("s]")]
    assert quoted_mask(text, "postgresql") == _reference_scan._scan(text, "postgresql").mask
