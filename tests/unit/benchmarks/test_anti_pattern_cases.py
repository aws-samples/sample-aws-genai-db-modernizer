"""Building the anti_pattern cases and their blind review sheets (#1084).

The sheets must carry no answer: no engine, no score, no marker, column value or
row position that separates the rows the pipeline flagged from the decoys, and
empty answer columns. The builder must never write or rewrite a key, and the
committed case membership must follow the sweep rule.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from pathlib import Path

import pytest

from benchmarks.runner import anti_pattern_cases as apc
from benchmarks.runner.loader import load_cases
from benchmarks.runner.review_sheet import build_rows
from benchmarks.runner.stage_anti_pattern import pipeline_flags, read_analyses
from tests.unit.benchmarks.test_review_sheet import ENGINE_WORDS, _engine_words, _q, _rows

REPO = Path(__file__).resolve().parents[3]
STAGE_DIR = REPO / "benchmarks" / "cases" / "anti_pattern"
CASES = [c.case_id for c in load_cases(STAGE_DIR)]
SAMPLES = sorted({c.sample for c in load_cases(STAGE_DIR)})
ANSWER_COLUMNS = 4  # shows, acceptable, ideal, rationale
COLUMNS = {"op": 3, "tables": 4, "tier": 7, "groups": 8, "facts": 9}


def row_features(row: list[str]) -> dict[str, object]:
    """A sheet row's low-cardinality features: what a reviewer could sort rows by
    without reading the query (the per-row numbers and the SQL are left out)."""
    groups = row[COLUMNS["groups"]]
    facts = frozenset(
        t.strip()
        for t in row[COLUMNS["facts"]].split(",")
        if t.strip() and not t.strip().startswith("rows~") and not t.strip().endswith("ms")
    )
    return {
        "op": row[COLUMNS["op"]],
        "tier": row[COLUMNS["tier"]],
        "co_dependency": "CD" in groups,
        "write_group": "W:" in groups,
        "facts": facts,
    }


def test_every_penalised_pattern_has_an_engine_free_definition():
    assert sorted(apc.DEFINITIONS) == apc.penalised_patterns()
    for pattern_id, text in apc.DEFINITIONS.items():
        assert not ENGINE_WORDS.search(text), pattern_id


def _ap(qid, cls):
    return {"query_id": qid, "class": cls, "reason": f"{cls} reason", "facts": []}


def test_decoys_match_each_deciding_query_on_kind_then_class_then_tables_then_rate():
    queries = [
        _q("d1", "SELECT * FROM t", tables=("db.t",), calls=5.0),
        _q("f1", "SELECT * FROM t", tables=("db.t",), calls=9.0),  # flagged: never a decoy
        _q("s1", "SELECT * FROM t ORDER BY a", tables=("db.t",), calls=0.01),
        _q("s2", "SELECT * FROM u", tables=("db.u",), calls=4.0),
        _q("s3", "SELECT * FROM v", tables=("db.v",), calls=500.0),
        _q("k1", "SELECT * FROM t WHERE id = ?", tables=("db.t",), calls=5.0),
        _q("w1", "UPDATE t SET a = ?", qtype="UPDATE", tables=("db.t",), calls=5.0),
        _q("x1", "SHOW TABLES", qtype="OTHER", tables=("unknown",), calls=5.0),
    ]
    classes = {q["query_id"]: "full_scan" for q in queries}
    classes.update({"k1": "key_lookup", "x1": "utility"})
    # same class first; within it the same table, then the nearest call rate
    assert apc.choose_decoys(queries, ["d1"], {"d1", "f1"}, classes) == ["s1", "s2"]
    # each query is used once; another class only once the class runs out; never a
    # write for a read, a utility statement or a flagged query
    assert apc.choose_decoys(queries, ["d1", "d1"], {"d1", "f1"}, classes) == [
        "s1",
        "s2",
        "s3",
        "k1",
    ]
    assert apc.choose_decoys(queries, [], set(), classes) == []


def test_case_queries_are_sorted_so_the_key_file_shows_no_grouping():
    spec = apc.CaseSpec("s-p", "s", "p", decides=["z", "b"], decoys=["a", "y"])
    assert spec.queries == ["a", "b", "y", "z"]


def test_row_order_depends_only_on_the_case_and_query_ids():
    ids = [f"q{i}" for i in range(20)]
    order = apc.row_order("case-a", ids)
    assert order == apc.row_order("case-a", list(reversed(ids)))  # not the input order
    assert sorted(order) == sorted(ids)
    assert order != apc.row_order("case-b", ids)  # seeded by the case id
    assert order != sorted(ids)


def test_the_sheet_never_shows_an_engine_a_score_a_class_or_a_flag():
    collector = {
        "queries": {
            "query_patterns": [
                _q("a1", "SELECT * FROM t", calls=10.0),
                _q("a2", "SELECT * FROM t WHERE id = ?", calls=5.0),
                _q("a3", "SELECT * FROM u WHERE id = ?", tables=("db.u",), calls=1.0),
            ]
        }
    }
    access = {
        "queries": [
            {
                **_ap(q, "reference_read"),
                "facts": ["partition_range"],
                "class_scores": {"dynamodb": 100},
                "parity_anchors": {"documentdb": "parity anchor"},
            }
            for q in ("a1", "a2", "a3")
        ]
    }
    rows = apc.case_rows(build_rows(collector, access, {}), "demo", ["a1", "a3"])
    text = apc.render_case("demo-frequent-full-scan", "demo", "frequent-full-scan", rows)
    shown = _rows(text)
    assert [r[0] for r in shown] == ["1", "2"]
    assert [r[1].strip("`") for r in shown] == apc.row_order("demo", ["a1", "a3"])
    body = text.split("<!-- rows -->", 1)[1]
    assert not ENGINE_WORDS.search(body)
    assert "100" not in body and "parity" not in body and "class_scores" not in body
    # the access-pattern class and its reason restate the detector's inputs
    assert "reference_read" not in body and "reason" not in body
    assert "partition_range" not in body
    assert "frequent-full-scan" not in body and "flag" not in body
    assert all(r[-ANSWER_COLUMNS:] == [""] * ANSWER_COLUMNS for r in shown)


def _write_stage(stage: Path, cases: dict[str, bool]) -> None:
    stage.mkdir(parents=True, exist_ok=True)
    entries = []
    for cid, reviewed in cases.items():
        (stage / cid).mkdir()
        key = {"reviewed": reviewed, "queries": ["q1"], "expected": {}}
        if reviewed:
            key["expected"] = {"q1": {"shows": True}}
        (stage / cid / "expected.json").write_text(json.dumps(key))
        entries.append({"id": cid, "path": cid, "sample": "wordpress", "anti_pattern": "x"})
    (stage / "index.json").write_text(json.dumps({"stage": "anti_pattern", "cases": entries}))


def test_the_builder_never_rewrites_a_reviewed_case(tmp_path, monkeypatch):
    stage = tmp_path / "anti_pattern"
    _write_stage(stage, {"wordpress-kept": True, "wordpress-stale": False})
    reviewed_key = (stage / "wordpress-kept" / "expected.json").read_text()
    monkeypatch.setattr(apc, "write_sheet", lambda *a, **k: None)
    specs = [
        apc.CaseSpec("wordpress-kept", "wordpress", "frequent-full-scan", ["q9"], []),
        apc.CaseSpec("wordpress-new", "wordpress", "ordered-scan", ["q2"], ["q3"]),
    ]
    written = apc.write_cases("wordpress", specs, ctx=None, stage_dir=stage)
    assert written == ["wordpress-new"]
    assert (stage / "wordpress-kept" / "expected.json").read_text() == reviewed_key
    assert not (stage / "wordpress-stale").exists()  # unreviewed and no longer selected
    new = json.loads((stage / "wordpress-new" / "expected.json").read_text())
    assert new["reviewed"] is False and new["expected"] == {}
    assert new["queries"] == ["q2", "q3"]
    ids = [e["id"] for e in json.loads((stage / "index.json").read_text())["cases"]]
    assert ids == ["wordpress-kept", "wordpress-new"]


def test_sheets_only_never_rewrites_a_reviewed_cases_sheet(tmp_path, monkeypatch):
    stage = tmp_path / "anti_pattern"
    _write_stage(stage, {"wordpress-kept": True, "wordpress-open": False})
    written = []
    monkeypatch.setattr(
        apc, "write_sheet", lambda case_id, *a, **k: written.append(case_id) or Path(case_id)
    )
    lines = apc.refresh_sheets("wordpress", ctx=None, stage_dir=stage)
    assert written == ["wordpress-open"]
    assert {"case": "wordpress-kept", "sheet": "kept (reviewed)"} in lines


def test_penalty_set_to_refuses_a_pattern_the_scorer_has_no_penalty_for():
    with pytest.raises(KeyError, match="no entry"):
        with apc.penalty_set_to("batch-in-list", 10):
            pass


# --- the committed cases and sheets --------------------------------------------


@pytest.fixture(scope="module")
def sample_runs(tmp_path_factory):
    """Each sample's deterministic pipeline, once: ``{sample: (ctx, work_dir)}``."""
    out = {}
    for sample in SAMPLES:
        work = tmp_path_factory.mktemp(f"ap-{sample}")
        out[sample] = (apc._pipeline_context(sample, work), work)
    return out


