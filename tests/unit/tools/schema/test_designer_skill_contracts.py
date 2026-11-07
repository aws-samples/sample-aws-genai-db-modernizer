# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each schema designer skill asks only for what its output contract accepts.

A designer that follows its skill writes the values and fields the skill names. A
value the contract's enum lacks (a ``SETEX`` operation, a ``"text"`` index key, a
``write_behind`` invalidation the owner contradicts) fails validation: a
structured-output re-ask of a whole design turn on Bedrock, a spent ``--finalize``
attempt on the headless path, or a delta merge error and a correction turn on
Aurora. The contracts, the merge and synthesis are the source of truth; these
tests read the DocumentDB, OpenSearch, ElastiCache and Aurora designer skills
against them. Nothing here calls a model.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from src.agents.referee.synthesis_prose import clean_schema_design_prose
from src.contracts import aurora_design_delta as delta
from src.contracts import aurora_postgresql_model_output as aurora_pg
from src.contracts import documentdb_model_output as docdb
from src.contracts import elasticache_model_output as ec
from src.contracts import opensearch_model_output as os_out
from src.contracts.schema_design_output import TradeOff
from src.tools.schema import (
    aurora_mysql_schema_agent,
    aurora_postgresql_schema_agent,
    documentdb_schema_agent,
    elasticache_schema_agent,
    opensearch_schema_agent,
)

#: The skill each designer loads, read through the designer module's own path.
SKILLS: dict[str, str] = {
    "documentdb": documentdb_schema_agent.DEFAULT_SKILL_PATH,
    "opensearch": opensearch_schema_agent.DEFAULT_SKILL_PATH,
    "elasticache": elasticache_schema_agent.DEFAULT_SKILL_PATH,
    "aurora_mysql": aurora_mysql_schema_agent.DEFAULT_SKILL_PATH,
    "aurora_postgresql": aurora_postgresql_schema_agent.DEFAULT_SKILL_PATH,
}
ENGINES = tuple(SKILLS)
AURORA = ("aurora_mysql", "aurora_postgresql")

#: Per engine: the contract model behind each customer-facing list a design carries.
CUSTOMER_LISTS: dict[str, dict[str, type[BaseModel]]] = {
    "documentdb": {
        "trade_offs": TradeOff,
        "access_patterns": docdb.AccessPattern,
        "unsupported_patterns": docdb.UnsupportedPattern,
        "migration_notes": docdb.MigrationNote,
    },
    "opensearch": {
        "trade_offs": TradeOff,
        "access_patterns": os_out.AccessPattern,
        "unsupported_patterns": os_out.UnsupportedPattern,
    },
    "elasticache": {
        "trade_offs": TradeOff,
        "access_patterns": ec.AccessPattern,
        "unsupported_patterns": ec.UnsupportedPattern,
        "migration_notes": ec.MigrationNote,
    },
    **{
        engine: {
            "trade_offs": TradeOff,
            "optimizations": aurora_pg.Optimization,
            "app_layer_notes": aurora_pg.AppLayerNote,
        }
        for engine in AURORA
    },
}
#: How a skill names each list in its "reach the customer's report" sentence.
LIST_LABELS = {
    "trade_offs": "Trade-offs",
    "access_patterns": "access patterns",
    "unsupported_patterns": "unsupported patterns",
    "migration_notes": "migration notes",
    "optimizations": "optimizations",
    "app_layer_notes": "app-layer notes",
}


def _skill(engine: str) -> str:
    return Path(SKILLS[engine]).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _literal_values(annotation: Any) -> set[str]:
    values: set[str] = set()
    for arg in typing.get_args(annotation):
        if isinstance(arg, str):
            values.add(arg)
        elif arg is not None:
            values |= _literal_values(arg)
    return values


def _code_spans(text: str) -> list[str]:
    return re.findall(r"`([^`]+)`", text)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    following = re.search(r"\n#{2,3} ", text[start + len(heading) :])
    return text[start : start + len(heading) + following.start()] if following else text[start:]


