"""``scripts/release_notes.py``: the routing diff and changelog lines a release carries (#1184).

The generator is tested on two small synthetic routing snapshots (the shape
``tests/e2e/routing_snapshot.py`` writes) and synthetic contract snapshots (the shape
``scripts/contract_schema_snapshot.py`` writes); the git layer only reads those files
at two refs.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import release_notes as rn


def _q(engine: str, scores: dict[str, Any], moves: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "f": {"c": "individual_highest_confidence", "e": engine},
        "s": scores,
        "mv": moves,
        **extra,
    }


BEFORE: dict[str, Any] = {
    "samples": {
        "shop": {
            "queries": {
                "Q1": _q("dynamodb", {"dynamodb": 80, "aurora_mysql": 70}, ["score:highest_score"]),
                "Q2": _q(
                    "aurora_mysql", {"aurora_mysql": 75, "dynamodb": 60}, ["score:highest_score"]
                ),
                "Q3": _q(
                    "documentdb", {"documentdb": 70, "aurora_mysql": 66}, ["score:highest_score"]
                ),
                "Q4": _q("aurora_mysql", {"aurora_mysql": 90}, ["score:highest_score"]),
                "Q5": _q(
                    "elasticache",
                    {"elasticache": {"base": 41, "gate": "write_gate", "used": 41}},
                    ["score:highest_score"],
                ),
            }
        }
    }
}

AFTER: dict[str, Any] = {
    "samples": {
        "shop": {
            "queries": {
                # Moved engine: its score is the new engine's, and the rule that placed it changed.
                "Q1": _q("aurora_mysql", {"dynamodb": 70, "aurora_mysql": 82}, ["tie:tie_rule"]),
                # Same engine, its score moved; another engine's score moving is not listed.
                "Q2": _q(
                    "aurora_mysql", {"aurora_mysql": 72, "dynamodb": 61}, ["score:highest_score"]
                ),
                # The reality check consolidated it: the final pick and its fit score.
                "Q3": _q(
                    "documentdb",
                    {"documentdb": 70, "aurora_mysql": 66},
                    ["score:highest_score"],
                    r={"c": "reality_check_no_unique_value", "e": "aurora_mysql", "fit": 66},
                    rmv=["reality_check:consolidated"],
                ),
                # Q4 removed; Q6 added.
                "Q5": _q(
                    "elasticache",
                    {"elasticache": {"base": 41, "gate": "write_gate", "used": 41}},
                    ["score:highest_score"],
                ),
                "Q6": _q("dynamodb", {"dynamodb": 88}, ["score:highest_score"]),
            }
        },
        "blog": {
            "queries": {
                "B1": _q("aurora_postgresql", {"aurora_postgresql": 77}, ["score:highest_score"])
            }
        },
    }
}


def _row(sample: str, qid: str, engine: tuple, score: tuple, reason: tuple) -> dict[str, Any]:
    return {
        "sample": sample,
        "query_id": qid,
        "engine": engine,
        "score": score,
        "reason_code": reason,
    }


def test_routing_changes_lists_each_query_whose_engine_score_or_reason_code_moved() -> None:
    assert rn.routing_changes(BEFORE, AFTER) == [
        _row("blog", "B1", (None, "aurora_postgresql"), (None, 77), (None, "highest_score")),
        _row("shop", "Q1", ("dynamodb", "aurora_mysql"), (80, 82), ("highest_score", "tie_rule")),
        _row(
            "shop",
            "Q2",
            ("aurora_mysql", "aurora_mysql"),
            (75, 72),
            ("highest_score", "highest_score"),
        ),
        _row(
            "shop",
            "Q3",
            ("documentdb", "aurora_mysql"),
            (70, 66),
            ("highest_score", "consolidated"),
        ),
        _row("shop", "Q4", ("aurora_mysql", None), (90, None), ("highest_score", None)),
        _row("shop", "Q6", (None, "dynamodb"), (None, 88), (None, "highest_score")),
    ]


def test_identical_snapshots_have_no_routing_changes() -> None:
    assert rn.routing_changes(BEFORE, BEFORE) == []
    # A gated score is compared by the score routing used.
    assert rn.final_placement(BEFORE["samples"]["shop"]["queries"]["Q5"]) == (
        "elasticache",
        41,
        "highest_score",
    )


def test_a_missing_snapshot_counts_every_query_as_added() -> None:
    rows = rn.routing_changes({}, BEFORE)
    assert [r["query_id"] for r in rows] == ["Q1", "Q2", "Q3", "Q4", "Q5"]
    assert all(r["engine"][0] is None for r in rows)


def _entry(version: str, fields: dict[str, Any]) -> dict[str, Any]:
    return {"version": version, "schema_hash": json.dumps(fields, sort_keys=True), "fields": fields}


_STR = {"required": True, "schema": {"kind": "scalar", "type": "string"}}
_OPT = {"required": False, "schema": {"kind": "scalar", "type": "string"}}


def test_contract_changes_gives_one_line_per_contract_that_moved() -> None:
    before = {
        "a.Same": _entry("1.0", {"x": _STR}),
        "b.Minor": _entry("1.2", {"x": _STR}),
        "c.Major": _entry("2.0", {"x": _STR, "y": _STR}),
        "d.Gone": _entry("1.0", {"x": _STR}),
    }
    after = {
        "a.Same": _entry("1.0", {"x": _STR}),
        "b.Minor": _entry("1.3", {"x": _STR, "z": _OPT}),
        "c.Major": _entry("3.0", {"x": _STR}),
        "e.New": _entry("1.0", {"x": _STR}),
    }
    assert rn.contract_changes(before, after) == [
        "Contract `b.Minor` 1.2 -> 1.3 (MINOR): z: added (optional) (minor)",
        "Contract `c.Major` 2.0 -> 3.0 (MAJOR): y: removed (major)",
        "Contract `d.Gone` removed (was 1.0)",
        "Contract `e.New` added at 1.0",
    ]


def test_rule_changes_gives_one_line_per_catalog_file() -> None:
    changed = [
        ("M", "src/catalog/patterns/key-value-lookup.toml"),
        ("A", "src/catalog/gaps/new-gap.toml"),
        ("D", "src/catalog/engines/old.toml"),
    ]
    subjects = {
        "src/catalog/patterns/key-value-lookup.toml": ["fix(routing): lower the fit", "feat: x"],
        "src/catalog/gaps/new-gap.toml": ["feat(catalog): add a gap rule"],
    }
    assert rn.rule_changes(changed, subjects) == [
        "Rule `engines/old.toml` removed",
        "Rule `gaps/new-gap.toml` added: feat(catalog): add a gap rule",
        "Rule `patterns/key-value-lookup.toml` changed: fix(routing): lower the fit; feat: x",
    ]


def test_the_attachment_lists_engine_changes_first_then_score_drift() -> None:
    rows = rn.routing_changes(BEFORE, AFTER)
    text = rn.render("v1.0.0", "v1.1.0", rows, ["Rule `a.toml` changed: fix: a|b"])
    assert text.startswith("## Routing and contract changes since v1.0.0\n")
    assert (
        "(v1.0.0..v1.1.0): 5 engine changes, 0 reason-code-only changes, 1 score-only changes"
        in text
    )
    assert "#### Engine and reason-code changes (blog: 1, shop: 4)" in text
    q1 = "| shop | Q1 | dynamodb -> aurora_mysql | 80 -> 82 | highest_score -> tie_rule |"
    q4 = "| shop | Q4 | aurora_mysql -> (none) | 90 -> (none) | highest_score -> (none) |"
    q2 = "| shop | Q2 | aurora_mysql | 75 -> 72 | highest_score |"
    assert text.index(q1) < text.index(q4) < text.index("#### Score-only changes") < text.index(q2)
    assert "1 queries keep their engine and reason code" in text
    assert "by sample shop: 1; by score change -3: 1" in text
    assert "- Rule `a.toml` changed: fix: a|b" in text


def test_engine_changes_come_before_reason_code_only_changes() -> None:
    def row(qid: str, engine: tuple, reason: tuple) -> dict[str, Any]:
        return _row("a", qid, engine, (70, 70), reason)

    rows = [
        row("Q1", ("dynamodb", "dynamodb"), ("highest_score", "tie_rule")),
        row("Q2", ("dynamodb", "aurora_mysql"), ("highest_score", "highest_score")),
    ]
    text = rn.render("v1", "v2", rows, [])
    assert "1 engine changes, 1 reason-code-only changes, 0 score-only changes" in text
    assert text.index("| a | Q2 |") < text.index("| a | Q1 |")


def test_the_body_caps_engine_changes_and_summarises_score_drift() -> None:
    rows = rn.routing_changes(BEFORE, AFTER)
    body = rn.render("v1.0.0", "v1.1.0", rows, [], body=True, max_rows=2)
    assert body.count("| shop |") + body.count("| blog |") == 2
    assert "... and 3 more in the attached release-changes.md." in body
    assert "| shop | Q2 |" not in body  # score drift is a summary line in the body
    assert "by score change -3: 1" in body and "Every row is in the attached" in body
    assert "No rule or contract changes." in body


def test_score_drift_counts_each_change() -> None:
    rows = [
        {"sample": "a", "score": (80, 81)},
        {"sample": "a", "score": (70, 71)},
        {"sample": "b", "score": (60, 59)},
        {"sample": "b", "score": (None, 59)},
    ]
    assert rn.score_drift(rows) == "by sample a: 2, b: 2; by score change -1: 1, +1: 2, n/a: 1"


def test_the_body_stays_under_the_release_limit() -> None:
    """GitHub refuses a release body over 125,000 characters: a long range, or one huge
    contract line, is cut in the body and kept whole in the attachment."""
    rows = [
        {
            "sample": "s",
            "query_id": f"Q{i:05d}",
            "engine": ("dynamodb", "aurora_mysql"),
            "score": (80, 81),
            "reason_code": ("highest_score", "tie_rule"),
        }
        for i in range(5000)
    ]
    changelog = [f"Contract `c{i}` 1.0 -> 1.1 (MINOR): " + "x" * 5000 for i in range(200)]
    body = rn.render("v1.0.0", "v1.1.0", rows, changelog, body=True)
    assert len(body) < rn.BODY_LIMIT
    assert body.count("| s |") == rn.BODY_ROWS
    assert "... and 160 more lines in the attached release-changes.md." in body
    assert max(len(line) for line in body.splitlines()) <= rn.BODY_LINE_CHARS + 2
    full = rn.render("v1.0.0", "v1.1.0", rows, changelog)
    assert full.count("| s |") == 5000 and full.count("x" * 5000) == 200
    # Even with every cap raised, the body is cut at the limit with a pointer.
    huge = rn.render("v1.0.0", "v1.1.0", rows, changelog, body=True, max_rows=5000)
    assert len(huge) < rn.BODY_LIMIT
    assert huge.endswith(
        "cut to fit the release body; everything is in the attached release-changes.md.\n"
    )


def test_render_with_nothing_moved() -> None:
    text = rn.render("v1.0.0", "HEAD", [], [])
    assert "No query moved." in text and "No rule or contract changes." in text
    assert "0 engine changes, 0 reason-code-only changes, 0 score-only changes" in text


TAGS = [
    "v0.1.0-beta.1",
    "v0.2.0-beta.2",
    "v0.3.0-beta.1",
    "v0.3.0-beta.2",
    "v0.3.0",
    "v0.3.1",
    "v0.4.0",
    "v0.4.0-rc.1",
    "v10.0.0",
    "nightly",
    "v0.3.0-local",
]


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v0.3.0", "v0.3.0-beta.2"),  # not skipped by a substring match on "v0.3.0"
        ("v0.3.0-beta.2", "v0.3.0-beta.1"),
        ("v0.3.1", "v0.3.0"),  # a hotfix after v0.4.0 exists: its own line, never newer
        ("v0.4.0", "v0.4.0-rc.1"),
        ("v0.4.0-rc.1", "v0.3.1"),
        ("v10.0.0", "v0.4.0"),  # version order, not string order
        ("v0.1.0-beta.1", None),
    ],
)
def test_previous_tag_is_the_highest_version_below(tag: str, expected: str | None) -> None:
    assert rn.previous_tag(tag, TAGS) == expected


def test_previous_tag_refuses_a_tag_that_is_not_a_version() -> None:
    with pytest.raises(SystemExit):
        rn.previous_tag("nightly", TAGS)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_main_reads_both_refs_from_git(tmp_path: Path) -> None:
    """End to end on a throwaway repository: two tags, a moved query, a rule change."""
    repo = tmp_path / "repo"
    snapshot = repo / rn.ROUTING_SNAPSHOT
    rule = repo / "src" / "catalog" / "patterns" / "kv.toml"
    snapshot.parent.mkdir(parents=True)
    rule.parent.mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    snapshot.write_text(json.dumps(BEFORE))
    rule.write_text("fit = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: first")
    _git(repo, "tag", "v1.0.0")
    snapshot.write_text(json.dumps(AFTER))
    rule.write_text("fit = 2\n")
    _git(repo, "commit", "-q", "-am", "fix(routing): move Q1")
    _git(repo, "tag", "v1.1.0")
    out = tmp_path / "release-changes.md"
    code = rn.main(
        ["--since", "v1.0.0", "--until", "v1.1.0", "--repo", str(repo), "--output", str(out)]
    )
    assert code == 0
    text = out.read_text()
    assert "5 engine changes, 0 reason-code-only changes, 1 score-only changes" in text
    assert "- Rule `patterns/kv.toml` changed: fix(routing): move Q1" in text


def test_a_tag_the_public_contract_was_not_recorded_for_gets_a_note(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    public = repo / rn.PUBLIC_CONTRACT_SNAPSHOT
    public.parent.mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    public.write_text(json.dumps({"released": "v1.0.0", "contracts": {}}))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: record v1.0.0")
    _git(repo, "tag", "v1.0.0")
    _git(repo, "tag", "v1.1.0")
    assert rn.changelog_between(repo, "v1.0.0", "v1.0.0") == []
    [note] = rn.changelog_between(repo, "v1.0.0", "v1.1.0")
    assert "not recorded for v1.1.0 (recorded: v1.0.0)" in note
    assert "--write-released v1.1.0" in note and "read from the tag itself" in note
    # A branch or HEAD is a preview, not a release: no note.
    assert rn.changelog_between(repo, "v1.0.0", "HEAD") == []


def test_main_refuses_an_unknown_ref(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    with pytest.raises(SystemExit):
        rn.main(["--since", "v9.9.9", "--repo", str(tmp_path)])


def test_main_prints_the_previous_tag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "dev@example.com")
    _git(tmp_path, "config", "user.name", "Dev")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "chore: first")
    for tag in ("v1.0.0", "v1.1.0-beta.1", "v1.1.0"):
        _git(tmp_path, "tag", tag)
    assert rn.main(["--previous-tag", "v1.1.0", "--repo", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "v1.1.0-beta.1\n"
    assert rn.main(["--previous-tag", "v1.0.0", "--repo", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "\n"
    with pytest.raises(SystemExit):
        rn.main(["--previous-tag", "v9.9.9", "--repo", str(tmp_path)])
