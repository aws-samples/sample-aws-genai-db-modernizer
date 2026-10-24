"""``load_collection`` reads the collection scripts' raw output for ``assess()`` (#1487)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.assessment import (
    AssessmentOptions,
    AssessmentRequest,
    CollectionLoadError,
    assess,
    load_collection,
)
from tests.unit.assessment.conftest import REPO, SAMPLES, sample_collection


def _zip(name: str) -> Path:
    return REPO / "docs" / "examples" / name / f"{name}.zip"


def _text(name: str) -> str:
    with zipfile.ZipFile(_zip(name)) as z:
        return z.read(f"{name}-collection.json").decode("utf-8")


MINIMAL: dict[str, list[object]] = {"tables": [], "queries": []}


@pytest.mark.parametrize("name", SAMPLES)
def test_bundled_sample_zip_loads_and_assesses(name: str) -> None:
    collection = load_collection(_zip(name))
    assert collection == sample_collection(name)
    result = assess(
        AssessmentRequest(
            database_name=name,
            collection=collection,
            options=AssessmentOptions(run_through="analysis"),
        )
    )
    assert result.statements


def test_str_with_header_line_and_trailing_chatter() -> None:
    text = "collection_output\n" + json.dumps(MINIMAL) + "\n(1 rows affected)\n"
    assert load_collection(text) == MINIMAL


def test_bytes_with_bom_and_header() -> None:
    raw = ("﻿collection_output\n" + json.dumps(MINIMAL)).encode("utf-8")
    assert load_collection(raw) == MINIMAL


@pytest.mark.parametrize("suffix", [".json", ".txt"])
def test_path_to_text_file(tmp_path: Path, suffix: str) -> None:
    path = tmp_path / f"out{suffix}"
    path.write_text("collection_output\n" + json.dumps(MINIMAL), encoding="utf-8")
    assert load_collection(path) == MINIMAL


def test_text_and_zip_of_a_sample_agree() -> None:
    assert load_collection(_text("trailgear")) == load_collection(_zip("trailgear"))


def test_no_json_object(tmp_path: Path) -> None:
    with pytest.raises(CollectionLoadError, match="no JSON object found"):
        load_collection("collection_output\n")
    with pytest.raises(CollectionLoadError, match="no JSON object found"):
        load_collection(b"nothing here")


def test_more_than_one_object_in_text() -> None:
    with pytest.raises(CollectionLoadError, match="more than one"):
        load_collection(json.dumps(MINIMAL) + "\n" + json.dumps(MINIMAL))


def test_invalid_json() -> None:
    with pytest.raises(CollectionLoadError, match="not valid JSON"):
        load_collection('{"tables": [,]}')


def test_zip_with_two_collections(tmp_path: Path) -> None:
    path = tmp_path / "two.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("a.json", json.dumps(MINIMAL))
        z.writestr("b.json", json.dumps(MINIMAL))
    with pytest.raises(CollectionLoadError, match="more than one collection"):
        load_collection(path)


def test_zip_with_no_collection(tmp_path: Path) -> None:
    path = tmp_path / "none.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("readme.md", "hello")
    with pytest.raises(CollectionLoadError, match="no collection"):
        load_collection(path)


def test_not_a_zip(tmp_path: Path) -> None:
    path = tmp_path / "bad.zip"
    path.write_bytes(b"not a zip")
    with pytest.raises(CollectionLoadError, match="not a valid zip"):
        load_collection(path)


@pytest.mark.parametrize("body", [{"tables": []}, {"queries": []}, {"other": 1}])
def test_not_a_collection(body: dict[str, object]) -> None:
    with pytest.raises(CollectionLoadError, match="not a collection"):
        load_collection(json.dumps(body))


def test_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(CollectionLoadError, match="file not found"):
        load_collection(tmp_path / "missing.json")


def test_non_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "latin.txt"
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(CollectionLoadError, match="not UTF-8"):
        load_collection(path)


def test_error_is_a_value_error() -> None:
    assert issubclass(CollectionLoadError, ValueError)
