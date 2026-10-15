"""Locking reads through the whole deterministic pipeline (#838, review of #871).

No bundled sample has a locking read, so this runs the wordpress sample with
copies of one hot, cached point read (``SELECT autoload FROM wp_options WHERE
option_name = ?``) added to it, each with a different lock or lock-like text:

- ``FOR NO KEY UPDATE`` and a ``WITH (HOLDLOCK)`` hint take a row lock: the
  ownership gate keeps them off every engine that is not a system of record,
  and the cache overlay never fronts them. The old gate regex missed both.
- ``'FOR UPDATE'`` in a string literal, ``/* FOR UPDATE */`` and
  ``-- FOR UPDATE`` take no lock: each copy is cached exactly like the
  original. The old regex counted all three as locks.
"""

from __future__ import annotations

import json
import zipfile

import pytest

from src.agents.referee.cache_overlay import NON_SYSTEM_OF_RECORD_ENGINES
from tests.e2e.pipeline import REPO, PipelineResult, run_pipeline_from_file

pytestmark = pytest.mark.e2e

ORIGINAL = "SELECT `autoload` FROM `wp_options` WHERE `option_name` = ?"
LOCKING = {
    "no-key-update": ORIGINAL + " FOR NO KEY UPDATE",
    "holdlock-hint": "SELECT `autoload` FROM `wp_options` WITH (HOLDLOCK) WHERE `option_name` = ?",
}
NOT_LOCKING = {
    "literal": ORIGINAL + " AND `autoload` != 'FOR UPDATE'",
    "block-comment": ORIGINAL + " /* FOR UPDATE */",
    "line-comment": ORIGINAL + " -- FOR UPDATE",
}


def _digest(name: str) -> str:
    return name.encode().hex().ljust(64, "0")[:64]


@pytest.fixture(scope="module")
def locking_run(tmp_path_factory: pytest.TempPathFactory) -> PipelineResult:
    tmp = tmp_path_factory.mktemp("locking-reads")
    with zipfile.ZipFile(REPO / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        z.extractall(tmp)
    header, body = (tmp / "wordpress-collection.json").read_text().split("\n", 1)
    collection = json.loads(body)
    base = next(q for q in collection["queries"] if q["query_text"] == ORIGINAL)
    for name, text in {**LOCKING, **NOT_LOCKING}.items():
        collection["queries"].append({**base, "query_text": text, "digest": _digest(name)})
    src = tmp / "locking-collection.json"
    src.write_text(header + "\n" + json.dumps(collection))
    return run_pipeline_from_file(src, "wordpress", tmp / "artifacts", job_id="locking-reads")


def _assignments(run: PipelineResult) -> list[dict]:
    """Every assignment version the run wrote, v1 first."""
    paths = sorted(
        run.job_dir().glob("assignment/v*/assignment.json"),
        key=lambda p: int(p.parent.name.lstrip("v")),
    )
    return [
        {qa["query_id"]: qa for qa in json.loads(p.read_text())["query_assignments"]} for p in paths
    ]


@pytest.mark.deterministic
def test_a_locking_read_is_never_owned_or_cached_by_a_non_system_of_record(
    locking_run: PipelineResult,
) -> None:
    for version in _assignments(locking_run):
        for name in LOCKING:
            qa = version[_digest(name)]
            assert qa["assigned_engine"] not in NON_SYSTEM_OF_RECORD_ENGINES, name
            assert not qa.get("cache_engine"), name


@pytest.mark.deterministic
def test_a_lock_word_in_a_literal_or_comment_is_cached_like_the_original(
    locking_run: PipelineResult,
) -> None:
    versions = _assignments(locking_run)
    original_id = next(
        q["query_id"]
        for q in json.loads((locking_run.job_dir() / "collector" / "output.json").read_text())[
            "queries"
        ]["query_patterns"]
        if q["query_text"] == ORIGINAL
    )
    # The fixture only proves something while the original is a cached hot read.
    assert versions[-1][original_id].get("cache_engine"), "pick another cached point read"
    for version in versions:
        for name in NOT_LOCKING:
            assert version[_digest(name)].get("cache_engine") == version[original_id].get(
                "cache_engine"
            ), name
