# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Routine bodies the credential scan flags are named in the routine sources' index
(#1933), by the repository's one scan (``src/shared/secret_scan.py``, which the CI
private scan of #2055 re-exports), never a scanner of their own. Its own rules run in
every image, without detect-secrets; the index says which rule set ran, and names each
flagged routine with the kinds found, never a value."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from src.agents.collector import routine_sources
from src.agents.collector.routine_sources import read_routine_sources, write_routine_sources
from src.contracts.routine_sources import RoutineSource, RoutineSourcesContract
from src.shared import secret_scan
from src.shared.contract_versioning import load_contract
from src.storage.local_store import LocalArtifactStore

_OUTPUT = {"metadata": {"source_database": {"engine": "mysql", "version": "8.0.36"}}}
# A credential's shape, not a real one: the scan finds the key name and the key id.
_LEAKY = (
    "CREATE PROCEDURE `sync`() BEGIN SET @password = 'not-a-real-one'; "  # pragma: allowlist secret
    "CALL push('AKIAIOSFODNN7EXAMPLE'); END"  # pragma: allowlist secret
)
_ADDRESS = "CREATE PROCEDURE `ping`() BEGIN CALL connect('8.8.8.8'); END"
_CLEAN = "CREATE FUNCTION `one`() RETURNS int RETURN 1"


def _source(name: str, text: str) -> RoutineSource:
    return RoutineSource(
        kind="procedure", routine_id=name, schema_name="shop", specific_name=name,
        create_statement=text, origin="show_create",
    )  # fmt: skip


def _index(root) -> dict:
    index: dict = json.loads((root / "shop/j/collector/routine_sources.json").read_text())
    return index


def _write(tmp_path, *sources: RoutineSource) -> LocalArtifactStore:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    write_routine_sources(store, "shop", "j", _OUTPUT, list(sources))
    return store


def test_a_body_that_looks_like_it_holds_a_credential_is_flagged_by_kind(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        store = _write(tmp_path, _source("sync", _LEAKY), _source("one", _CLEAN))
    scan = _index(tmp_path)["secret_scan"]
    assert scan["rule_set"] == secret_scan.rule_set()
    ((flag),) = scan["flagged"]
    assert (flag["kind"], flag["schema_name"], flag["specific_name"]) == (
        "procedure",
        "shop",
        "sync",
    )
    assert {"AWS Access Key", "password value"} <= set(flag["credential_kinds"])
    assert flag["address_kinds"] == []
    text = json.dumps(_index(tmp_path)) + caplog.text
    assert "AKIAIOSFODNN7EXAMPLE" not in text  # pragma: allowlist secret
    assert "not-a-real-one" not in text
    assert "1 routine body" in caplog.text and "sync" in caplog.text
    # the bodies themselves are carried unchanged
    assert [s.create_statement for s in read_routine_sources(store, "shop", "j")] == [
        _LEAKY, _CLEAN,
    ]  # fmt: skip


def test_a_hard_coded_address_is_listed_apart_from_credentials(tmp_path):
    _write(tmp_path, _source("ping", _ADDRESS))
    ((flag),) = _index(tmp_path)["secret_scan"]["flagged"]
    assert (flag["credential_kinds"], flag["address_kinds"]) == ([], ["Public IP (ipv4)"])


def test_clean_bodies_are_scanned_and_nothing_is_flagged(tmp_path):
    _write(tmp_path, _source("one", _CLEAN))
    assert _index(tmp_path)["secret_scan"] == {"rule_set": secret_scan.rule_set(), "flagged": []}


def test_the_own_rules_run_without_detect_secrets(tmp_path, monkeypatch):
    """The deployed images have no detect-secrets: the repository's own rules still find
    the key id and the password, and the index says only they ran."""
    monkeypatch.setitem(sys.modules, "detect_secrets", None)  # unimportable
    monkeypatch.setitem(sys.modules, "detect_secrets.core.scan", None)
    assert secret_scan._load_detect_secrets() == (None, None)
    monkeypatch.setattr(secret_scan, "_scan_line", None)
    monkeypatch.setattr(secret_scan, "_transient_settings", None)
    assert (secret_scan.detect_secrets_available(), secret_scan.rule_set()) == (False, "regex")
    _write(tmp_path, _source("sync", _LEAKY))
    scan = _index(tmp_path)["secret_scan"]
    assert scan["rule_set"] == "regex"
    assert {"AWS Access Key", "password value"} <= set(scan["flagged"][0]["credential_kinds"])


def test_the_dev_extra_adds_detect_secrets_on_top():
    assert secret_scan.rule_set() == "regex+detect-secrets"


def test_an_unknown_finding_kind_is_recorded_as_other(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_scan, "secret_kinds", lambda text: {"new/kind!"})
    _write(tmp_path, _source("sync", _LEAKY))
    assert _index(tmp_path)["secret_scan"]["flagged"][0]["credential_kinds"] == ["other"]


def test_a_failing_scan_never_stops_the_write(tmp_path, monkeypatch):
    def boom(text):
        raise RuntimeError("scanner broke")

    monkeypatch.setattr(secret_scan, "secret_kinds", boom)
    store = _write(tmp_path, _source("sync", _LEAKY))
    assert _index(tmp_path)["secret_scan"]["flagged"] == []
    assert len(list(read_routine_sources(store, "shop", "j"))) == 1


@pytest.mark.passes_on_main
def test_a_scan_session_that_cannot_start_never_stops_the_write(tmp_path, monkeypatch):
    """detect-secrets' settings that fail to build are a failed scan of each body, as
    when each scan built its own (#2098): the write goes on."""

    def broken(config):
        raise RuntimeError("settings broke")

    monkeypatch.setattr(secret_scan, "_transient_settings", broken)
    store = _write(tmp_path, _source("sync", _LEAKY), _source("one", _CLEAN))
    assert _index(tmp_path)["secret_scan"]["flagged"] == []
    assert len(list(read_routine_sources(store, "shop", "j"))) == 2


def test_one_write_builds_the_scan_settings_once(tmp_path, monkeypatch):
    """Building detect-secrets' settings costs more than scanning a short body: a write
    builds them once for all its bodies, not once per body (#2098)."""
    built = []
    transient = secret_scan._transient_settings

    def counting(config):
        built.append(config)
        return transient(config)

    monkeypatch.setattr(secret_scan, "_transient_settings", counting)
    _write(tmp_path, *(_source(f"r{i}", _LEAKY) for i in range(5)))
    assert len(built) == 1
    assert len(_index(tmp_path)["secret_scan"]["flagged"]) == 5


def test_the_ci_private_scan_and_the_collector_share_one_definition():
    from ci.llm import private_scan

    assert private_scan.secret_findings.__module__ == "src.shared.secret_scan"
    # the collector runs where ci/ is not packaged: it never imports it
    assert "ci.llm" not in Path(routine_sources.__file__).read_text()


@pytest.mark.passes_on_main
def test_an_index_written_before_the_scan_still_loads():
    old = {"contract_version": "1.0", "job_id": "j", "source_engine": "mysql",
           "source_version": "8.0.36", "total": 0, "parts": []}  # fmt: skip
    index = load_contract(RoutineSourcesContract, old, artifact="routine_sources.json")
    assert getattr(index, "secret_scan", None) is None
