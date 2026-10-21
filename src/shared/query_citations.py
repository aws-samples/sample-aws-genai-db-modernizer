"""Model prose cites a query by its report label, never by a bare id (#1321).

A designer or a model summary can name a query by its hash or a prefix of it
("query 8c66eaee"). The report names a query by its short id in a code span
(`` `8c66eaee` ``, ``short_query_ids``: the first 8 characters, or the whole id
when another query of the job shares them), so a bare id in model prose reads
as a stray hash and fails the engineering report's concision check
(``tests/report_concision.bare_ids``).

:class:`QueryCitations` rewrites every bare citation of a known query into that
label, deterministically, where model prose is accepted: the schema-design
output gate (``write_schema_output``, every designer and every write path), and
the str fields of the synthesis and reality-check responses once they are
validated. Matching follows the concision check's words (``[0-9A-Za-z_-]+``):

- a hex query id (8 or more hex characters) is matched, without regard to case,
  by a word of 8 or more hex characters with at least one letter that is the id
  or a prefix of it;
- a word of digits only (a number in prose, or a numeric PostgreSQL
  ``queryid``) is matched only in an explicit citation, right after "query" or
  "queries" ("query 4503599627"); a number anywhere else is left alone;
- any other query id (``q1``, ``count``) never matches, so ordinary words are
  never rewritten.

A prefix shared by several queries names none of them: it is put in a code span
as written. Code is never rewritten: a code span of any number of backticks, a
word touching a backtick, a str with an unpaired backtick run (left whole), a
str with no whitespace (an id, a key name), and, in :meth:`QueryCitations.walk`,
every field that is not prose (:func:`is_prose_field`: an allowlist, so DDL, SQL,
filters, commands and any new field are left as written). The labels it writes
are code spans, so a second pass changes nothing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Iterable
from typing import Any, TypeVar

from pydantic import BaseModel

from src.shared.report_facts import SHORT_ID_LENGTH, short_query_ids

# A word as the concision check splits prose.
_WORD = re.compile(r"[0-9A-Za-z_-]+")
_HEX_ID = re.compile(rf"[0-9a-fA-F]{{{SHORT_ID_LENGTH},}}")
_HEX_WORD = re.compile(rf"(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{{{SHORT_ID_LENGTH},}}")
_NUMBER = re.compile(rf"-?\d{{{SHORT_ID_LENGTH},}}")
_NUMERIC_ID = re.compile(r"-?\d+")
# The words before a number that make it a query citation.
_CITATION = re.compile(r"\b(?:query|queries)\s+$", re.I)
_TICKS = re.compile(r"`+")

#: The prose fields: only a str under one of these keys (or a list of str under
#: one) is rewritten; every other field (ids, names, DDL, SQL, query filters,
#: pipelines, commands, scripts, templates, examples, and any field a contract
#: adds later) is written exactly as given. Prose is the small, stable set.
PROSE_FIELDS = frozenset(
    {
        "description", "impact", "rationale", "reason", "recommendation", "workaround",
        "mitigation", "justification", "explanation", "application_logic_required",
        "purpose", "use_case", "note", "notes", "summary", "details", "executive_summary",
    }
)  # fmt: skip
_PROSE_SUFFIXES = ("_reason", "_rationale", "_notes", "_description", "_summary", "_explanation")


def is_prose_field(key: str) -> bool:
    """Whether a dict key names a field that holds prose (``PROSE_FIELDS``, or a
    key ending in one of ``_PROSE_SUFFIXES``)."""
    low = str(key).lower()
    return low in PROSE_FIELDS or low.endswith(_PROSE_SUFFIXES)


def clip(text: str, limit: int) -> str:
    """``text`` cut to at most ``limit`` characters, never inside a code span: when
    the cut would leave a backtick run of a balanced ``text`` unpaired, it moves
    back to before that run (a cut label is dropped, never left half quoted)."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if _segments(text) is not None:
        while _segments(cut) is None:
            cut = cut[: cut.rfind("`")]
        cut = cut.rstrip()
    return cut


M = TypeVar("M", bound=BaseModel)


def _segments(value: str) -> list[tuple[bool, str]] | None:
    """``value`` as ``(is_code, text)`` parts, a run of n backticks opening a code
    span the next run of exactly n closes (as in Markdown); None when a run is
    left unpaired, so a str whose code cannot be told apart is not touched."""
    parts: list[tuple[bool, str]] = []
    pos = 0
    runs = list(_TICKS.finditer(value))
    i = 0
    while i < len(runs):
        opening = runs[i]
        close = next(
            (j for j in range(i + 1, len(runs)) if len(runs[j].group(0)) == len(opening.group(0))),
            None,
        )
        if close is None:
            return None
        parts.append((False, value[pos : opening.start()]))
        parts.append((True, value[opening.start() : runs[close].end()]))
        pos = runs[close].end()
        i = close + 1
    parts.append((False, value[pos:]))
    return parts


