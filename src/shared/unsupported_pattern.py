"""
Shared field-reading for schema-design ``unsupported_patterns`` entries.

The four schema-design contracts (``*_model_output.py`` in this package)
disagree on field names for an unsupported-pattern entry:

- dynamodb / opensearch key the source query ids as ``query_ids``, the
  pattern label as ``pattern_type`` (dynamodb only), and the suggested fix
  as ``recommendation``.
- documentdb / elasticache key ids as ``source_query_ids`` and the fix as
  ``workaround``, alongside a ``reason`` field opensearch also carries.
- opensearch additionally carries ``source_query`` (the original SQL) and
  has both ``reason`` and ``recommendation``.

Two independent consumers need to read "whichever fields are present" the
same way, or they drift:

- ``src.agents.referee.synthesis_report.build_risk_assessment`` turns every
  unsupported pattern into a risk. Before this module existed it read only
  ``pattern_type``/``recommendation``, so DocumentDB and ElastiCache entries
  (neither of which has those fields) became risks with the literal text
  ``"[elasticache] unknown: "`` and no mitigation (#210).
- ``src.report.renderers._unsupported_pattern_md`` renders the same entries
  as Markdown for the engineering report (#204).

Putting this in ``src.report`` would make the referee agent depend on the
report-rendering package; putting it in ``src.agents`` would make the report
package depend on the agents package -- for what is really just a "read this
dict" helper, tied to the schema-design contracts' shapes, not to either
consumer. ``src.contracts`` was the natural dependency-leaf candidate, but
``tests/contract/test_contract_properties.py`` enforces that every module
directly under ``src/contracts/`` defines at least one Pydantic
``BaseModel`` -- a real invariant (every file there is a wire contract) that
this module, being plain functions over a dict, would violate. ``src.shared``
is a new, tiny, dependency-free package for exactly this: helpers more than
one top-level package needs that aren't themselves a pydantic contract.
Neither ``src.agents`` nor ``src.report`` currently depends on the other, and
this module imports nothing from either, so adding it introduces no new
coupling between them.

Version History:
- 1.0 (2026-10-03): Initial version, extracted from the duplicated,
  incomplete field-reading in synthesis_report.py and renderers.py (#210).
- 1.1 (2026-10-06): ``is_dedup_only_group_by`` (#336): a dedup-only ``GROUP
  BY`` has no aggregate function, no ``HAVING``, no window function and no
  ``ROLLUP``/``CUBE``/``GROUPING SETS``. Rewritten to prove dedup rather than
  denylist known aggregate names, after a review found the original
  ``COUNT``/``SUM``/``AVG``/``MIN``/``MAX``-only denylist let through
  ``GROUP_CONCAT``, ``STRING_AGG``, window functions and more.
"""

from __future__ import annotations

import re
from typing import Any


def unsupported_pattern_ids(u: dict[str, Any]) -> list[str]:
    """Query ids for one entry, whichever key they're under.

    A bare string (one id, not a list) is a malformed/legacy shape;
    ``list("abc123")`` would silently explode it into one character per
    "id", so that shape is detected and wrapped instead. Falsy entries
    (``None``, ``""``) are dropped.
    """
    ids = u.get("query_ids") or u.get("source_query_ids") or []
    if isinstance(ids, str):
        ids = [ids]
    elif not isinstance(ids, list):
        ids = []
    return [str(i) for i in ids if i]


def unsupported_pattern_label(u: dict[str, Any]) -> str:
    """The pattern's type/category, or a generic fallback.

    Only the dynamodb contract carries ``pattern_type``; documentdb,
    elasticache and opensearch have no equivalent field, so a generic label
    is used instead of leaving the entry unlabeled. A leading/trailing
    ``*`` (seen from Markdown-flavoured LLM output, e.g. ``*aggregation*``)
    is stripped so the label does not read with stray emphasis markers.
    """
    label = str(u.get("pattern_type") or "").strip().strip("*")
    return label.replace("_", " ") if label else "unsupported pattern"