# ---------------------------------------------------------------------------
# Every designer skill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "engine",
    [
        pytest.param(e, marks=pytest.mark.passes_on_main) if e in AURORA else e  # never had them
        for e in ENGINES
    ],
)
def test_no_dated_scaffolding(engine: str) -> None:
    """Structured output decides what a designer returns, and the contract module states
    its own version, so these lines only contradict or go stale."""
    text = _flat(_skill(engine))
    for dated in (
        "Silent Analysis",
        "silently",
        "Do not output anything",
        "MUST be a JSON object",
        "MUST be a structured object",
        "MUST be valid JSON",
    ):
        assert dated not in text, (engine, dated)
    assert not re.search(r"contract v\d", text), engine


def _customer_fields_named(engine: str) -> set[tuple[str, str]]:
    text = _flat(_skill(engine))
    sentence = next(s for s in re.split(r"(?<=\.) ", text) if "reach the customer's report" in s)
    named: set[tuple[str, str]] = set()
    for key, label in LIST_LABELS.items():
        match = re.search(re.escape(label) + r" \(([^)]*)\)", sentence)
        if match:
            named |= {(key, field) for field in _code_spans(match.group(1))}
    return named


def _customer_fields_cleaned(engine: str) -> set[tuple[str, str]]:
    """The model-written fields synthesis checks against the plain-prose style list,
    found by running ``clean_schema_design_prose`` over a design that breaks the list
    in every string field of every customer-facing list."""
    lists = CUSTOMER_LISTS[engine]
    design = {
        key: [
            {
                name: f"We leverage {name}."
                for name, field in model.model_fields.items()
                if field.annotation in (str, str | None)
            }
        ]
        for key, model in lists.items()
    }
    cleaned, _ = clean_schema_design_prose(engine, design)
    assert cleaned is not None
    return {
        (key, name)
        for key, entries in design.items()
        for name, value in entries[0].items()
        if cleaned[key][0][name] != value
    }


@pytest.mark.parametrize("engine", ENGINES)
def test_the_customer_facing_fields_are_the_ones_synthesis_checks(engine: str) -> None:
    """The skill names exactly the fields of its own contract that reach the report
    through the plain-prose check, no field another engine has."""
    assert _customer_fields_named(engine) == _customer_fields_cleaned(engine)


# ---------------------------------------------------------------------------
# DocumentDB
# ---------------------------------------------------------------------------


def _bullet(text: str, field: str) -> str:
    return next(line for line in text.splitlines() if line.startswith(f"- `{field}`:"))


def test_documentdb_names_exactly_the_contract_operations() -> None:
    line = _bullet(_skill("documentdb"), "operation")
    camel = {s for s in _code_spans(line) if re.fullmatch(r"[a-z]+[A-Z]\w*|find|aggregate", s)}
    assert camel == _literal_values(docdb.DOCDB_OPERATION)


def test_documentdb_names_exactly_the_contract_index_types() -> None:
    text = _flat(_skill("documentdb"))
    listed = re.search(r"`index_type` is one of (.*?):", text)
    assert listed, "the skill lists the index types"
    assert set(_code_spans(listed.group(1))) == _literal_values(docdb.INDEX_TYPE)
    assert 'never a string such as `"text"`' in text


def test_documentdb_names_exactly_the_migration_object_types() -> None:
    line = _flat(
        next(ln for ln in _skill("documentdb").splitlines() if "`migration_notes` —" in ln)
    )
    listed = re.search(r"`object_type` \(([^)]*)\)", line)
    assert listed
    allowed = _literal_values(docdb.MigrationNote.model_fields["object_type"].annotation)
    assert set(_code_spans(listed.group(1))) == allowed


@pytest.mark.passes_on_main  # a guard: the contract accepts the form the skill now asks for
def test_a_documentdb_index_as_the_skill_writes_it_validates() -> None:
    for index in (
        {"index_name": "idx_posts_text", "keys": {"title": 1, "body": 1}, "index_type": "text"},
        {
            "index_name": "idx_users_email",
            "keys": {"email": 1},
            "index_type": "partial",
            "partial_filter": {"email": {"$exists": True}},
        },
    ):
        docdb.IndexDefinition.model_validate({**index, "purpose": "p"})


