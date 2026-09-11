import pytest

from src.tools.schema.aurora_common.source_family import classify_source_family, migration_strategy


@pytest.mark.parametrize(
    "engine,expected",
    [
        ("postgresql", "postgresql"),
        ("Postgres", "postgresql"),
        ("aurora_postgresql", "postgresql"),
        ("mysql", "mysql"),
        ("MariaDB", "mysql"),
        ("oracle", "other"),
        ("sqlserver", "other"),
        ("", "other"),
    ],
)
def test_classify_source_family(engine, expected):
    assert classify_source_family(engine) == expected


@pytest.mark.parametrize(
    "engine,target,expected",
    [
        ("postgresql", "aurora_postgresql", "carry_over"),
        ("oracle", "aurora_postgresql", "translate"),
        ("sqlserver", "aurora_postgresql", "translate"),
        ("mysql", "aurora_mysql", "carry_over"),
        ("oracle", "aurora_mysql", "translate"),
    ],
)
def test_migration_strategy(engine, target, expected):
    assert migration_strategy(engine, target) == expected
