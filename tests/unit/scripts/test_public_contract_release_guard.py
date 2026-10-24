"""The released-contract guard for the public ``assess()`` contract (#1184), on small
synthetic models: one case per rule.

``evaluate_against_released`` compares the public contract's current shape with the
shape recorded at the last release (``tests/contract/assess_released_snapshot.json``),
with the same ``field_summary``/``diff_fields`` classification the internal contracts'
snapshot uses. A breaking change (a field removed or renamed, a type changed, an enum
value removed or changed) needs a MAJOR bump; once a release is recorded, an additive
change needs at least a MINOR bump.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from scripts.contract_schema_snapshot import (
    build_public_snapshot,
    check_released_against,
    evaluate_against_released,
    public_snapshot_at,
    released_document,
    write_released,
)

KEY = "assessment.Result"


class Base(BaseModel):
    contract_version: Literal["1.0"] = "1.0"
    engine: str | None = None
    role: Literal["owner", "cache_layer"]


def _released(model: type[BaseModel], tag: str | None = "v1.0.0") -> dict[str, Any]:
    return {"released": tag, "contracts": build_public_snapshot({KEY: model})}


def _problems(released: dict[str, Any], model: type[BaseModel]) -> list[str]:
    return evaluate_against_released(released, build_public_snapshot({KEY: model}))


def test_an_unchanged_contract_passes() -> None:
    assert _problems(_released(Base), Base) == []


def test_a_removed_field_needs_a_major_bump() -> None:
    class Removed(BaseModel):
        contract_version: Literal["1.0"] = "1.0"
        role: Literal["owner", "cache_layer"]

    [problem] = _problems(_released(Base), Removed)
    assert "engine: removed (major)" in problem and "MAJOR" in problem


def test_a_renamed_field_needs_a_major_bump() -> None:
    class Renamed(BaseModel):
        contract_version: Literal["1.0"] = "1.0"
        engine_id: str | None = None
        role: Literal["owner", "cache_layer"]

    [problem] = _problems(_released(Base), Renamed)
    assert "engine: removed (major)" in problem


def test_a_changed_enum_value_needs_a_major_bump() -> None:
    class Changed(BaseModel):
        contract_version: Literal["1.0"] = "1.0"
        engine: str | None = None
        role: Literal["owner", "cache"]

    [problem] = _problems(_released(Base), Changed)
    assert "enum value removed: 'cache_layer'" in problem


def test_a_retyped_field_needs_a_major_bump() -> None:
    class Retyped(BaseModel):
        contract_version: Literal["1.0"] = "1.0"
        engine: int | None = None
        role: Literal["owner", "cache_layer"]

    [problem] = _problems(_released(Base), Retyped)
    assert "engine" in problem and "MAJOR" in problem


def test_a_minor_bump_does_not_cover_a_breaking_change() -> None:
    class Minor(BaseModel):
        contract_version: Literal["1.1"] = "1.1"
        role: Literal["owner", "cache_layer"]

    [problem] = _problems(_released(Base), Minor)
    assert "MAJOR" in problem and "'1.1'" in problem


def test_a_major_bump_covers_a_breaking_change() -> None:
    class Major(BaseModel):
        contract_version: Literal["2.0"] = "2.0"
        role: Literal["owner", "cache"]

    assert _problems(_released(Base), Major) == []


def test_an_additive_change_after_a_release_needs_a_minor_bump() -> None:
    class Added(BaseModel):
        contract_version: Literal["1.0"] = "1.0"
        engine: str | None = None
        role: Literal["owner", "cache_layer", "consolidated"]

    [problem] = _problems(_released(Base), Added)
    assert "MINOR" in problem and "enum value added: 'consolidated'" in problem

    class AddedAndBumped(Added):
        contract_version: Literal["1.1"] = "1.1"  # type: ignore[assignment]

    assert _problems(_released(Base), AddedAndBumped) == []


def test_an_additive_change_before_the_first_release_needs_no_bump() -> None:
    """docs/contracts/assess.md: 1.0 is not released yet, so additions keep 1.0."""

    class Added(BaseModel):
        contract_version: Literal["1.0"] = "1.0"
        engine: str | None = None
        role: Literal["owner", "cache_layer"]
        reason: str | None = None

    assert _problems(_released(Base, tag=None), Added) == []

    class Removed(BaseModel):
        contract_version: Literal["1.0"] = "1.0"
        role: Literal["owner", "cache_layer"]

    # A breaking change is refused before the first release too: callers build on it.
    assert _problems(_released(Base, tag=None), Removed)


def test_a_version_never_goes_back() -> None:
    class Older(BaseModel):
        contract_version: Literal["0.9"] = "0.9"
        engine: str | None = None
        role: Literal["owner", "cache_layer"]

    [problem] = _problems(_released(Base), Older)
    assert "'1.0' -> '0.9'" in problem


def test_a_removed_or_unrecorded_contract_is_reported() -> None:
    released = _released(Base)
    assert evaluate_against_released(released, {}) == [
        f"{KEY}: public contract removed since the last release (a MAJOR change); "
        "callers built on it"
    ]
    [problem] = evaluate_against_released(
        {"released": "v1.0.0", "contracts": {}}, released["contracts"]
    )
    assert problem.startswith(f"{KEY}: public contract not in the released snapshot")


# --------------------------------------------------------------------------- recording


_MODELS = """from typing import Literal