@pytest.mark.passes_on_main  # a guard: the contract already refuses a string key
def test_a_documentdb_text_key_string_is_refused() -> None:
    with pytest.raises(ValueError):
        docdb.IndexDefinition.model_validate(
            {"index_name": "i", "keys": {"title": "text"}, "index_type": "text", "purpose": "p"}
        )


# ---------------------------------------------------------------------------
# ElastiCache
# ---------------------------------------------------------------------------


def _use_case_table(text: str) -> list[list[str]]:
    rows = [ln for ln in text.splitlines() if ln.startswith("| ") and "`" in ln]
    cells = [[cell.strip() for cell in row.strip("|").split("|")] for row in rows]
    return [row for row in cells if row[1] != "`use_case`"]  # past the header


def test_elasticache_names_exactly_the_contract_use_cases_and_data_types() -> None:
    text = _skill("elasticache")
    table = _use_case_table(_section(text, "### Phase 1"))
    use_cases = {span for row in table for span in _code_spans(row[1])}
    data_types = {span for row in table for span in _code_spans(row[2])}
    others = re.search(r"The contract's other values are (.*)", text)
    assert others
    other_spans = re.findall(r"`([^`]+)` \(`(use_case|data_type)`\)|`([^`]+)`", others.group(1))
    extra = [a or c for a, _, c in other_spans if (a or c) not in ("use_case", "data_type")]
    allowed_use_cases = _literal_values(ec.USE_CASE_TYPE)
    allowed_types = _literal_values(ec.REDIS_DATA_TYPE)
    assert use_cases <= allowed_use_cases and data_types <= allowed_types
    assert use_cases | data_types | set(extra) == allowed_use_cases | allowed_types


def test_elasticache_commands_are_contract_operations() -> None:
    section = _section(_skill("elasticache"), "### Phase 3")
    commands = {
        span.split()[0]
        for span in _code_spans(section)
        if re.fullmatch(r"[A-Z][A-Z.]+", span.split()[0])
    }
    assert commands - {"SETEX"} <= _literal_values(ec.REDIS_OPERATION)
    assert "never `SETEX`" in _flat(section)
    assert "SETEX" not in _literal_values(ec.REDIS_OPERATION)


def test_elasticache_invalidation_strategies_follow_the_contract_and_the_owner() -> None:
    section = _section(_skill("elasticache"), "### Phase 4")
    listed = set(re.findall(r"^\s+- `(\w+)`:", section, re.M))
    allowed = _literal_values(ec.CacheInvalidationStrategy.model_fields["strategy"].annotation)
    assert listed == allowed - {"write_behind"}
    assert "`write_behind`; never use it" in _flat(section)


def test_elasticache_asks_for_one_access_pattern_per_query_pattern() -> None:
    """ "At least one access pattern per source query" contradicted "design each pattern
    once" for a query pattern group."""
    text = _flat(_skill("elasticache"))
    assert "per source query" not in text
    assert "one access pattern per query pattern" in text


# ---------------------------------------------------------------------------
# Aurora
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", AURORA)
def test_aurora_optimization_categories_are_the_contracts(engine: str) -> None:
    """Every category is named; ``io_optimized`` only as never used, since the storage
    configuration comes from the pipeline's cost estimate."""
    text = _flat(_skill(engine))
    listed = re.search(r"with a `category` of (.*?), a `target`", text)
    assert listed
    allowed = _literal_values(aurora_pg.Optimization.model_fields["category"].annotation)
    assert set(_code_spans(listed.group(1))) == allowed - {"io_optimized"}
    assert "The contract also has `io_optimized`; never use it" in text