def unsupported_pattern_text(u: dict[str, Any]) -> str:
    """The explanatory text, from whichever of ``reason``, ``recommendation``
    and ``workaround`` are present, each kept once (some contracts repeat the
    same sentence across two fields) and joined in that fixed order.

    ``source_query`` (opensearch only -- the original SQL text) is
    intentionally never included here: it is the query being described, not
    an explanation of why it is unsupported or what to do about it.
    """
    seen: set[str] = set()
    bits: list[str] = []
    for key in ("reason", "recommendation", "workaround"):
        val = str(u.get(key) or "").strip()
        if val and val not in seen:
            seen.add(val)
            bits.append(val)
    return " ".join(bits)


def unsupported_pattern_mitigation(u: dict[str, Any]) -> str | None:
    """The suggested fix alone (no ``reason``), e.g. for a risk's ``mitigation``
    field. ``recommendation`` (dynamodb/opensearch) is preferred over
    ``workaround`` (documentdb/elasticache); ``None`` when neither is present.
    """
    val = str(u.get("recommendation") or u.get("workaround") or "").strip()
    return val or None


# Comments and string literals are stripped before any other check, so a
# GROUP BY, HAVING, function call, OVER, or ROLLUP/CUBE/GROUPING SETS that
# only appears inside one of them (dead SQL, or incidental text in a quoted
# value) never changes the verdict either way. A single alternation, applied
# left to right, so an opening ``'``/``"``/``--``/``/*`` is never
# misinterpreted while a different kind of literal is still open.
_STRIP_RE = re.compile(
    r"/\*.*?\*/"  # block comment
    r"|--[^\n]*"  # line comment
    r"|'(?:[^'\\]|\\.|'')*'"  # single-quoted string (backslash or doubled '' escapes)
    r'|"(?:[^"\\]|\\.|"")*"',  # double-quoted string/identifier (same escapes)
    re.DOTALL,
)

_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)
_HAVING_RE = re.compile(r"\bHAVING\b", re.IGNORECASE)
_OVER_RE = re.compile(r"\bOVER\s*\(", re.IGNORECASE)
_SUPER_AGGREGATE_RE = re.compile(
    r"\bROLLUP\b|\bCUBE\s*\(|\bGROUPING\s+SETS\b|\bGROUPING\s*\(", re.IGNORECASE
)
_FUNCTION_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# A function name quoted with backticks or double quotes (e.g.
# ``GROUP_CONCAT`` ( x ) or "string_agg"(x)) must be matched against the
# ORIGINAL text, before _STRIP_RE runs: a double-quoted name is stripped
# outright (treated as a string literal), and a backtick-quoted one survives
# but no longer sits directly next to "(" (the closing backtick is in the
# way), so _FUNCTION_CALL_RE's word-boundary match never fires for either.
_QUOTED_FUNCTION_CALL_RE = re.compile(r'[`"]([A-Za-z_][A-Za-z0-9_]*)[`"]\s*\(')

# Scalar, per-row operations that never aggregate or window -- safe anywhere
# in a query alongside a dedup-only GROUP BY. Deliberately short and limited
# to operations with no cross-row effect, plus the handful of SQL keywords
# that are followed by ``(`` but are not function calls (``IN``, ``EXISTS``,
# ``NOT``). Anything NOT on this list is treated as potentially aggregating
# or windowing: this function proves dedup, it does not guess at it, so an
# unrecognised function call anywhere in the query is a reason to say no, not
# a reason to assume it is harmless. That is deliberately broader than "the
# SELECT list": a correlated subquery or CTE can carry a real aggregate the
# outer query's own column list never names, and that must disqualify the
# query exactly the same way.
_SCALAR_OPERATION_ALLOWLIST = frozenset(
    {
        # scalar string/number/date functions
        "upper",
        "lower",
        "trim",
        "ltrim",
        "rtrim",
        "concat",
        "coalesce",
        "nullif",
        "ifnull",
        "isnull",
        "cast",
        "convert",
        "substring",
        "substr",
        "replace",
        "length",
        "char_length",
        "round",
        "ceil",
        "ceiling",
        "floor",
        "abs",
        "mod",
        "now",
        "curdate",
        "current_date",
        "current_timestamp",
        "date",
        "year",
        "month",
        "day",
        "hour",
        "minute",
        "second",
        "date_format",
        "to_char",
        "to_date",
        "extract",
        # keywords that are followed by "(" but are not function calls -- a
        # parenthesised boolean group ("AND (...)"), list ("IN (...)"),
        # subquery ("FROM (SELECT ...)") or similar, not an aggregate.
        "and",
        "or",
        "not",
        "in",
        "exists",
        "values",
        "from",
        "where",
        "on",
        "select",
        "union",
        "intersect",
        "except",
        "all",
        "any",
        "some",
        "case",
        "when",
        "between",
    }
)


