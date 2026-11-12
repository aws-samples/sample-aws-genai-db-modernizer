# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``cache_front_description`` is the one place every deliverable's
cache-fronting sentence is built (review of #375): wave 2 fronts the
retained relational engine, and once wave 3 moves those reads it fronts
the final owner instead -- stated identically, wherever it is said."""

from __future__ import annotations

from src.shared.migration_wave_engines import (
    CACHE_FRONT_REFERENCE,
    cache_front_description,
    cache_front_is_staged,
    cache_front_reference,
)


class TestCacheFrontDescription:
    def test_fronts_only_the_relational_engine_when_the_final_owner_matches(self) -> None:
        """No wave 3 move happened: the cache still fronts the same engine wave
        2 put it in front of -- say just that engine, not a two-stage sentence."""
        desc = cache_front_description("aurora_postgresql", {"aurora_postgresql": 3})
        assert desc == "Aurora PostgreSQL"

    def test_fronts_only_the_relational_engine_when_there_are_no_final_owners_yet(self) -> None:
        desc = cache_front_description("aurora_mysql", {})
        assert desc == "Aurora MySQL"

    def test_states_both_stages_when_the_final_owner_differs(self) -> None:
        """The exact discourse case the review found contradicting itself:
        the Recommended architecture table said DynamoDB, the roadmap's wave 2
        card said Aurora PostgreSQL -- both are now this one sentence."""
        desc = cache_front_description("aurora_postgresql", {"dynamodb": 3})
        assert desc == "Aurora PostgreSQL in wave 2, then DynamoDB once wave 3 moves these reads"

    def test_accepts_a_plain_iterable_of_engine_ids_too(self) -> None:
        desc = cache_front_description("aurora_mysql", ["dynamodb"])
        assert desc == "Aurora MySQL in wave 2, then DynamoDB once wave 3 moves these reads"

    def test_no_retained_engine_falls_back_to_the_final_owners(self) -> None:
        desc = cache_front_description(None, {"dynamodb": 3})
        assert desc == "DynamoDB"

    def test_no_retained_engine_and_no_final_owners_is_a_generic_fallback(self) -> None:
        desc = cache_front_description(None, {})
        assert desc == "its owner engine"

    def test_final_owner_that_still_matches_the_retained_engine_is_not_repeated(self) -> None:
        """A mixed final-owner set (some cached reads settle on the retained
        engine, others move to DynamoDB in wave 3 -- common when wave 3 only
        moves some of them) must not repeat the retained engine in the "then"
        clause: "Aurora MySQL in wave 2, then Aurora MySQL, DynamoDB ..." is
        confusing and redundant."""
        desc = cache_front_description("aurora_mysql", {"aurora_mysql": 16, "dynamodb": 4})
        assert desc == "Aurora MySQL in wave 2, then DynamoDB once wave 3 moves these reads"

    def test_multiple_final_owners_are_all_named(self) -> None:
        desc = cache_front_description("aurora_postgresql", {"dynamodb": 2, "documentdb": 1})
        assert desc == (
            "Aurora PostgreSQL in wave 2, then DocumentDB, DynamoDB once wave 3 moves these reads"
        )


class TestCacheFrontReference:
    """#2171: the staged front ("Aurora PostgreSQL in wave 2, then DocumentDB, DynamoDB
    once wave 3 moves these reads") is the wave 2 rationale's; a sentence elsewhere
    refers to it instead of repeating it."""

    def test_a_staged_front_is_a_short_reference(self) -> None:
        ref = cache_front_reference("aurora_postgresql", {"dynamodb": 2, "documentdb": 1})
        assert ref == CACHE_FRONT_REFERENCE
        assert "wave 2" not in ref and "once wave 3" not in ref

    def test_a_one_stage_front_is_named_as_before(self) -> None:
        for retained, owners in (
            ("aurora_postgresql", {"aurora_postgresql": 3}),
            ("aurora_mysql", {}),
            (None, {"dynamodb": 3}),
            (None, {}),
        ):
            assert cache_front_reference(retained, owners) == cache_front_description(
                retained, owners
            )


def test_staging_is_decided_from_the_data_not_the_sentence() -> None:
    """#2171 review (ADR-032 Decision 6): whether the front is staged is read from the
    retained engine and the owners, the same data the sentence is worded from."""
    cases = [
        ("aurora_postgresql", {"dynamodb": 1}, True),
        ("aurora_mysql", {"aurora_mysql": 16, "dynamodb": 4}, True),
        ("aurora_postgresql", {"aurora_postgresql": 3}, False),
        ("aurora_mysql", {}, False),
        (None, {"dynamodb": 3}, False),
    ]
    for retained, owners, staged in cases:
        assert cache_front_is_staged(retained, owners) is staged
        assert ("once wave 3" in cache_front_description(retained, owners)) is staged