from pydantic import BaseModel


class AssessmentRequest(BaseModel):
    contract_version: Literal["1.0"] = "1.0"
    database_name: str


class AssessmentResult(BaseModel):
    contract_version: Literal["1.0"] = "1.0"
    role: Literal[{roles}]
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit_models(repo: Path, roles: str, message: str) -> None:
    models = repo / "src" / "assessment" / "models.py"
    models.write_text(_MODELS.format(roles=roles))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def toy_repo(tmp_path: Path) -> Path:
    """A throwaway repository whose ``src.assessment.models`` holds two tiny public
    models: ``v1.0.0`` has roles owner, cache_layer and consolidated; HEAD drops cache_layer."""
    repo = tmp_path / "toy"
    (repo / "src" / "assessment").mkdir(parents=True)
    (repo / "tests" / "contract").mkdir(parents=True)
    (repo / "src" / "__init__.py").write_text("")
    (repo / "src" / "assessment" / "__init__.py").write_text("")
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    _commit_models(repo, '"owner", "cache_layer", "consolidated"', "feat: v1.0.0 models")
    _git(repo, "tag", "v1.0.0")
    _commit_models(repo, '"owner", "consolidated"', "feat!: drop cache_layer")
    return repo


def _snapshot_path(repo: Path) -> Path:
    return repo / "tests" / "contract" / "assess_released_snapshot.json"


def _roles(document: dict[str, Any]) -> list[str]:
    fields = document["contracts"]["assessment.AssessmentResult"]["fields"]
    return sorted(fields["role"]["schema"]["values"])


def test_write_released_refuses_a_tag_that_does_not_exist(toy_repo: Path) -> None:
    with pytest.raises(SystemExit, match="'v9.9.9' is not a tag"):
        write_released("v9.9.9", repo=toy_repo, path=_snapshot_path(toy_repo))
    assert not _snapshot_path(toy_repo).exists()


def test_write_released_records_the_shape_at_the_tag_not_the_working_tree(
    toy_repo: Path,
) -> None:
    """Run on a later main, it still records what the tag shipped."""
    document = write_released("v1.0.0", repo=toy_repo, path=_snapshot_path(toy_repo))
    assert document["released"] == "v1.0.0"
    assert _roles(document) == ["cache_layer", "consolidated", "owner"]
    assert json.loads(_snapshot_path(toy_repo).read_text()) == document
    assert public_snapshot_at("HEAD", toy_repo) != document["contracts"]


def _commit_snapshot(repo: Path, document: dict[str, Any]) -> str:
    _snapshot_path(repo).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: record")
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_a_branch_cannot_re_record_the_baseline_it_is_checked_against(toy_repo: Path) -> None:
    """main records v1.0.0 (cache_layer present); HEAD's models drop it. Re-recording the
    branch's own copy (no tag, or a tag that does not exist) is refused, and the check
    still uses main's copy, so the breaking change fails."""
    path = _snapshot_path(toy_repo)
    base = _commit_snapshot(toy_repo, released_document("v1.0.0", toy_repo))
    head = public_snapshot_at("HEAD", toy_repo)
    assert check_released_against(base, toy_repo, path, head)  # the break, against main
    for tag in (None, "v9.9.9"):
        path.write_text(json.dumps({"released": tag, "contracts": head}))
        problems = check_released_against(base, toy_repo, path, head)
        assert any("without recording an existing release tag" in p for p in problems)
        assert any("enum value removed: 'cache_layer'" in p for p in problems)


def test_a_recording_must_equal_the_tags_own_shape(toy_repo: Path) -> None:
    path = _snapshot_path(toy_repo)
    _git(toy_repo, "tag", "v2.0.0")  # HEAD's models, as a release
    base = _commit_snapshot(toy_repo, released_document("v1.0.0", toy_repo))
    head = public_snapshot_at("HEAD", toy_repo)
    # A real tag with another shape than the tag's own is refused.
    path.write_text(
        json.dumps(
            {"released": "v2.0.0", "contracts": released_document("v1.0.0", toy_repo)["contracts"]}
        )
    )
    [problem, *_] = check_released_against(base, toy_repo, path, head)
    assert "with a shape that is not v2.0.0's" in problem
    # The release step's recording of v2.0.0 is accepted, and becomes the baseline.
    write_released("v2.0.0", repo=toy_repo, path=path)
    assert check_released_against(base, toy_repo, path, head) == []


def test_with_no_copy_on_the_base_the_branch_copy_is_used(toy_repo: Path) -> None:
    path = _snapshot_path(toy_repo)
    write_released("v1.0.0", repo=toy_repo, path=path)
    head = public_snapshot_at("HEAD", toy_repo)
    [problem] = check_released_against("v1.0.0", toy_repo, path, head)
    assert "enum value removed: 'cache_layer'" in problem
