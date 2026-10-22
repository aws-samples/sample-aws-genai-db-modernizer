"""Write-group members their engine serves poorly, and how the target serves them (#1362).

A table has one writer (ADR-031 Decision 1 Amendment 9), so a write group can carry a
member whose own score on the group's engine is low (#1359: 14 maintenance writes on
DynamoDB, scoring 11 to 26). Routing keeps the group; the designer input and the risk
register name each such statement's intent, read deterministically from its parsed facts,
and the engine's target-native approach from the write-intent catalog. No routing change;
no test calls a model.
"""

from __future__ import annotations

import pytest

from src.agents.referee import carried_writes as cw
from src.agents.referee.carried_writes import (
    BULK_STATUS_FLIP,
    CONSISTENCY_REPAIR,
    DUPLICATE_CLEANUP,
    INTENTS,
    MISSING_PARENT_CLEANUP,
    MULTI_REFERENCE_DELETE,
    RETENTION_DELETE,
    SET_BASED_WRITE,
    carried_write_records,
    carried_writes,
    classify_write_intent,
)
from src.agents.referee.query_scoring import EngineScore
from src.shared.catalog.engines import ACTIVE, engine_ids
from src.shared.catalog.write_intents import carried_write_floor, write_intents

COLUMNS = {
    "id": {"column_name": "id", "data_type": "bigint"},
    "user_id": {"column_name": "user_id", "data_type": "bigint"},
    "target_user_id": {"column_name": "target_user_id", "data_type": "bigint"},
    "topic_id": {"column_name": "topic_id", "data_type": "bigint"},
    "status": {"column_name": "status", "data_type": "varchar(20)"},
    "created_at": {"column_name": "created_at", "data_type": "timestamp without time zone"},
    "expires_at": {"column_name": "expires_at", "data_type": "datetime2"},
    "note": {"column_name": "note", "data_type": "text"},
}


def _intent(text: str, engine: str = "postgresql", table: str = "app.events") -> str:
    return classify_write_intent({"query_text": text}, table, engine, COLUMNS)


