# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Putting a routine's literals back into a model's redacted repair (#1941).

The repair is rebuilt onto the exact text token by token, in the tokenizer's order
(``src.tools.routines.repair_rebuild``): never by searching the text for ``'?'``. A
routine whose literals cannot be put back by position stays *needs attention*, with the
reason. Every text here is a synthetic fixture; the redacted texts are what
``redact_definition`` makes of the exact ones, as the collection reads them (#1625).
"""

from __future__ import annotations

import pytest

from src.tools.routines.proof_screen import literal_sequence
from src.tools.routines.repair_rebuild import (
    RebuildRefused,
    literal_spans,
    rebuild_exact,
    rebuild_refusal,
)
from src.tools.sql.redact import redact_definition

PG = "postgresql"
MY = "mysql"

# Quotes doubled, an escape string, a dollar-quoted string inside the body, and a comment
# that reads '?' (no literal).
PG_EXACT = (
    "CREATE OR REPLACE FUNCTION public.greet(p integer)\n RETURNS text\n LANGUAGE plpgsql\n"
    "AS $function$\nBEGIN\n  -- returns '?' on no match\n"
    "  RETURN 'it''s ' || E'a\\tb' || $q$Dollar Alpha$q$ || p::text || 'Omega';\nEND\n"
    "$function$\n"
)
MY_EXACT = (
    "CREATE DEFINER=`app`@`%` PROCEDURE `hello`(IN p int, OUT r varchar(64))\n"
    "BEGIN\n  /* '?' in a comment */\n"
    "  SELECT CONCAT('Don''t', \"Quoted Alpha\", _utf8mb4'Intro Beta', p, X'0A') INTO r;\nEND"
)


def _seen(exact: str, engine: str) -> str:
    return redact_definition(exact, engine)


def test_the_walk_reads_the_literals_the_screen_reads() -> None:
    for text, engine in ((PG_EXACT, PG), (MY_EXACT, MY)):
        assert [(x.kind, x.content) for x in literal_spans(text, engine)] == list(
            literal_sequence(text, engine)
        )
        for lit in literal_spans(text, engine):
            assert lit.span is not None and text[lit.span[0] : lit.span[1] + 1] == lit.raw


@pytest.mark.parametrize(("exact", "engine"), [(PG_EXACT, PG), (MY_EXACT, MY)], ids=["pg", "mysql"])
def test_each_literal_goes_back_in_its_place_with_its_quotes_and_escapes(exact, engine) -> None:
    seen = _seen(exact, engine)
    assert "Alpha" not in seen and "Omega" not in seen and "Beta" not in seen
    # The model's repair: an unrelated edit before the literals, every placeholder kept.
    candidate = (
        seen.replace("p::text", "CAST(p AS text)")
        if engine == PG
        else seen.replace("IN p int", "IN p integer")
    )
    assert candidate != seen
    rebuilt = rebuild_exact(exact, seen, candidate, engine)
    assert literal_sequence(rebuilt, engine) == literal_sequence(exact, engine)
    for raw in (
        ("'it''s '", "E'a\\tb'", "$q$Dollar Alpha$q$", "'Omega'")
        if engine == PG
        else ("'Don''t'", '"Quoted Alpha"', "_utf8mb4'Intro Beta'", "X'0A'")
    ):
        assert raw in rebuilt
    # The candidate's own edit is kept, and the comment's '?' is left as the model wrote it.
    assert ("CAST(p AS text)" if engine == PG else "IN p integer") in rebuilt
    assert rebuilt.count("'?'") == 1


def test_a_moved_placeholder_takes_the_literal_of_its_new_position() -> None:
    """Position decides: a swapped placeholder takes the other literal, which only the
    side-by-side proof can tell (the screen sees '?' and '?')."""
    exact = (
        "CREATE FUNCTION public.sign_word(p integer) RETURNS text LANGUAGE sql "
        "AS $$ SELECT CASE WHEN p > 0 THEN 'pos' ELSE 'neg' END $$"
    )
    seen = _seen(exact, PG)
    flipped = seen.replace("p > 0", "p <= 0")
    rebuilt = rebuild_exact(exact, seen, flipped, PG)
    assert "WHEN p <= 0 THEN 'pos' ELSE 'neg'" in rebuilt


def test_the_text_is_never_searched_for_the_placeholder() -> None:
    """A '?' inside a comment of the candidate is no literal: it is left alone, and the
    real placeholder after it is the one filled."""
    exact = "CREATE FUNCTION public.f() RETURNS text LANGUAGE sql AS $$ SELECT 'Alpha' $$"
    seen = _seen(exact, PG)
    candidate = seen.replace("SELECT", "/* was '?' */ SELECT")
    rebuilt = rebuild_exact(exact, seen, candidate, PG)
    assert "/* was '?' */ SELECT 'Alpha'" in rebuilt


@pytest.mark.parametrize(
    ("exact", "engine", "code"),
    [
        # A source literal that itself reads '?'.
        (
            "CREATE FUNCTION public.f() RETURNS text LANGUAGE sql AS $$ SELECT '?' || 'Alpha' $$",
            PG,
            "literal_reads_placeholder",
        ),
        # A LIKE pattern keeps its shape ('%x%'): the model saw it only in part.
        (
            "CREATE FUNCTION public.f(p text) RETURNS boolean LANGUAGE sql "
            "AS $$ SELECT p LIKE '%Jane%' $$",
            PG,
            "literal_partly_redacted",
        ),
        # A dynamic statement redacted as SQL: the literal inside it was never seen.
        (
            "CREATE FUNCTION public.f() RETURNS void LANGUAGE plpgsql AS $$ BEGIN "
            "EXECUTE 'DELETE FROM t WHERE a = ''Alpha'''; END $$",
            PG,
            "literal_partly_redacted",
        ),
        # A statement built at run time from a redacted literal.
        (
            "CREATE PROCEDURE p() BEGIN SET @s = 'DELETE FROM t'; PREPARE st FROM @s; "
            "EXECUTE st; END",
            MY,
            "literal_holds_sql",
        ),
        # #1941 review: a statement word anywhere in a redacted literal, not only first.
        (
            "CREATE FUNCTION public.f() RETURNS text LANGUAGE plpgsql AS $$ BEGIN "
            "RETURN ';DROP TABLE public.orders'; END $$",
            PG,
            "literal_holds_sql",
        ),
        (
            "CREATE FUNCTION public.f() RETURNS text LANGUAGE plpgsql AS $$ BEGIN "
            "RETURN 'x; select secret from vault'; END $$",
            PG,
            "literal_holds_sql",
        ),
        # A backslash escape the redaction cannot read under unknown string settings: it
        # fails closed, and the counts no longer match.
        (
            "CREATE PROCEDURE p(OUT r text) BEGIN SELECT CONCAT('a\\'b', 'Alpha') INTO r; END",
            MY,
            "literal_count",
        ),
    ],
    ids=[
        "reads-placeholder",
        "like-shape",
        "dynamic-sql",
        "prepared-from-literal",
        "sql-after-a-semicolon",
        "sql-in-lower-case",
        "escape",
    ],
)
def test_a_routine_whose_literals_cannot_be_put_back_is_refused(exact, engine, code) -> None:
    seen = _seen(exact, engine)
    assert rebuild_refusal(exact, seen, engine) == code
    with pytest.raises(RebuildRefused) as refused:
        rebuild_exact(exact, seen, seen + " ", engine)
    assert refused.value.code == code
    # The words name no literal.
    assert "Alpha" not in str(refused.value) and "Jane" not in str(refused.value)


def test_a_candidate_that_breaks_the_placeholders_is_not_rebuilt() -> None:
    exact = "CREATE FUNCTION public.f() RETURNS text LANGUAGE sql AS $$ SELECT 'Alpha' $$"
    seen = _seen(exact, PG)
    assert rebuild_refusal(exact, seen, PG) is None
    for bad, code in (
        (seen.replace("'?'", "'?' || '?'"), "literal_count"),
        (seen.replace("'?'", "'x'"), "not_rebuilt"),
    ):
        with pytest.raises(RebuildRefused) as refused:
            rebuild_exact(exact, seen, bad, PG)
        assert refused.value.code == code


def test_a_literal_that_would_close_the_candidates_quotes_is_not_rebuilt() -> None:
    """The exact literal holds the candidate's own dollar tag: put back, it would end the
    body early. The rebuilt text no longer reads back to the exact literals."""
    exact = "CREATE FUNCTION public.f() RETURNS text LANGUAGE sql AS $f$ SELECT 'a$$b' $f$"
    seen = _seen(exact, PG)
    assert "'?'" in seen
    candidate = seen.replace("$f$", "$$")
    with pytest.raises(RebuildRefused) as refused:
        rebuild_exact(exact, seen, candidate, PG)
    assert refused.value.code == "not_rebuilt"


def test_a_word_that_only_contains_a_statement_word_is_not_sql() -> None:
    """Word boundaries: 'Selection' and 'updated_at' hold no statement."""
    exact = (
        "CREATE FUNCTION public.f() RETURNS text LANGUAGE sql "
        "AS $$ SELECT 'Selection' || 'updated_at' $$"
    )
    assert rebuild_refusal(exact, _seen(exact, PG), PG) is None


def test_a_prose_word_counts_only_at_the_start() -> None:
    exact = (
        "CREATE FUNCTION public.f() RETURNS text LANGUAGE sql "
        "AS $$ SELECT 'Please do not show the table' $$"
    )
    assert rebuild_refusal(exact, _seen(exact, PG), PG) is None
