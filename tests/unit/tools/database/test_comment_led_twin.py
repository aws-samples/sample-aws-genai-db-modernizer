"""A commented statement routes like its uncommented twin (#1178).

A sample with every query added a second time behind leading comments, run in this
process through collect, triage and the deterministic analyses: each twin gets the
same ``query_type``, the same stored facts, the same access-pattern class and the
same score on every engine as its original. Before #1178 every commented twin was
``OTHER`` (trailgear: 88 of its 95 twins scored differently on some engine).
"""

from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from src.agents.referee.query_pattern import QueryPatternModel
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.storage.local_store import LocalArtifactStore
from src.tools.database.collection_text import extract_json_object
from src.tools.database.offline_parser import parse_offline_collection

_REPO = Path(__file__).resolve().parents[4]
# sample -> the comments its twins open with, in the sample's own dialect
_TAGS = {
    "trailgear": "-- endpoint: SupportController.GetTicket\n/* request: 42 */\n",  # SQL Server
    "wordpress": "# endpoint: wp-admin/post.php\n-- request: 42\n/* trace */ ",  # MySQL
    # PostgreSQL nests block comments: the whole "/* x /* y */ z */" is one comment.
    "discourse": "-- a /* nested */\n/* x /* y */ z */ ",
}


def _sample(name: str) -> dict:
    with zipfile.ZipFile(_REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        member = next(n for n in z.namelist() if n.endswith("-collection.json"))
        data: dict = json.loads(extract_json_object(z.read(member).decode("utf-8")))
    return data


def _with_twins(base: dict, tag: str) -> tuple[dict, dict[str, str]]:
    """``base`` with a commented twin of every query; original digest -> twin digest."""
    raw = copy.deepcopy(base)
    twins: dict[str, str] = {}
    for i, q in enumerate(list(raw["queries"])):
        twin_digest = f"0xTWIN{i:06d}"
        raw["queries"].append({**q, "digest": twin_digest, "query_text": tag + q["query_text"]})
        twins[str(q["digest"])] = twin_digest
    return raw, twins


@pytest.fixture(scope="module", params=sorted(_TAGS))
def run(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict:
    import scripts.run_assessment as run_assessment

    name = request.param
    raw, twins = _with_twins(_sample(name), _TAGS[name])
    tmp = tmp_path_factory.mktemp(f"twin-{name}")
    collection = tmp / f"{name}-collection.json"
    collection.write_text(json.dumps(raw), encoding="utf-8")
    store = LocalArtifactStore(base_dir=str(tmp / "artifacts"))
    mp = pytest.MonkeyPatch()
    mp.chdir(tmp)  # phase_collect writes .modernizer-state.json here
    try:
        job_id, db = run_assessment.phase_collect(str(collection), name, store)
        selected = run_assessment.phase_triage(store, job_id, db)
        run_assessment.phase_analysis(store, job_id, db, selected)
    finally:
        mp.undo()
    base = f"{db}/{job_id}"
    collector = store.read_json(f"{base}/collector/output.json")
    by_id = {q["query_id"]: q for q in collector["queries"]["query_patterns"]}
    pairs = [(by_id[a], by_id[t]) for a, t in twins.items() if a in by_id and t in by_id]
    return {
        "name": name,
        "collector": collector,
        "triage": store.read_json(f"{base}/referee-triage/triage.json"),
        "analyses": {e: store.read_json(f"{base}/analysis-{e}/analysis.json") for e in selected},
        "pairs": pairs,
    }


def _facts(query: dict) -> dict:
    # The stored facts, without the hash of the text they were read from.
    return {k: v for k, v in (query.get("parsed_facts") or {}).items() if k != "text_sha256"}


def test_every_twin_has_the_same_type_and_facts(run: dict) -> None:
    pairs = run["pairs"]
    assert len(pairs) >= 50
    assert any(plain["query_type"] != "OTHER" for plain, _ in pairs)
    for plain, tagged in pairs:
        assert tagged["query_text"].startswith(_TAGS[run["name"]][:2])
        assert tagged["query_type"] == plain["query_type"], plain["query_text"][:80]
        assert _facts(tagged) == _facts(plain), plain["query_text"][:80]


def test_every_twin_has_the_same_access_class_and_engine_scores(run: dict) -> None:
    model = QueryPatternModel.from_pipeline(run["collector"], run["analyses"])
    ctx = ScoringContext.from_pipeline(run["analyses"], run["triage"], run["collector"])
    differing = []
    for plain, tagged in run["pairs"]:
        scores = {
            engine: (
                score_query_for_engine(plain, engine, ctx, exact=True).score,
                score_query_for_engine(tagged, engine, ctx, exact=True).score,
            )
            for engine in run["analyses"]
        }
        if model.classification(plain) != model.classification(tagged) or any(
            a != b for a, b in scores.values()
        ):
            differing.append(plain["query_text"][:80])
    assert not differing


@pytest.mark.passes_on_main
def test_query_id_is_still_the_digest_else_a_hash_of_the_original_text() -> None:
    raw, twins = _with_twins(_sample("trailgear"), _TAGS["trailgear"])
    twin = raw["queries"][-1]
    del twin["digest"]
    parsed = parse_offline_collection(raw)
    ids = {q["query_id"] for q in parsed["queries"]}
    assert set(twins) <= ids  # every original keeps its digest
    # A query without a digest: the hash of its text as collected, comment included.
    assert hashlib.sha256(twin["query_text"].encode()).hexdigest()[:16] in ids
