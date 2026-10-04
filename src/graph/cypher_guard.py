"""Statement filter for caller-supplied Cypher: only single read statements pass.

LadybugDB does not expose a statement classifier to Python, so this is a
conservative token check. String literals, backtick-quoted identifiers and
comments are blanked out first, so keywords that appear inside them never
trigger a rejection. The remaining text must be a single statement that begins
with a read clause and contains none of the disallowed clause keywords. The
only procedures that may be called are the schema-introspection ones listed in
``ALLOWED_PROCEDURES``.

This filter is defence in depth: API reads also run on a read-only database
handle with a query timeout and a row cap (see ``src/graph/store.py``).
"""

from __future__ import annotations

import re

# Clause keywords that are rejected anywhere in a statement.
DISALLOWED_KEYWORDS = frozenset(
    {
        "ALTER",
        "ATTACH",
        "BEGIN",
        "CHECKPOINT",
        "COMMIT",
        "COPY",
        "CREATE",
        "DELETE",
        "DETACH",
        "DROP",
        "EXPORT",
        "FOREACH",
        "IMPORT",
        "INSTALL",
        "LOAD",
        "MERGE",
        "REMOVE",
        "ROLLBACK",
        "SET",
        "UNINSTALL",
        "USE",
    }
)

# Keywords a read statement may start with.
READ_START_KEYWORDS = frozenset({"MATCH", "OPTIONAL", "WITH", "UNWIND", "RETURN", "CALL"})

# Read-only schema-introspection procedures that may follow CALL.
ALLOWED_PROCEDURES = frozenset({"show_tables", "table_info", "show_connection", "db_version"})

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CALL_TARGET = re.compile(r"\bCALL\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.IGNORECASE)


class DisallowedStatementError(ValueError):
    """Raised when a statement is not a single read-only query."""


def _blank_literals_and_comments(cypher: str) -> str:
    """Replace string literals, backtick identifiers and comments with spaces.

    Literal contents are replaced by a neutral placeholder so token positions
    stay meaningful; an unterminated literal or block comment is rejected.
    Only ``//`` and ``/* */`` are treated as comments: ``--`` is a valid
    relationship pattern in Cypher, so it is left as code.
    """
    out: list[str] = []
    i = 0
    n = len(cypher)
    while i < n:
        ch = cypher[i]
        nxt = cypher[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            end = cypher.find("\n", i)
            i = n if end == -1 else end
            out.append(" ")
            continue
        if ch == "/" and nxt == "*":
            end = cypher.find("*/", i + 2)
            if end == -1:
                raise DisallowedStatementError("Unterminated comment in query.")
            i = end + 2
            out.append(" ")
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            j = i + 1
            while j < n:
                if cypher[j] == "\\" and quote != "`":
                    j += 2
                    continue
                if cypher[j] == quote:
                    break
                j += 1
            if j >= n:
                raise DisallowedStatementError("Unterminated quoted text in query.")
            out.append(" _lit_ " if quote != "`" else " _ident_ ")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _keywords(text: str) -> list[str]:
    """Upper-cased bare words, skipping property accesses (``n.x``) and params (``$x``)."""
    words: list[str] = []
    for match in _WORD.finditer(text):
        start = match.start()
        prev = text[start - 1] if start > 0 else ""
        if prev in (".", "$"):
            continue
        words.append(match.group(0).upper())
    return words


def validate_read_only_cypher(cypher: str) -> None:
    """Raise DisallowedStatementError unless ``cypher`` is a single read statement."""
    if not isinstance(cypher, str) or not cypher.strip():
        raise DisallowedStatementError("Query is empty.")

    text = _blank_literals_and_comments(cypher).strip()
    # Allow one optional trailing terminator; anything else after ';' is a
    # second statement.
    if text.endswith(";"):
        text = text[:-1].rstrip()
    if ";" in text:
        raise DisallowedStatementError("Only a single statement is allowed.")

    words = _keywords(text)
    if not words or words[0] not in READ_START_KEYWORDS:
        raise DisallowedStatementError("Only read queries are allowed.")

    blocked = sorted({w for w in words if w in DISALLOWED_KEYWORDS})
    if blocked:
        raise DisallowedStatementError(
            f"Only read queries are allowed; disallowed keyword(s): {', '.join(blocked)}."
        )

    call_count = words.count("CALL")
    targets = [m.group(1) for m in _CALL_TARGET.finditer(text)]
    if call_count != len(targets) or any(t.lower() not in ALLOWED_PROCEDURES for t in targets):
        raise DisallowedStatementError(
            "Only read queries are allowed; CALL is limited to: "
            + ", ".join(sorted(ALLOWED_PROCEDURES))
            + "."
        )
