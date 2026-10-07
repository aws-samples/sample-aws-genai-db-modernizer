"""Offline collections declare their engine via ``metadata.engine`` (#381) when the
collector script sets it (SQL Server), falling back to the version string (every
other collector script) when it does not.
"""

from __future__ import annotations

import pytest

from src.tools.database.offline_parser import detect_source_engine, offline_version_label


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


class TestMetadataEngineField:
    """#381: ``collect-sqlserver.sql`` sets ``metadata.engine`` explicitly; before
    this fix, ``detect_source_engine`` ignored it and mislabelled every SQL Server
    offline collection as MySQL."""

    def test_sqlserver_metadata_engine_is_read_first(self) -> None:
        # The SQL Server script writes version_full/product_version, not version.
        meta = {
            "engine": "sqlserver",
            "version_full": "Microsoft SQL Server 2019 ...",
            "product_version": "15.0.2000.5",
        }
        assert detect_source_engine(meta) == ("sqlserver", 1433)

    def test_metadata_engine_wins_over_a_misleading_version_string(self) -> None:
        meta = {"engine": "sqlserver", "version": "something with postgres in it"}
        assert detect_source_engine(meta) == ("sqlserver", 1433)

    def test_unrecognized_metadata_engine_falls_back_to_version_string(self) -> None:
        meta = {"engine": "not_a_real_engine", "version": "PostgreSQL 16.1"}
        assert detect_source_engine(meta) == ("postgresql", 5432)

    def test_oracle_version_banner_without_an_engine_field(self) -> None:
        # The Oracle collector script sets no ``engine`` field, only ``version``
        # (the BANNER string).
        meta = {"version": "Oracle Database 19c Enterprise Edition Release 19.0.0.0.0"}
        assert detect_source_engine(meta) == ("oracle", 1521)

    def test_sql_server_version_banner_without_an_engine_field(self) -> None:
        meta = {"version": "Microsoft SQL Server 2019 (RTM) ..."}
        assert detect_source_engine(meta) == ("sqlserver", 1433)

    def test_mysql_metadata_engine_field(self) -> None:
        assert detect_source_engine({"engine": "mysql", "version": "8.0.35"}) == (
            "mysql",
            3306,
        )


class TestOfflineVersionLabel:
    """#381 review: wave 1's rationale must never embed the full @@VERSION banner
    (newlines, tabs, build date) -- a short, human-readable label only.

    #381 review round 2: a SQL Server banner's first line still starts with
    "Microsoft", not "SQL Server" -- taking it verbatim (as round 1 did) duplicated
    the engine name in wave 1's rationale ("SQL Server Microsoft SQL Server 2019
    (RTM) - 15.0.2000.5"). It is now run through ``_parse_sqlserver_version`` to
    extract just the build number instead.
    """

    def test_prefers_product_version(self) -> None:
        meta = {
            "product_version": "15.0.2000.5",
            "version_full": "Microsoft SQL Server 2019 (RTM) - 15.0.2000.5 (X64) \n\tJun "
            "15 2019 13:51:40 \n\tCopyright (C) 2019 Microsoft Corporation\n\tDeveloper "
            "Edition (64-bit) on Windows 10 Pro 10.0 <X64>",
        }
        assert offline_version_label(meta) == "15.0.2000.5"

    def test_sql_server_banner_in_version_full_extracts_the_build_number(self) -> None:
        meta = {
            "version_full": "Microsoft SQL Server 2019 (RTM) - 15.0.2000.5 (X64) \n\tJun "
            "15 2019 13:51:40 \n\tCopyright (C) 2019 Microsoft Corporation"
        }
        assert offline_version_label(meta) == "15.0.2000.5"

    def test_sql_server_banner_in_version_extracts_the_build_number(self) -> None:
        meta = {"version": "Microsoft SQL Server 2019 (RTM) - 15.0.2000.5 (X64)"}
        assert offline_version_label(meta) == "15.0.2000.5"

    def test_sql_server_banner_with_no_build_number_falls_back_to_the_marketing_year(
        self,
    ) -> None:
        meta = {"version_full": "Microsoft SQL Server 2022\n\tDeveloper Edition"}
        assert offline_version_label(meta) == "2022"

    def test_falls_back_to_the_first_line_of_version(self) -> None:
        meta = {"version": "Oracle Database 19c Enterprise Edition Release 19.0.0.0.0\nPL/SQL"}
        assert (
            offline_version_label(meta)
            == "Oracle Database 19c Enterprise Edition Release 19.0.0.0.0"
        )

    def test_unknown_when_neither_field_is_present(self) -> None:
        assert offline_version_label({}) == "unknown"
        assert offline_version_label(None) == "unknown"
