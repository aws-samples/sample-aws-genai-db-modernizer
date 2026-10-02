"""Offline collections declare their engine only through the version string."""

from __future__ import annotations

import pytest

from src.tools.database.offline_parser import detect_source_engine


@pytest.mark.parametrize(
    "version, expected",
    [
        ("PostgreSQL 15.4 on x86_64-pc-linux-gnu", ("postgresql", 5432)),
        ("postgres 13", ("postgresql", 5432)),
        ("8.0.35", ("mysql", 3306)),
        ("8.0.mysql_aurora.3.04.0", ("mysql", 3306)),
        ("", ("mysql", 3306)),
        (None, ("mysql", 3306)),
    ],
)
def test_detect_source_engine(version, expected) -> None:
    assert detect_source_engine({"version": version}) == expected


def test_missing_metadata_defaults_to_mysql() -> None:
    assert detect_source_engine({}) == ("mysql", 3306)
    assert detect_source_engine(None) == ("mysql", 3306)
