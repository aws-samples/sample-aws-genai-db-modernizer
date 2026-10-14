"""Neutralise references to ``WITH``-defined CTE names before table extraction.

A CTE name is a query-local result set, not a source table, so the regex table
extractors must not report it (#682). ``drop_cte_references`` blanks out the
``FROM name`` / ``JOIN name`` spans that point at a CTE, leaving every other
reference (including a real table that happens to share the CTE's name) alone:

- a CTE is visible only in the later CTE bodies and the statement that follows
  its WITH list, up to the end of the enclosing subquery (or ``;``);
- inside its own body a CTE name is a reference to itself only for a recursive
  CTE (``RECURSIVE`` or a ``UNION`` in the body); otherwise it is the real table;
- string literals and comments are ignored, and qualified names are never CTEs.
"""

from __future__ import annotations

import re

_WITH_RE = re.compile(r"\bWITH\b(\s+RECURSIVE\b)?", re.I)
_NAME_RE = re.compile(r'\s*(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|(\w+))')
_AS_RE = re.compile(r"\s*AS\b(?:\s+NOT\b)?(?:\s+MATERIALIZED\b)?\s*\(", re.I)
_XMLNS_RE = re.compile(r"\s*XMLNAMESPACES\s*\(", re.I)
_OPEN_RE = re.compile(r"\s*\(")
_COMMA_RE = re.compile(r"\s*,")
_UNION_RE = re.compile(r"\bUNION\b", re.I)
_REF_RE = re.compile(r'(?<![\w@])(?:FROM|JOIN)\s+(?:"([^"]+)"|`([^`]+)`|\[([^\]]+)\]|(\w+))')
_MASK_RE = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S)


def _mask(sql: str) -> str:
    """Blank string literals and comments, keeping every offset unchanged."""
    return _MASK_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group()), sql)


def _skip_parens(sql: str, pos: int) -> int:
    """Index just after the ``)`` matching the ``(`` at ``pos`` (or len)."""
    depth = 0
    for i in range(pos, len(sql)):
        if sql[i] == "(":
            depth += 1
        elif sql[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(sql)


def _scope_end(sql: str, pos: int) -> int:
    """End of the statement or subquery that a WITH list ending at ``pos`` governs."""
    depth = 0
    for i in range(pos, len(sql)):
        c = sql[i]
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                return i
            depth -= 1
        elif c == ";" and depth == 0:
            return i
    return len(sql)


def _visible_ctes(sql: str) -> list[tuple[str, int, int]]:
    """``(lower-case name, visible_from, visible_to)`` for every CTE in ``sql``."""
    found: list[tuple[str, int, int]] = []
    for w in _WITH_RE.finditer(sql):
        recursive_kw = bool(w.group(1))
        pos = w.end()
        pending: list[tuple[str, int, int]] = []  # name, visible_from
        while True:
            xml = _XMLNS_RE.match(sql, pos)
            if xml:  # SQL Server: WITH XMLNAMESPACES (...), cte AS (...)
                pos = _skip_parens(sql, xml.end() - 1)
                comma = _COMMA_RE.match(sql, pos)
                if not comma:
                    break
                pos = comma.end()
                continue
            m = _NAME_RE.match(sql, pos)
            if not m:
                break
            name = next(g for g in m.groups() if g)
            pos = m.end()
            cols = _OPEN_RE.match(sql, pos)
            if cols:
                pos = _skip_parens(sql, cols.end() - 1)
            a = _AS_RE.match(sql, pos)
            if not a:
                break  # a table hint such as ``WITH (NOLOCK)``, not a CTE
            body_start = a.end()
            pos = _skip_parens(sql, body_start - 1)
            self_visible = recursive_kw or bool(_UNION_RE.search(sql, body_start, pos))
            pending.append((name.lower(), body_start if self_visible else pos, pos))
            comma = _COMMA_RE.match(sql, pos)
            if not comma:
                break
            pos = comma.end()
        if pending:
            end = _scope_end(sql, pos)
            found.extend((n, start, end) for n, start, _ in pending)
    return found


def drop_cte_references(sql: str) -> str:
    """Return ``sql`` with ``FROM``/``JOIN`` references to CTE names blanked out."""
    masked = _mask(sql)
    ctes = _visible_ctes(masked)
    if not ctes:
        return sql
    out = list(sql)
    for m in _REF_RE.finditer(masked):
        if re.compile(r"\s*\.").match(masked, m.end()):
            continue  # qualified: ``cte.x`` style or schema.table, never a CTE
        name = next(g for g in m.groups() if g).lower()
        if any(n == name and lo <= m.start() < hi for n, lo, hi in ctes):
            out[m.start() : m.end()] = " " * (m.end() - m.start())
    return "".join(out)