class TestTheIntentIsReadFromTheParsedFacts:
    @pytest.mark.parametrize(
        "text",
        [
            "DELETE FROM events WHERE created_at < $1",
            'DELETE FROM "events" WHERE (NOT confirmed AND expired) AND (created_at < $1)',
            'DELETE FROM "events" WHERE (created_at < TIMESTAMP $1)',
            "DELETE FROM events WHERE created_at <= now() - interval '30 days'",
        ],
    )
    def test_a_delete_by_age_is_a_retention_delete(self, text):
        assert _intent(text) == RETENTION_DELETE

    def test_a_sql_server_expiry_delete_is_a_retention_delete(self):
        text = "DELETE TOP (5000) FROM [events] WHERE [expires_at] < SYSUTCDATETIME()"
        assert _intent(text, "sqlserver") == RETENTION_DELETE

    def test_a_range_on_a_column_that_is_no_timestamp_is_not_retention(self):
        assert _intent("DELETE FROM events WHERE id < $1") == SET_BASED_WRITE

    @pytest.mark.parametrize(
        "text",
        [
            "DELETE FROM events WHERE topic_id IN (SELECT topic_id FROM events "
            "LEFT JOIN topics ON topic_id = topics.id WHERE topics.id IS NULL)",
            "DELETE FROM events e WHERE NOT EXISTS (SELECT 1 FROM topics t WHERE t.id = e.topic_id)",
            "DELETE FROM events WHERE topic_id NOT IN (SELECT id FROM topics)",
        ],
    )
    def test_an_anti_join_delete_is_an_orphan_clean_up(self, text):
        assert _intent(text) == MISSING_PARENT_CLEANUP

    def test_clearing_a_dangling_reference_is_an_orphan_clean_up(self):
        text = (
            "UPDATE events SET topic_id = $1 WHERE topic_id IN (SELECT e.topic_id FROM events e "
            "LEFT JOIN topics t ON e.topic_id = t.id WHERE e.topic_id IS NOT NULL AND t.id IS NULL)"
        )
        assert _intent(text) == MISSING_PARENT_CLEANUP

    @pytest.mark.parametrize(
        "text",
        [
            "UPDATE events e SET status = $1 FROM users u WHERE e.user_id = u.id AND u.title IS NULL",
            "UPDATE events SET topic_id = (SELECT topic_id FROM posts WHERE posts.id = events.id) "
            "WHERE topic_id <> (SELECT topic_id FROM posts WHERE posts.id = events.id)",
            # A NOT EXISTS in an UPDATE re-aligns a value; it removes nothing.
            "UPDATE events e SET status = $1 FROM users u WHERE u.id = e.user_id AND NOT EXISTS "
            "(SELECT 1 FROM badges b WHERE b.user_id = u.id)",
        ],
    )
    def test_an_update_that_reads_another_table_is_a_consistency_repair(self, text):
        assert _intent(text) == CONSISTENCY_REPAIR

    def test_a_self_join_delete_is_a_duplicate_clean_up(self):
        text = (
            "DELETE FROM events USING events e2 WHERE events.user_id = e2.user_id "
            "AND events.topic_id = e2.topic_id AND events.id > e2.id"
        )
        assert _intent(text) == DUPLICATE_CLEANUP

    def test_an_or_of_reference_equalities_is_a_multi_reference_delete(self):
        text = 'DELETE FROM "events" WHERE (user_id = $1 OR target_user_id = $2)'
        assert _intent(text) == MULTI_REFERENCE_DELETE

    @pytest.mark.parametrize(
        "text",
        [
            "UPDATE events SET status = $1 WHERE status = $2 AND created_at < $3",
            "UPDATE events SET status = 'archived' WHERE note IS NULL",
        ],
    )
    def test_an_update_of_the_table_alone_is_a_bulk_status_flip(self, text):
        assert _intent(text) == BULK_STATUS_FLIP

    # --- the review's false positives (#1370 review) -------------------------------

    def test_a_self_join_on_its_own_key_is_an_anti_join_not_a_duplicate_clean_up(self):
        # discourse's directory_items / user_badges / user_avatars clean-ups.
        text = (
            "DELETE FROM events e USING events e2 LEFT JOIN users u ON u.id = e2.user_id "
            "WHERE e.id = e2.id AND u.id IS NULL"
        )
        assert _intent(text) == MISSING_PARENT_CLEANUP

    def test_a_self_join_on_a_column_that_is_not_its_key_is_a_duplicate_clean_up(self):
        text = "DELETE FROM events e USING events e2 WHERE e.user_id = e2.user_id AND e.id > e2.id"
        assert (
            classify_write_intent({"query_text": text}, "app.events", "postgresql", COLUMNS, ["id"])
            == DUPLICATE_CLEANUP
        )
        assert (
            classify_write_intent(
                {"query_text": text}, "app.events", "postgresql", COLUMNS, ["user_id"]
            )
            != DUPLICATE_CLEANUP
        )

    @pytest.mark.parametrize(
        "text",
        [
            "DELETE FROM events WHERE created_at >= $1",  # discourse gamification_scores
            "DELETE FROM events WHERE created_at > $1",
            "DELETE FROM events WHERE $1 < created_at",
            "DELETE FROM events WHERE created_at <> $1",
        ],
    )
    def test_a_newer_than_window_is_not_a_retention_delete(self, text):
        assert _intent(text) != RETENTION_DELETE

    @pytest.mark.parametrize(
        "text",
        [
            'DELETE FROM "events" WHERE ("created_at" <= TIMESTAMP $1)',
            'DELETE FROM "events" WHERE (created_at <= TIMESTAMP $1)',
            "DELETE FROM events WHERE $1 > created_at",
            "DELETE FROM events e WHERE $1 >= e.created_at",
        ],
    )
    def test_an_older_than_cut_off_in_any_form_is_a_retention_delete(self, text):
        assert _intent(text) == RETENTION_DELETE

    def test_a_cut_off_in_a_comment_or_a_string_does_not_count(self):
        assert _intent("DELETE FROM events WHERE note = 'created_at < now' -- created_at < x") != (
            RETENTION_DELETE
        )

    @pytest.mark.parametrize(
        "text",
        [
            "UPDATE events SET status = $1 WHERE id = $2",
            "UPDATE events e SET status = $1 WHERE e.id = $2 AND status = $3",
        ],
    )
    def test_a_single_row_update_by_its_key_is_not_a_bulk_status_flip(self, text):
        assert _intent(text) == SET_BASED_WRITE

    @pytest.mark.parametrize(
        "text",
        [
            "INSERT INTO events (id, note) SELECT id, note FROM staging",
            "DELETE FROM events WHERE note LIKE '%x%'",
            "not sql at all",
        ],
    )
    def test_anything_else_is_a_set_based_write(self, text):
        assert _intent(text) == SET_BASED_WRITE


class TestTheCatalog:
    def test_every_intent_the_classifier_returns_has_a_catalog_file(self):
        assert sorted(INTENTS) == sorted(i.id for i in write_intents())

    def test_every_engine_that_owns_writes_has_an_approach_to_every_intent(self):
        owners = engine_ids(status=ACTIVE, system_of_record=True)
        assert owners  # a write group's engine is always one of them
        for intent in write_intents():
            assert owners <= set(intent.engines), intent.id
            for engine in owners:
                assert intent.engines[engine].approach and intent.engines[engine].docs

    def test_the_dynamodb_approaches_name_the_native_mechanisms(self):
        by_id = {i.id: i.engines["dynamodb"].approach for i in write_intents()}
        assert "Time to Live" in by_id[RETENTION_DELETE]
        for intent in (MISSING_PARENT_CLEANUP, CONSISTENCY_REPAIR):
            assert "TransactWriteItems" in by_id[intent] and "Streams" in by_id[intent]
        assert "sparse GSI" in by_id[CONSISTENCY_REPAIR]


# ---------------------------------------------------------------------------
# Which members are carried: the scorer is stubbed with fixed scores per query
# ---------------------------------------------------------------------------


