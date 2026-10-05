"""Engine buckets for the migration-waves sequencing rule (#225).

Single source of truth for which engines play which role in the wave
sequence (cache, key-value, search/analytics read model, document, retained
relational). Before this module existed, ``src.agents.referee.migration_waves``
(the builder), ``src.report.renderers`` (the legacy on-the-fly fallback for
reports written before #225) and ``src.report.pptx_report`` (deck accents)
each kept their own hand-written copy of these sets, and nothing enforced
that the copies stayed in sync (#225). Importing
from here instead means a new engine added to the rule only has to be added
once.

These are deliberately the narrow sets the wave rule itself cares about, not
the broader membership ``src.report.renderers._CACHE_ENGINES`` /
``_RELATIONAL_ENGINES`` use for whole-report role classification (which also
recognize ``memorydb`` and generic ``aurora``): the wave rule only ever
sequences the six engines synthesis can target.
"""

from __future__ import annotations

# Engines that can only ever be a cache layer, never a system of record (#296).
CACHE_ENGINES: frozenset[str] = frozenset({"elasticache"})
# Key-value / point-lookup engine, Wave 2.
KV_ENGINES: frozenset[str] = frozenset({"dynamodb"})
# Read-model engines: they index data synced from an owner, never own it (#303).
SEARCH_ENGINES: frozenset[str] = frozenset({"opensearch"})
# Document-shaped data, kept as an owner engine.
DOCUMENT_ENGINES: frozenset[str] = frozenset({"documentdb"})
# The source-compatible relational core, retained and carried over 1:1.
RELATIONAL_ENGINES: frozenset[str] = frozenset({"aurora_mysql", "aurora_postgresql"})

# Every engine the sequencing rule names explicitly; anything else falls into
# "any other direct migration target" (step 3 of the rule).
NAMED_ENGINES: frozenset[str] = (
    CACHE_ENGINES | KV_ENGINES | SEARCH_ENGINES | DOCUMENT_ENGINES | RELATIONAL_ENGINES
)