def _flags(ctx, pattern_id) -> set[str]:
    return pipeline_flags(read_analyses(ctx["store"], ctx["prefix"]), pattern_id, ctx["collector"])


@pytest.mark.parametrize("case_id", CASES)
def test_committed_case_membership_follows_the_sweep_rule(case_id, sample_runs):
    """The case's flagged queries are the busiest that the 0..40 sweep moves; the
    rest are unflagged decoys, two per deciding query, of the deciding queries'
    kind, of a deciding query's class wherever the sample has one left."""
    case = next(c for c in load_cases(STAGE_DIR) if c.case_id == case_id)
    ctx, work = sample_runs[case.sample]
    flagged = _flags(ctx, case.anti_pattern)
    with contextlib.redirect_stdout(sys.stderr):
        moved = apc.moved_by_sweep(
            work / "artifacts", case.sample, case.anti_pattern, work / f"sweep-{case_id}"
        )
    queries = ctx["collector"]["queries"]["query_patterns"]
    calls = {q["query_id"]: apc._calls(q) for q in queries}
    decides = sorted(flagged & moved, key=lambda q: (-calls.get(q, 0.0), q))[: apc.MAX_DECIDES]
    classes = {q["query_id"]: q.get("class", "") for q in ctx["access"]["queries"]}
    decoys = apc.choose_decoys(queries, decides, flagged, classes)
    assert decides
    assert case.queries == sorted({*decides, *decoys})
    assert len(decoys) >= len(decides)


