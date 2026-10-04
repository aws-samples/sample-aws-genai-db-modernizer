"""Statement filter for caller-supplied Cypher: only single, bounded read statements pass.

LadybugDB does not expose a statement classifier to Python, so this is a
conservative token check. String literals, backtick-quoted identifiers and
comments are blanked out first (length-preserving), so keywords that appear
inside them never trigger a rejection. The remaining text must:

- be printable ASCII (plus tab/newline/carriage return),
- be at most ``MAX_QUERY_LENGTH`` characters with bounded nesting,
- be a single statement that begins with a read clause,
- contain none of ``DISALLOWED_KEYWORDS``, and
- only ``CALL`` procedures in the ``ALLOWED_PROCEDURES`` allowlist.

This filter is the control that decides which statements may run. The
read-only database handle used for API reads rejects data and schema writes,
but it does not reject every statement this filter refuses (for example
``COPY ... TO``, ``EXPORT DATABASE``, ``INSTALL`` or ``CALL <option>=<value>``
succeed on a read-only handle), so the filter must stay an allowlist.

``bound_result_rows`` then rewrites the accepted statement so the engine
itself stops after ``max_rows + 1`` rows.
"""

from __future__ import annotations

import re

MAX_QUERY_LENGTH = 10_000
# Deepest allowed nesting of (), [] and {} combined, and of CASE ... END.
MAX_BRACKET_DEPTH = 32
MAX_CASE_DEPTH = 8

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
        "UNION",
        "USE",
    }
)

# Keywords a read statement may start with.
READ_START_KEYWORDS = frozenset({"MATCH", "OPTIONAL", "WITH", "UNWIND", "RETURN", "CALL"})

# Allowlist of read-only schema-introspection procedures that may follow CALL.
# Anything else (other table functions, `CALL { ... }`, `CALL <option>=<value>`)
# is rejected.
ALLOWED_PROCEDURES = frozenset({"show_tables", "table_info", "show_connection", "db_version"})

