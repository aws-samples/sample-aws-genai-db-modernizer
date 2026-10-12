"""
Property-based test for collector digest normalization (#556).

Property: normalization is idempotent -- running ``normalize_digest_text``
on its own output produces the same text, with no further substitutions.
That is the guarantee callers rely on: it is always safe to normalize text
that may already be normalized. The ``truncated`` flag is also checked for
stability across a second call: nothing a substitution rule does should
flip it (see ``src/tools/sql/normalize.py`` -- every rule only ever
rewrites a "?"-based pattern to "NULL"/"(NULL)", which cannot change a
paren count or cross a quote/comment boundary).

The character alphabet below is deliberately wide (comments, doubled and
backslash-escaped quotes, double-quoted identifiers, dollar-quoting, and
characters packed together with no separating whitespace) rather than a
fixed set of pre-formed tokens, so Hypothesis can combine them in ways a
hand-written token list would not think to try. This is also how review
round 1 on PR #560 found the bug fixed in the same round: a chained
``..., ...`` wasn't a fixed point for the (now rewritten) trailing-ellipsis
rule in a single call.
"""

from __future__ import annotations

from hypothesis import example, given, settings
from hypothesis import strategies as st

from src.tools.sql.normalize import normalize_digest_text

# ---------------------------------------------------------------------------
# Strategy: raw character soup over everything normalize.py's scanner and
# substitution rules care about, plus plain letters/digits so most
# generated text still looks like ordinary (if nonsensical) SQL.
# ---------------------------------------------------------------------------

_KEYWORD_LETTERS = "SELECT FROM WHERE AND OR NOT IN LIKE VALUES LIMIT BY UPDATE ON FOR ALL SET"
_IDENTIFIER_CHARS = "abcdeft123"
_PUNCTUATION = "?.,()'\"`$#-/*\\ \n\t"  # pragma: allowlist secret -- SQL punctuation, not a secret
_ALPHABET = _KEYWORD_LETTERS + _IDENTIFIER_CHARS + _PUNCTUATION

_query_text = st.text(alphabet=_ALPHABET, max_size=80)

_dialect = st.sampled_from(["mysql", "mariadb", "postgresql", "sqlserver"])


@given(query_text=_query_text, dialect=_dialect)
@settings(max_examples=300, deadline=None)
@example(query_text="SELECT `id` FROM `t` WHERE `status` IN (...) LIMIT ?, ...", dialect="mysql")
@example(query_text="SELECT * FROM t WHERE name = 'what?' AND id = ?", dialect="mysql")
@example(query_text="SELECT * FROM t /* it's ? */ WHERE a = ?", dialect="mysql")
@example(query_text="SELECT 1 WHERE a = ? -- comment", dialect="mysql")
@example(query_text="SELECT $$it's a $1 test$$ WHERE a = $2", dialect="postgresql")
@example(query_text="SELECT * FROM t WHERE a = 'it\\'s' AND b = ?", dialect="mysql")
def test_normalization_is_idempotent_and_truncated_is_stable(query_text: str, dialect: str) -> None:
    first = normalize_digest_text(query_text, dialect)
    second = normalize_digest_text(first.text, dialect)

    assert second.text == first.text
    assert second.substitutions == ()
    assert second.truncated == first.truncated
