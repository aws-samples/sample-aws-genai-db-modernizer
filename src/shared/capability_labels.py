"""Display strings for the hard capabilities ``capability_registry`` can require.

Shared between ``src.agents.referee.assignment_resolver`` (the "lacks required
capability: ..." reason it builds when a hard capability gate zeroes an engine,
#572 review suggestion 7) and ``src.agents.referee.synthesis_report``
(``_capability_pin_reason``, which reads that same reason text back out to
explain why an Aurora engine's queries stay relational, #393) -- one map, so
the two can never show a different name, or a raw id, for the same capability.
Same shape as ``src.shared.signal_labels.SIGNAL_NOUNS``/``signal_noun``.

Every capability ``capability_registry.detect_required_capabilities`` can put
in a "lacks required capability" reason needs an entry here (#393 review:
``inverted_index`` and ``scan_engine`` had no entry, so a query pinned to
Aurora for either leaked the raw capability id into customer text instead of
reading as prose; ``tests/unit/agents/referee/
test_synthesis_capability_pin_display_coverage.py`` asserts this dict covers
every capability name the registry can produce). ``multi_doc_acid`` is in
``ENGINE_CAPABILITIES`` but ``detect_required_capabilities`` never emits it
today (its own ``CAPABILITY_FACTS`` entry is an empty, structural-only
placeholder); kept here anyway so this never silently regresses if that
changes.

``CAPABILITY_REDESIGN_HINTS`` and ``GATE_REDESIGN_HINTS`` (#729) say how to
redesign a query a fallback rule placed, for the engines that ruled it out:
one sentence per capability and engine, else per gate kind;
``redesign_hints`` picks them for one query.
"""

from __future__ import annotations

from collections.abc import Iterable

from src.shared.engine_names import display_engine

CAPABILITY_DISPLAY_NAMES: dict[str, str] = {
    "aggregation": "aggregation",
    "complex_joins": "multi-table joins",
    "computed_join": "a join on a computed expression",
    "sql_admin": "a utility or DDL statement",
    "inverted_index": "full-text or pattern search",
    "scan_engine": "window or recursive queries",
    "multi_doc_acid": "multi-document transactions",
}


def capability_label(name: str) -> str:
    """``complex_joins`` -> ``multi-table joins``; unknown capabilities read as
    their name with underscores turned to spaces, same fallback shape as
    ``src.shared.signal_labels.signal_noun``."""
    return CAPABILITY_DISPLAY_NAMES.get(name, name.replace("_", " "))


# How a query that requires a capability reads after "it" in a customer-facing
# rejection (#801 review): "it is a utility or DDL statement", never "it needs
# a utility or DDL statement". A capability not listed here reads as "needs
# <capability_label>", which is right for every noun-shaped label.
_CAPABILITY_GAP_CLAUSES: dict[str, str] = {
    "sql_admin": "is a utility or DDL statement",
    "computed_join": "joins on a computed expression",
    "scan_engine": "uses a window function or a recursive query",
}