# Functions whose output size is set by an argument rather than by their input
# (they materialise the whole value before the timeout or buffer pool apply).
# They are allowed only with integer-literal size arguments up to
# MAX_GENERATED_ELEMENTS; any other use is rejected.
MAX_GENERATED_ELEMENTS = 1_000
_INT = r"(-?\d+)"
_ARG = r"[^(),]*"
_BOUNDED_CALLS = {
    "range": re.compile(rf"\(\s*{_INT}\s*,\s*{_INT}\s*(?:,\s*{_INT}\s*)?\)", re.IGNORECASE),
    "repeat": re.compile(rf"\({_ARG},\s*{_INT}\s*\)", re.IGNORECASE),
    "lpad": re.compile(rf"\({_ARG},\s*{_INT}\s*,{_ARG}\)", re.IGNORECASE),
    "rpad": re.compile(rf"\({_ARG},\s*{_INT}\s*,{_ARG}\)", re.IGNORECASE),
}
_GENERATOR_CALL = re.compile(
    r"(?<![.$\w])(" + "|".join(_BOUNDED_CALLS) + r")\s*(?=\()", re.IGNORECASE
)

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CALL_TARGET = re.compile(r"\bCALL\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.IGNORECASE)
_TRAILING_LIMIT = re.compile(r"\bLIMIT\s+(\d+)$", re.IGNORECASE)
_RETURN = re.compile(r"(?<![.$\w])RETURN\b", re.IGNORECASE)
_LIMIT = re.compile(r"(?<![.$\w])LIMIT\b", re.IGNORECASE)
_NESTING_TOKEN = re.compile(r"[()\[\]{}]|(?<![.$\w])(?:CASE|END)\b", re.IGNORECASE)
_ALLOWED_CONTROL = frozenset("\t\n\r")
_QUOTED = "#"
_QUOTED_CALL = re.compile(r"#\s*\(")
_OPENERS = frozenset("([{")
_CLOSERS = frozenset(")]}")
_CASE = "CASE"


class DisallowedStatementError(ValueError):
    """Raised when a statement is not a single bounded read-only query."""


def _blank_literals_and_comments(cypher: str) -> str:
    """Blank out comments (spaces) and quoted text (``#`` placeholders).

    The result has the same length as the input, so offsets map back to the
    original text. An unterminated literal or block comment is rejected. Only
    ``//`` and ``/* */`` are treated as comments: ``--`` is a valid
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
            end = n if end == -1 else end
            out.append(" " * (end - i))
            i = end
            continue
        if ch == "/" and nxt == "*":
            end = cypher.find("*/", i + 2)
            if end == -1:
                raise DisallowedStatementError("Unterminated comment in query.")
            out.append(" " * (end + 2 - i))
            i = end + 2
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
            # Quoted spans become a non-space placeholder so a trailing
            # identifier or string is not mistaken for trailing whitespace.
            out.append(_QUOTED * (j + 1 - i))
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


def _check_characters(text: str) -> None:
    for ch in text:
        if ch in _ALLOWED_CONTROL:
            continue
        if not (" " <= ch <= "~"):
            raise DisallowedStatementError(
                "Query contains unsupported characters outside quoted text."
            )


def _check_nesting(text: str) -> None:
    brackets = 0
    cases = 0
    for match in _NESTING_TOKEN.finditer(text):
        symbol = match.group(0).upper()
        if symbol in _OPENERS:
            brackets += 1
            if brackets > MAX_BRACKET_DEPTH:
                raise DisallowedStatementError("Query is nested too deeply.")
        elif symbol in _CLOSERS:
            brackets -= 1
        elif symbol == _CASE:
            cases += 1
            if cases > MAX_CASE_DEPTH:
                raise DisallowedStatementError("Query is nested too deeply.")
        else:  # END
            cases = max(cases - 1, 0)


def _generated_size(name: str, numbers: list[int]) -> int:
    if name == "range":
        start, stop = numbers[0], numbers[1]
        step = numbers[2] if len(numbers) > 2 else 1
        if step == 0:
            raise DisallowedStatementError("range() step must not be zero.")
        return max(0, (stop - start) // step + 1)
    return numbers[0]


def _check_generators(text: str) -> None:
    """Allow size-generating functions only with small integer-literal sizes."""
    for match in _GENERATOR_CALL.finditer(text):
        name = match.group(1).lower()
        args = _BOUNDED_CALLS[name].match(text, match.end())
        if args is None:
            raise DisallowedStatementError(
                f"{name}() is only allowed with integer-literal size arguments."
            )
        numbers = [int(g) for g in args.groups() if g is not None]
        if _generated_size(name, numbers) > MAX_GENERATED_ELEMENTS:
            raise DisallowedStatementError(
                f"{name}() may generate at most {MAX_GENERATED_ELEMENTS:,} elements."
            )


def _statement_text(cypher: str) -> tuple[str, int]:
    """Validate and return (blanked statement text, end offset in the original).

    The end offset excludes trailing whitespace, comments and one optional
    trailing ``;``.
    """
    if not isinstance(cypher, str) or not cypher.strip():
        raise DisallowedStatementError("Query is empty.")
    if len(cypher) > MAX_QUERY_LENGTH:
        raise DisallowedStatementError(
            f"Query is longer than the {MAX_QUERY_LENGTH:,}-character limit."
        )

    blanked = _blank_literals_and_comments(cypher)
    _check_characters(blanked)

    end = len(blanked.rstrip())
    if end and blanked[end - 1] == ";":
        end = len(blanked[: end - 1].rstrip())
    text = blanked[:end]
    if ";" in text:
        raise DisallowedStatementError("Only a single statement is allowed.")

    _check_nesting(text)
    if _QUOTED_CALL.search(text):
        raise DisallowedStatementError("Quoted function names are not supported.")
    _check_generators(text)

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
    return text, end


def validate_read_only_cypher(cypher: str) -> None:
    """Raise DisallowedStatementError unless ``cypher`` is a single read statement."""
    _statement_text(cypher)


def bound_result_rows(cypher: str, max_rows: int) -> str:
    """Validate ``cypher`` and return it with a top-level LIMIT of at most ``max_rows + 1``.

    The extra row lets the caller tell whether the result was truncated. An
    existing trailing ``LIMIT <integer>`` is kept when it is already within
    the bound and lowered otherwise; a trailing LIMIT that is not an integer
    literal is rejected. Trailing comments and ``;`` are dropped.
    """
    text, end = _statement_text(cypher)
    bound = max_rows + 1
    statement = cypher[:end]
    match = _TRAILING_LIMIT.search(text)
    if match is None:
        returns = list(_RETURN.finditer(text))
        tail = text[returns[-1].end() :] if returns else text
        if _LIMIT.search(tail):
            raise DisallowedStatementError("A trailing LIMIT must be an integer literal.")
        return f"{statement} LIMIT {bound}"
    if int(match.group(1)) <= bound:
        return statement
    return f"{statement[: match.start(1)]}{bound}"