def test_aurora_mysql_never_asks_for_a_postgresql_only_index_field() -> None:
    """The merge refuses ``method``, ``include`` and ``where`` on Aurora MySQL, so the
    skill must not send a ``not_null_test`` to a partial index there."""
    text = _flat(_skill("aurora_mysql"))
    assert "or a partial index" not in text
    assert "Aurora MySQL has no partial indexes" in text
    assert "takes no `method`, `include` or `where`" in text


def test_aurora_postgresql_names_the_contract_index_methods() -> None:
    text = _flat(_skill("aurora_postgresql"))
    listed = re.search(r"also takes `method` \((.*?); only `btree`", text)
    assert listed
    assert set(_code_spans(listed.group(1))) == set(delta.INDEX_METHODS)
    assert "`where` set to `<column> IS NOT NULL`" in text


@pytest.mark.passes_on_main  # a guard: the contract accepts the entries the skills now describe
def test_the_index_entries_the_aurora_skills_describe_merge() -> None:
    """The skills' index advice, run through the delta contract: a PostgreSQL partial
    index with ``where`` validates; MySQL's plain index carries none of those fields."""
    pg = delta.IndexSpec.model_validate(
        {"index_name": "idx_t_c", "columns": ["c"], "where": "c IS NOT NULL", "method": "btree"}
    )
    mysql = delta.IndexSpec.model_validate({"index_name": "idx_t_c", "columns": ["c"]})
    assert pg.where and mysql.where is None and mysql.method is None


@pytest.mark.passes_on_main  # a guard: the merge already behaves as the skill now says
def test_a_postgresql_modify_with_no_method_is_a_btree() -> None:
    """The skill says a ``modify_indexes`` entry replaces the whole draft definition, so
    one that leaves out the draft's ``method`` turns a GIN index into a B-tree."""
    from src.tools.schema.aurora_common.delta_merge import merge_design_delta
    from tests.unit.tools.schema.aurora_common.test_draft_indexes import (
        _pg_base_with_gin_indexes,
    )

    base = _pg_base_with_gin_indexes()

    def ddl(spec: dict) -> list[str]:
        merged = merge_design_delta(
            base,
            {
                "delta_version": "1.0",
                "tables": [{"table_name": "topics", "modify_indexes": [spec]}],
            },
        )
        assert merged.errors == []
        return [ln for ln in merged.output["generated_ddl"].splitlines() if "idx_body" in ln]

    assert ddl({"index_name": "idx_body", "columns": ["body"]}) == [
        'CREATE INDEX "idx_body" ON "topics" ("body");'
    ]
    assert ddl({"index_name": "idx_body", "columns": ["body"], "method": "gin"}) == [
        'CREATE INDEX "idx_body" ON "topics" USING gin ("body");'
    ]


@pytest.mark.passes_on_main  # a guard: the contract already refuses these
@pytest.mark.parametrize(
    ("value", "engine"),
    [
        ("DECIMAL(66,0)", "aurora_mysql"),
        ("DECIMAL(10,30)", "aurora_mysql"),
        ("ENUM('a','b')", "aurora_postgresql"),
    ],
)
def test_the_aurora_types_the_skills_rule_out_are_refused(value: str, engine: str) -> None:
    with pytest.raises(ValueError):
        delta.validate_aurora_type(value, engine)


@pytest.mark.passes_on_main  # a guard: the contract already types these fields
@pytest.mark.parametrize(
    ("field", "value"),
    [("example_value", {"name": "x"}), ("estimated_avg_value_bytes", 1.5)],
)
def test_the_elasticache_key_design_types_the_skill_states(field: str, value: Any) -> None:
    """``example_value`` is a string (a JSON string for a hash or JSON value), and the
    size estimates are whole numbers."""
    key = {
        "key_pattern": "users:{id}",
        "data_type": "hash",
        "use_case": "caching",
        "source_tables": ["users"],
        "rationale": "r",
        "example_key": "users:1",
        "example_value": '{"name": "x"}',
    }
    ec.KeyDesign.model_validate(key)
    with pytest.raises(ValueError):
        ec.KeyDesign.model_validate({**key, field: value})
