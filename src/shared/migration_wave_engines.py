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

from collections.abc import Iterable, Mapping

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

# Engines that never durably own a table (#317): a cache fronts hot reads only
# (``CACHE_ENGINES``) and a search/read-model engine indexes data synced from a
# real owner (``SEARCH_ENGINES``, #303). A table's ``primary_engine`` must
# always be a system-of-record owner. Single source of truth for the resolver
# (``assignment_resolver.derive_table_assignments``) and the wave builder's own
# ``migration_waves._durable_owner``.
NON_OWNER_ENGINES: frozenset[str] = CACHE_ENGINES | SEARCH_ENGINES


def cache_front_description(
    retained_engine: str | None, final_owners: Mapping[str, int] | Iterable[str] | None
) -> str:
    """What a cache overlay fronts, worded identically everywhere (#375 review).

    Wave 2 adds the cache in front of the retained relational engine (wave 1
    has already carried the schema there by the time wave 2 runs). If the
    reads it fronts later move to a different engine -- wave 3's own move,
    typically onto DynamoDB -- the cache ends up fronting that engine
    instead, with no change of its own: it still serves the same reads,
    invalidated by whichever engine currently owns the cached table.

    A review of #375 found deliverables disagreeing on which single engine
    the cache fronts: the Recommended architecture table said the final
    owner (correct once every wave has run), the roadmap's wave 2 card said
    the retained relational engine (also correct, but only partway through
    the roadmap) -- each built its own sentence independently, so a reader
    comparing the two saw a straight contradiction. This is the one place
    that sentence is built; every deliverable that names what the cache
    fronts must call this, not write its own version, so they cannot drift
    apart again.

    ``retained_engine`` is the Aurora engine wave 1 reached, or ``None`` when
    there is none (the cache then fronts "its owner engine(s)" instead of a
    named wave). ``final_owners`` is the cache overlay's own ``owners`` (the
    engine(s) actually serving the cached queries once every wave has run) --
    a ``{engine: count}`` dict, or any iterable of engine ids.
    """
    from src.shared.engine_names import display_engine

    owners = sorted({e for e in (final_owners or []) if e})
    relational = display_engine(retained_engine) if retained_engine else None
    if not relational:
        return ", ".join(display_engine(e) for e in owners) or "its owner engine"
    # Queries whose final owner already IS the retained engine need no "then"
    # clause -- only the reads that move again (a mixed final-owner set, some
    # cached reads settling on the retained engine and others moving to
    # DynamoDB, is common once wave 3 only moves some of them) are named there.
    moved_on = [e for e in owners if e != retained_engine]
    if not moved_on:
        return relational
    final_names = ", ".join(display_engine(e) for e in moved_on)
    return f"{relational} in wave 2, then {final_names} once wave 3 moves these reads"
