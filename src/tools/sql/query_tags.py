"""Leading comment tags of a collected query (#1180).

Applications and drivers often open a statement with a comment naming the code
that issued it: sqlcommenter's ``/*controller='cart',action='add'*/`` (when the
driver prepends it), Rails QueryLogs, Hibernate's ``use_sql_comments``, or a plain
``-- endpoint: CartController.Add``. A caller that joins database statements to its
own code needs those tags, and when rows that differ only in literals merge into one
query shape, every row's tags, not only the representative text's.

``QueryPattern.query_tags`` holds them: each distinct leading-comment text, markers
removed and trimmed, in first-seen order across every row merged into the shape.
Trailing and inline comments are not tags. The leading comments are read with the
shared comment rule (:func:`src.shared.sql_comments.leading_comments`) in the
source dialect: ``#`` line comments and executable comments (``/*! ... */``, code,
not a comment) in the MySQL family, nested block comments in PostgreSQL and SQL
Server. An optimizer hint (``/*+ ... */``) is a comment but not a tag.

Tags are capped: at most :data:`MAX_QUERY_TAGS` per shape, each at most
:data:`MAX_QUERY_TAG_CHARS` characters. A longer tag is dropped, not cut (a cut tag
would no longer match the code that wrote it), and so is every distinct tag past
the count cap; ``QueryPattern.query_tags_dropped`` counts the distinct tags either
cap dropped, so a caller knows the list is incomplete.

Tags are untrusted customer text, and they only ever come from the query text the
collector already holds (``query_text`` keeps the comments; nothing new is
collected). They are carried for the caller only: no routing, scoring, triage or
access-pattern step reads them (``tests/unit/tools/sql/test_query_tags.py`` scans
for readers), and every model-facing step reads the collector output through
:func:`src.storage.collector_for_models.read_collector_for_models`, which removes them
with :func:`without_query_tags`. ``query_text`` itself is unchanged (#1180), so the
representative row's leading comment still reaches prompts inside it.

Which sources produce tags depends on the text each collector reads: PostgreSQL keeps
every leading comment; MySQL and MariaDB read ``DIGEST_TEXT``, which has its comments
removed, so they give none; the SQL Server and Oracle collectors replace newlines with
spaces, so only block-comment tags survive there.

:func:`parse_sqlcommenter` reads a tag in sqlcommenter's ``key='value'`` format
into its URL-decoded pairs, for callers that join on them; the contract keeps the
raw text.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote

from src.shared.sql_comments import block_comments_nest, leading_comments
from src.tools.sql.facade import sqlglot_dialect

__all__ = [
    "MAX_QUERY_TAGS",
    "MAX_QUERY_TAG_CHARS",
    "QUERY_TAG_FIELDS",
    "QueryTags",
    "parse_sqlcommenter",
    "statement_tags",
    "without_query_tags",
]

# At most this many distinct tags per query shape. A tag that names the calling
# code takes a handful of values per shape; one that carries a per-request value
# (a trace id) takes one per row, and is what this cap is for.
MAX_QUERY_TAGS = 32
# A tag longer than this is dropped whole. sqlcommenter tags with a W3C
# traceparent run to about 200 characters.
MAX_QUERY_TAG_CHARS = 512
# The QueryPattern fields this module fills.
QUERY_TAG_FIELDS = ("query_tags", "query_tags_dropped")


def statement_tags(sql: str, source_engine: str) -> list[str]:
    """The tags ``sql`` opens with, read in ``source_engine``'s dialect: each leading
    comment's text, trimmed, in order. Empty comments and optimizer hints are not
    tags. Not deduplicated or capped (see :class:`QueryTags`). Never raises."""
    engine = (source_engine or "").strip().lower()
    mysql_family = sqlglot_dialect(engine) == "mysql"
    texts = leading_comments(
        sql or "",
        hash_comments=mysql_family,
        nested=block_comments_nest(engine),
        mysql_executable=mysql_family,
    )
    tags = []
    for text in texts:
        tag = text.strip()
        # An optimizer hint has its + right after the marker (/*+ or --+); a comment
        # that only starts with + after a space (/* +1 retry */) is a tag.
        if tag and not text.startswith("+"):
            tags.append(tag)
    return tags


class QueryTags:
    """The capped, deduplicated tags of one query shape, in first-seen order.

    ``add`` the tags of each row merged into the shape, in row order; ``tags`` and
    ``dropped`` are the shape's ``query_tags`` and ``query_tags_dropped``.
    """

    def __init__(self) -> None:
        self.tags: list[str] = []
        self._kept: set[str] = set()
        # Digests, not the texts: an over-long tag is not held in memory.
        self._dropped: set[str] = set()
        self._dropped_before = 0

    @property
    def dropped(self) -> int:
        return len(self._dropped) + self._dropped_before

    def add(self, tags: Iterable[str], dropped: int = 0) -> None:
        """Add ``tags`` in order. ``dropped`` is a count some earlier merge already
        dropped (an existing pattern's ``query_tags_dropped``); it is added as is."""
        self._dropped_before += max(int(dropped or 0), 0)
        for tag in tags:
            if tag in self._kept:
                continue
            if len(tag) > MAX_QUERY_TAG_CHARS or len(self.tags) >= MAX_QUERY_TAGS:
                self._dropped.add(hashlib.sha256(tag.encode("utf-8")).hexdigest())
                continue
            self._kept.add(tag)
            self.tags.append(tag)

    def fields(self) -> dict[str, Any]:
        """The QueryPattern fields: ``{"query_tags": [...], "query_tags_dropped": n}``."""
        return {"query_tags": list(self.tags), "query_tags_dropped": self.dropped}

    @classmethod
    def of(cls, texts: Iterable[str], source_engine: str) -> QueryTags:
        """The tags of the statements ``texts``, one per merged row, in order."""
        acc = cls()
        for text in texts:
            acc.add(statement_tags(text, source_engine))
        return acc


def parse_sqlcommenter(tag: str) -> dict[str, str] | None:
    """The ``key='value'`` pairs of a sqlcommenter tag, URL-decoded; None otherwise.

    sqlcommenter writes ``key='value'`` pairs separated by commas, with each key and
    value URL-encoded and a ``'`` inside a value escaped as ``\\'``; the pairs are
    sorted by key. So ``controller='cart',route='%2Fcart%2Fadd'`` is
    ``{"controller": "cart", "route": "/cart/add"}``. A tag in any other shape (a
    missing quote, an empty key, a key given twice, text after a value) is not a
    sqlcommenter tag and returns None, so its raw text is all a caller has.
    """
    pairs: dict[str, str] = {}
    i, n = 0, len(tag)
    while i < n:
        eq = tag.find("=", i)
        if eq <= i or not tag.startswith("'", eq + 1):
            return None
        key = unquote(tag[i:eq].strip())
        j = eq + 2
        value: list[str] = []
        while True:
            if j >= n:
                return None  # unterminated value
            ch = tag[j]
            if ch == "\\" and j + 1 < n and tag[j + 1] == "'":
                value.append("'")
                j += 2
            elif ch == "'":
                break
            else:
                value.append(ch)
                j += 1
        if not key or key in pairs:
            return None
        pairs[key] = unquote("".join(value))
        j += 1
        if j == n:
            break
        if tag[j] != ",":
            return None
        i = j + 1
        if i == n:
            return None  # a trailing comma
    return pairs or None


def without_query_tags(collector_output: dict) -> dict:
    """``collector_output`` with every query pattern's tag fields removed.

    For model inputs that carry whole query patterns: tags join a caller's code,
    they are no input to a design. The value given is not modified; the patterns are
    copied only when one has a tag field.
    """
    queries = collector_output.get("queries")
    if not isinstance(queries, dict):
        return collector_output
    patterns = queries.get("query_patterns")
    if not patterns or not any(
        isinstance(q, dict) and any(f in q for f in QUERY_TAG_FIELDS) for q in patterns
    ):
        return collector_output
    stripped = [
        {k: v for k, v in q.items() if k not in QUERY_TAG_FIELDS} if isinstance(q, dict) else q
        for q in patterns
    ]
    return {**collector_output, "queries": {**queries, "query_patterns": stripped}}
