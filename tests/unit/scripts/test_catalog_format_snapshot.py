# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The catalog format guard (#1647), against temporary copies of the catalog.

Each test copies ``src/catalog/`` to a temporary directory, records its format, changes
the copy and compares. The real files are never edited.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 -- git in a throwaway repository
from pathlib import Path
from typing import Any

import pytest

from scripts import catalog_format_snapshot as cfs

CATALOG = Path(cfs.CATALOG_DIR)


@pytest.fixture
def copy(tmp_path: Path) -> Path:
    root = tmp_path / "catalog"
    shutil.copytree(CATALOG, root)
    return root


def _rewrite(files: list[Path], pattern: str, replacement: str) -> None:
    """Apply a line regex to each file; every file must match once."""
    for path in files:
        text, count = re.subn(pattern, replacement, path.read_text(), count=1, flags=re.M)
        assert count == 1, f"{pattern!r} not in {path.name}"
        path.write_text(text)


def _problems(copy: Path, recorded: dict[str, Any]) -> list[str]:
    return cfs.evaluate_format(recorded, cfs.build_format(copy))


def test_an_unchanged_catalog_passes(copy: Path) -> None:
    assert _problems(copy, cfs.build_format(copy)) == []


def test_the_committed_snapshot_is_the_catalog_s_format() -> None:
    recorded = json.loads(cfs.SNAPSHOT_PATH.read_text())["kinds"]
    assert cfs.evaluate_format(recorded, cfs.build_format()) == []


def test_removing_a_key_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    _rewrite(sorted((copy / "engines").glob("*.toml")), r"^display_name = .*\n", "")
    assert _problems(copy, recorded) == ["engines: key display_name removed or renamed"]


def test_removing_a_nested_key_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    _rewrite([copy / "aurora_versions.toml"], r"^docs = .*\n", "")
    problems = _problems(copy, recorded)
    assert "aurora_versions: key engines.aurora_mysql.docs removed or renamed" in problems


def test_renaming_a_key_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    _rewrite(sorted((copy / "engines").glob("*.toml")), r"^display_name = ", "label = ")
    assert _problems(copy, recorded) == ["engines: key display_name removed or renamed"]


def test_retyping_a_key_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    _rewrite([copy / "engines" / "dynamodb.toml"], r"^display_order = 1$", 'display_order = "1"')
    assert _problems(copy, recorded) == [
        "engines: key display_order retyped: was integer, now also string"
    ]


def test_an_integer_key_given_a_float_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    _rewrite([copy / "engines" / "dynamodb.toml"], r"^display_order = 1$", "display_order = 1.5")
    assert _problems(copy, recorded) == [
        "engines: key display_order retyped: was integer, now also float"
    ]


def test_making_an_optional_key_required_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    key = "joins_need_denormalized_design"
    assert recorded["engines"]["keys"][key]["required"] is False
    lacking = [p for p in sorted((copy / "engines").glob("*.toml")) if key not in p.read_text()]
    assert lacking
    _rewrite(lacking, r"^(id = .*)$", rf"\1\n{key} = false")
    assert _problems(copy, recorded) == [
        f"engines: key {key} was optional and is now in every file"
    ]


def test_deleting_a_file_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    (copy / "patterns" / "caching.toml").unlink()
    assert "patterns: file caching.toml removed" in _problems(copy, recorded)


def test_deleting_an_engine_file_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    (copy / "engines" / "neptune.toml").unlink()
    assert "engines: file neptune.toml removed" in _problems(copy, recorded)


def test_deleting_a_file_kind_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    (copy / "aurora_versions.toml").unlink()
    shutil.rmtree(copy / "write_intents")
    problems = _problems(copy, recorded)
    assert "aurora_versions: file kind removed (src/catalog/aurora_versions.toml)" in problems
    assert "write_intents: file kind removed (src/catalog/write_intents/*.toml)" in problems