class QueryCitations:
    """The job's query ids and the label each one is cited by."""

    def __init__(self, query_ids: Iterable[Any]) -> None:
        ids = {
            str(q)
            for q in query_ids
            if isinstance(q, str) and (_HEX_ID.fullmatch(q) or _NUMERIC_ID.fullmatch(q))
        }
        self._label = short_query_ids(ids, ids)
        self._ids = sorted(ids, key=str.lower)
        self._lower = {q: q.lower() for q in ids}

    def __bool__(self) -> bool:
        return bool(self._label)

    def _cite(self, word: str, before: str) -> str | None:
        """The label ``word`` is cited by, or None when it names no known query."""
        if _NUMBER.fullmatch(word):
            if not _CITATION.search(before):
                return None
        elif not _HEX_WORD.fullmatch(word):
            return None
        low = word.lower()
        exact = [q for q in self._ids if self._lower[q] == low]
        hits = exact or [q for q in self._ids if self._lower[q].startswith(low)]
        if not hits:
            return None
        return f"`{self._label[hits[0]]}`" if len(hits) == 1 else f"`{word}`"

    def _prose(self, text: str, after_code: bool, before_code: bool) -> str:
        """``text`` (prose with no backtick, between code spans when ``after_code`` /
        ``before_code``) with each citation labelled; a word touching a span is not."""
        out: list[str] = []
        pos = 0
        for m in _WORD.finditer(text):
            out.append(text[pos : m.start()])
            pos = m.end()
            touches = (m.start() == 0 and after_code) or (m.end() == len(text) and before_code)
            label = None if touches else self._cite(m.group(0), text[: m.start()])
            out.append(label or m.group(0))
        out.append(text[pos:])
        return "".join(out)

    def text(self, value: str) -> str:
        """``value`` with every bare citation of a known query replaced by its label."""
        if not self._label or not any(ch.isspace() for ch in value):
            return value
        parts = _segments(value)
        if parts is None:
            return value
        return "".join(
            text if code else self._prose(text, i > 0, i < len(parts) - 1)
            for i, (code, text) in enumerate(parts)
        )

    def walk(
        self, value: Any, keep: Callable[[str], bool] | Collection[str] = (), prose: bool = False
    ) -> Any:
        """A copy of ``value`` (dicts, lists, str) with :meth:`text` applied to each str
        under a prose field (:func:`is_prose_field`, ``prose``: the enclosing key is
        one). A dict key ``keep`` names (free-form data, or a field this code writes)
        is copied as it is, with everything under it."""
        skip = keep if callable(keep) else (lambda key, names=frozenset(keep): key in names)
        if isinstance(value, str):
            return self.text(value) if prose else value
        if isinstance(value, dict):
            return {
                k: v if skip(k) else self.walk(v, skip, is_prose_field(k)) for k, v in value.items()
            }
        if isinstance(value, list):
            return [self.walk(v, skip, prose) for v in value]
        return value

    def fields(self, response: M) -> M:
        """``response`` (a validated model answer) with :meth:`text` applied to each
        of its top-level prose str fields; nothing is validated again, so the step
        never decides whether an answer passes its contract."""
        update = {
            name: self.text(value)
            for name in type(response).model_fields
            if isinstance(value := getattr(response, name), str) and is_prose_field(name)
        }
        return response.model_copy(update=update) if update else response


def collector_query_ids(collector: Any) -> set[str]:
    """Every query id the collection holds (``queries.query_patterns[].query_id``)."""
    queries = (collector or {}).get("queries") if isinstance(collector, dict) else None
    patterns = queries.get("query_patterns") if isinstance(queries, dict) else None
    return {
        str(q["query_id"])
        for q in patterns or []
        if isinstance(q, dict) and isinstance(q.get("query_id"), str) and q["query_id"]
    }


def named_query_ids(value: Any) -> set[str]:
    """Every query id ``value`` names in an id field (``query_id``, ``*query_ids``) at any depth."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("query_id", "representative_id") and isinstance(item, str):
                found.add(item)
            elif key.endswith("query_ids") and isinstance(item, list):
                found |= {v for v in item if isinstance(v, str)}
            else:
                found |= named_query_ids(item)
    elif isinstance(value, list):
        for item in value:
            found |= named_query_ids(item)
    return found
