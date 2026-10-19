"""The one rule that turns a collection file name into a database name (#1096)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.shared.collection_database_name import (
    database_name_from_collection_file,
    resolve_database_name,
)
from src.shared.database_name import DATABASE_NAME_PATTERN, validate_database_name


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("wordpress-collection.json", "wordpress"),
        ("docs/examples/discourse/discourse-collection.json", "discourse"),
        ("/abs/dir/my.app-collection.json", "my_app"),
        ("sales data.json", "sales_data"),
        ("sales.JSON", "sales"),
        ("a.b.c.json", "a_b_c"),
        ("already_ok-1.json", "already_ok-1"),
        ("no-extension", "no-extension"),
        ("x-collection-collection.json", "x-collection"),
        ("-lead.json", "_lead"),
        ("--x-collection.json", "_-x"),
        ("café.json", "caf_"),
        ("日本語.json", "___"),
        ("a/b\\c.json", "c"),
        (Path("dir") / "q-collection.json", "q"),
    ],
)
def test_derivation(path: object, expected: str) -> None:
    assert database_name_from_collection_file(path) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "path", [".json", "-collection.json", "", "dir/.json", "dir/-collection.json"]
)
def test_empty_falls_back_to_database(path: str) -> None:
    assert database_name_from_collection_file(path) == "database"


def test_long_names_are_cut_at_128() -> None:
    name = database_name_from_collection_file("a" * 300 + "-collection.json")
    assert name == "a" * 128


def test_long_name_is_cut_after_replacement() -> None:
    assert database_name_from_collection_file("é" * 200 + ".json") == "_" * 128


@pytest.mark.parametrize("path", ["..json", "...json", "..", ".", "a\n.json", " .json", "-.json"])
def test_result_always_validates(path: str) -> None:
    validate_database_name(database_name_from_collection_file(path))


def test_cap_matches_the_validator_pattern() -> None:
    assert DATABASE_NAME_PATTERN.endswith("{0,127}")
    assert len(database_name_from_collection_file("a" * 500)) == 128


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"metadata": {"database_name": "shop"}}, ("shop", "metadata")),
        ({"metadata": '{"database_name": "shop"}'}, ("shop", "metadata")),
        ({"metadata": '{"schema_name": "shop"}'}, ("shop", "schema_name")),
        ({"metadata": {"schema_name": "shop"}}, ("shop", "schema_name")),
        ({"metadata": {"database_name": "a", "schema_name": "b"}}, ("a", "metadata")),
        ({"metadata": {"database_name": "-lead"}}, ("my_app", "file_name")),
        ({"metadata": {"source_database": {"database_name": "shop"}}}, ("shop", "metadata")),
        ({"metadata": {"database_name": "../evil"}}, ("my_app", "file_name")),
        ({"metadata": {"database_name": ""}}, ("my_app", "file_name")),
        ({"metadata": {"database_name": 5}}, ("my_app", "file_name")),
        ({"metadata": "not json"}, ("my_app", "file_name")),
        ({"metadata": None}, ("my_app", "file_name")),
        ({}, ("my_app", "file_name")),
        ([], ("my_app", "file_name")),
    ],
)
def test_resolve_prefers_a_valid_metadata_name(data: object, expected: tuple[str, str]) -> None:
    assert resolve_database_name(data, "my.app-collection.json") == expected


def test_from_file_name_ignores_valid_metadata() -> None:
    data = {"metadata": {"database_name": "acme"}}
    assert resolve_database_name(data, "acme-prod-collection.json") == ("acme", "metadata")
    assert resolve_database_name(data, "acme-prod-collection.json", from_file_name=True) == (
        "acme-prod",
        "file_name",
    )
