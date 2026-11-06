# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Parse and check one MySQL routine against Aurora MySQL (#1936), no model.

The input is a routine's ``CREATE`` statement plus the metadata the collection gives with
it (:class:`MySqlRoutine`); the output is the shared verdict
(:class:`~src.tools.routines.verdict.RoutineVerdict`): whether the statement reads, one
result per applicable rule of ``src/catalog/routine_checks/``
(:mod:`src.shared.catalog.routine_checks`), and the tables and routines it names. Every
rule is data; this module only applies them, one routine at a time, with no I/O beyond
reading the catalog. It never puts routine text in a verdict or an error, and it never
raises on any text: what it cannot read is a ``parse`` failure, or an incomplete
dependency list.

Reading the text, in order (every step linear in the text's length):

1. **Client statements.** The text may come from a dump rather than ``SHOW CREATE``:
   ``DELIMITER`` lines before the routine's ``CREATE`` are honoured as the ``mysql``
   client honours them (a body line such as ``delimiter = 1`` in a ``SHOW CREATE`` text
   is not one), executable comments (``/*!50003 CREATE*/``) are unwrapped, and the
   ``DROP``, ``SET`` and ``USE`` statements a dump puts around a routine are skipped.
   Exactly one ``CREATE`` must remain. A backslash escapes a quote unless the routine's
   ``sql_mode`` has ``NO_BACKSLASH_ESCAPES``.
2. **Tokens**, from the SQL facade's tokenizer in the ``mysql`` dialect
   (:func:`src.tools.sql.facade.routine_tokens`).
3. **Header**: ``DEFINER``, the object kind and name, parameters, ``RETURNS``, the
   characteristics (``SQL SECURITY``), a trigger's table and ``FOLLOWS``/``PRECEDES``, an
   event's schedule up to ``DO``.
4. **Body structure**: compound statements (``BEGIN ... END``, ``IF``, ``CASE``, ``LOOP``,
   ``WHILE``, ``REPEAT``, nested at most :data:`MAX_NESTING` deep) nest and close, labels
   match, ``LEAVE`` names an enclosing label and ``ITERATE`` an enclosing loop's, a
   handler's statement names no label of the blocks around it, ``DECLARE`` statements
   open their block in MySQL's order (variables and conditions, then cursors, then
   handlers), and each handler names valid, declared, distinct conditions.
5. **Statements**: each simple statement in the body (and each condition, ``RETURN`` or
   ``DEFAULT`` expression) is read by the SQL facade
   (:func:`src.tools.sql.facade.routine_statement_refs`) for the tables and routines it
   names. The facade's parser does not read MySQL's stored-program syntax by itself
   (``IN``/``OUT`` parameters, ``DECLARE``, handlers, ``IF``/``CASE``/``LOOP``, triggers,
   events, ``SELECT ... INTO`` a variable list), which is why steps 3 and 4 are this
   module's own; a statement the facade cannot read leaves ``dependencies_complete``
   False and is never a failed check.