def test_adding_a_key_passes(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    _rewrite([copy / "engines" / "dynamodb.toml"], r"^(id = .*)$", r'\1\nnew_key = "x"')
    _rewrite([copy / "aurora_versions.toml"], r"^(docs = .*)$", r"\1\nnew_nested = 1")
    assert _problems(copy, recorded) == []
    assert "new_key" in cfs.build_format(copy)["engines"]["keys"]


def test_adding_a_key_to_every_file_passes(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    _rewrite(sorted((copy / "engines").glob("*.toml")), r"^(id = .*)$", r"\1\nbrand_new = 1")
    assert _problems(copy, recorded) == []


def test_adding_a_file_and_a_file_kind_passes(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    shutil.copy(copy / "patterns" / "caching.toml", copy / "patterns" / "caching-two.toml")
    (copy / "new_kind").mkdir()
    (copy / "new_kind" / "one.toml").write_text('id = "one"\n')
    (copy / "extra.toml").write_text("value = 1\n")
    assert _problems(copy, recorded) == []


def test_a_required_key_lost_from_one_file_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    assert recorded["patterns"]["keys"]["id"]["required"] is True
    _rewrite([copy / "patterns" / "caching.toml"], r'^id = "caching"\n', "")
    assert _problems(copy, recorded) == [
        "patterns: required key id is missing from a file or table that had to have it"
    ]


def test_a_new_file_without_a_required_key_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    (copy / "engines" / "zzz.toml").write_text('id = "zzz"\n')
    problems = _problems(copy, recorded)
    assert (
        "engines: required key display_name is missing from a file or table that had to "
        "have it" in problems
    )


def test_an_optional_key_removed_from_one_file_passes(copy: Path) -> None:
    """An optional key may be absent from any file; only its removal from every file fails."""
    recorded = cfs.build_format(copy)
    files = sorted((copy / "patterns").glob("*.toml"))
    key, carrying = next(
        (key, found)
        for key, entry in sorted(recorded["patterns"]["keys"].items())
        if entry.get("required") is False and "." not in key and "[" not in key
        if len(found := [p for p in files if re.search(rf"^{key} = ", p.read_text(), re.M)]) >= 2
    )
    _rewrite(carrying[:1], rf"^{key} = .*\n", "")
    assert _problems(copy, recorded) == []


def test_an_array_of_tables_element_missing_a_required_key_fails(tmp_path: Path) -> None:
    (tmp_path / "kind").mkdir()
    rows = '[[rows]]\nk = "v"\nn = 1\n[[rows]]\nk = "w"\nn = 2\n'
    (tmp_path / "kind" / "a.toml").write_text(rows)
    recorded = cfs.build_format(tmp_path)
    assert recorded["kind"]["keys"]["rows[].n"]["required"] is True
    (tmp_path / "kind" / "a.toml").write_text('[[rows]]\nk = "v"\nn = 1\n[[rows]]\nk = "w"\n')
    assert cfs.evaluate_format(recorded, cfs.build_format(tmp_path)) == [
        "kind: required key rows[].n is missing from a file or table that had to have it"
    ]


# --------------------------------------------------------------------------- data-keyed maps


def test_data_keyed_maps_record_one_value_schema(copy: Path) -> None:
    keys = cfs.build_format(copy)
    assert keys["prices"]["keys"]["aurora_mysql.standard.instance_hourly.*"] == {"types": ["float"]}
    assert keys["prices"]["keys"]["aurora_mysql.standard"]["required"] is True
    assert keys["prices"]["keys"]["published.*"] == {"types": ["date"]}
    assert keys["prices"]["keys"]["region"]["required"] is True
    assert keys["model_prices"]["keys"]["*.*.cache_read"] == {"required": True, "types": ["float"]}
    assert not [k for k in keys["prices"]["keys"] if '"db.' in k or '"cache.' in k]
    assert not [k for k in keys["model_prices"]["keys"] if "opus" in k]


def test_an_instance_type_added_or_removed_passes(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    text = (copy / "prices.toml").read_text()
    text = text.replace('"db.t4g.medium" = 0.073\n', "", 1)
    text = text.replace(
        '"db.r6g.large" = 0.26\n', '"db.r6g.large" = 0.26\n"db.r9g.large" = 0.5\n', 1
    )
    (copy / "prices.toml").write_text(text)
    assert _problems(copy, recorded) == []


def test_an_offer_date_removed_passes(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    _rewrite([copy / "prices.toml"], r"^AWSDataTransfer = .*\n", "")
    assert _problems(copy, recorded) == []


def test_a_model_renamed_passes(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    text = (copy / "model_prices.toml").read_text().replace('"opus-5.5"', '"opus-6"')
    (copy / "model_prices.toml").write_text(text)
    assert _problems(copy, recorded) == []


def test_an_instance_price_retyped_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    _rewrite([copy / "prices.toml"], r'^"db.t4g.medium" = 0.073$', '"db.t4g.medium" = "0.073"')
    assert _problems(copy, recorded) == [
        "prices: key aurora_mysql.standard.instance_hourly.* retyped: was float, now also string"
    ]


def test_a_model_price_field_dropped_from_one_model_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    _rewrite([copy / "model_prices.toml"], r"^cache_read = .*\n", "")
    assert _problems(copy, recorded) == [
        "model_prices: required key *.*.cache_read is missing from a file or table that had "
        "to have it"
    ]


def test_a_literal_key_of_a_pattern_is_not_collapsed(tmp_path: Path) -> None:
    (tmp_path / "prices.toml").write_text(
        'region = "r"\n[published]\nA = 2026-01-01\n[x.y]\nfee = 1.0\n'
        '[x.y.instance_hourly]\n"i.1" = 1.0\n'
    )
    assert set(cfs.build_format(tmp_path)["prices"]["keys"]) == {
        "region",
        "published",
        "published.*",
        "x",
        "x.y",
        "x.y.fee",
        "x.y.instance_hourly",
        "x.y.instance_hourly.*",
    }


def test_a_price_section_removed_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    text = (copy / "prices.toml").read_text()
    start = text.index("[aurora_mysql.io_optimized]\n")
    end = text.index("\n[", text.index("[aurora_mysql.io_optimized.instance_hourly]\n") + 1)
    (copy / "prices.toml").write_text(text[:start] + text[end + 1 :])
    problems = _problems(copy, recorded)
    assert "prices: key aurora_mysql.io_optimized removed or renamed" in problems
    assert "prices: key aurora_mysql.io_optimized.acu_price_per_hour removed or renamed" in problems


def test_a_field_dropped_from_one_price_section_fails(copy: Path) -> None:
    recorded = cfs.build_format(copy)
    _rewrite([copy / "prices.toml"], r"^acu_price_per_hour = 0\.12\n", "")
    assert _problems(copy, recorded) == [
        "prices: key aurora_mysql.standard.acu_price_per_hour removed or renamed"
    ]


def test_key_paths_cover_nested_tables_and_arrays_of_tables(tmp_path: Path) -> None:
    (tmp_path / "kind").mkdir()
    (tmp_path / "kind" / "a.toml").write_text(
        'id = "a"\ntags = ["x"]\n[t]\nn = 1\n"d.k" = 2\n[[rows]]\nk = "v"\nopt = true\n'
    )
    (tmp_path / "kind" / "b.toml").write_text('id = "b"\n[[rows]]\nk = "w"\n')
    keys = cfs.build_format(tmp_path)["kind"]["keys"]
    assert keys == {
        "id": {"types": ["string"], "required": True},
        "rows": {"types": ["array"], "required": True},
        "rows[]": {"types": ["table"]},
        "rows[].k": {"types": ["string"], "required": True},
        "rows[].opt": {"types": ["boolean"], "required": False},
        "t": {"types": ["table"], "required": False},
        't."d.k"': {"types": ["integer"], "required": True},
        "t.n": {"types": ["integer"], "required": True},
        "tags": {"types": ["array"], "required": False},
        "tags[]": {"types": ["string"]},
    }


def _git(repo: Path, *args: str) -> None:
    subprocess.run(  # nosec B603 B607 -- git with fixed arguments
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_write_released_reads_the_tag_not_the_working_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "catalog" / "engines").mkdir(parents=True)
    engine = repo / "src" / "catalog" / "engines" / "x.toml"
    engine.write_text('id = "x"\nold = 1\n')
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "one")
    _git(repo, "tag", "v1.0.0")
    engine.write_text('id = "x"\nnew = 1\n')
    snapshot = tmp_path / "snapshot.json"
    document = cfs.write_released("v1.0.0", repo=repo, path=snapshot)
    assert document["released"] == "v1.0.0"
    assert set(document["kinds"]["engines"]["keys"]) == {"id", "old"}
    assert json.loads(snapshot.read_text()) == document
    fixture = cfs.record_files("v1.0.0", repo=repo, directory=tmp_path / "files")
    assert fixture == tmp_path / "files" / "v1.0.0"
    assert sorted(p.relative_to(fixture).as_posix() for p in fixture.rglob("*")) == [
        "engines",
        "engines/x.toml",
    ]
    assert (fixture / "engines" / "x.toml").read_text() == 'id = "x"\nold = 1\n'


def test_the_command_line_needs_a_tag_or_draft(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    written: list[object] = []

    def fake_write(tag: str | None) -> dict[str, Any]:
        written.append(tag)
        return {"kinds": {}}

    monkeypatch.setattr(cfs, "write_released", fake_write)
    with pytest.raises(SystemExit):
        cfs.main(["--write-released", ""])
    with pytest.raises(SystemExit):
        cfs.main(["--write-released", "v1.0.0", "--draft"])
    assert cfs.main(["--draft"]) == 0
    assert written == [None]


def test_write_released_refuses_a_tag_that_does_not_exist(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    with pytest.raises(SystemExit, match="not a tag"):
        cfs.write_released("v9.9.9", repo=repo, path=tmp_path / "s.json")


def test_the_snapshot_and_the_release_files_are_protected_from_hand_edits() -> None:
    from scripts.hooks.block_baseline_edit import PROTECTED, regeneration_command

    rel = cfs.SNAPSHOT_PATH.relative_to(cfs.REPO).as_posix()
    assert PROTECTED[rel] == cfs.WRITE_RELEASED_COMMAND
    fixture = cfs.RELEASE_FILES_DIR / "v0.4.0-beta.2" / "patterns" / "caching.toml"
    assert fixture.is_file()
    assert regeneration_command(str(fixture)) == cfs.RECORD_FILES_COMMAND
