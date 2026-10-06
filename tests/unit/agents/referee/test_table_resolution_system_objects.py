"""``is_engine_system_object`` (#380 review): a database-engine-internal
object (a system catalog schema, a stats-extension table/view) is never
counted as part of "the whole database" wave 1 carries over -- but a real
application table must never be excluded just because no later wave happens
to map it, or because its name merely looks like a sequence.

#380 round 2 review: this is called on ``known_tables``, the collector's own
verified tables and views. The collector contract has no ``Sequence`` model
at all, so a name here that only *looks* like a sequence (``orders_seq``) is
always a real collected table -- an earlier revision's ``_seq$`` suffix
heuristic could only misclassify real tables (``order_seq``, ``shop.item_seq``)
and has been removed. A schema design's own hallucinated sequence name
(``public.badge_groupings_id_seq``) is a different problem, already solved
by ``build_table_mappings``'s own ``TableNameResolver`` check (see
``test_synthesis_table_mappings_noise.py``): it never reaches ``known_tables``
in the first place, because no such table exists in the collector's schema.
"""

from __future__ import annotations

from src.agents.referee.table_resolution import is_engine_system_object


class TestIsEngineSystemObject:
    def test_system_schema_is_an_engine_object(self) -> None:
        assert is_engine_system_object("information_schema.tables") is True
        assert is_engine_system_object("pg_catalog.pg_class") is True
        assert is_engine_system_object("performance_schema.events") is True
        assert is_engine_system_object("mysql.user") is True

    def test_stats_extension_object_is_an_engine_object_regardless_of_schema(self) -> None:
        assert is_engine_system_object("pg_stat_statements") is True
        assert is_engine_system_object("public.pg_stat_statements") is True
        assert is_engine_system_object("pg_stat_statements_info") is True

    def test_a_real_application_table_is_not_an_engine_object(self) -> None:
        assert is_engine_system_object("discourse.ar_internal_metadata") is False
        assert is_engine_system_object("discourse.chat_drafts") is False
        assert is_engine_system_object("discourse.github_commits") is False
        assert is_engine_system_object("orders") is False

    def test_a_real_collected_table_whose_name_merely_looks_like_a_sequence_stays(self) -> None:
        # #380 round 2: the collector has no Sequence model, so a "known
        # table" named this way is always real -- never an actual sequence.
        assert is_engine_system_object("orders_seq") is False
        assert is_engine_system_object("order_seq") is False
        assert is_engine_system_object("shop.item_seq") is False
        assert is_engine_system_object("inventory_seq") is False

    def test_empty_or_none_is_not_an_engine_object(self) -> None:
        assert is_engine_system_object("") is False
