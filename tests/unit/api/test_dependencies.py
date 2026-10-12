"""src.api.dependencies: the FastAPI dependencies that validate a
database_name or filename query/path parameter outside a request body model
(#505). Exercised directly (not through TestClient) so these are fast and
isolate the dependency logic from routing/encoding edge cases, which are
covered end-to-end in test_database_name_query_param_validation.py."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.dependencies import database_name_param, validated_filename


class TestDatabaseNameParam:
    def test_accepts_a_valid_name(self) -> None:
        assert database_name_param("wordpress") == "wordpress"

    def test_strips_and_returns_the_stripped_value(self) -> None:
        assert database_name_param("abc\n") == "abc"

    @pytest.mark.parametrize("bad_name", ["../escape", "..", ".", "a/b", "a b", "", "café"])
    def test_rejects_invalid_names_with_422(self, bad_name: str) -> None:
        with pytest.raises(HTTPException) as exc_info:
            database_name_param(bad_name)
        assert exc_info.value.status_code == 422
        assert "database_name" in exc_info.value.detail


class TestValidatedFilename:
    def test_accepts_a_normal_filename(self) -> None:
        assert validated_filename("collector-output.json") == "collector-output.json"

    @pytest.mark.parametrize(
        "bad_filename",
        ["", ".", "..", "a/b", "a\\b", "../escape", "a\x00b"],
    )
    def test_rejects_unsafe_filenames_with_422(self, bad_filename: str) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validated_filename(bad_filename)
        assert exc_info.value.status_code == 422
        assert "filename" in exc_info.value.detail
