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
"""

from __future__ import annotations

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
