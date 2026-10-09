"""Engine capability facts shared by cross-engine scoring (#477).

Lives next to ``migration_wave_engines.py`` (engine roles) because this is the
same kind of fact: a per-engine property every caller must agree on rather
than re-derive its own copy of. Before this module existed,
``assignment_resolver.ANTI_PATTERN_PENALTIES``'s "patterns detected by other
engines penalise OTHER engines" step treated a capability signal (e.g. "this
query needs ACID transactions") the same as a pure workload-fit anti-pattern
(e.g. "this query is a better fit elsewhere"): it penalised every other
engine flatly, including ones that support the capability natively. On today's
catalog that step never actually fires for ``acid-transactions`` (no analysis
agent emits it as a positive ``patterns_detected`` entry, only OpenSearch's own
anti-pattern), so it never pushed a query off Aurora in practice -- but it
would the moment any engine's analysis started reporting it that way, which is
exactly the latent bug #477 closes before it has a live victim.

This module is deliberately narrow: it only names capabilities that
``ANTI_PATTERN_PENALTIES`` keys need to check themselves against, not a
general replacement for the broader (and already duplicated-by-necessity)
capability tables in ``src.agents.referee.capability_registry`` (hard
architectural constraints used by the reality check's serviceability gate)
or ``src.agents.referee.reality_check`` (workload-pattern fit scoring). Adding
a third, differently-shaped capability table for every future scoring need
would make the exact kind of drift this fix is cleaning up worse, not
better -- so this module only grows when another ``ANTI_PATTERN_PENALTIES``
key turns out to be a capability statement too.

``ACID_TRANSACTION_ENGINES`` below intentionally mirrors
``capability_registry.ENGINE_CAPABILITIES``'s ``multi_doc_acid`` set (same
three engines) rather than importing it: ``src.shared`` must not depend on
``src.agents`` (the dependency runs the other way everywhere else in this
codebase), so this stays a small literal constant, not an import, and
``tests/unit/test_capability_registry.py`` asserts the two sets agree so they
cannot drift apart silently. Separately, ``reality_check.ENGINE_CAPABILITIES``
(a third, older capability table, workload-fit scoring rather than hard
constraints) does NOT list ``transactions`` for ``documentdb`` at all, which
disagrees with both this module and ``capability_registry`` -- a pre-existing
gap this fix does not touch; see PR #479 review round 1, comment 1.
"""

from __future__ import annotations

# Engines with native multi-row / multi-document ACID transactions: a single
# statement, or an explicit BEGIN/COMMIT block, that applies to more than one
# row or document atomically and durably.
#   - aurora_postgresql / aurora_mysql: full multi-statement, multi-table ACID
#     transactions (BEGIN/COMMIT/ROLLBACK) -- this is what "Aurora" means by
#     ACID compliance.
#   - documentdb: multi-document ACID transactions, but bounded -- only on
#     engine version 4.0 or later, and every transaction has a hard one-minute
#     limit (longer-running multi-document writes need to be batched).
#   - dynamodb is deliberately NOT included: ``TransactWriteItems`` has no
#     interactive BEGIN/COMMIT (the whole set of writes is one API call), can
#     reference the same item at most once per transaction, is bounded (<=100
#     items, <=4 MB, one AWS account and Region), and costs 2x the write
#     capacity of the equivalent non-transactional writes. That reshapes the
#     write into DynamoDB's own API rather than "already having" a SQL-shaped
#     transaction -- using it, or an idempotent multi-step flow instead, is a
#     redesign with a real cost, not a pre-existing capability. See
#     ``CAPABILITY_REDESIGN_PENALTIES`` in ``assignment_resolver`` for how
#     that cost stays visible instead of collapsing to either "no penalty" or
#     "wrong engine".
#   - opensearch / elasticache: neither is a system of record for
#     transactional data (opensearch is a read model, elasticache is a cache
#     -- ``migration_wave_engines.NON_OWNER_ENGINES``), and neither has ACID
#     transactions: ElastiCache's Redis ``MULTI``/``EXEC`` only guarantees the
#     commands run back-to-back with no other client interleaved (atomicity)
#     -- there is no rollback on a failed command and no durability guarantee
#     across a replica failover, so it is not a transaction in the ACID sense.
ACID_TRANSACTION_ENGINES: frozenset[str] = frozenset(
    {"aurora_postgresql", "aurora_mysql", "documentdb"}
)
