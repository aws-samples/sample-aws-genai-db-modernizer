"""Each schema designer names its output after the target's own vocabulary.

There is no single field meaning "a design exists". DynamoDB, Aurora
PostgreSQL and Aurora MySQL emit ``table_definitions``; DocumentDB emits
``collections``; ElastiCache emits ``key_designs``; OpenSearch emits
``index_designs``.

Testing ``table_definitions`` universally shipped a false customer-facing claim:
three engines that had designed 20 collections, 10 key structures and 5 index
mappings were reported as needing "a separate schema conversion assessment", with
the source engine given as the reason — while their designs sat in the artifact
the same report was built from. Field counts below are the real ones measured on
job ``v2-e2e-08`` (2026-08-26), so this file also pins the shapes rather than
asserting them from the map it is testing.
"""

from __future__ import annotations

import pytest

from src.atx_orchestrator.core import _DESIGN_SHAPE, _design_shape

# (target, artifact field, count observed on v2-e2e-08)
OBSERVED = [
    ("dynamodb", "table_definitions", 13),
    ("documentdb", "collections", 20),
    ("elasticache", "key_designs", 10),
    ("opensearch", "index_designs", 5),
]


class TestDesignShape:
    @pytest.mark.parametrize("target,field,_count", OBSERVED)
    def test_maps_each_engine_to_its_own_field(self, target, field, _count) -> None:
        assert _design_shape(target)[0] == field

    def test_no_two_engines_share_a_design_field(self) -> None:
        """A shared field would let one engine's emptiness mask another's design.

        dynamodb, aurora_postgresql and aurora_mysql are the intentional
        exception: all three are genuinely relational-shaped ("tables"), each
        writes to its own per-engine artifact path, so there is no artifact
        where the three fields could collide and mask one another. Every other
        pair must still be distinct.
        """
        fields = [f for f, _ in _DESIGN_SHAPE.values()]
        assert fields.count("table_definitions") == 3
        other_fields = [f for f in fields if f != "table_definitions"]
        assert len(other_fields) == len(set(other_fields))

    def test_unknown_target_falls_back_rather_than_raising(self) -> None:
        """A target with no designer at all has no design field.

        Classification is advisory and must not be able to fail the phase, so an
        unmapped target (e.g. a not-yet-supported engine like Neptune) returns a
        usable shape instead of raising.
        """
        field, unit = _design_shape("neptune")
        assert field and unit

    def test_units_are_human_readable_and_engine_specific(self) -> None:
        """The unit reaches the chat, so "target tables" for DocumentDB is wrong."""
        assert _design_shape("documentdb")[1] == "collections"
        assert _design_shape("elasticache")[1] == "key designs"
        assert _design_shape("dynamodb")[1] != _design_shape("opensearch")[1]

    @pytest.mark.parametrize("target,field,count", OBSERVED)
    def test_observed_artifact_shape_yields_a_present_design(self, target, field, count) -> None:
        """The regression, stated in terms of the real artifacts.

        Each engine's artifact is reconstructed with ONLY its own design field
        populated — which is what upstream actually emits — and must read as a
        design present. Under the old ``table_definitions`` test the last three
        read as empty and produced a warning.
        """
        artifact = {field: [{"n": i} for i in range(count)], "status": "completed"}
        resolved, _unit = _design_shape(target)
        assert artifact.get(resolved), (
            f"{target} design not found via {resolved}; an artifact carrying only "
            f"{field} must still read as a design"
        )

    def test_every_engine_with_a_designer_is_mapped(self) -> None:
        """All six target engines now have designers. A seventh would silently
        take the fallback."""
        assert set(_DESIGN_SHAPE) == {
            "dynamodb",
            "documentdb",
            "elasticache",
            "opensearch",
            "aurora_postgresql",
            "aurora_mysql",
        }
