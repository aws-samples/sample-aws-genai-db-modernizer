"""The write-intent catalog loads strictly and is rendered into the rule-catalog page (#1362).

``src/catalog/write_intents/<id>.toml`` words each intent and every write-owning engine's
target-native approach; ``src/catalog/carried_writes.toml`` holds the fit floor. Like the
rest of the rule catalog (ADR-031 Decision 8), a wrong file fails at load time, and
``scripts/render_catalog.py`` renders both into ``docs/architecture/rule-catalog.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.shared.catalog.engines import CatalogError
from src.shared.catalog.write_intents import (
    WRITE_INTENTS_DIR,
    carried_write_floor,
    load_carried_write_settings,
    load_write_intents,
    write_intent,
    write_intents,
)

REPO = Path(__file__).resolve().parents[3]
OWNERS = ("aurora_mysql", "aurora_postgresql", "documentdb", "dynamodb")


def _engines(skip: str = "", extra: str = "") -> str:
    names = [e for e in OWNERS if e != skip] + ([extra] if extra else [])
    return "".join(f'[engines.{e}]\napproach = "do it"\ndocs = "a guide"\n\n' for e in names)


def _write(directory: Path, name: str, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.toml").write_text(body)


def _intent(intent_id: str = "x-intent", **fields: str) -> str:
    head = {"id": intent_id, "name": "X", "summary": "What it is for."} | fields
    return "".join(f'{k} = "{v}"\n' for k, v in head.items()) + "\n"


def test_the_bundled_catalog_loads():
    intents = write_intents()
    assert [i.id for i in intents] == sorted(p.stem for p in WRITE_INTENTS_DIR.glob("*.toml"))
    assert write_intent("retention-delete").name == "Retention delete"
    with pytest.raises(KeyError):
        write_intent("no-such-intent")
    assert 0 < carried_write_floor() <= 100


def test_a_valid_file_loads(tmp_path):
    _write(tmp_path, "x-intent", _intent() + _engines(extra="elasticache"))
    (intent,) = load_write_intents(tmp_path)
    assert intent.approach("elasticache") is not None and intent.approach("opensearch") is None


@pytest.mark.parametrize(
    "body,message",
    [
        (_intent() + _engines(skip="dynamodb"), "no approach for the write-owning engines"),
        (_intent() + _engines(extra="neptune"), "not an active engine"),
        (_intent() + _engines(extra="nosuch"), "not an active engine"),
        (_intent("other-id") + _engines(), "does not match the file name"),
        (_intent(colour="red") + _engines(), "Extra inputs"),
        (_intent(name="two\\nlines") + _engines(), "String should match pattern"),
    ],
)
def test_a_wrong_file_fails_at_load_time(tmp_path, body, message):
    _write(tmp_path, "x-intent", body)
    with pytest.raises(CatalogError, match=message):
        load_write_intents(tmp_path)


def test_an_empty_directory_fails(tmp_path):
    with pytest.raises(CatalogError, match="no write intent files"):
        load_write_intents(tmp_path)


@pytest.mark.parametrize(
    "body,message",
    [
        ('[thresholds.other]\nvalue = 1\nwhy = "x"\n', "carried_write_fit_floor"),
        ('[thresholds.carried_write_fit_floor]\nvalue = 0\nwhy = "x"\n', "between 0 and 100"),
        ('[thresholds.carried_write_fit_floor]\nvalue = 101\nwhy = "x"\n', "between 0 and 100"),
    ],
)
def test_a_wrong_floor_fails_at_load_time(tmp_path, body, message):
    path = tmp_path / "carried_writes.toml"
    path.write_text(body)
    with pytest.raises(CatalogError, match=message):
        load_carried_write_settings(path)


def test_the_rule_catalog_page_lists_every_intent_and_the_floor():
    page = (REPO / "docs" / "architecture" / "rule-catalog.md").read_text(encoding="utf-8")
    assert "## Write intents: serving a carried write the target's way" in page
    assert f"`carried_write_fit_floor` = {carried_write_floor():g}" in page
    for intent in write_intents():
        assert f"### {intent.id}" in page
        assert intent.engines["dynamodb"].approach in page
