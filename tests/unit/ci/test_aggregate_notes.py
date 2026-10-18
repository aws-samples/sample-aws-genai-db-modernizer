"""ci/llm/aggregate_notes.py: grouping recurring judge notes across saved
runs, on small synthetic judge.json files. No model is called."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from ci.llm import aggregate_notes as agg

ADOPT = "Hedge persists: 'One suggested adoption path, not the only one'."


def _write(root: Path, name: str, scores: dict, notes: dict) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "judge.json").write_text(json.dumps({"scores": scores, "notes": notes}))
    return d / "judge.json"


def _runs(notes_by_run: dict[str, dict]) -> dict[str, dict]:
    return {run: {"scores": {}, "notes": notes} for run, notes in notes_by_run.items()}


def _recurring(runs, **kw):
    return agg.aggregate(runs, **kw)["recurring"]


@pytest.fixture
def runs_dir(tmp_path: Path) -> Path:
    root = tmp_path / "test-results"
    wave = "Wave 2 shows 'tables: []' which is ambiguous."
    _write(
        root,
        "a",
        {"roadmap": 4, "tone": 3},
        {"roadmap": f"Waves are coherent. {wave}", "tone": ADOPT},
    )
    _write(
        root,
        "b",
        {"roadmap": 3, "tone": 4},
        {"roadmap": "Wave 7 shows 'tables: []', which is ambiguous.", "tone": ADOPT},
    )
    _write(
        root,
        "c",
        {"roadmap": 4, "tone": 4},
        {"roadmap": f"{wave} Ordering is justified.", "tone": "Direct tone throughout the report."},
    )
    return root


# --- text handling ---------------------------------------------------------


def test_split_sentences_keeps_decimals_and_identifiers():
    text = "Cost is $582.76 total. See executive_pdf > Cost for facts.tco.cost. Next point."
    assert agg.split_sentences(text) == [
        "Cost is $582.76 total",
        "See executive_pdf > Cost for facts.tco.cost",
        "Next point.",
    ]


def test_numbers_collapse_to_one_token():
    toks = agg.normalize("Wave 2 has 13 tables and $582.76")
    assert "#" in toks and toks == agg.normalize("Wave 7 has 4 tables and $9.10")
    assert not any(t.isdigit() for t in toks)
    assert "the" not in agg.normalize("the wave is here")


def test_negations_are_tokens():
    assert "not" in agg.normalize("figures are not consistent")
    assert agg.normalize("figures are consistent") != agg.normalize("figures are not consistent")


def test_similarity_bounds():
    a = agg.normalize("wave shows empty tables")
    assert agg.similarity(a, a) == 1.0
    assert agg.similarity(a, agg.normalize("cost matches facts exactly")) == 0.0
    assert agg.similarity(frozenset(), a) == 0.0


# --- concerns filter -------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        "'could run on a simpler engine' in the resolved risks",
        "The cost figures disagree across deliverables, which is a gap",
        "The mitigation is somewhat soft",
        "Hedging remains in the roadmap wording",
        "The single mitigation strategy is generic",
        "Hedges that survive: 'One suggested adoption path, not the only one'",
        "Small blemishes remain",
        "Wave 2 shows an empty table list, which is ambiguous",
        "Cost is shown, but the source cost is missing",
    ],
)
def test_concern_wording_passes(sentence):
    assert agg._CONCERN.search(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "The executive_pdf contradicts nothing",
        "Current cost is honestly stated as 'not provided'",
        "Only one engine owns each table, as the facts show",
        "No grader-directed text is present",
        "Core figures match facts across all deliverables",
        "engineering_report > Cost repeats $582.76 consistently, and the executive_pdf omits cost by design",
        "engineering_report > Cost repeats the same figures without contradiction",
        "Each engine gets a specific reason rather than a one-size reason",
    ],
)
def test_praise_with_negations_is_not_a_concern(sentence):
    assert not agg._CONCERN.search(sentence)


def test_concerns_only_keeps_concern_drops_praise():
    runs = _runs(
        {
            n: {
                "tone": "Direct and declarative throughout the report. The mitigation is somewhat soft here."
            }
            for n in "abc"
        }
    )
    assert len(_recurring(runs)) == 2
    only = _recurring(runs, concerns_only=True)
    assert len(only) == 1 and "soft" in only[0]["note"]


# --- grouping --------------------------------------------------------------


def test_recurring_note_with_varying_numbers(runs_dir):
    runs, _ = agg.load_runs(agg.expand_paths([str(runs_dir)])[0])
    result = agg.aggregate(runs, min_runs=3)
    assert [(r["criterion"], r["run_count"], r["runs"]) for r in result["recurring"]] == [
        ("roadmap", 3, ["a", "b", "c"])
    ]
    two = agg.aggregate(runs, min_runs=2)["recurring"]
    assert [r["criterion"] for r in two] == ["roadmap", "tone"]
    assert two[1]["runs"] == ["a", "b"]


def test_same_run_counted_once():
    runs = _runs({"a": {"tone": f"{ADOPT} {ADOPT}"}})
    assert _recurring(runs, min_runs=2) == []
    assert _recurring(runs, min_runs=1)[0]["run_count"] == 1


def test_same_sentence_in_two_criteria_stays_apart():
    runs = _runs({n: {"tone": ADOPT, "risks": ADOPT} for n in "abc"})
    rec = _recurring(runs)
    assert sorted((r["criterion"], r["run_count"]) for r in rec) == [("risks", 3), ("tone", 3)]
    # a group holding both criteria would report 3 runs once; two groups of 3 prove they stayed apart


def test_negation_pair_does_not_group():
    runs = _runs(
        {
            "a": {"cost": "Cost figures are consistent across the deliverables"},
            "b": {"cost": "Cost figures are not consistent across the deliverables"},
            "c": {"cost": "Cost figures are consistent across the deliverables"},
        }
    )
    rec = _recurring(runs, min_runs=1)
    assert sorted((r["run_count"], "not" in r["note"]) for r in rec) == [(1, True), (2, False)]


def test_polarity_only_blocks_opposite_use_of_the_same_word():
    plain = "The executive_pdf omits cost by design"
    extra = "The executive_pdf omits cost by design and contradicts nothing in the report"
    assert not agg.opposite_polarity(agg.polarity(plain), agg.polarity(extra))
    runs = _runs({"a": {"cost": plain}, "b": {"cost": extra}, "c": {"cost": plain}})
    assert [r["run_count"] for r in _recurring(runs, min_runs=1, threshold=0.5)] == [3]
    assert agg.opposite_polarity(
        agg.polarity("figures are consistent"), agg.polarity("figures are not consistent")
    )


def test_threshold_boundary_is_inclusive():
    # 3 shared tokens out of 5 distinct: Jaccard exactly 0.6
    runs = _runs(
        {"a": {"tone": "alpha bravo charlie delta"}, "b": {"tone": "alpha bravo charlie echo"}}
    )
    assert len(_recurring(runs, min_runs=2, threshold=0.6)) == 1
    assert _recurring(runs, min_runs=2, threshold=0.61) == []


def test_groups_do_not_chain():
    # A~B and B~C pass the threshold but A~C does not: C must not join A's group.
    runs = _runs(
        {
            "a": {"tone": "alpha bravo charlie delta"},
            "b": {"tone": "bravo charlie delta echo"},
            "c": {"tone": "charlie delta echo foxtrot"},
        }
    )
    assert (
        agg.similarity(
            agg.normalize("alpha bravo charlie delta"), agg.normalize("charlie delta echo foxtrot")
        )
        < 0.5
    )
    groups = agg.group_notes(runs, threshold=0.5)
    assert sorted(len(g.runs) for g in groups) == [1, 2]


def test_short_fragments_are_ignored():
    runs = _runs({n: {"tone": "Fine overall."} for n in "abc"})
    assert _recurring(runs) == []


def test_recurring_sorted_by_run_count_then_criterion():
    notes_x = {"tone": "alpha bravo charlie delta", "risks": "kilo lima mike november"}
    notes_y = {"tone": "alpha bravo charlie delta"}
    runs = _runs({"a": notes_x, "b": notes_x, "c": notes_x, "d": notes_y})
    rec = _recurring(runs)
    assert [(r["criterion"], r["run_count"]) for r in rec] == [("tone", 4), ("risks", 3)]
    runs2 = _runs(
        {
            n: {"tone": "alpha bravo charlie delta", "risks": "kilo lima mike november"}
            for n in "abc"
        }
    )
    assert [r["criterion"] for r in _recurring(runs2)] == ["risks", "tone"]


def test_result_is_independent_of_input_order_and_run_names():
    sentences = [
        "Hedge persists in the roadmap wording",
        "Hedge persists in the roadmap text",
        "Mitigation is generic and thin",
        "Mitigation is generic",
        "Wave two lists no tables at all",
        "Wave three lists no tables at all",
    ]
    rng = random.Random(7)
    base = {
        f"run{i}": {"tone": " ".join(f"{s}." for s in rng.sample(sentences, 4))} for i in range(8)
    }
    expected = agg.aggregate(_runs(base), min_runs=2)
    for seed in range(5):
        r = random.Random(seed)
        keys = list(base)
        r.shuffle(keys)
        renamed = {f"zz{n}-{k[::-1]}": base[k] for n, k in enumerate(keys)}
        got = agg.aggregate(_runs(renamed), min_runs=2)
        assert [(g["note"], g["run_count"]) for g in got["recurring"]] == [
            (g["note"], g["run_count"]) for g in expected["recurring"]
        ]
        shuffled = {k: base[k] for k in keys}
        assert agg.aggregate(_runs(shuffled), min_runs=2) == expected


# --- scores ----------------------------------------------------------------


def test_score_stats(runs_dir):
    runs, _ = agg.load_runs(agg.expand_paths([str(runs_dir)])[0])
    stats = agg.score_stats(runs)
    assert stats["roadmap"]["mean"] == pytest.approx(3.667, abs=1e-3)
    assert (stats["roadmap"]["min"], stats["roadmap"]["max"]) == (3, 4)
    assert stats["tone"]["n"] == 3


# --- files and CLI ---------------------------------------------------------


def test_bad_files_are_reported_distinctly(tmp_path):
    _write(tmp_path, "ok", {"tone": 4}, {"tone": ADOPT})
    for name, body in {
        "badjson": "{not json",
        "err": json.dumps({"error": "judge failed"}),
        "nonotes": json.dumps({"scores": {}}),
    }.items():
        (tmp_path / name).mkdir()
        (tmp_path / name / "judge.json").write_text(body)
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    (unreadable / "judge.json").mkdir()  # a directory named judge.json: read_text raises
    paths = sorted(tmp_path.glob("*/judge.json"))
    runs, warnings = agg.load_runs(paths)
    assert len(runs) == 1
    joined = "\n".join(warnings)
    assert "not valid JSON" in joined and "judge error" in joined
    assert "no 'notes' object" in joined and "unreadable" in joined
    assert len(warnings) == 4


def test_expand_paths_files_dirs_globs_and_unmatched(runs_dir):
    by_dir, _ = agg.expand_paths([str(runs_dir)])
    by_glob, _ = agg.expand_paths([str(runs_dir / "*" / "judge.json")])
    by_file, _ = agg.expand_paths([str(runs_dir / "a" / "judge.json")])
    by_dir_glob, _ = agg.expand_paths([str(runs_dir / "[ab]")])
    assert (len(by_dir), len(by_glob), len(by_file), len(by_dir_glob)) == (3, 3, 1, 2)
    _, unmatched = agg.expand_paths([str(runs_dir), str(runs_dir / "typo*")])
    assert unmatched == [str(runs_dir / "typo*")]


def test_paths_are_deduplicated_before_the_unmatched_check(runs_dir):
    a = str(runs_dir / "a")
    files, unmatched = agg.expand_paths([a, a, str(runs_dir), str(runs_dir / "a" / ".." / "a")])
    assert unmatched == []
    assert len(files) == 3 and len(set(files)) == 3
    link = runs_dir.parent / "link"
    link.symlink_to(runs_dir)
    files2, unmatched2 = agg.expand_paths([str(runs_dir), str(link)])
    assert unmatched2 == [] and len(files2) == 3


def test_repeated_path_args_do_not_error(runs_dir, capsys):
    assert agg.main([str(runs_dir), str(runs_dir), str(runs_dir / "a")]) == 0
    assert "across 3 runs" in capsys.readouterr().out


def test_no_args_without_test_results_gives_friendly_message(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert agg.main([]) == 1
    err = capsys.readouterr().err
    assert "test-results/" in err and "make e2e-llm" in err and "saved run folders" in err


def test_single_run_label_hides_home_and_absolute_prefix(tmp_path, monkeypatch):
    p = _write(tmp_path, "only", {"tone": 4}, {"tone": ADOPT})
    monkeypatch.chdir(tmp_path)
    runs, _ = agg.load_runs([p])
    assert list(runs) == ["only"]


def test_cli_json_and_text(runs_dir, capsys):
    assert agg.main([str(runs_dir), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["runs"] == 3 and data["recurring"][0]["criterion"] == "roadmap"
    assert agg.main([str(runs_dir), "--min-runs", "2"]) == 0
    out = capsys.readouterr().out
    assert "across 3 runs" in out and "in 3 runs" in out and "in 2 runs" in out
    assert "mean 3.67" in out and "- a" in out


def test_cli_singular_run(tmp_path, capsys):
    _write(tmp_path, "a", {"tone": 4}, {"tone": ADOPT})
    assert agg.main([str(tmp_path), "--min-runs", "1"]) == 0
    out = capsys.readouterr().out
    assert "across 1 run\n" in out and "in 1 run:" in out and "1 runs" not in out


def test_cli_no_files_and_unmatched_path(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "test-results").mkdir()
    assert agg.main([]) == 1
    assert "no judge.json files found" in capsys.readouterr().err
    assert agg.main([str(tmp_path / "typo")]) == 2
    assert agg.main([str(tmp_path)]) == 2  # an explicit directory with no judge.json
    assert "no judge.json matches" in capsys.readouterr().err


def test_cli_all_files_bad_prints_warnings(tmp_path, capsys):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "judge.json").write_text(json.dumps({"error": "boom"}))
    assert agg.main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "judge error" in err and "no usable judge.json" in err


@pytest.mark.parametrize(
    "flags", [["--min-runs", "0"], ["--threshold", "0"], ["--threshold", "1.5"]]
)
def test_cli_rejects_bad_numbers(flags, tmp_path):
    with pytest.raises(SystemExit) as exc:
        agg.main([str(tmp_path), *flags])
    assert exc.value.code == 2


# --- structured defects (#952) ---------------------------------------------


def _defect(criterion: str, kind: str, quote: str, where: str = "engineering_report > X") -> dict:
    return {"criterion": criterion, "kind": kind, "where": where, "quote": quote, "samples": 3}


def test_recurring_defects_group_by_criterion_kind_and_quote():
    adopt = "One suggested adoption path, not the only one."
    runs = {
        "a": {"scores": {}, "notes": {}, "defects": [_defect("hedging", "hedge", adopt)]},
        "b": {
            "scores": {},
            "notes": {},
            "defects": [
                _defect("hedging", "hedge", "One suggested adoption path - not the only one"),
                _defect("contradictions", "contradiction", "Use Aurora MySQL for all writes"),
            ],
        },
        "c": {
            "scores": {},
            "notes": {},
            "defects": [
                _defect("hedging", "hedge", adopt),
                # same words, other criterion/kind: never merged with the hedge
                _defect("concision", "repetition", adopt),
            ],
        },
    }
    out = agg.aggregate(runs, min_runs=2)
    assert out["recurring_defects"] == [
        {
            "criterion": "hedging",
            "kind": "hedge",
            "run_count": 3,
            # same token count, so the alphabetically first spelling seeds the group
            "quote": "One suggested adoption path - not the only one",
            "where": "engineering_report > X",
            "runs": ["a", "b", "c"],
        }
    ]
    assert agg.aggregate(runs, min_runs=1)["recurring_defects"][-1]["run_count"] == 1


def test_judge_json_without_defects_still_aggregates(runs_dir: Path):
    paths, _ = agg.expand_paths([str(runs_dir)])
    runs, warnings = agg.load_runs(paths)
    out = agg.aggregate(runs)
    assert out["recurring_defects"] == []
    assert "Defects recurring" in agg.render_text(out, warnings)


def test_malformed_defects_are_skipped():
    runs = {
        "a": {"scores": {}, "notes": {}, "defects": "nope"},
        "b": {"scores": {}, "notes": {}, "defects": [None, {"criterion": "hedging"}, 3]},
    }
    assert agg.aggregate(runs, min_runs=1)["recurring_defects"] == []


def test_recurring_defects_render_in_text():
    runs = {
        r: {
            "scores": {},
            "notes": {},
            "defects": [_defect("hedging", "hedge", "it might help a lot")],
        }
        for r in ("a", "b", "c")
    }
    text = agg.render_text(agg.aggregate(runs), [])
    assert "[hedging/hedge] in 3 runs: it might help a lot (engineering_report > X)" in text