def is_dedup_only_group_by(query_sql: str | None) -> bool:
    """True when ``query_sql``'s ``GROUP BY`` only de-duplicates rows (#336).

    A common idiom (seen in the WordPress sample) joins tables and then adds
    ``GROUP BY <primary key>`` purely to collapse the duplicate rows the join
    produces -- not to compute anything. That is trivially servable as a plain
    key-value ``Query`` (optionally with client-side de-duplication), unlike a
    real aggregation, which no target engine here computes server-side.

    This proves the absence of aggregation rather than pattern-matching a
    denylist of known aggregate names (an earlier version of this function
    only rejected ``COUNT``/``SUM``/``AVG``/``MIN``/``MAX``, so
    ``GROUP_CONCAT``, ``STRING_AGG``, ``ARRAY_AGG``, every ``*_AGG``/``BOOL_*``/
    ``BIT_*`` variant, ``STDDEV``/``VARIANCE``, ``PERCENTILE_CONT``,
    ``ANY_VALUE`` and more all slipped through as "dedup-only"). After
    stripping comments and string literals, the ``GROUP BY`` is dedup-only
    only when ALL of these hold:

    - a ``GROUP BY`` is present;
    - no ``HAVING`` clause (filters on an aggregate, so there is one);
    - no ``OVER (`` (a window function computes a per-row value, not a
      duplicate-collapsing one);
    - no ``ROLLUP``, ``CUBE(``, ``GROUPING SETS`` or ``GROUPING(`` (these add
      subtotal rows -- not de-duplication, extra rows);
    - every function call anywhere in the query is on a short scalar
      allowlist (a handful of per-row string/number/date functions, plus the
      "(" keywords ``IN``/``EXISTS``/``NOT``). Anything else -- a known
      aggregate, an unrecognised one, or a window function the ``OVER``
      check alone might miss -- disqualifies the query. A quoted function
      name (`` `GROUP_CONCAT` (x) `` or ``"string_agg"(x)``) is checked
      against the original text first, before stripping: ``_STRIP_RE``
      would otherwise erase a double-quoted name outright (it reads as a
      string literal) or leave a backtick-quoted one with its closing
      backtick between the name and ``(``, so neither ever matches
      ``_FUNCTION_CALL_RE``'s plain, unquoted pattern.

    This does not attempt to track which columns are grouped vs. projected; a
    false negative (real aggregation misclassified as dedup-only) is far
    worse than a false positive here (a dedup-only pattern still flagged as
    unsupported), so the check only fires when it is confident -- it proves
    dedup, it does not assume it.

    ``has_aggregations`` on the collector's ``QueryPattern`` cannot be used
    for this: it is set by a regex that treats ``GROUP BY`` itself as an
    aggregation signal (``src/tools/database/offline_parser.py``), so it is
    true for every dedup-only ``GROUP BY`` too.
    """
    original = query_sql or ""
    for match in _QUOTED_FUNCTION_CALL_RE.finditer(original):
        if match.group(1).lower() not in _SCALAR_OPERATION_ALLOWLIST:
            return False

    sql = _STRIP_RE.sub(" ", original)
    if not _GROUP_BY_RE.search(sql):
        return False
    if _HAVING_RE.search(sql):
        return False
    if _OVER_RE.search(sql):
        return False
    if _SUPER_AGGREGATE_RE.search(sql):
        return False
    for match in _FUNCTION_CALL_RE.finditer(sql):
        if match.group(1).lower() not in _SCALAR_OPERATION_ALLOWLIST:
            return False
    return True
