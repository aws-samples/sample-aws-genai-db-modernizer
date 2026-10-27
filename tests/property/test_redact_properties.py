"""Property tests for literal redaction (#1625, ``src.tools.sql.redact``).

Redaction is idempotent for every input: redacting redacted text changes nothing, so
a collector output read many times, or written and read back, keeps one text. A tag
read from redacted text is already redacted, so the contract and the read of a
stored output leave it as it is.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.tools.sql.query_tags import statement_tags
from src.tools.sql.redact import redact_comment, redact_definition, redact_literals

_PIECES = list("ab1 2'\"%_$-/*#\n()[],.@:?=xNXq{}\\^~|+0eE;") + [
    "LIKE ",
    "TOP ",
    "DATE ",
    "$$",
    "$a$",
    "/* ",
    " */",
    "-- ",
    "q'[",
    "]'",
    "N'",
    "X'",
    "0x1F",
    "EXEC ",
    "ORDER BY ",
    " AS ",
    "COUNT(",
    "SELECT ",
    "date_trunc(",
    "jane@example.com",
    "4111111111111111",
    "user='jane%40example.com'",
    "controller='cart',",
]
_TEXT = st.lists(st.sampled_from(_PIECES), max_size=40).map("".join)
_ENGINE = st.sampled_from(["mysql", "postgresql", "sqlserver", "oracle", "db2", ""])
_SETTINGS = settings(max_examples=400, deadline=None, suppress_health_check=list(HealthCheck))


@_SETTINGS
@given(_TEXT, _ENGINE)
def test_redaction_is_idempotent(text: str, engine: str) -> None:
    for redact in (redact_literals, redact_definition, redact_comment):
        once = redact(text, engine)
        assert redact(once, engine) == once


@_SETTINGS
@given(_TEXT, _ENGINE)
def test_tags_of_redacted_text_are_already_redacted(text: str, engine: str) -> None:
    for tag in statement_tags(redact_literals(text, engine), engine):
        assert redact_comment(tag, engine) == tag