def _collector(*queries: dict) -> dict:
    return {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {
            "tables": [
                {
                    "table_id": "app.events",
                    "table_name": "events",
                    "primary_key": ["id"],
                    "columns": list(COLUMNS.values()),
                    "indexes": [],
                }
            ]
        },
        "queries": {"query_patterns": list(queries)},
    }


def _q(qid: str, text: str, qtype: str) -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": qtype,
        "calls_per_second": 1.0,
        "tables_accessed": ["app.events"],
    }


KEYED = _q("keyed", "UPDATE events SET note = $1 WHERE id = $2", "UPDATE")
PURGE = _q("purge", "DELETE FROM events WHERE created_at < $1", "DELETE")
FLIP = _q("flip", "UPDATE events SET status = $1 WHERE status = $2", "UPDATE")


def _assignment(groups: dict, engines: dict[str, str]) -> dict:
    return {
        "write_groups": groups,
        "query_assignments": [
            {"query_id": q, "assigned_engine": e, "in_scope": True} for q, e in engines.items()
        ],
    }


@pytest.fixture
def scores(monkeypatch):
    """``query_id -> score`` the stubbed scorer returns on every engine (None: gated)."""
    table: dict[str, float | None] = {}

    def fake(query, engine, ctx, *, exact=False):
        return EngineScore(score=table.get(str(query.get("query_id"))))

    import src.agents.referee.query_scoring as scoring

    monkeypatch.setattr(scoring, "score_query_for_engine", fake)
    return table


def _carried(assignment: dict, *queries: dict) -> list[tuple[str, str, float, str]]:
    found = carried_writes(assignment, _collector(*queries), {"dynamodb": {}}, {})
    return [(c.query_id, c.engine, c.score, c.intent) for c in found]


class TestWhichMembersAreCarried:
    def test_a_member_below_the_floor_on_its_tables_engine(self, scores):
        scores.update(keyed=90, purge=20, flip=45)
        assignment = _assignment(
            {"app.events": ["keyed", "purge", "flip"]},
            {"keyed": "dynamodb", "purge": "dynamodb", "flip": "dynamodb"},
        )
        assert _carried(assignment, KEYED, PURGE, FLIP) == [
            ("flip", "dynamodb", 45.0, BULK_STATUS_FLIP),
            ("purge", "dynamodb", 20.0, RETENTION_DELETE),
        ]

    def test_a_score_at_the_floor_is_not_carried(self, scores):
        floor = carried_write_floor()
        scores.update(keyed=90, purge=floor)
        assignment = _assignment(
            {"app.events": ["keyed", "purge"]}, {"keyed": "dynamodb", "purge": "dynamodb"}
        )
        assert _carried(assignment, KEYED, PURGE) == []
        scores["purge"] = floor - 0.5
        assert [c[0] for c in _carried(assignment, KEYED, PURGE)] == ["purge"]

    def test_a_table_with_one_write_statement_carries_nothing(self, scores):
        scores.update(purge=5)
        assignment = _assignment({"app.events": ["purge"]}, {"purge": "dynamodb"})
        assert _carried(assignment, PURGE) == []

    def test_a_gated_score_and_a_member_placed_elsewhere_are_not_carried(self, scores):
        scores.update(keyed=90, purge=None, flip=10)
        assignment = _assignment(
            {"app.events": ["keyed", "purge", "flip"]},
            {"keyed": "dynamodb", "purge": "dynamodb", "flip": "aurora_postgresql"},
        )
        assert _carried(assignment, KEYED, PURGE, FLIP) == []

    def test_no_write_groups_or_no_collection_carries_nothing(self, scores):
        scores.update(purge=5)
        assert carried_writes({"query_assignments": []}, _collector(PURGE), {}, {}) == []
        assignment = _assignment({"app.events": ["keyed", "purge"]}, {"purge": "dynamodb"})
        assert carried_writes(assignment, None, {}, {}) == []
        assert carried_writes(None, _collector(PURGE), {}, {}) == []

    def test_the_record_carries_the_catalogs_words_for_its_engine(self, scores):
        scores.update(keyed=90, purge=20)
        assignment = _assignment(
            {"app.events": ["keyed", "purge"]}, {"keyed": "dynamodb", "purge": "dynamodb"}
        )
        (record,) = carried_write_records(assignment, _collector(KEYED, PURGE), {}, {})
        assert record["intent"] == RETENTION_DELETE
        assert record["intent_name"] == "Retention delete"
        assert "Time to Live" in record["approach"] and record["docs"]
        assert (record["score"], record["floor"], record["group_size"]) == (
            20.0,
            carried_write_floor(),
            2,
        )
        assert (
            carried_write_records(assignment, _collector(KEYED, PURGE), {}, {}, "documentdb") == []
        )

    def test_the_module_names_no_engine(self):
        import inspect

        source = inspect.getsource(cw)
        for engine in engine_ids(status=ACTIVE):
            assert f'"{engine}"' not in source, engine
