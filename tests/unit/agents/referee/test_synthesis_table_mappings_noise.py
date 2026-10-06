"""``build_table_mappings`` must not count a schema design's own hallucinated
"source table" as a mapped table (#380).

Reproduces the Discourse evidence (PR #375 review): the DynamoDB schema
design represented Postgres auto-increment sequences as an
``id_sequence_counters`` table, listing eight ``public.*_id_seq`` names as
its ``source_tables``. None of those names was ever a real table the
collector saw, yet they ended up in ``table_mappings`` with
``confidence_score: 0``, inflating the engineering report's migration map
("tables migrate") count past the real, mapped total. ``build_table_mappings``
now resolves every ``source_table`` against the collector's own schema
(:class:`TableNameResolver`, the same canonical check migration-wave
scoping and assignment resolution use) and drops a name that does not
resolve to a real table or view.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_table_mappings


def _data(**schema_designs: dict) -> SynthesisData:
    return SynthesisData(
        job_id="j",
        database_name="discourse",
        collector={
            "database_schema": {
                "tables": [{"table_id": "discourse.badges", "table_name": "badges"}],
                "views": [],
            }
        },
        engines={
            engine: EngineArtifacts(engine, analysis={}, schema_design=schema)
            for engine, schema in schema_designs.items()
        },
    )


class TestTableMappingsDropsUncollectedNoise:
    def test_a_sequence_name_never_collected_is_dropped(self) -> None:
        data = _data(
            dynamodb={
                "table_definitions": [
                    {
                        "table_name": "id_sequence_counters",
                        "aggregate_pattern": "separate",
                        "source_tables": [
                            "public.badge_groupings_id_seq",
                            "public.badge_types_id_seq",
                        ],
                    },
                    {
                        "table_name": "badges",
                        "aggregate_pattern": "relational_table",
                        "source_tables": ["discourse.badges"],
                    },
                ]
            }
        )
        mappings = build_table_mappings(data)
        sources = {m["source_table"] for m in mappings}
        assert sources == {"discourse.badges"}
        assert "public.badge_groupings_id_seq" not in sources
        assert "public.badge_types_id_seq" not in sources

    def test_a_real_table_under_a_different_spelling_is_kept_canonically(self) -> None:
        """A bare name the schema design wrote without the collector's own
        schema qualifier still resolves and is counted once, under the
        canonical id -- not dropped, and not double-counted."""
        data = _data(
            aurora_postgresql={
                "source_database": "discourse",
                "table_definitions": [{"table_name": "badges", "source_tables": ["badges"]}],
            }
        )
        mappings = build_table_mappings(data)
        assert [m["source_table"] for m in mappings] == ["discourse.badges"]

    def test_no_collector_schema_fails_open_and_keeps_every_name(self) -> None:
        """Back-compat: a caller with no collector schema to check against
        (e.g. a hand-built artifact) gets the pre-#380 behaviour -- every
        name kept, not treated as all noise."""
        data = SynthesisData(
            job_id="j",
            database_name="db",
            collector={},
            engines={
                "dynamodb": EngineArtifacts(
                    "dynamodb",
                    analysis={},
                    schema_design={
                        "table_definitions": [
                            {"table_name": "counters", "source_tables": ["db.made_up_seq"]}
                        ]
                    },
                )
            },
        )
        mappings = build_table_mappings(data)
        assert [m["source_table"] for m in mappings] == ["db.made_up_seq"]
