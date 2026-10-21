"""Model prose cites a query by its report label, never by a bare id (#1321).

A DynamoDB designer wrote "The per-post comment count (query 8c66eaee) runs as a
Query ..." in a trade-off; the text reached report.json and the engineering
report as written, and the concision check (``bare_ids``) failed an e2e-llm run.
The step rewrites each bare id or id prefix of a known query into the label the
report uses (its short id in a code span) where model prose is accepted: the
schema-design output gate, for every designer, and synthesis' apply step.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.referee import post_schema_router, reality_check_handler
from src.agents.referee.reality_check_handler import apply_reality_check_llm_output
from src.agents.referee.synthesis_handler import apply_synthesis_llm_output
from src.agents.schema_design import handler, output_contract
from src.agents.schema_design.output_contract import (
    engine_contract,
    free_form_fields,
    write_schema_output,
)
from src.contracts.model_flag import MAX_EXPLANATION_CHARS
from src.contracts.reality_check_model_response import parse_reality_check_response
from src.shared.query_citations import (
    QueryCitations,
    clip,
    collector_query_ids,
    is_prose_field,
    named_query_ids,
)
from src.storage.local_store import LocalArtifactStore
from tests.report_concision import bare_ids
from tests.unit.agents.schema_design.valid_designs import ddb_design, ddb_pattern, ddb_table

# The two queries of the failing run (job 1d59ac1b).
COUNT = (
    "8c66eaee9125d49703cd068cdba72c305ac38bcbb8e1dab7f399e224cb40a7d6"  # pragma: allowlist secret
)
IN_LIST = (
    "65f7884e6670d34fbefc85ff4977b07d421f3ff1ff9603e001ac2a7575b894ca"  # pragma: allowlist secret
)
# Two queries sharing their first 8 characters: the report names them by full id.
TWIN_A = "abcdef01" + "0" * 56
TWIN_B = "abcdef01" + "1" * 56
KNOWN = {COUNT, IN_LIST, TWIN_A, TWIN_B}
FLAG_REASON = "client_side_join"
# A live PostgreSQL collection's numeric queryid, and a hex id whose first 8
# characters are digits.
NUMERIC = "4503599627370496"
DIGIT_PREFIX = "12345678" + "ab" * 28

# The run's text, word for word.
RUN_DESCRIPTION = (
    "The per-post comment count (query 8c66eaee) runs as a Query with Select=COUNT on "
    "the CommentsByPost index, keyed by comment_post_id and comment_approved."
)
RUN_IMPACT = (
    "Reading a comment with its meta is one Query on one partition instead of a join. "
    "Query 65f7884e reads several comments at once as one Query per comment_id."
)


@pytest.fixture
def cite() -> QueryCitations:
    return QueryCitations(KNOWN)


# --- the rewrite --------------------------------------------------------------


def test_the_runs_text_cites_each_query_by_its_label(cite: QueryCitations) -> None:
    assert cite.text(RUN_DESCRIPTION).startswith(
        "The per-post comment count (query `8c66eaee`) runs as a Query"
    )
    assert "Query `65f7884e` reads several comments" in cite.text(RUN_IMPACT)
    assert bare_ids(cite.text(RUN_DESCRIPTION + " " + RUN_IMPACT), KNOWN) == []
    assert bare_ids(RUN_DESCRIPTION + " " + RUN_IMPACT, KNOWN) == ["65f7884e", "8c66eaee"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"query {COUNT} counts comments", "query `8c66eaee` counts comments"),  # full id
        ("query 8c66eaee counts comments", "query `8c66eaee` counts comments"),  # 8-char prefix
        ("query 8c66eaee9125 counts", "query `8c66eaee` counts"),  # longer prefix
        ("query 8C66EaEe counts comments", "query `8c66eaee` counts comments"),  # mixed case
        (f"Query {COUNT.upper()} counts", "Query `8c66eaee` counts"),  # upper-case full id
        ("(8c66eaee), 65f7884e; and 8c66eaee.", "(`8c66eaee`), `65f7884e`; and `8c66eaee`."),
        ('"8c66eaee" and [65f7884e]', '"`8c66eaee`" and [`65f7884e`]'),
    ],
)
def test_a_bare_citation_becomes_the_label(cite: QueryCitations, text: str, expected: str) -> None:
    assert cite.text(text) == expected
    assert bare_ids(cite.text(text), KNOWN) == []


@pytest.mark.parametrize(
    "text",
    [
        "The hash deadbeef12345678 is not a query of this job.",  # unknown hex token
        "Item key 8c66eaef is another hash.",  # one character off a known prefix
        "query 8c66eae is too short to name one",  # 7 characters: no prefix match
        "cafebabe, decade and facade are plain words",  # ordinary hex words
        "x8c66eaee and 8c66eaee_count are other words",  # the prefix inside a longer word
        "No query is cited here.",
        "",
    ],
)
def test_text_naming_no_known_query_is_left_alone(cite: QueryCitations, text: str) -> None:
    assert cite.text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "query `8c66eaee` counts comments",  # the label already present
        f"query `{COUNT}` counts comments",  # a full id in a code span
        "see:\n```\nSELECT 1 -- 8c66eaee\n```\ndone",  # a fenced block
    ],
)
def test_a_label_or_code_already_present_stays(cite: QueryCitations, text: str) -> None:
    assert cite.text(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "use `foo and 8c66eaee here",  # an unpaired backtick: left whole
        "a `` span and 8c66eaee",  # an unpaired double run
        "``8c66eaee`` counts",  # a double-backtick span is code
        "```8c66eaee``` counts",  # so is a triple one
        "`a`8c66eaee touches a span",  # a word touching a backtick
        "8c66eaee` trails a backtick and `x`",
    ],
)
def test_code_and_unpaired_backticks_are_never_rewritten(cite: QueryCitations, text: str) -> None:
    assert cite.text(text) == text


def test_spans_of_any_length_keep_the_prose_between_them_cited(cite: QueryCitations) -> None:
    text = "``a`b`` then query 8c66eaee, then ```x``` and 65f7884e"
    assert cite.text(text) == "``a`b`` then query `8c66eaee`, then ```x``` and `65f7884e`"


@pytest.mark.parametrize(
    "text",
    [
        "Count the rows of q1 and count them again.",  # short, non-hex ids
        "The table has 45035996 rows.",  # a number that is a numeric id's prefix
        "It ran 12345678 times.",  # a number that is a hex id's digit prefix
        f"About {NUMERIC} rows were read.",  # a number equal to a numeric id
    ],
)
def test_ordinary_words_and_numbers_are_left_alone(text: str) -> None:
    cite = QueryCitations({*KNOWN, "q1", "count", NUMERIC, DIGIT_PREFIX})
    assert cite.text(text) == text


def test_a_number_is_cited_only_right_after_query() -> None:
    """Numeric ids (live PostgreSQL ``queryid``) and digit-only prefixes count only
    in an explicit citation: "query <number>" or "queries <number>"."""
    cite = QueryCitations({*KNOWN, NUMERIC, DIGIT_PREFIX})
    assert cite.text(f"query {NUMERIC} reads it") == "query `45035996` reads it"
    assert cite.text("Queries 45035996 read it") == "Queries `45035996` read it"
    assert cite.text("query 12345678 reads it") == "query `12345678` reads it"
    assert cite.text("query -45035996 reads it") == "query -45035996 reads it"


def test_an_id_that_does_not_look_like_one_never_matches() -> None:
    cite = QueryCitations({"q1", "count", "abc", "not-hex-id-at-all"})
    assert not cite
    assert cite.text("query q1 and count") == "query q1 and count"


_TEXT = st.text(
    alphabet=st.sampled_from(list("` \n.,()-_8c66eaee65f7884eabcdef01query1234x")),
    max_size=80,
) | st.lists(
    st.sampled_from(
        ["`", "``", "```", " ", "query ", "8c66eaee", "65f7884e", COUNT, "abcdef01",
         "deadbeef12", "12345678", "x", ".", "\n"]
    ),
    max_size=20,
).map("".join)  # fmt: skip


@settings(max_examples=400, deadline=None)
@given(_TEXT)
def test_applying_the_step_twice_equals_applying_it_once(text: str) -> None:
    cite = QueryCitations({*KNOWN, DIGIT_PREFIX})
    once = cite.text(text)
    assert cite.text(once) == once


def test_the_step_is_idempotent(cite: QueryCitations) -> None:
    once = cite.text(RUN_DESCRIPTION)
    assert cite.text(once) == once


def test_a_shared_prefix_cites_the_full_id_or_stays_as_written(cite: QueryCitations) -> None:
    # The report names a query whose prefix another shares by its full id.
    assert cite.text(f"query {TWIN_A} reads") == f"query `{TWIN_A}` reads"
    # A prefix both share names neither: it is quoted, not resolved to one.
    assert cite.text("query abcdef01 reads") == "query `abcdef01` reads"
    assert bare_ids(cite.text("query abcdef01 reads"), KNOWN) == []


def test_a_value_with_no_whitespace_is_not_prose(cite: QueryCitations) -> None:
    assert cite.text(COUNT) == COUNT
    assert cite.text("8c66eaee") == "8c66eaee"


def test_walk_rewrites_only_prose_fields(cite: QueryCitations) -> None:
    design: dict[str, Any] = {
        "trade_offs": [
            {"description": RUN_DESCRIPTION, "impact": RUN_IMPACT, "query_ids": [COUNT, IN_LIST]}
        ],
        "notes": ["see query 8c66eaee first"],
        "shard_sizing_rationale": "query 8c66eaee sets the shard count",
        "a_new_field_no_list_names": {"nested": ["see query 8c66eaee first"]},
        "item_size_bytes": 100,
        "validation_passed": True,
        "nothing": None,
    }
    out = cite.walk(design)
    assert out["trade_offs"][0]["query_ids"] == [COUNT, IN_LIST]  # ids stay ids
    assert "query `8c66eaee`" in out["trade_offs"][0]["description"]
    assert out["notes"] == ["see query `8c66eaee` first"]
    assert out["shard_sizing_rationale"] == "query `8c66eaee` sets the shard count"
    # A field that is not prose (a new one included) is written as given.
    assert out["a_new_field_no_list_names"] == design["a_new_field_no_list_names"]
    assert (out["item_size_bytes"], out["validation_passed"], out["nothing"]) == (100, True, None)
    assert design["trade_offs"][0]["description"] == RUN_DESCRIPTION  # input not mutated


CODE_FIELDS: dict[str, Any] = {
    "generated_ddl": "CREATE TABLE t (id INT); -- query 8c66eaee\n",
    "query_text": "SELECT COUNT(*) FROM wp_comments -- 8c66eaee",
    "source_query": "SELECT 1 /* query 8c66eaee */",
    "sql_text": "UPDATE t SET a = 1 -- query 8c66eaee",
    "opensearch_dsl": '{"query": {"term": {"id": "8c66eaee"}}} query 8c66eaee',
    "lua_script": "return redis.call('GET', KEYS[1]) -- query 8c66eaee",
    "command_example": "GET user:1 # query 8c66eaee",
    "key_condition": "PK = :id AND query 8c66eaee",
    "condition_expression": "attribute_exists(id) query 8c66eaee",
    "foreign_keys": ["ALTER TABLE t ADD FOREIGN KEY -- query 8c66eaee"],
    "pk_template": "POST#{id} query 8c66eaee",
    # Aurora: CREATE INDEX statements.
    "indexes": ["CREATE INDEX idx_a ON t (a) WHERE k = 'x' -- query 8c66eaee"],
    # DocumentDB: filters, projections, sorts, pipelines and examples (free-form data).
    "query_filter": {"$comment": "serves query 8c66eaee", "description": "query 8c66eaee x"},
    "projection": {"_id": 0, "note": "query 8c66eaee"},
    "sort": {"created": -1, "$comment": "query 8c66eaee"},
    "pipeline": [{"$match": {"$expr": "query 8c66eaee and more"}}],
    "partial_filter": {"$comment": "query 8c66eaee x"},
    "document_examples": [{"description": "query 8c66eaee in sample data"}],
}


def test_code_fields_are_written_byte_for_byte(cite: QueryCitations) -> None:
    design = {"tables": [{"table_name": "t", **CODE_FIELDS, "description": "query 8c66eaee reads"}]}
    out = cite.walk(design, keep=free_form_fields())
    table = out["tables"][0]
    assert {k: table[k] for k in CODE_FIELDS} == CODE_FIELDS
    assert table["description"] == "query `8c66eaee` reads"


def test_the_free_form_fields_are_read_from_the_contracts() -> None:
    assert {
        "query_filter",
        "projection",
        "sort",
        "pipeline",
        "partial_filter",
        "document_examples",
    } <= free_form_fields()


@pytest.mark.parametrize(
    "key",
    ["description", "impact", "reason", "workaround", "recommendation", "rationale",
     "application_logic_required", "mitigation", "justification", "explanation", "details",
     "out_of_scope_reason", "shard_sizing_rationale", "eviction_notes", "executive_summary"],
)  # fmt: skip
def test_each_prose_field_is_recognised(key: str) -> None:
    assert is_prose_field(key)


@pytest.mark.parametrize("key", [*CODE_FIELDS, "where", "join_conditions", "contract_version"])
def test_no_code_field_is_taken_for_prose(key: str) -> None:
    assert not is_prose_field(key)


SAMPLE = "see query 8c66eaee here"


def _filled(schema: dict, defs: dict, seen: tuple[str, ...] = ()) -> Any:
    """An instance of ``schema`` with every string set to ``SAMPLE`` (validity aside)."""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return None if name in seen else _filled(defs[name], defs, (*seen, name))
    for key in ("anyOf", "oneOf", "allOf"):
        options = [o for o in schema.get(key) or [] if o.get("type") != "null"]
        if options:
            return _filled(options[0], defs, seen)
    kind = schema.get("type")
    if kind == "string":
        return SAMPLE
    if kind == "array":
        return [_filled(schema.get("items") or {}, defs, seen)]
    if kind == "object" or "properties" in schema:
        props = schema.get("properties")
        if not props:  # free-form data, with a prose-looking key inside
            return {"description": SAMPLE, "$comment": SAMPLE}
        return {n: _filled(p, defs, seen) for n, p in props.items()}
    return None


def _leaves(value: Any, path: tuple[str, ...] = ()) -> dict[tuple[str, ...], str]:
    if isinstance(value, str):
        return {path: value}
    if isinstance(value, dict):
        return {p: v for k, item in value.items() for p, v in _leaves(item, (*path, k)).items()}
    if isinstance(value, list):
        return {
            p: v for i, item in enumerate(value) for p, v in _leaves(item, (*path, str(i))).items()
        }
    return {}


@pytest.mark.parametrize(
    "engine",
    ["dynamodb", "documentdb", "opensearch", "elasticache", "aurora_postgresql", "aurora_mysql"],
)
def test_every_non_prose_string_of_each_designer_contract_is_byte_identical(
    tmp_path, engine: str
) -> None:
    """Every string field of the contract, filled with a citation: only a prose field
    outside free-form data is rewritten; every other one comes back byte for byte."""
    contract = engine_contract(engine)
    assert contract is not None
    schema = contract.model_json_schema()
    design = _filled(schema, schema.get("$defs") or {})
    out = output_contract.cite_queries(
        _store(tmp_path), f"{DB}/{JOB}/schema-{engine}/v1/x.json", design
    )
    before, after = _leaves(design), _leaves(out)
    assert before.keys() == after.keys()
    free = free_form_fields()
    rewritten = set()
    for path, value in before.items():
        names = [p for p in path if not p.isdigit()]
        prose = bool(names) and is_prose_field(names[-1]) and not free & set(names)
        prose = prose and "validation_failures" not in names
        if prose:
            assert after[path] == "see query `8c66eaee` here", path
            rewritten.add(names[-1])
        else:
            assert after[path] == value, path
    assert rewritten, engine  # each contract has prose fields
    assert all(is_prose_field(k) for k in rewritten)


@pytest.mark.passes_on_main
def test_an_aurora_designs_ddl_is_byte_identical_through_the_gate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(output_contract, "_gate_issues", lambda *_a, **_k: [])
    store = _store(tmp_path)
    key = f"{DB}/{JOB}/schema-aurora_mysql/v1/schema_output.json"
    ddl = "CREATE TABLE `wp_comments` (\n  `id` BIGINT -- query 8c66eaee\n);\n"
    index = "CREATE INDEX idx_post ON wp_comments (post_id) -- query 8c66eaee"
    design = {
        "target_engine": "aurora_mysql",
        "generated_ddl": ddl,
        "table_definitions": [
            {"table_name": "wp_comments", "generated_ddl": ddl, "indexes": [index]}
        ],
        "optimizations": [{"rationale": "Query 8c66eaee reads by post."}],
    }
    write_schema_output(store, key, design, "aurora_mysql")
    raw = (tmp_path / key).read_text(encoding="utf-8")
    written = json.loads(raw)
    assert written["generated_ddl"] == ddl
    assert written["table_definitions"][0]["generated_ddl"] == ddl
    assert written["table_definitions"][0]["indexes"] == [index]
    assert json.dumps(ddl)[1:-1] in raw
    assert written["optimizations"][0]["rationale"] == "Query `8c66eaee` reads by post."


# --- the cut to a flag's length ---------------------------------------------------


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        ("short", 300, "short"),
        ("abc query `8c66eaee`", 17, "abc query"),  # a cut inside a label drops it
        ("abc query `8c66eaee` tail", 20, "abc query `8c66eaee`"),  # a cut after it keeps it
        ("a ``b`` c `d`", 9, "a ``b`` c"),
        ("stray ` backtick and more", 10, "stray ` ba"),  # unpaired already: a plain cut
    ],
)
def test_clip_never_leaves_an_unpaired_backtick(text: str, limit: int, expected: str) -> None:
    assert clip(text, limit) == expected


def test_a_designer_reason_cut_for_its_flag_keeps_no_half_label(tmp_path) -> None:
    """A designer's unsupported-pattern reason becomes a flag explanation cut to the
    flag's limit; after the gate wrote a label at its end, the cut drops the label
    whole instead of leaving "query `8c66eae"."""
    head = "x" * (MAX_EXPLANATION_CHARS - len(" query 8c66eaee")) + " query 8c66eaee"
    assert len(head) == MAX_EXPLANATION_CHARS
    store = _store(tmp_path)
    key = f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json"
    design = {
        **_ddb_design(),
        "unsupported_patterns": [
            {"pattern_type": "join", "query_ids": [COUNT], "reason": head, "workaround": "w"}
        ],
    }
    gated = output_contract.cite_queries(store, key, design)
    reason = gated["unsupported_patterns"][0]["reason"]
    assert len(reason) == MAX_EXPLANATION_CHARS + 2
    (flag,) = post_schema_router._unsupported_as_flags(gated, "dynamodb")
    assert len(flag.explanation) <= MAX_EXPLANATION_CHARS
    assert "`" not in flag.explanation
    assert flag.explanation == head[: -len(" 8c66eaee")]


def test_no_known_query_changes_nothing() -> None:
    assert QueryCitations([]).text(RUN_DESCRIPTION) == RUN_DESCRIPTION


def test_the_job_query_ids_come_from_the_collection_and_the_id_fields() -> None:
    collector = {"queries": {"query_patterns": [{"query_id": COUNT}, {"query_id": None}, "x"]}}
    assert collector_query_ids(collector) == {COUNT}
    assert collector_query_ids(None) == set()
    named = {"a": [{"query_id": IN_LIST}], "b": {"source_query_ids": [TWIN_A]}, "query_ids": [1]}
    assert named_query_ids(named) == {IN_LIST, TWIN_A}


# --- every designer's output passes through the step ---------------------------

DB, JOB = "wordpress", "job-001"
SOURCE = "wordpress.wp_comments"


def _store(tmp_path, *, with_collector: bool = True) -> LocalArtifactStore:
    store = LocalArtifactStore(str(tmp_path))
    if with_collector:
        store.write_json(
            f"{DB}/{JOB}/collector/output.json",
            {"queries": {"query_patterns": [{"query_id": q} for q in sorted(KNOWN)]}},
        )
    return store


def _ddb_design() -> dict:
    return ddb_design(
        [ddb_table("Comments", SOURCE, pk="comment_id")],
        [ddb_pattern("DDB-AP-1", "Comments", SOURCE, COUNT, description=RUN_DESCRIPTION)],
        trade_offs=[{"description": RUN_DESCRIPTION, "impact": RUN_IMPACT}],
        job_id=JOB,
        database=DB,
    )


def test_the_gate_writes_the_label_into_a_dynamodb_design(tmp_path) -> None:
    store = _store(tmp_path)
    key = f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json"
    write_schema_output(store, key, _ddb_design(), "dynamodb")
    written = store.read_json(key)
    text = json.dumps(written["trade_offs"]) + json.dumps(written["access_patterns"][0])
    assert "query `8c66eaee`" in written["trade_offs"][0]["description"]
    assert "Query `65f7884e`" in written["trade_offs"][0]["impact"]
    assert "query `8c66eaee`" in written["access_patterns"][0]["description"]
    assert written["access_patterns"][0]["query_ids"] == [COUNT]
    assert set(bare_ids(text, KNOWN)) <= {COUNT}  # only the id field holds the id


def test_without_a_collection_the_designs_own_query_ids_are_known(tmp_path) -> None:
    store = _store(tmp_path, with_collector=False)
    key = f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json"
    write_schema_output(store, key, _ddb_design(), "dynamodb")
    written = store.read_json(key)
    # COUNT is in the design's query_ids; IN_LIST is not, so it is not known.
    assert "query `8c66eaee`" in written["trade_offs"][0]["description"]
    assert "Query 65f7884e reads" in written["trade_offs"][0]["impact"]


def test_finalize_writes_the_label_into_the_design(tmp_path) -> None:
    store = _store(tmp_path)
    store.write_json(f"{DB}/{JOB}/llm_responses/schema_design_dynamodb.json", _ddb_design())
    result = handler.finalize_schema_design(JOB, DB, "dynamodb", store, 0)
    written = store.read_json(result["output_path"])
    assert "query `8c66eaee`" in written["trade_offs"][0]["description"]
    assert "Query `65f7884e`" in written["trade_offs"][0]["impact"]


@pytest.mark.parametrize(
    "engine",
    ["dynamodb", "documentdb", "opensearch", "elasticache", "aurora_postgresql", "aurora_mysql"],
)
def test_every_designers_output_passes_through_the_step(tmp_path, monkeypatch, engine) -> None:
    """Every designer writes through ``write_schema_output`` (#1186's guard test), and
    the gate cites queries whatever the engine. The contract check itself is the
    gate's own tests' business, so it is passed here."""
    assert engine_contract(engine) is not None
    monkeypatch.setattr(output_contract, "_gate_issues", lambda *_a, **_k: [])
    store = _store(tmp_path)
    key = f"{DB}/{JOB}/schema-{engine}/v1/schema_output.json"
    design = {
        "target_engine": engine,
        "trade_offs": [{"description": RUN_DESCRIPTION, "impact": RUN_IMPACT}],
        "migration_notes": [{"application_logic_required": "Rewrite query 65f7884e in code."}],
    }
    write_schema_output(store, key, design, engine)
    written = store.read_json(key)
    assert bare_ids(json.dumps(written), KNOWN) == []
    assert "`65f7884e`" in written["migration_notes"][0]["application_logic_required"]


# --- synthesis' apply step -----------------------------------------------------


def _det() -> dict:
    return {
        "database_name": DB,
        "summary": "Deterministic summary of the plan.",
        "table_mappings": [],
        "known_tables": [],
        "query_traces": {"queries": [{"query_id": COUNT}, {"query_id": IN_LIST}]},
    }


def test_synthesis_cites_queries_in_the_model_summary() -> None:
    det = _det()
    summary = "DynamoDB serves the comment reads; query 8c66eaee counts comments per post."
    result = apply_synthesis_llm_output(det, {"executive_summary": summary})
    assert result["summary_source"] == "llm"
    assert result["executive_summary"] == (
        "DynamoDB serves the comment reads; query `8c66eaee` counts comments per post."
    )
    assert result["summary_llm"] == summary  # kept as received, for audit


def test_synthesis_leaves_an_unknown_hash_in_the_summary() -> None:
    summary = "DynamoDB serves the comment reads; build deadbeef12 ships the change."
    result = apply_synthesis_llm_output(_det(), {"executive_summary": summary})
    assert result["executive_summary"] == summary


# --- the prompts ------------------------------------------------------------------

SKILLS = Path(__file__).resolve().parents[3] / "src" / "skills"


@pytest.mark.parametrize(
    "skill",
    [
        "dynamodb-data-modeling",
        "documentdb-data-modeling",
        "opensearch-index-modeling",
        "elasticache-data-modeling",
        "aurora_postgresql-data-modeling",
        "aurora_mysql-data-modeling",
        "executive-summary",
    ],
)
def test_every_prose_prompt_says_to_cite_queries_by_label(skill: str) -> None:
    text = (SKILLS / f"{skill}.md").read_text(encoding="utf-8")
    assert "Cite a query by its label" in text
    assert "never by a bare id or id prefix" in text


def test_the_gates_own_verdict_is_kept_as_written(tmp_path) -> None:
    store = _store(tmp_path)
    key = f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json"
    failure = "design_metrics: design_rps disagrees for query 8c66eaee"
    design = {**_ddb_design(), "validation_passed": False, "validation_failures": [failure]}
    write_schema_output(store, key, design, "dynamodb")
    assert store.read_json(key)["validation_failures"] == [failure]


# --- the reality check's apply step ----------------------------------------------


def _rc_det() -> dict:
    return {
        "collector_output": {"queries": {"query_patterns": [{"query_id": COUNT}]}},
        "assignment": {"query_assignments": [{"query_id": IN_LIST}]},
        "executive_summary": None,
    }


def test_the_reality_check_cites_queries_in_the_model_summary() -> None:
    summary = "The consolidation moves query 8c66eaee and query 65f7884e onto DynamoDB."
    result = apply_reality_check_llm_output(_rc_det(), {"executive_summary": summary})
    assert result["executive_summary"] == (
        "The consolidation moves query `8c66eaee` and query `65f7884e` onto DynamoDB."
    )


def _flag(explanation: str) -> dict:
    return {
        "query_ids": [COUNT],
        "flagged_engine": "dynamodb",
        "reason_category": FLAG_REASON,
        "explanation": explanation,
    }


def _applied_flags(monkeypatch, explanation: str) -> tuple[list, dict]:
    """The flags the reality check applies for one model flag, and the result."""
    applied: list = []
    monkeypatch.setattr(
        reality_check_handler, "_apply_model_flags", lambda _r, flags: applied.extend(flags)
    )
    result = apply_reality_check_llm_output(
        _rc_det(), {"consolidation_corrections": [_flag(explanation)]}
    )
    return applied, result


@pytest.mark.passes_on_main
def test_a_flag_at_the_length_limit_citing_a_prefix_stays_accepted(monkeypatch) -> None:
    """ADR-031 Decision 11: the flag is checked as the model wrote it. Its label
    would make it 302 characters; it is never rewritten, so it still counts."""
    head = "Query 8c66eaee needs a join. "
    explanation = head + "x" * (MAX_EXPLANATION_CHARS - len(head))
    assert len(explanation) == MAX_EXPLANATION_CHARS
    applied, result = _applied_flags(monkeypatch, explanation)
    assert [f.explanation for f in applied] == [explanation]
    assert not result.get("model_flags_rejected")


@pytest.mark.passes_on_main
def test_a_flag_over_the_limit_citing_a_full_id_stays_rejected(monkeypatch) -> None:
    """Its label would bring it under the limit; it is never rewritten, so it is
    still rejected."""
    head = f"Query {COUNT} needs a join. "
    explanation = head + "x" * (315 - len(head))
    assert len(explanation) == 315
    applied, result = _applied_flags(monkeypatch, explanation)
    assert applied == []
    assert result["model_flags_rejected"] == 1


def test_the_reality_check_leaves_each_flag_as_written() -> None:
    response = parse_reality_check_response(
        {"consolidation_corrections": [_flag("Query 8c66eaee needs a join.")]}
    )
    cited = reality_check_handler._cite_queries(_rc_det(), response)
    assert cited.consolidation_corrections == response.consolidation_corrections


def test_the_reality_check_leaves_an_unknown_hash_alone() -> None:
    summary = "The consolidation keeps build deadbeef12 and moves nothing else."
    result = apply_reality_check_llm_output(_rc_det(), {"executive_summary": summary})
    assert result["executive_summary"] == summary