The checks (each rule's ``[match]`` says which):

- ``parse``: steps 1 to 4 read the text (the header, the quoting, the blocks);
- ``declare_order`` and ``handler``: step 4's ``DECLARE`` and handler rules;
- ``definer``: the ``DEFINER`` names a user other than the target user (a ``CURRENT_USER``
  form passes), for a routine that runs as its definer;
- ``statement``: a body statement starts with or contains a listed keyword sequence
  (unless a listed exception starts there), SETs a listed scope or variable, or calls a
  listed function; the text of a ``PREPARE ... FROM '<literal>'`` and of a string literal
  assigned to the user variable a ``PREPARE ... FROM @var`` names is checked too;
- ``sql_mode``: the routine's stored ``sql_mode`` holds a listed mode, or one that no
  rule of the catalog names and MySQL does not list;
- ``dynamic_sql``: a ``PREPARE`` from a user variable, whose text at run time no check
  can see (the literals assigned to it are checked, but the variable may hold anything).

``parse``, ``declare_order`` and ``handler`` fail together with a ``parse_failure``; the
``definer`` check runs when the header reads, the ``sql_mode`` check always (it reads
metadata), and the ``statement`` and ``dynamic_sql`` checks only when the body reads.

Dependencies are recorded, not checked: the dependency check compares them with what
moves in the same step (#1921). A called function that is not a MySQL built-in
(:mod:`src.tools.routines.mysql_builtins`) is recorded as a routine, with its schema when
the call names one. A routine with a ``PREPARE`` has an incomplete dependency list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.shared.catalog.gaps import aurora_version_lines
from src.shared.catalog.routine_checks import (
    RoutineCheck,
    RoutineCheckRule,
    RoutineKind,
    routine_check_rules,
)
from src.tools.routines.mysql_builtins import is_builtin_function
from src.tools.routines.verdict import (
    ParseFailure,
    RoutineCheckResult,
    RoutineDependency,
    RoutineVerdict,
)
from src.tools.sql.facade import (
    RoutineToken,
    RoutineTokenError,
    routine_statement_refs,
    routine_tokens,
)

__all__ = [
    "MAX_NESTING",
    "SOURCE_ENGINE",
    "MySqlHeader",
    "MySqlRoutine",
    "check_mysql_routine",
    "is_target_user",
    "mysql_header",
    "parse_account",
    "split_client_statements",
]

# The collector source engine these routines come from (the rules' source_engines).
SOURCE_ENGINE = "mysql"
#: The deepest nesting of compound statements the body reader follows; a deeper body is
#: unparseable (detail ``nesting too deep``), so no text can exhaust the stack.
MAX_NESTING = 64

Token = RoutineToken


class MySqlRoutine(BaseModel):
    """One MySQL routine, as the routine collection (#1652, #1920) gives it.

    - ``routine_id``: ``schema.name`` (a trigger's or event's own name, not its table).
    - ``kind``: ``procedure``, ``function``, ``trigger`` or ``event``.
    - ``create_sql``: the ``Create Procedure`` / ``Create Function`` /
      ``SQL Original Statement`` / ``Create Event`` column of ``SHOW CREATE``, exactly;
      text from a dump (``DELIMITER`` lines, executable comments) reads too.
    - ``definer``: ``user@host`` as ``information_schema`` reports it; the check reads the
      text's own ``DEFINER`` clause and falls back to this one when the text has none.
    - ``sql_mode``: the routine's stored ``sql_mode`` (comma-separated, as reported).
    - ``security_type``: ``DEFINER`` or ``INVOKER`` for a procedure or function; the text's
      ``SQL SECURITY`` clause wins when it has one.
    - ``character_set_client``, ``collation_connection``, ``database_collation``: the
      session settings the routine was created under, carried with it unchanged.

    A validation error never repeats the input (``hide_input_in_errors``): the text keeps
    its literals.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    routine_id: str = Field(min_length=1)
    kind: RoutineKind
    create_sql: str
    definer: str | None = None
    sql_mode: str = ""
    security_type: Literal["DEFINER", "INVOKER"] | None = None
    character_set_client: str | None = None
    collation_connection: str | None = None
    database_collation: str | None = None

    @property
    def schema_and_name(self) -> tuple[str | None, str]:
        """The ``routine_id`` split into its schema (or None) and name, lower case."""
        schema, _, name = self.routine_id.lower().rpartition(".")
        return (schema or None), name

    @property
    def modes(self) -> list[str]:
        """The stored ``sql_mode``'s modes, upper case."""
        return [m.strip().upper() for m in self.sql_mode.split(",") if m.strip()]


# --------------------------------------------------------------------------- errors


class _Unparseable(Exception):
    """Steps 1-4 failed: ``check`` names the catalog check, ``evidence`` is a fixed word
    from this module (never routine text)."""

    def __init__(self, evidence: str, check: str = "parse") -> None:
        super().__init__(evidence)
        self.evidence = evidence
        self.check = check


# --------------------------------------------------------------------------- step 1


def _skip_quoted(text: str, i: int, backslash_escapes: bool) -> int:
    """The index after the quoted run starting at ``text[i]``."""
    quote = text[i]
    n = len(text)
    i += 1
    while i < n:
        c = text[i]
        if c == "\\" and backslash_escapes and quote != "`":
            i += 2
            continue
        if c == quote:
            if i + 1 < n and text[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    raise _Unparseable("unterminated quote")


class _DelimiterLine(Exception):
    """A ``DELIMITER`` line before the routine's ``CREATE``, met while reading a text as
    one statement."""


def _is_word_char(c: str) -> bool:
    return c.isalnum() or c in "_$"


def split_client_statements(text: str, *, backslash_escapes: bool = True) -> list[str]:
    """``text`` split into statements as the ``mysql`` client splits it.

    A text with no ``DELIMITER`` line before its ``CREATE`` is one statement
    (``SHOW CREATE`` output keeps its body's ``;``); a trailing delimiter is dropped.
    Executable comments (``/*!NNNNN ... */``) are unwrapped; other comments are kept for
    the tokenizer. ``backslash_escapes`` is False under ``NO_BACKSLASH_ESCAPES``. Raises
    ``_Unparseable`` on an unterminated quote or comment. Linear in the text's length.
    """
    try:
        return _scan(text, delimiter=None, backslash_escapes=backslash_escapes)
    except _DelimiterLine:
        return _scan(text, delimiter=";", backslash_escapes=backslash_escapes)


def _scan(text: str, delimiter: str | None, backslash_escapes: bool) -> list[str]:
    """The client's statements; ``delimiter`` None reads the text as one statement and
    raises ``_DelimiterLine`` at a ``DELIMITER`` line outside quotes and comments that
    comes before the first ``CREATE``."""
    client = delimiter is not None
    out: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(text)
    in_exec_comment = False
    line_start = True  # only spaces and tabs since the last line break (or the start)
    recent = ""  # the last few plain characters, to see a CREATE go by
    seen_create = False

    def flush() -> None:
        stmt = "".join(buf).strip()
        if stmt:
            out.append(stmt)
        buf.clear()

    def boundary() -> None:
        """A word ends here: note a CREATE that just went by."""
        nonlocal recent, seen_create
        if recent[-6:].upper() == "CREATE" and (len(recent) < 7 or not _is_word_char(recent[-7])):
            seen_create = True
        recent = " "

    while i < n:
        c = text[i]
        if c in "'\"`":
            j = _skip_quoted(text, i, backslash_escapes)
            buf.append(text[i:j])
            i = j
            line_start = False
            boundary()
            continue
        if c == "/" and text.startswith("/*!", i):
            if in_exec_comment:
                raise _Unparseable("nested comment")
            i += 3
            while i < n and text[i].isdigit():
                i += 1
            in_exec_comment = True
            buf.append(" ")
            line_start = False
            boundary()
            continue
        if in_exec_comment and c == "*" and text.startswith("*/", i):
            in_exec_comment = False
            buf.append(" ")
            i += 2
            line_start = False
            boundary()
            continue
        if c == "/" and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise _Unparseable("unterminated comment")
            buf.append(text[i : j + 2])
            i = j + 2
            line_start = False
            boundary()
            continue
        if c == "#" or (
            c == "-"
            and text.startswith("--", i)
            and text[i + 2 : i + 3] in ("", " ", "\t", "\r", "\n")
        ):
            j = text.find("\n", i)
            j = n if j < 0 else j
            buf.append(text[i:j])
            i = j
            boundary()
            continue
        if (
            line_start
            and c in "dD"
            and not in_exec_comment
            and text[i : i + 9].upper() == "DELIMITER"
            and text[i + 9 : i + 10] in ("", " ", "\t", "\r", "\n")
            and (client or not seen_create)
        ):
            if not client:
                raise _DelimiterLine
            flush()
            j = text.find("\n", i)
            j = n if j < 0 else j
            new = text[i + 9 : j].strip().split()
            if not new:
                raise _Unparseable("delimiter")
            delimiter = new[0]
            i = j
            continue
        if delimiter is not None and c == delimiter[0] and text.startswith(delimiter, i):
            flush()
            i += len(delimiter)
            line_start = False
            boundary()
            continue
        buf.append(c)
        i += 1
        if c in "\r\n":
            line_start = True
        elif c not in " \t":
            line_start = False
        if not seen_create:
            if _is_word_char(c):
                recent = (recent + c)[-8:]
            else:
                boundary()
    if in_exec_comment:
        raise _Unparseable("unterminated comment")
    flush()
    if not client and out:
        last = out[-1].rstrip()
        out[-1] = last[:-1].rstrip() if last.endswith(";") else last
        out = [s for s in out if s]
    return out


def _mask_backslashes(text: str) -> str:
    """``text`` with each backslash inside a string written as ``/``, so a tokenizer that
    reads backslash escapes reads the strings of a ``NO_BACKSLASH_ESCAPES`` routine as
    MySQL does. Offsets do not move."""
    chars = list(text)
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "'\"`":
            j = _skip_quoted(text, i, backslash_escapes=False)
            if c != "`":
                for k in range(i + 1, j - 1):
                    if chars[k] == "\\":
                        chars[k] = "/"
            i = j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "#" or (
            text.startswith("--", i) and text[i + 2 : i + 3] in ("", " ", "\t", "\r", "\n")
        ):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        i += 1
    return "".join(chars)


# --------------------------------------------------------------------------- step 2


def _tokens(sql: str) -> list[Token]:
    try:
        return list(routine_tokens(sql, SOURCE_ENGINE))
    except RoutineTokenError as exc:
        raise _Unparseable("tokens") from exc


_QUOTED = {"STRING", "IDENTIFIER"}
_ROUTINE_WORDS = {"PROCEDURE", "FUNCTION", "TRIGGER", "EVENT"}
_BLOCK_WORDS = {"END", "IF", "LOOP", "WHILE", "REPEAT", "CASE", "UNTIL", "THEN", "DO"}


def _word(tok: Token) -> str:
    """A token's keyword form: upper-case, or ``''`` for a quoted string or identifier."""
    if tok.kind in _QUOTED:
        return ""
    return tok.text.upper()


def _the_create(statements: Sequence[str]) -> str:
    """The one routine ``CREATE`` among a text's statements."""
    creates: list[str] = []
    for stmt in statements:
        toks = _tokens(stmt)
        if not toks:
            continue
        first = _word(toks[0])
        if first == "CREATE":
            creates.append(stmt)
        elif first in {"DROP", "SET", "USE"}:
            continue
        else:
            raise _Unparseable("more than one statement")
    if len(creates) != 1:
        raise _Unparseable("more than one statement" if creates else "no create")
    return creates[0]


# --------------------------------------------------------------------------- step 3


@dataclass
class _Header:
    kind: str
    definer: tuple[str, str | None] | Literal["current_user"] | None
    security: str | None
    body_start: int
    trigger_table: tuple[str | None, str] | None = None
    trigger_order: tuple[str | None, str] | None = None
    # The DEFINER clause's tokens, ``DEFINER`` up to the account's last token (exclusive).
    definer_tokens: tuple[int, int] | None = None


class _Cursor:
    """A position in a token list."""

    def __init__(self, toks: list[Token], i: int = 0) -> None:
        self.toks = toks
        self.i = i

    def peek(self, k: int = 0) -> Token | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def kind(self, k: int = 0) -> str:
        tok = self.peek(k)
        return tok.kind if tok is not None else ""

    def word(self, k: int = 0) -> str:
        """The token's keyword form (a few are two words, such as ``SQL SECURITY``)."""
        tok = self.peek(k)
        return _word(tok) if tok is not None else ""

    def first_word(self) -> str:
        """The first word of the token's keyword form."""
        words = self.word().split()
        return words[0] if words else ""

    def at_end(self) -> bool:
        return self.i >= len(self.toks)

    def take(self) -> Token:
        tok = self.peek()
        if tok is None:
            raise _Unparseable("unexpected end")
        self.i += 1
        return tok

    def expect(self, *words: str) -> None:
        if not self.accept(*words):
            if self.i < len(self.toks) and not set(words) & _BLOCK_WORDS:
                raise _Unparseable("header" if set(words) & _ROUTINE_WORDS else "unexpected token")
            raise _Unparseable("unbalanced block")

    def accept(self, *words: str) -> bool:
        """Past ``words`` when the next tokens spell them (a token may hold two words)."""
        j = self.i
        idx = 0
        while idx < len(words):
            if j >= len(self.toks):
                return False
            got = _word(self.toks[j]).split()
            if not got or list(words[idx : idx + len(got)]) != got:
                return False
            idx += len(got)
            j += 1
        self.i = j
        return True

    def skip_parens(self) -> None:
        """Past a balanced ``( ... )`` starting here."""
        if self.kind() != "L_PAREN":
            raise _Unparseable("header")
        depth = 0
        while not self.at_end():
            kind = self.take().kind
            if kind == "L_PAREN":
                depth += 1
            elif kind == "R_PAREN":
                depth -= 1
                if depth == 0:
                    return
        raise _Unparseable("unbalanced parentheses")


def _name_part(tok: Token) -> str:
    if tok.kind in {"L_PAREN", "R_PAREN", "SEMICOLON", "COMMA", "DOT"}:
        raise _Unparseable("header")
    if not tok.text:
        raise _Unparseable("empty name")
    return tok.text


def _qualified_name(cur: _Cursor) -> tuple[str | None, str]:
    first = _name_part(cur.take())
    if cur.kind() == "DOT":
        cur.take()
        return first.lower(), _name_part(cur.take()).lower()
    return None, first.lower()


def _definer(cur: _Cursor) -> tuple[str, str | None] | Literal["current_user"]:
    if cur.kind() == "CURRENT_USER":
        cur.take()
        if cur.kind() == "L_PAREN":
            cur.skip_parens()
        return "current_user"
    user = _name_part(cur.take())
    host: str | None = None
    tok = cur.peek()
    if tok is not None and tok.kind == "PARAMETER" and tok.text == "@":
        cur.take()
        host = _name_part(cur.take())
    return user, host


_CHARACTERISTIC_STARTS = {"COMMENT", "LANGUAGE", "NOT", "DETERMINISTIC", "CONTAINS", "NO"}
_CHARACTERISTIC_STARTS |= {"READS", "MODIFIES", "SQL"}
_TYPE_WORDS = {"UNSIGNED", "SIGNED", "ZEROFILL", "BINARY", "ASCII", "UNICODE", "VARYING"}
_TYPE_WORDS |= {"PRECISION", "COLLATE", "CHARSET", "CHARACTER", "NATIONAL"}


def _characteristics(cur: _Cursor) -> str | None:
    """Past a procedure's or function's characteristics; its ``SQL SECURITY``, if any."""
    security: str | None = None
    while cur.first_word() in _CHARACTERISTIC_STARTS:
        if cur.accept("COMMENT"):
            cur.take()
        elif cur.accept("LANGUAGE", "SQL") or cur.accept("NOT", "DETERMINISTIC"):
            pass
        elif cur.accept("DETERMINISTIC") or cur.accept("CONTAINS", "SQL"):
            pass
        elif cur.accept("NO", "SQL") or cur.accept("READS", "SQL", "DATA"):
            pass
        elif cur.accept("MODIFIES", "SQL", "DATA"):
            pass
        elif cur.accept("SQL", "SECURITY"):
            security = cur.word()
            if security not in {"DEFINER", "INVOKER"}:
                raise _Unparseable("header")
            cur.take()
        else:
            break
    return security


def _returns(cur: _Cursor) -> None:
    """Past a function's ``RETURNS <type>``."""
    cur.expect("RETURNS")
    cur.take()  # the type name
    while not cur.at_end():
        if cur.kind() == "L_PAREN":
            cur.skip_parens()
            continue
        w = cur.word()
        if w == "CHARACTER" and cur.word(1) == "SET":
            cur.i += 3
        elif w in {"CHARSET", "COLLATE"} or cur.kind() == "CHARACTER_SET":
            cur.i += 2
        elif w in _TYPE_WORDS:
            cur.i += 1
        else:
            return


def _header(toks: list[Token]) -> _Header:
    cur = _Cursor(toks)
    cur.expect("CREATE")
    definer: tuple[str, str | None] | Literal["current_user"] | None = None
    definer_tokens: tuple[int, int] | None = None
    if cur.accept("DEFINER"):
        first = cur.i - 1
        if cur.take().kind != "EQ":
            raise _Unparseable("header")
        definer = _definer(cur)
        definer_tokens = (first, cur.i)
    kind = cur.word()
    if kind not in _ROUTINE_WORDS:
        raise _Unparseable("header")
    cur.take()
    cur.accept("IF", "NOT", "EXISTS")
    _qualified_name(cur)
    header = _Header(
        kind=kind.lower(),
        definer=definer,
        security=None,
        body_start=0,
        definer_tokens=definer_tokens,
    )
    if kind in {"PROCEDURE", "FUNCTION"}:
        cur.skip_parens()
        if kind == "FUNCTION":
            _returns(cur)
        header.security = _characteristics(cur)
    elif kind == "TRIGGER":
        if cur.word() not in {"BEFORE", "AFTER"}:
            raise _Unparseable("header")
        cur.take()
        if cur.word() not in {"INSERT", "UPDATE", "DELETE"}:
            raise _Unparseable("header")
        cur.take()
        cur.expect("ON")
        header.trigger_table = _qualified_name(cur)
        cur.expect("FOR", "EACH", "ROW")
        if cur.word() in {"FOLLOWS", "PRECEDES"}:
            cur.take()
            header.trigger_order = _qualified_name(cur)
    else:  # EVENT
        cur.expect("ON", "SCHEDULE")
        while not cur.at_end() and cur.word() != "DO":
            if cur.kind() == "L_PAREN":
                cur.skip_parens()
            else:
                cur.take()
        cur.expect("DO")
    if cur.at_end():
        raise _Unparseable("no body")
    header.body_start = cur.i
    return header


# --------------------------------------------------------------------------- step 4


@dataclass(frozen=True)
class _Leaf:
    """A simple statement, or an expression a compound statement evaluates."""

    start: int  # token index
    end: int  # token index, exclusive
    expression: bool = False  # a condition, RETURN or DEFAULT value, read as ``SELECT <expr>``


@dataclass(frozen=True)
class _Label:
    name: str
    loop: bool  # a LOOP, WHILE or REPEAT label, which ITERATE may name


@dataclass
class _Block:
    labels: tuple[_Label, ...]
    conditions: set[str] = field(default_factory=set)


_COMPOUND_ENDS = {"END", "ELSE", "ELSEIF", "UNTIL", "WHEN"}
# Statements that need more than their first word.
_NEEDS_ARGUMENTS = {"SELECT", "INSERT", "REPLACE", "UPDATE", "DELETE", "SET", "CALL"}
_NEEDS_ARGUMENTS |= {"PREPARE", "EXECUTE", "OPEN", "FETCH", "CLOSE", "WITH"}


class _Body:
    """The body-structure reader (step 4): walks compound statements, collects leaves."""

    def __init__(self, toks: list[Token], start: int) -> None:
        self.cur = _Cursor(toks, start)
        self.toks = toks
        self.leaves: list[_Leaf] = []
        self.depth = 0

    # -- top level

    def read(self) -> None:
        self._statement(_Block(labels=()), top=True)
        if not self.cur.at_end():
            if self._is("SEMICOLON"):
                self.cur.take()
            if not self.cur.at_end():
                raise _Unparseable("unbalanced block")

    # -- helpers

    def _is(self, kind: str, k: int = 0) -> bool:
        return self.cur.kind(k) == kind

    def _semicolon(self) -> None:
        if not self._is("SEMICOLON"):
            raise _Unparseable("unbalanced block")
        self.cur.take()

    def _label(self) -> str | None:
        """A ``label:`` starting a statement, consumed."""
        tok = self.cur.peek()
        if tok is not None and self._is("COLON", 1) and tok.kind != "STRING":
            self.cur.i += 2
            return tok.text.lower()
        return None

    def _end_label(self, label: str | None) -> None:
        tok = self.cur.peek()
        if tok is None or tok.kind in {"SEMICOLON", "R_PAREN"} or _word(tok) in _COMPOUND_ENDS:
            return
        if label is None or tok.text.lower() != label:
            raise _Unparseable("end label")
        self.cur.take()

    def _expression_until(self, *stops: str) -> None:
        """Past an expression ending at one of ``stops`` (outside parentheses and ``CASE``)."""
        start = self.cur.i
        depth = 0
        case = 0
        while not self.cur.at_end():
            w = self.cur.word()
            kind = self.cur.kind()
            if depth == 0 and case == 0 and w in stops:
                if self.cur.i == start:
                    raise _Unparseable("unexpected token")
                self.leaves.append(_Leaf(start, self.cur.i, expression=True))
                return
            if kind == "L_PAREN":
                depth += 1
            elif kind == "R_PAREN":
                depth -= 1
            elif w == "CASE":
                case += 1
            elif w == "END" and case:
                case -= 1
            elif kind == "SEMICOLON" and depth == 0:
                break
            self.cur.take()
        raise _Unparseable("unbalanced block")

    def _to_semicolon(self, top: bool) -> int:
        """Past tokens to the ``;`` that ends a statement (or the end, for a one-statement
        body); the index it started at."""
        start = self.cur.i
        depth = 0
        while not self.cur.at_end():
            kind = self.cur.kind()
            if kind == "SEMICOLON" and depth == 0:
                break
            if kind == "L_PAREN":
                depth += 1
            elif kind == "R_PAREN":
                depth -= 1
            self.cur.take()
        if depth != 0:
            raise _Unparseable("unbalanced parentheses")
        if self.cur.i == start:
            raise _Unparseable("unexpected token")
        if self.cur.at_end() and not top:
            raise _Unparseable("unbalanced block")
        return start

    def _simple(self, top: bool) -> None:
        """Past a simple statement, to its ``;``."""
        start = self._to_semicolon(top)
        first = _word(self.toks[start])
        if first in _NEEDS_ARGUMENTS and self.cur.i - start < 2:
            raise _Unparseable("incomplete statement")
        if first == "CALL":
            _call_target(self.toks[start : self.cur.i])
        self.leaves.append(_Leaf(start, self.cur.i))

    def _enter(self) -> None:
        self.depth += 1
        if self.depth > MAX_NESTING:
            raise _Unparseable("nesting too deep")

    # -- statements

    def _statements(self, block: _Block, *stops: str) -> None:
        """Statements, each with its ``;``, until one of ``stops`` (not consumed)."""
        while True:
            if self.cur.at_end():
                raise _Unparseable("unbalanced block")
            if self.cur.word() in stops:
                return
            if self.cur.word() == "DECLARE":
                raise _Unparseable("declare after statement", check="declare_order")
            self._statement(block, top=False)
            self._semicolon()

    def _statement(self, block: _Block, top: bool) -> None:
        self._enter()
        try:
            self._one_statement(block, top)
        finally:
            self.depth -= 1

    def _one_statement(self, block: _Block, top: bool) -> None:
        label = self._label()
        w = self.cur.word()
        loop = w in {"LOOP", "WHILE", "REPEAT"}
        labels = (*block.labels, _Label(label, loop)) if label else block.labels
        inner = _Block(labels, block.conditions)
        if w == "BEGIN":
            self.cur.take()
            self.cur.accept("NOT", "ATOMIC")
            scope = _Block(labels=labels, conditions=set(block.conditions))
            self._declarations(scope)
            self._statements(scope, "END")
            self.cur.expect("END")
            self._end_label(label)
            return
        if w == "LOOP":
            self.cur.take()
            self._statements(inner, "END")
            self.cur.expect("END", "LOOP")
            self._end_label(label)
            return
        if w == "WHILE":
            self.cur.take()
            self._expression_until("DO")
            self.cur.expect("DO")
            self._statements(inner, "END")
            self.cur.expect("END", "WHILE")
            self._end_label(label)
            return
        if w == "REPEAT":
            self.cur.take()
            self._statements(inner, "UNTIL")
            self.cur.expect("UNTIL")
            self._expression_until("END")
            self.cur.expect("END", "REPEAT")
            self._end_label(label)
            return
        if label is not None:
            raise _Unparseable("label")
        if w == "IF":
            self.cur.take()
            self._expression_until("THEN")
            self.cur.expect("THEN")
            self._statements(block, "ELSEIF", "ELSE", "END")
            while self.cur.accept("ELSEIF"):
                self._expression_until("THEN")
                self.cur.expect("THEN")
                self._statements(block, "ELSEIF", "ELSE", "END")
            if self.cur.accept("ELSE"):
                self._statements(block, "END")
            self.cur.expect("END", "IF")
            return
        if w == "CASE":
            self.cur.take()
            if self.cur.word() != "WHEN":
                self._expression_until("WHEN")
            if self.cur.word() != "WHEN":
                raise _Unparseable("unbalanced block")
            while self.cur.accept("WHEN"):
                self._expression_until("THEN")
                self.cur.expect("THEN")
                self._statements(block, "WHEN", "ELSE", "END")
            if self.cur.accept("ELSE"):
                self._statements(block, "END")
            self.cur.expect("END", "CASE")
            return
        if w in {"LEAVE", "ITERATE"}:
            self.cur.take()
            name = self.cur.take().text.lower()
            known = [lb for lb in block.labels if lb.name == name]
            if not known:
                raise _Unparseable("unknown label")
            if w == "ITERATE" and not known[-1].loop:
                raise _Unparseable("iterate on a block label")
            return
        if w == "RETURN":
            self.cur.take()
            start = self._to_semicolon(top)
            self.leaves.append(_Leaf(start, self.cur.i, expression=True))
            return
        if w in {"END", "ELSE", "ELSEIF", "UNTIL", "WHEN", "THEN", "DO"} or w == "":
            raise _Unparseable("unbalanced block")
        if w == "DECLARE":
            raise _Unparseable("declare outside a block", check="declare_order")
        self._simple(top)

    # -- declarations

    def _declarations(self, block: _Block) -> None:
        """A block's ``DECLARE`` statements, checked for MySQL's order and its handler rules."""
        stage = 0  # 0: variables and conditions, 1: cursors, 2: handlers
        handled: set[str] = set()
        while self.cur.word() == "DECLARE":
            self.cur.take()
            if self.cur.word() in {"CONTINUE", "EXIT", "UNDO"} and self.cur.word(1) == "HANDLER":
                action = self.cur.word()
                self.cur.i += 2
                stage = 2
                if action == "UNDO":
                    raise _Unparseable("undo handler", check="handler")
                self.cur.expect("FOR")
                self._handler_conditions(block, handled)
                # A handler's statement sees no label of the blocks around it.
                self._statement(_Block(labels=(), conditions=block.conditions), top=False)
                self._semicolon()
                continue
            self.cur.take()  # the name
            if self.cur.word() == "CURSOR":
                if stage > 1:
                    raise _Unparseable("cursor after handler", check="declare_order")
                stage = 1
                self.cur.take()
                self.cur.expect("FOR")
                self._simple(top=False)
                self._semicolon()
                continue
            if stage > 0:
                raise _Unparseable("variable after cursor or handler", check="declare_order")
            if self.cur.word() == "CONDITION":
                block.conditions.add(self.toks[self.cur.i - 1].text.lower())
                self.cur.take()
                self.cur.expect("FOR")
                if self.cur.accept("SQLSTATE"):
                    self.cur.accept("VALUE")
                    self._sqlstate(self.cur.take())
                else:
                    self.cur.take()
                self._semicolon()
                continue
            # A variable list: DECLARE a, b <type> [DEFAULT <expr>]; its default is read.
            start = self._to_semicolon(top=False)
            words = [_word(t) for t in self.toks[start : self.cur.i]]
            if "DEFAULT" in words:
                at = start + words.index("DEFAULT") + 1
                if at < self.cur.i:
                    self.leaves.append(_Leaf(at, self.cur.i, expression=True))
            self._semicolon()

    def _sqlstate(self, tok: Token) -> None:
        value = tok.text
        if tok.kind != "STRING" or len(value) != 5 or value.startswith("00"):
            raise _Unparseable("sqlstate", check="handler")

    def _handler_conditions(self, block: _Block, handled: set[str]) -> None:
        while True:
            if self.cur.accept("SQLSTATE"):
                self.cur.accept("VALUE")
                tok = self.cur.take()
                self._sqlstate(tok)
                key = f"sqlstate:{tok.text}"
            elif self.cur.accept("NOT", "FOUND"):
                key = "not found"
            elif self.cur.word() in {"SQLWARNING", "SQLEXCEPTION"}:
                key = self.cur.take().text.lower()
            else:
                tok = self.cur.take()
                if tok.kind == "NUMBER":
                    if tok.text == "0":
                        raise _Unparseable("error code", check="handler")
                    key = f"code:{tok.text}"
                elif tok.text.lower() in block.conditions:
                    key = f"condition:{tok.text.lower()}"
                else:
                    raise _Unparseable("undeclared condition", check="handler")
            if key in handled:
                raise _Unparseable("duplicate handler", check="handler")
            handled.add(key)
            if not self._is("COMMA"):
                return
            self.cur.take()


def _command_rest(part: Sequence[Token]) -> list[Token]:
    """The tokens after a command word (``CALL``, ``PREPARE``): the tokenizer keeps them as
    one string, read again here."""
    if len(part) > 1 and part[1].command_rest:
        return _tokens(part[1].text)
    return list(part[1:])


def _expanded(part: Sequence[Token]) -> list[Token]:
    """The statement's tokens with a command's rest (``REPLACE ...``, ``CALL ...`` after
    ``;`` or ``BEGIN``) read again as tokens, so the statement checks see its words; a rest
    that does not tokenize stays one opaque token."""
    if len(part) > 1 and part[1].command_rest:
        try:
            return [part[0], *_tokens(part[1].text)]
        except _Unparseable:
            return list(part)
    return list(part)


def _call_target(part: Sequence[Token]) -> tuple[str | None, str]:
    """The routine a ``CALL`` names; ``_Unparseable`` when it names none."""
    rest = _command_rest(part)
    if not rest:
        raise _Unparseable("incomplete statement")
    return _qualified_name(_Cursor(rest))


def _call_arguments(sql: str, part: Sequence[Token]) -> str | None:
    """The text of a ``CALL``'s argument list, without its parentheses; None for none.

    ``sql`` is the text the statement's token offsets point into; a command's rest is
    tokenized again, and its own text holds the offsets then."""
    if len(part) > 1 and part[1].command_rest:
        sql, rest = part[1].text, _tokens(part[1].text)
    else:
        rest = list(part[1:])
    cur = _Cursor(rest)
    _qualified_name(cur)
    if cur.kind() != "L_PAREN":
        return None
    open_at = cur.i
    cur.skip_parens()
    inner = rest[open_at + 1 : cur.i - 1]
    if not inner:
        return None
    return _text(sql, inner)


# --------------------------------------------------------------------------- step 5


_SELECT_INTO_STOPS = {"FROM", "WHERE", "GROUP", "HAVING", "WINDOW", "ORDER", "LIMIT"}
_SELECT_INTO_STOPS |= {"FOR", "LOCK", "UNION", "EXCEPT", "INTERSECT"}
_NO_TABLES = {"CALL", "OPEN", "FETCH", "CLOSE", "SIGNAL", "RESIGNAL", "LEAVE", "ITERATE"}
_NO_TABLES |= {"DECLARE", "GET", "PREPARE", "EXECUTE", "DEALLOCATE", "COMMIT", "ROLLBACK"}
_NO_TABLES |= {"SAVEPOINT", "RELEASE", "START", "STOP", "CHANGE", "RESET", "PURGE", "KILL"}
_NO_TABLES |= {"INSTALL", "UNINSTALL", "SHUTDOWN", "RESTART", "FLUSH", "DO"}


def _without_select_into(toks: list[Token]) -> list[Token]:
    """A ``SELECT``'s tokens without its ``INTO`` clause, which the parser cannot read."""
    if not toks or _word(toks[0]) not in {"SELECT", "WITH", "("}:
        return toks
    out: list[Token] = []
    i = 0
    depth = 0
    while i < len(toks):
        tok = toks[i]
        if tok.kind == "L_PAREN":
            depth += 1
        elif tok.kind == "R_PAREN":
            depth -= 1
        if depth == 0 and _word(tok) == "INTO":
            i += 1
            while i < len(toks) and _word(toks[i]) not in _SELECT_INTO_STOPS:
                i += 1
            continue
        out.append(tok)
        i += 1
    return out


def _text(sql: str, toks: Sequence[Token]) -> str:
    """The statement text the tokens cover, spaced token by token."""
    return " ".join(t.text if t.command_rest else sql[t.start : t.end + 1] for t in toks)


def _set_values(toks: Sequence[Token]) -> list[list[Token]]:
    """The value expressions of a ``SET a = x, b = y``."""
    values: list[list[Token]] = []
    current: list[Token] | None = None
    depth = 0
    for tok in toks[1:]:
        if tok.kind == "L_PAREN":
            depth += 1
        elif tok.kind == "R_PAREN":
            depth -= 1
        if depth == 0 and tok.kind == "COMMA":
            if current:
                values.append(current)
            current = None
            continue
        if current is None:
            if depth == 0 and tok.kind in {"EQ", "COLON_EQ"}:
                current = []
            continue
        current.append(tok)
    if current:
        values.append(current)
    return values


def _set_assignments(toks: Sequence[Token]) -> list[tuple[str | None, str, list[Token]]]:
    """``(scope, variable, value)`` of each assignment of a ``SET`` (scope upper case,
    ``@`` for a user variable, or None)."""
    out: list[tuple[str | None, str, list[Token]]] = []
    i = 1
    n = len(toks)
    while i < n:
        scope: str | None = None
        if toks[i].kind == "SESSION_PARAMETER":
            i += 1
            if i + 1 < n and toks[i + 1].kind == "DOT":
                scope = _word(toks[i])
                i += 2
        elif toks[i].kind == "PARAMETER" and toks[i].text == "@":
            scope = "@"
            i += 1
        elif _word(toks[i]) in {"GLOBAL", "PERSIST", "PERSIST_ONLY", "SESSION", "LOCAL"}:
            scope = _word(toks[i])
            i += 1
        if i >= n:
            break
        variable = toks[i].text.lower()
        i += 1
        value: list[Token] = []
        depth = 0
        while i < n:
            kind = toks[i].kind
            if kind == "L_PAREN":
                depth += 1
            elif kind == "R_PAREN":
                depth -= 1
            elif kind == "COMMA" and depth == 0:
                i += 1
                break
            if kind not in {"EQ", "COLON_EQ"} or value:
                value.append(toks[i])
            i += 1
        out.append((scope, variable, value))
    return out


@dataclass
class _Reading:
    tables: list[tuple[str | None, str]] = field(default_factory=list)
    # Each called routine: its schema, its name, and procedure (CALL) or function.
    routines: list[tuple[str | None, str, RoutineKind]] = field(default_factory=list)
    complete: bool = True


def _read_sql(sql: str, reading: _Reading) -> None:
    """Tables and called functions of one statement, read by the SQL facade."""
    refs = routine_statement_refs(sql, SOURCE_ENGINE)
    if not refs.read:
        reading.complete = False
        return
    reading.tables += refs.tables
    reading.routines += [
        (s, n, "function") for s, n in refs.calls if s is not None or not is_builtin_function(n)
    ]


def _read_leaf(sql: str, part: list[Token], expression: bool, reading: _Reading) -> None:
    if not part:
        return
    if expression:
        _read_sql("SELECT " + _text(sql, part), reading)
        return
    first = _word(part[0])
    if first == "CALL":
        reading.routines.append((*_call_target(part), "procedure"))
        arguments = _call_arguments(sql, part)
        if arguments is not None:
            _read_sql("SELECT " + arguments, reading)
        return
    if first in {"PREPARE", "EXECUTE"}:
        reading.complete = False
        return
    if first in _NO_TABLES:
        return
    if first == "SET":
        for value in _set_values(part):
            _read_sql("SELECT " + _text(sql, value), reading)
        return
    if first == "REPLACE":
        # The parser reads a statement-level REPLACE as a bare command; its tables are an
        # INSERT's.
        _read_sql("INSERT " + _text(sql, part[1:]), reading)
        return
    _read_sql(_text(sql, _without_select_into(part)), reading)


# --------------------------------------------------------------------------- dynamic SQL


@dataclass
class _Statement:
    """A statement the statement checks read: its tokens, and whether it is an expression."""

    tokens: list[Token]
    expression: bool = False


def _prepared(
    statements: Sequence[_Statement],
) -> tuple[list[_Statement], bool]:
    """The statements the body's ``PREPARE``\\ s run, where they are string literals, and
    whether a ``PREPARE`` runs a user variable (whose text at run time is unknown).

    ``PREPARE s FROM '<text>'`` runs the literal; ``PREPARE s FROM @v`` runs whatever
    ``@v`` holds, so each string literal the body assigns to ``@v`` (``SET @v = '...'``)
    is read too.
    """
    literals: dict[str, list[str]] = {}
    for stmt in statements:
        if stmt.expression or not stmt.tokens or _word(stmt.tokens[0]) != "SET":
            continue
        for scope, variable, value in _set_assignments(stmt.tokens):
            if scope == "@" and len(value) == 1 and value[0].kind == "STRING":
                literals.setdefault(variable, []).append(value[0].text)
    texts: list[str] = []
    variables: set[str] = set()  # each prepared variable's literals are read once
    from_variable = False
    for stmt in statements:
        if stmt.expression or not stmt.tokens or _word(stmt.tokens[0]) != "PREPARE":
            continue
        try:
            rest = _command_rest(stmt.tokens)
        except _Unparseable:
            from_variable = True
            continue
        words = [_word(t) for t in rest]
        if "FROM" not in words:
            from_variable = True
            continue
        source = rest[words.index("FROM") + 1 :]
        if len(source) == 1 and source[0].kind == "STRING":
            texts.append(source[0].text)
        elif len(source) == 2 and source[0].kind == "PARAMETER":
            from_variable = True
            variables.add(source[1].text.lower())
        else:
            from_variable = True
    for variable in sorted(variables):
        texts += literals.get(variable, [])
    out: list[_Statement] = []
    for text in texts:
        try:
            tokens = _tokens(text.strip().rstrip(";"))
        except _Unparseable:
            from_variable = True
            continue
        if tokens:
            out.append(_Statement(tokens))
    return out, from_variable


# --------------------------------------------------------------------------- checks

#: The checks that fail when the text does not read, each with a ``parse_failure``.
_STRUCTURE_CHECKS: tuple[RoutineCheck, ...] = ("parse", "declare_order", "handler")
_CHECK_ORDER: tuple[RoutineCheck, ...] = (
    *_STRUCTURE_CHECKS,
    "definer",
    "statement",
    "dynamic_sql",
    "sql_mode",
)
_STAGES: dict[str, Literal["statement", "kind", "body"]] = {
    "header": "statement",
    "no create": "statement",
    "more than one statement": "statement",
    "tokens": "statement",
    "unterminated quote": "statement",
    "unterminated comment": "statement",
    "nested comment": "statement",
    "delimiter": "statement",
    "definer": "statement",
    "empty name": "statement",
    "kind": "kind",
}


def _words(part: Sequence[Token]) -> list[str]:
    """The statement's keyword words, a quoted token as ``?``."""
    return [w for t in part for w in (_word(t) or "?").split()]


def _phrase_at(words: list[str], i: int, phrase: str) -> bool:
    want = phrase.split()
    return words[i : i + len(want)] == want


def _result(rule: RoutineCheckRule, findings: Iterable[str]) -> RoutineCheckResult:
    found = tuple(dict.fromkeys(findings))
    return RoutineCheckResult(
        rule_id=rule.id,
        check=rule.match.check,
        outcome="fail" if found else "pass",
        impact=rule.impact,
        reason=rule.reason,
        fix_hint=rule.fix_hint,
        findings=found,
    )


_QUOTED_NAME = r"'(?:[^'\\]|\\.|'')*'|`(?:[^`]|``)*`|\"(?:[^\"\\]|\\.|\"\")*\""
_ACCOUNT_RE = re.compile(
    rf"(?P<user>{_QUOTED_NAME}|[^@\s'\"`]+)\s*(?:@\s*(?P<host>{_QUOTED_NAME}|[^@\s'\"`]+))?"
)


def _unquote(part: str) -> str:
    quote = part[:1]
    if quote not in ("'", "`", '"') or len(part) < 2:
        return part
    inner = part[1:-1].replace(quote * 2, quote)
    return inner if quote == "`" else re.sub(r"\\(.)", r"\1", inner)


def _parse_user(text: str) -> tuple[str, str | None] | Literal["current_user"]:
    """An account name as MySQL writes one: ``user@host``, either part quoted with ``'``,
    ``"`` or backticks, or a ``CURRENT_USER`` form. An unquoted name splits at its last
    ``@``, as ``information_schema`` reports a ``DEFINER`` (``app@10.0.0.%``,
    ``my-app@%``). ``_Unparseable`` for anything else."""
    stripped = text.strip()
    if stripped.upper().replace(" ", "") in {"CURRENT_USER", "CURRENT_USER()"}:
        return "current_user"
    if not any(q in stripped for q in "'`\""):
        user, at, host = stripped.rpartition("@")
        if not at:
            user, host = stripped, ""
        if user and not any(c.isspace() for c in stripped) and (host or not at):
            return user, (host or None)
        raise _Unparseable("definer")
    match = _ACCOUNT_RE.fullmatch(stripped)
    if match is None:
        raise _Unparseable("definer")
    user = _unquote(match.group("user"))
    host = match.group("host")
    if not user:
        raise _Unparseable("definer")
    return user, (_unquote(host) if host is not None else None)


def _is_target_user(
    definer: tuple[str, str | None] | Literal["current_user", "unreadable"],
    target: tuple[str, str | None] | Literal["current_user"] | None,
) -> bool:
    if definer == "current_user":
        return True
    if definer == "unreadable" or target is None or target == "current_user":
        return False
    (t_user, t_host), (user, host) = target, definer
    return user == t_user and (t_host is None or (host or "%").lower() == t_host.lower())


def _definer_findings(
    rule: RoutineCheckRule,
    kind: RoutineKind,
    definer: tuple[str, str | None] | Literal["current_user", "unreadable"] | None,
    security: str | None,
    target: tuple[str, str | None] | Literal["current_user"] | None,
) -> list[str]:
    if definer == "unreadable":
        return ["unreadable definer"]  # never passes unchecked: it needs review
    if definer is None or _is_target_user(definer, target):
        return []
    if kind not in rule.match.always_definer_kinds and (security or "DEFINER") == "INVOKER":
        return []
    return ["named definer"]


def _mode_findings(rule: RoutineCheckRule, modes: list[str], named: set[str]) -> list[str]:
    listed = set(rule.match.sql_modes)
    known = named | set(rule.match.known_sql_modes)
    out: list[str] = []
    for mode in modes:
        if mode in listed:
            out.append(mode)
        elif rule.match.unknown_sql_modes and mode not in known:
            out.append("unknown mode")
    return out


def _statement_findings(rule: RoutineCheckRule, statements: Iterable[_Statement]) -> list[str]:
    match = rule.match
    out: list[str] = []
    for stmt in statements:
        words = _words(_expanded(stmt.tokens))
        for i, word in enumerate(words):
            if word in match.function_calls and words[i + 1 : i + 2] == ["("]:
                out.append(f"{word}()")
        if stmt.expression:
            continue
        out += [p for p in match.statement_starts if _phrase_at(words, 0, p)]
        for phrase in match.statement_contains:
            for i in range(len(words)):
                if _phrase_at(words, i, phrase) and not any(
                    _phrase_at(words, i, e) for e in match.statement_exceptions
                ):
                    out.append(phrase)
        if words[:1] == ["SET"]:
            for scope, variable, _ in _set_assignments(stmt.tokens):
                if scope is not None and scope in match.set_scopes:
                    out.append(f"SET {scope}")
                if scope != "@" and variable in match.set_variables:
                    out.append(f"SET {variable}")
    return sorted(out)


def _dependencies(
    reading: _Reading, header: _Header | None, own: tuple[str | None, str]
) -> tuple[RoutineDependency, ...]:
    deps: dict[tuple[str, str | None, str, str | None], RoutineDependency] = {}

    def add(
        kind: Literal["table", "routine"],
        schema: str | None,
        name: str,
        routine_kind: RoutineKind | None = None,
    ) -> None:
        if not name:
            reading.complete = False
            return
        if kind == "routine" and (schema or own[0]) == own[0] and name == own[1]:
            return  # a recursive call is not a dependency
        deps.setdefault(
            (kind, schema, name, routine_kind),
            RoutineDependency(kind=kind, name=name, schema_name=schema, routine_kind=routine_kind),
        )

    if header is not None and header.trigger_table is not None:
        add("table", *header.trigger_table)
    if header is not None and header.trigger_order is not None:
        add("routine", *header.trigger_order, routine_kind="trigger")
    for schema, name in reading.tables:
        add("table", schema, name)
    for schema, name, routine_kind in reading.routines:
        add("routine", schema, name, routine_kind)
    return tuple(
        sorted(
            deps.values(),
            key=lambda d: (d.kind, d.schema_name or "", d.name, d.routine_kind or ""),
        )
    )


def _target_user(
    target_user: str | None,
) -> tuple[str, str | None] | Literal["current_user"] | None:
    if target_user is None:
        return None
    try:
        return _parse_user(target_user)
    except _Unparseable:
        raise ValueError("target_user is not a MySQL account name (user or user@host)") from None


# --------------------------------------------------------------------------- entry point


@dataclass
class _Read:
    """What steps 1-5 read from one routine."""

    header: _Header | None = None
    failure: _Unparseable | None = None
    statements: list[_Statement] = field(default_factory=list)
    reading: _Reading = field(default_factory=_Reading)
    from_variable: bool = False


def _read(routine: MySqlRoutine) -> _Read:
    out = _Read()
    escapes = "NO_BACKSLASH_ESCAPES" not in routine.modes
    toks: list[Token] = []
    create = ""
    body: _Body | None = None
    try:
        statements = split_client_statements(routine.create_sql, backslash_escapes=escapes)
        if not escapes:
            statements = [_mask_backslashes(stmt) for stmt in statements]
        create = _the_create(statements)
        toks = _tokens(create)
        out.header = _header(toks)
        if out.header.kind != routine.kind:
            raise _Unparseable("kind")
        body = _Body(toks, out.header.body_start)
        body.read()
    except _Unparseable as exc:
        out.failure = exc
        body = None
    except Exception:  # noqa: BLE001 -- any other failure on odd text reads as unparseable
        out.failure = _Unparseable("unreadable")
        body = None
    if body is None:
        out.reading.complete = False
        return out
    out.statements = [
        _Statement(toks[leaf.start : leaf.end], leaf.expression) for leaf in body.leaves
    ]
    for stmt in out.statements:
        try:
            _read_leaf(create, stmt.tokens, stmt.expression, out.reading)
        except Exception:  # noqa: BLE001 -- a statement read for its names never fails a routine
            out.reading.complete = False
    prepared, out.from_variable = _prepared(out.statements)
    out.statements += prepared
    return out


def check_mysql_routine(
    routine: MySqlRoutine,
    *,
    target_engine: str,
    target_line: str | None = None,
    target_user: str | None = None,
    rules: Iterable[RoutineCheckRule] | None = None,
) -> RoutineVerdict:
    """The verdict for ``routine`` on ``target_engine`` (``target_line`` ``"8.0"`` or
    ``"8.4"``, or None for every line).

    Every rule for a MySQL source, ``target_engine`` and the routine's kind runs, in check
    order (parse, declare order, handlers, definer, statements, dynamic SQL, sql_mode);
    the dependency check is :func:`src.tools.routines.dependencies.check_dependencies`'s.
    ``target_user`` is the user the routines are created as on the target (``admin`` or
    ``admin@%``); a ``DEFINER`` naming anyone else, or any named ``DEFINER`` when it is
    None, fails the definer check. ``rules`` defaults to the catalog's. A target engine
    that is not a MySQL source's Aurora engine, a ``target_line`` it does not list, or a
    ``target_user`` that is not an account name is a ValueError; the routine's text never
    raises.
    """
    lines = aurora_version_lines().get(target_engine)
    if lines is None or SOURCE_ENGINE not in lines.source_engines:
        raise ValueError(f"{target_engine!r} is not the Aurora engine for {SOURCE_ENGINE}")
    if target_line is not None and target_line not in lines.lines:
        raise ValueError(f"{target_engine} line {target_line!r} is not listed ({lines.lines})")
    target = _target_user(target_user)
    given = tuple(routine_check_rules() if rules is None else rules)
    applicable = [
        r
        for r in given
        if SOURCE_ENGINE in r.source_engines
        and target_engine in r.targets
        and routine.kind in r.kinds
        # The dependency check needs the step's moved objects (dependencies.py).
        and r.match.check != "dependencies"
    ]
    # The loader keeps every other MySQL rule's check one this module implements.
    applicable.sort(key=lambda r: (_CHECK_ORDER.index(r.match.check), r.id))

    read = _read(routine)
    header, failure = read.header, read.failure
    # Every mode a rule of the whole catalog names is a known mode, whichever rules run.
    named = {
        m
        for r in (*routine_check_rules(), *given)
        if r.match.check == "sql_mode"
        for m in r.match.sql_modes
    }
    definer: tuple[str, str | None] | Literal["current_user", "unreadable"] | None = (
        header.definer if header is not None else None
    )
    if header is not None and definer is None and routine.definer:
        try:
            definer = _parse_user(routine.definer)
        except _Unparseable:
            definer = "unreadable"
    security = (header.security if header is not None else None) or routine.security_type

    results: list[RoutineCheckResult] = []
    for rule in applicable:
        check = rule.match.check
        if check in _STRUCTURE_CHECKS:
            failed = failure is not None and failure.check == check
            results.append(_result(rule, [failure.evidence] if failed and failure else []))
        elif check == "definer":
            found = _definer_findings(rule, routine.kind, definer, security, target)
            results.append(_result(rule, found))
        elif check == "statement":
            results.append(_result(rule, _statement_findings(rule, read.statements)))
        elif check == "dynamic_sql":
            results.append(_result(rule, ["dynamic SQL"] if read.from_variable else []))
        else:
            results.append(_result(rule, _mode_findings(rule, routine.modes, named)))

    parse_failure = None
    if failure is not None:
        parse_failure = ParseFailure(
            stage=_STAGES.get(failure.evidence, "body"), detail=failure.evidence
        )
    return RoutineVerdict(
        routine_id=routine.routine_id,
        kind=routine.kind,
        target_engine=target_engine,
        target_line=target_line,
        parse_failure=parse_failure,
        checks=tuple(results),
        dependencies=_dependencies(read.reading, header, routine.schema_and_name),
        dependencies_complete=read.reading.complete,
    )


# --------------------------------------------------------------------------- the header, for rewrites


@dataclass(frozen=True)
class MySqlHeader:
    """Where a routine's header lies in its ``create_sql``, for the rewrites (#1937).

    Offsets are character offsets into ``create_sql``, end exclusive, from the tokens of
    the one ``CREATE`` statement (never a pattern over the text):

    - ``definer``: the ``DEFINER = <account>`` clause, None when the text has none;
    - ``account``: the account it names (``(user, host)``, or ``"current_user"``);
    - ``security``: the ``SQL SECURITY`` clause's ``DEFINER`` or ``INVOKER``, None when the
      text has none;
    - ``body_start``: where the body starts (everything before it is the header);
    - ``tokens``: every token of the statement, at its offset in ``create_sql``.
    """

    definer: tuple[int, int] | None
    account: tuple[str, str | None] | Literal["current_user"] | None
    body_start: int
    tokens: tuple[Token, ...]
    security: str | None = None


def mysql_header(routine: MySqlRoutine) -> MySqlHeader | None:
    """The header of ``routine``'s ``CREATE`` (:class:`MySqlHeader`), or None when the text
    does not read as one routine of its kind or its ``CREATE`` is not written verbatim in
    it (an executable comment around it, which the reader unwraps). Never raises."""
    escapes = "NO_BACKSLASH_ESCAPES" not in routine.modes
    text = routine.create_sql
    try:
        create = _the_create(
            [
                stmt if escapes else _mask_backslashes(stmt)
                for stmt in split_client_statements(text, backslash_escapes=escapes)
            ]
        )
        raw = create if escapes else None
        if raw is None:
            # The masked statement keeps its offsets: find the unmasked one it came from.
            for stmt in split_client_statements(text, backslash_escapes=escapes):
                if _mask_backslashes(stmt) == create:
                    raw = stmt
                    break
        base = text.find(raw) if raw is not None else -1
        if base < 0:
            return None
        toks = _tokens(create)
        header = _header(toks)
        if header.kind != routine.kind or header.body_start >= len(toks):
            return None
        _Body(toks, header.body_start).read()
    except Exception:  # noqa: BLE001 -- text that does not read has no header to rewrite
        return None
    shifted = tuple(
        Token(t.kind, t.text, t.start + base, t.end + base, t.command_rest) for t in toks
    )
    span = None
    if header.definer_tokens is not None:
        first, after = header.definer_tokens
        span = (shifted[first].start, shifted[after - 1].end + 1)
    return MySqlHeader(
        definer=span,
        account=header.definer,
        body_start=shifted[header.body_start].start,
        tokens=shifted,
        security=header.security,
    )


def parse_account(text: str) -> tuple[str, str | None] | Literal["current_user"]:
    """An account name (``user@host``, either part quoted, or a ``CURRENT_USER`` form), as
    the definer check reads one. A ValueError, naming no part of the text, otherwise."""
    try:
        return _parse_user(text)
    except _Unparseable:
        raise ValueError("not a MySQL account name (user or user@host)") from None


def is_target_user(
    account: tuple[str, str | None] | Literal["current_user"],
    target: tuple[str, str | None] | Literal["current_user"],
) -> bool:
    """Whether a ``DEFINER`` naming ``account`` is the target user, as the definer check
    decides it (a ``CURRENT_USER`` form always is; a target with no host matches any)."""
    return _is_target_user(account, target)
