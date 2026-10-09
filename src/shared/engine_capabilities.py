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

# Engines that can run a text/pattern-search-shaped query at all, without a
# dedicated search engine -- the same "CAN serve this pattern, even if
# poorly" bar ``capability_registry.ENGINE_CAPABILITIES`` already uses (#480).
#   - aurora_postgresql: LIKE/ILIKE, ``~``/``~*`` (regex) and ``tsvector``/
#     ``tsquery`` (full-text) are all native SQL. ``pg_trgm``'s GIN/GiST
#     index is also the only one of the three that indexes a LEADING
#     wildcard (``LIKE '%term%'``) -- the other two below still run it,
#     just as an unindexed scan.
#   - aurora_mysql: LIKE, REGEXP/RLIKE (regex) and FULLTEXT (``MATCH ...
#     AGAINST``, full-text) are all native SQL. A leading-wildcard LIKE runs
#     unindexed -- the same scan cost as DocumentDB below, not Aurora
#     PostgreSQL's indexed case.
#   - documentdb: ``$regex`` (LIKE/regex-equivalent) and the ``$text``
#     operator (full-text) are both native query operators. A
#     leading-wildcard ``$regex`` also runs unindexed, same scan cost as
#     Aurora MySQL's LIKE.
#   - dynamodb / elasticache are deliberately NOT included: neither can
#     express any of these patterns at all (DynamoDB has no LIKE/regex
#     equivalent; ElastiCache is a cache, not a queryable text store) -- they
#     keep ``ANTI_PATTERN_PENALTIES``'s full "wrong engine" penalty, with no
#     reduced redesign penalty (unlike ``acid-transactions``'s DynamoDB
#     case): "redesign around no text-search feature at all" is a much
#     bigger, vaguer leap than a concrete API like ``TransactWriteItems``.
#   - opensearch is excluded for a different reason: it is the engine these
#     signals come FROM, not a capability question for this set --
#     ``assignment_resolver``'s cross-engine step never penalises the
#     detecting engine for its own signal.
#
# Does NOT cover ``fuzzy-search``: see ``FUZZY_SEARCH_ENGINES`` below, a
# narrower, separate set.
#
# Deliberately the same three engines as ACID_TRANSACTION_ENGINES, but a
# second, independent fact, not inferred from it: a future engine could gain
# one capability without the other.
#
# Known gap, tracked as #485, not fixed here: the HARD capability gate
# (``capability_registry.ENGINE_CAPABILITIES``'s ``inverted_index``, checked
# in ``AssignmentResolver.resolve`` Step 3b, before this set is ever
# consulted) excludes DocumentDB but not Aurora MySQL, even though both only
# run these patterns unindexed. Its detector fires both on a leading
# wildcard (``LIKE '%term'``) and on full-text syntax (``MATCH ... AGAINST``,
# ``tsvector``/``tsquery``), so a leading-wildcard query AND a full-text
# query both still score 0 on DocumentDB after this fix, hard-excluded
# before this set is ever reached. A non-leading LIKE or a regex query is
# unaffected by the hard gate and does benefit from the exemption below.
# ``tests/unit/test_capability_registry.py`` tracks the gap against #485
# instead of asserting it as a permanent spec.
TEXT_SEARCH_ENGINES: frozenset[str] = frozenset({"aurora_postgresql", "aurora_mysql", "documentdb"})

# Engines with a native fuzzy/similarity-match operator. Today that is only
# ``pg_trgm``'s ``similarity()`` on Aurora PostgreSQL: Aurora MySQL and
# DocumentDB have no trigram or edit-distance operator to fall back to --
# unlike the other three search patterns above, where every
# ``TEXT_SEARCH_ENGINES`` engine runs the pattern at least as an unindexed
# scan, there is no SQL/query-language construct for ``fuzzy-search`` on
# those two, so this is its own, narrower set rather than reusing
# ``TEXT_SEARCH_ENGINES``.
FUZZY_SEARCH_ENGINES: frozenset[str] = frozenset({"aurora_postgresql"})
