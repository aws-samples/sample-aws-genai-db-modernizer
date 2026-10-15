"""Cross-check of the sqlglot and regex table references (#557, #553).

The sqlglot facade's answer (:mod:`src.tools.sql.facade`) drives the
pipeline's ``tables_accessed`` since #553; the regex scan
(:mod:`src.tools.sql.regex_tables`) is the facade's fallback. Shadow mode
started the other way round (#557) and is kept as a cross-check: for each
query the offline parser also runs the regex scan, resolves its references to
table ids the same way, and hands both answers to :class:`TableShadow`. The
recorder only observes: it never changes a query, and the parser output is
identical with or without it. Its :meth:`TableShadow.to_dict` result is
written per job as ``collector/sql_shadow.json`` -- a summary (agreement
rate, fallback reasons, disagreement categories) plus one row per
disagreement -- so a sqlglot bump or a facade change that starts reading a
query differently from the regex shows up per job, not only in the samples'
snapshot.

Disagreement categories are a best-effort reading of the query text, so a
reviewer can see at a glance which side is wrong (on the samples, every
disagreement is a regex misread):

- ``is_distinct_from``: the regex read ``IS [NOT] DISTINCT FROM x`` as a table;
- ``from_in_function``: the regex read ``EXTRACT(... FROM x)``-style syntax;
- ``comment_text``: the regex read a word in a SQL comment (``-- join chat``);
- ``pseudo_table``: the regex reported ``DUAL``;
- ``delete_using``: the regex missed ``DELETE ... USING t`` (PostgreSQL);
- ``comma_join``: the regex missed a table after a comma (``FROM a, b``);
- ``regex_only_other`` / ``facade_only_other``: anything else.

A query where the facade raised (``fallback_reason="facade_error"``) is
recorded too, as a fallback that agrees by construction, so every collected
query has a row in the counts.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version

from src.tools.sql.facade import TableReferences

logger = logging.getLogger(__name__)

SHADOW_ARTIFACT = "collector/sql_shadow.json"
SCHEMA_VERSION = 2  # 2: the facade drives the output; ``current`` became ``regex``
_QUERY_TEXT_LIMIT = 500

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_QUAL = r'(?:[`"\[]?\w+[`"\]]?\s*\.\s*){0,2}'


def _name_re(table_id: str) -> str:
    name = table_id.rsplit(".", 1)[-1]
    return _QUAL + r'[`"\[]?' + re.escape(name) + r'[`"\]]?(?!\w)'


def _keyword_ref(table_id: str) -> re.Pattern[str]:
    return re.compile(r"(?<![\w@$#])(?:FROM|JOIN|INTO|UPDATE)\s+" + _name_re(table_id), re.I)


def _categories(query_text: str, regex_only: list[str], facade_only: list[str]) -> list[str]:
    stripped = _COMMENT_RE.sub(" ", query_text)
    found: list[str] = []
    for table_id in regex_only:
        name = _name_re(table_id)
        if not table_id.count(".") and table_id.lower() == "dual":
            found.append("pseudo_table")
        elif re.search(r"\bIS\s+(?:NOT\s+)?DISTINCT\s+FROM\s+" + name, stripped, re.I):
            found.append("is_distinct_from")
        elif re.search(
            r"\b(?:EXTRACT|SUBSTRING|TRIM|OVERLAY|POSITION)\s*\([^()]*\bFROM\s+" + name,
            stripped,
            re.I,
        ):
            found.append("from_in_function")
        elif _keyword_ref(table_id).search(query_text) and not _keyword_ref(table_id).search(
            stripped
        ):
            found.append("comment_text")
        else:
            found.append("regex_only_other")
    for table_id in facade_only:
        name = _name_re(table_id)
        if re.search(r"\bDELETE\b.*\bUSING\b[^;]*?" + name, stripped, re.I | re.S):
            found.append("delete_using")
        elif re.search(r",\s*" + name, stripped, re.I):
            found.append("comma_join")
        else:
            found.append("facade_only_other")
    return sorted(set(found))


def _sqlglot_version() -> str:
    try:
        return version("sqlglot")
    except PackageNotFoundError:  # pragma: no cover -- sqlglot is a hard dependency
        return "unknown"


@dataclass
class TableShadow:
    """Collects the facade-vs-regex comparison for one collection."""

    source_engine: str = ""
    dialect: str = ""
    queries: int = 0
    agree: int = 0
    order_differs: int = 0
    parsed_agree: int = 0  # agreements among sqlglot-parsed queries only
    parsed_by: Counter[str] = field(default_factory=Counter)
    fallback_reasons: Counter[str] = field(default_factory=Counter)
    categories: Counter[str] = field(default_factory=Counter)
    disagreements: list[dict] = field(default_factory=list)
    fallbacks: list[dict] = field(default_factory=list)

    def record(
        self,
        query_id: str,
        query_text: str,
        regex: list[str],
        facade: list[str],
        refs: TableReferences,
    ) -> None:
        """Compare one query's table ids (``facade`` drives the output)."""
        self.queries += 1
        self.dialect = refs.dialect
        self.parsed_by[refs.parsed_by] += 1
        if refs.fallback_reason:
            self.fallback_reasons[refs.fallback_reason] += 1
            self.fallbacks.append(
                {
                    "query_id": query_id,
                    "reason": refs.fallback_reason,
                    "truncated": refs.truncated,
                }
            )
        if set(regex) == set(facade):
            self.agree += 1
            if refs.parsed_by == "sqlglot":
                self.parsed_agree += 1
            if regex != facade:
                self.order_differs += 1
            return
        # "unknown" is the resolver's placeholder for "no table", not a table.
        regex_only = [t for t in regex if t not in facade and t != "unknown"]
        facade_only = [t for t in facade if t not in regex and t != "unknown"]
        categories = _categories(query_text, regex_only, facade_only)
        self.categories.update(categories)
        self.disagreements.append(
            {
                "query_id": query_id,
                "regex": regex,
                "facade": facade,
                "regex_only": regex_only,
                "facade_only": facade_only,
                "categories": categories,
                "parsed_by": refs.parsed_by,
                "query_text": query_text[:_QUERY_TEXT_LIMIT],
            }
        )

    @property
    def disagree(self) -> int:
        return self.queries - self.agree

    def summary(self) -> dict:
        rate = round(self.agree / self.queries, 4) if self.queries else 1.0
        parsed = self.parsed_by.get("sqlglot", 0)
        parsed_rate = round(self.parsed_agree / parsed, 4) if parsed else 1.0
        return {
            "queries": self.queries,
            "agree": self.agree,
            "disagree": self.disagree,
            "agreement_rate": rate,
            # A fallback query agrees by construction (it *is* the regex
            # answer), so this rate, over sqlglot-parsed queries only, is the
            # one that compares the two readings.
            "sqlglot_parsed": parsed,
            "sqlglot_agree": self.parsed_agree,
            "sqlglot_agreement_rate": parsed_rate,
            "order_differs": self.order_differs,
            "parsed_by": dict(sorted(self.parsed_by.items())),
            "fallback_reasons": dict(sorted(self.fallback_reasons.items())),
            "disagreement_categories": dict(sorted(self.categories.items())),
        }

    def status_summary(self) -> dict:
        """The compact counts for a phase status line (#553): how many queries
        fell back to the regex (and why), and how many the regex reads the
        same way as the facade."""
        return {
            "regex_fallback": sum(self.fallback_reasons.values()),
            "reasons": dict(sorted(self.fallback_reasons.items())),
            "agree": self.agree,
            "disagree": self.disagree,
        }

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "mode": "cross_check",
            "drives_output": "facade",
            "sqlglot_version": _sqlglot_version(),
            "source_engine": self.source_engine,
            "dialect": self.dialect,
            "summary": self.summary(),
            "disagreements": self.disagreements,
            "fallbacks": self.fallbacks,
        }


def write_shadow_artifact(store, db_name: str, job_id: str, shadow: TableShadow) -> str | None:
    """Write ``<db>/<job>/collector/sql_shadow.json`` and log its summary.

    Returns the artifact path, or None when the write failed: the cross-check must
    never fail a collection, so an error is logged and swallowed.
    """
    path = f"{db_name}/{job_id}/{SHADOW_ARTIFACT}"
    try:
        store.write_json(path, shadow.to_dict())
    except Exception:  # noqa: BLE001 -- the cross-check only observes
        logger.warning("sql shadow: could not write %s", path, exc_info=True)
        return None
    s = shadow.summary()
    logger.info(
        "sql shadow: %d/%d queries agree (%s), %d disagree, fallbacks %s, categories %s",
        s["agree"],
        s["queries"],
        s["agreement_rate"],
        s["disagree"],
        s["fallback_reasons"],
        s["disagreement_categories"],
    )
    return path