@pytest.mark.parametrize("sample", ["wordpress", "trailgear"])
def test_the_selection_reproduces_the_committed_cases(sample, tmp_path):
    """Every penalised pattern, not only the committed ones: a pattern with no
    committed case on the sample decides no query there."""
    specs, _ = apc.select_cases(sample, tmp_path)
    committed = {c.case_id: c.queries for c in load_cases(STAGE_DIR) if c.sample == sample}
    assert {s.case_id: s.queries for s in specs} == committed


@pytest.mark.parametrize("case_id", CASES)
def test_no_sheet_column_or_position_separates_flagged_rows(case_id, sample_runs):
    case = next(c for c in load_cases(STAGE_DIR) if c.case_id == case_id)
    ctx, _ = sample_runs[case.sample]
    flagged = _flags(ctx, case.anti_pattern)
    text = (apc.REVIEW_DIR / f"{case_id}.md").read_text()
    rows = _rows(text)
    full = {next(q for q in case.queries if q.startswith(r[1].strip("`"))): r for r in rows}
    shown_flagged = [q for q in full if q in flagged]
    decoys = [q for q in full if q not in flagged]
    assert shown_flagged and len(decoys) >= len(shown_flagged)
    # position: the order is the case-seeded hash order, whatever the verdict
    assert list(full) == apc.row_order(case_id, case.queries)
    # columns: every feature value a flagged row shows, some decoy shows too
    for name in ("op", "tier", "co_dependency", "write_group", "facts"):
        on_decoys = {row_features(full[q])[name] for q in decoys}
        only_flagged = {row_features(full[q])[name] for q in shown_flagged} - on_decoys
        assert not only_flagged, (name, only_flagged)


@pytest.mark.parametrize("case_id", CASES)
def test_committed_sheet_is_blind_and_covers_the_case(case_id):
    case = next(c for c in load_cases(STAGE_DIR) if c.case_id == case_id)
    text = (apc.REVIEW_DIR / f"{case_id}.md").read_text()
    head, body = text.split("<!-- rows -->", 1)
    assert not ENGINE_WORDS.search(head.split("## How to fill it in")[0])
    rows = _rows(text)
    width = apc.HEADER.count("|") - 1
    assert all(len(r) == width for r in rows)
    for r in rows:
        assert _engine_words(r) == [], r
        assert r[-ANSWER_COLUMNS:] == [""] * ANSWER_COLUMNS, r  # no answer filled in
    for r in rows:  # outside the customer's own SQL and table names
        routing_cells = " ".join(c for i, c in enumerate(r) if i not in (2, COLUMNS["tables"]))
        assert case.anti_pattern not in routing_cells and "flag" not in routing_cells, r
    assert re.findall(r"^Default: .*$", body, re.M) == [
        "Default: shows = ; acceptable = ; ideal = ; rationale ="
    ]
    assert [int(r[0]) for r in rows] == list(range(1, len(rows) + 1))
    shown = [r[1].strip("`") for r in rows]
    assert len(shown) == len(case.queries) == len(set(shown))
    assert all(sum(q.startswith(s) for q in case.queries) == 1 for s in shown)
