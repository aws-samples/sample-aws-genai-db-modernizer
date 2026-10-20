"""Model-written schema-design prose, cleaned before synthesis uses it (#1034).

A schema design's ``trade_offs``, ``unsupported_patterns``, ``migration_notes``,
``access_patterns[].description`` (every engine but Aurora, whose description
is the structural "operation on table" text this code writes) and Aurora's
``optimizations[].rationale``/``.recommendation`` and
``app_layer_notes[].recommendation`` are written by a model and
reach the customer through ``report.json``: the trade-off list, the per-engine
``schema_designs`` projection, the query groups' access patterns and the risks
built from unsupported patterns and migration notes. Synthesis reads every
schema design through ``clean_schema_design_prose``
(``synthesis_data.load_synthesis_data``), so this is the one seam where that
text is checked, whatever mode wrote the design and however old it is:

- an em dash is rewritten where that cannot change a fact
  (``prose_style.replace_dashes``, which never touches code, en dashes or
  identifiers); short labels (an optimization's target, a pattern type, an
  object name) only get that rewrite;
- a field with any other item of the plain-prose style list goes back to
  deterministic text: a trade-off or an access pattern gets a short sentence
  built from its own structured fields, an optimization's recommendation names
  its category and target, an unsupported pattern's fix becomes "Review these
  queries with the <engine> schema design before migration." (so the risk never
  loses its mitigation), and every other field becomes empty, so the risk
  builder uses its own wording (``synthesis_report._unsupported_pattern_problem``
  and the migration-note mitigation).

The collected table and column names are passed in as ``identifiers``, so a
customer table named ``journeys`` never counts as the word. Each rejection is
returned as one line; synthesis writes them to the job's
``_logs/prose_style_rejections.json`` (diagnostics, never ``report.json``, #1211). The schema design output on disk is never
changed: it keeps the model's text.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Iterable
from functools import partial
from typing import Any

from src.agents.referee.aurora_choice import AURORA_ENGINES
from src.shared.engine_names import display_engine
from src.shared.prose_style import find_style_hits, replace_dashes

# Model-written fields that fall back to empty text: the risk builder then uses
# its own deterministic wording (the problem stated from the queries, or the
# "Review ... and implement its logic" mitigation), and an app-layer note line
# already names its feature and object.
_EMPTY_FALLBACK_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("unsupported_patterns", ("reason", "description")),
    ("migration_notes", ("application_logic_required",)),
)
# Short model-written labels: only their dashes are rewritten (a label with a
# dash that cannot be rewritten keeps it; it is a name, never sent back).
_LABEL_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("optimizations", ("target",)),
    ("unsupported_patterns", ("pattern_type",)),
    ("migration_notes", ("object_name", "object_type")),
    ("app_layer_notes", ("source_object",)),
)


def _names(values: Any, limit: int = 3) -> str:
    names = [str(v) for v in values or [] if v][:limit]
    return ", ".join(names)


def _trade_off_fallback(engine: str, field: str, trade_off: dict[str, Any]) -> str:
    name = display_engine(engine)
    if field == "description":
        source = _names(trade_off.get("source_tables"))
        target = _names(trade_off.get("target_tables"))
        if source and target:
            return f"{name} design trade-off: {source} maps to {target}."
        if source or target:
            return f"{name} design trade-off for {source or target}."
        return f"{name} design trade-off."
    return f"Review this trade-off with the {name} schema design before migration."


def _access_pattern_fallback(engine: str, ap: dict[str, Any]) -> str:
    operation = str(ap.get("operation") or "").strip()
    table = str(ap.get("table_name") or ap.get("index") or ap.get("collection") or "").strip()
    if operation and table:
        return f"{operation} on {table}"
    return operation or table


def _unsupported_fix_fallback(engine: str) -> str:
    """The mitigation a risk keeps when the model's fix is rejected."""
    return f"Review these queries with the {display_engine(engine)} schema design before migration."


def _optimization_fallback(field: str, opt: dict[str, Any]) -> str:
    if field != "recommendation":
        return ""
    category = str(opt.get("category") or "").replace("_", " ").strip()
    target = str(opt.get("target") or "").strip()
    head = f"{category.capitalize()} optimization" if category else "Optimization"
    return f"{head} on {target}." if target else f"{head}."


def clean_schema_design_prose(
    engine: str, schema: dict[str, Any] | None, identifiers: Iterable[str] = ()
) -> tuple[dict[str, Any] | None, list[str]]:
    """A copy of ``schema`` with its model-written customer prose cleaned, and
    one line per field sent back to deterministic text. ``None`` stays ``None``."""
    if not isinstance(schema, dict):
        return schema, []
    names = list(identifiers)
    out = copy.deepcopy(schema)
    rejections: list[str] = []

    def clean(path: str, value: str, fallback: Callable[[], str]) -> str:
        cleaned = replace_dashes(value, names)
        terms = sorted({f'"{h.term}"' for h in find_style_hits(cleaned, names)})
        if not terms:
            return cleaned
        rejections.append(f"{engine} {path}: plain-prose style list ({', '.join(terms)})")
        return fallback()

    def each_field(key: str, fields: tuple[str, ...], fallback: Callable[..., str]) -> None:
        entries = out.get(key)
        if not isinstance(entries, list):
            return
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            for field in fields:
                value = entry.get(field)
                if isinstance(value, str) and value:
                    entry[field] = clean(
                        f"{key}[{i}].{field}",
                        value,
                        partial(fallback, field, entry),
                    )

    trade_offs = out.get("trade_offs")
    if isinstance(trade_offs, list):
        for i, t in enumerate(trade_offs):
            if isinstance(t, str) and t:
                trade_offs[i] = clean(
                    f"trade_offs[{i}]", t, lambda: _trade_off_fallback(engine, "description", {})
                )
    each_field(
        "trade_offs", ("description", "impact"), lambda f, t: _trade_off_fallback(engine, f, t)
    )
    for key, fields in _EMPTY_FALLBACK_FIELDS:
        each_field(key, fields, lambda f, e: "")
    each_field(
        "unsupported_patterns",
        ("recommendation", "workaround"),
        lambda f, e: _unsupported_fix_fallback(engine),
    )
    for key, fields in _LABEL_FIELDS:
        for entry in out.get(key) or []:
            if isinstance(entry, dict):
                for field in fields:
                    if isinstance(entry.get(field), str):
                        entry[field] = replace_dashes(entry[field], names)
    if engine in AURORA_ENGINES:
        each_field("optimizations", ("rationale", "recommendation"), _optimization_fallback)
        each_field("app_layer_notes", ("recommendation",), lambda f, e: "")
    else:
        each_field(
            "access_patterns", ("description",), lambda f, ap: _access_pattern_fallback(engine, ap)
        )
    return out, rejections