def _join_and(items: list[str]) -> str:
    """``[a]`` -> ``a``; ``[a, b]`` -> ``a and b``; ``[a, b, c]`` -> ``a, b and c``."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def capability_gap_phrase(names: Iterable[str]) -> str:
    """The capabilities a query requires, as a verb phrase that reads after
    "it" or "query q2 in that group": ``["aggregation", "complex_joins"]`` ->
    ``needs aggregation and multi-table joins``; ``["sql_admin"]`` -> ``is a
    utility or DDL statement``. Clauses come in sorted capability order,
    needs last, joined with ", and "."""
    unique = sorted(set(names))
    clauses = [_CAPABILITY_GAP_CLAUSES[n] for n in unique if n in _CAPABILITY_GAP_CLAUSES]
    needs = [capability_label(n) for n in unique if n not in _CAPABILITY_GAP_CLAUSES]
    if needs:
        clauses.append(f"needs {_join_and(needs)}")
    return ", and ".join(clauses)


# How to redesign a query a fallback rule placed (#729) so an engine that
# ruled it out could serve it: one sentence per hard capability and per engine
# that lacks it (``capability_registry.ENGINE_CAPABILITIES``; the test
# ``test_every_engine_that_lacks_a_capability_has_a_hint`` keeps the two in
# step). An engine that has the capability has no entry: it was not ruled
# out for it, so nothing about it is suggested.
CAPABILITY_REDESIGN_HINTS: dict[str, dict[str, str]] = {
    "aggregation": {
        "dynamodb": (
            "keep the aggregate as an item updated on each write (an atomic counter, or a "
            "summary item maintained from DynamoDB Streams) and read that item, or compute it "
            "in the application."
        ),
    },
    "complex_joins": {
        "dynamodb": (
            "store the joined entities together under one partition key, one sort key prefix "
            "per entity type, so one Query returns them, or split the query into key lookups "
            "the application combines."
        ),
        "documentdb": (
            "embed the joined data in the document the query reads, or keep a reference and "
            "join it with a $lookup stage."
        ),
        "opensearch": (
            "index the joined fields into the one document the search reads, denormalized "
            "when the data is synced; OpenSearch has no joins."
        ),
    },
    "computed_join": {
        "dynamodb": (
            "store the computed join key as its own attribute, with a global secondary index "
            "on it, so the join becomes a key lookup."
        ),
        "documentdb": (
            "store the computed join key as its own field, with an index on it, so the join "
            "becomes an indexed lookup."
        ),
        "opensearch": "compute the join key when the data is synced and index it as a field.",
    },
    "sql_admin": {
        "dynamodb": (
            "this is session, transaction or schema traffic, not a data access: BEGIN and "
            "COMMIT become one TransactWriteItems call or a conditional write, SET and USE "
            "become client configuration, LOCK becomes a conditional write on a version "
            "attribute, and SHOW or DDL become control-plane API calls."
        ),
        "documentdb": (
            "this is session, transaction or schema traffic, not a data access: BEGIN and "
            "COMMIT become a client session transaction, SET and USE become driver options, "
            "LOCK becomes an update guarded by a version field, and SHOW or DDL become "
            "database commands such as listCollections and createIndex."
        ),
        "opensearch": (
            "this is session, transaction or schema traffic, not a data access: OpenSearch "
            "has no transactions or locks, so BEGIN, COMMIT and LOCK stay with the engine "
            "that owns the data, and SHOW or DDL become index API calls (mappings, settings)."
        ),
    },
    "inverted_index": {
        "dynamodb": (
            "serve the search from an index synced from DynamoDB (for example DynamoDB "
            "Streams to an OpenSearch Serverless collection), or replace the leading-wildcard "
            "pattern with a prefix match on a sort key."
        ),
    },
    "scan_engine": {
        "dynamodb": (
            "precompute the window or recursive result when the data is written (a rank or a "
            "hierarchy path stored on each item), or run it as an analytics query over an "
            "export to Amazon S3 with Amazon Athena."
        ),
        "documentdb": (
            "precompute the window or recursive result when the data is written (a rank "
            "field or a materialized path), or compute it in the application."
        ),
    },
    "multi_doc_acid": {
        "dynamodb": (
            "fit the write into one TransactWriteItems call (up to 100 items), or make it an "
            "idempotent multi-step flow with compensating writes."
        ),
        "opensearch": (
            "OpenSearch is not the system of record: keep the transaction on the engine that "
            "owns the data and sync the result into the index."
        ),
    },
}

# When no capability hint applies: one sentence per gate kind that ruled an
# engine out (``move_reasons.GATE_TEXT_KINDS``).
GATE_REDESIGN_HINTS: dict[str, str] = {
    "capability": "Rewrite the query so it needs only what the engines that ruled it out support.",
    "hard_exclusion": "Rewrite the query into a shape the engines that ruled it out accept.",
    "model_flag": "Redesign the query as the design review describes.",
    "write_gate": (
        "Send the write to the engine that owns the table; a cache or a search index only "
        "serves reads synced from it."
    ),
    "group_owner": (
        "Split the co-dependency group so its queries no longer need one engine for every "
        "member."
    ),
}

GENERIC_REDESIGN_HINT = "Rewrite the query so an analyzed engine can serve it."


def _names(engines: list[str]) -> str:
    names = [display_engine(e) for e in engines]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def redesign_hints(capabilities: Iterable[str], gates: Iterable[tuple[str, str]]) -> list[str]:
    """How to redesign a query a fallback rule placed (#729), for the engines
    that ruled it out.

    ``gates`` are the ``(engine, gate kind)`` pairs that ruled engines out.
    For each capability the query needs (sorted), one sentence for the
    capability-gated engines that lack it, engines with the same advice named
    together: ``For DynamoDB: keep the aggregate ...``. Without one, one
    sentence per gate kind (in order, each once); with neither, the generic
    sentence. Never advice for an engine the gate did not rule out.
    """
    pairs = list(gates)
    gated = sorted({e for e, g in pairs if g == "capability"})
    hints: list[str] = []
    for cap in sorted(set(capabilities)):
        by_text: dict[str, list[str]] = {}
        for engine in gated:
            text = CAPABILITY_REDESIGN_HINTS.get(cap, {}).get(engine)
            if text:
                by_text.setdefault(text, []).append(engine)
        for text, engines in by_text.items():
            hints.append(f"For {_names(engines)}: {text}")
    if not hints:
        for _, gate in pairs:
            hint = GATE_REDESIGN_HINTS.get(gate)
            if hint and hint not in hints:
                hints.append(hint)
    return hints or [GENERIC_REDESIGN_HINT]
