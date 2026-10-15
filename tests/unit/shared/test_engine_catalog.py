"""The engine catalog (ADR-031 Decision 8, #492): strict loading, completeness, generated copies.

``src/catalog/engines/<id>.toml`` is the one place an engine's display name, colours,
status and role are written down. This file guards that:

- the loader rejects a malformed engine file (unknown, missing or mistyped field; an id
  that is not the file name; a repeated display order; a cache or search engine that
  claims to be a system of record);
- every engine the pipeline knows (contract enums, triage's agent sets, the local
  runner and the ATX orchestrator) has a catalog file, and every catalog engine is one
  the contracts can carry;
- every field the catalog schema defines is read by some consumer, so no field is dead;
- the constants that used to be hand-kept copies are now exactly the catalog's view;
- the UI's generated module is fresh (``scripts/render_catalog.py --check``).

This is a partial guard for Decision 8: the pattern files, their per-engine entries and
the "no routing rule outside the catalog" check are #639's.
"""

from __future__ import annotations

import ast
import importlib.util
import textwrap
from pathlib import Path

import pytest

from src.agents.referee import triage
from src.agents.referee.assignment_resolver import SIGNAL_ENGINE_OVERRIDES
from src.api.services import local_execution
from src.atx_orchestrator import core as atx_core
from src.contracts import analysis_input, analysis_output, referee_output
from src.shared import catalog, report_facts
from src.shared.cache_engines import CACHE_ENGINES
from src.shared.catalog import CatalogError, EngineColors, EngineEntry, load_engine_catalog
from src.shared.engine_names import ENGINE_DISPLAY_NAMES, PLACEHOLDER_ENGINES
from src.shared.migration_wave_engines import (
    DOCUMENT_ENGINES,
    KV_ENGINES,
    NON_OWNER_ENGINES,
    RELATIONAL_ENGINES,
    SEARCH_ENGINES,
)

ROOT = Path(__file__).resolve().parents[3]
RENDER_SCRIPT = ROOT / "scripts" / "render_catalog.py"

IDS = {e.id for e in catalog.engines()}
ACTIVE_IDS = catalog.engine_ids(status=catalog.ACTIVE)


def _render_module():
    spec = importlib.util.spec_from_file_location("render_catalog", RENDER_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALID = textwrap.dedent("""\
    id = "{id}"
    display_name = "Example"
    display_order = {order}
    ui_picker_order = {order}
    status = "active"
    role = "key_value"
    system_of_record = true

    [colors]
    ui = "#123456"
    ui_badge = "blue"
    """)


def _write(directory: Path, engine_id: str, text: str) -> None:
    (directory / f"{engine_id}.toml").write_text(text, encoding="utf-8")


class TestLoaderIsStrict:
    def test_a_valid_file_loads(self, tmp_path: Path) -> None:
        _write(tmp_path, "example", VALID.format(id="example", order=1))
        (entry,) = load_engine_catalog(tmp_path)
        assert entry.id == "example"
        assert entry.colors.report_badge is None

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(lambda t: t + 'unknown_field = "x"\n', id="unknown-field"),
            pytest.param(lambda t: t.replace('display_name = "Example"\n', ""), id="missing"),
            pytest.param(lambda t: t.replace('"active"', '"maybe"'), id="bad-status"),
            pytest.param(lambda t: t.replace('"key_value"', '"kv"'), id="bad-role"),
            pytest.param(lambda t: t.replace('"#123456"', '"blue"'), id="bad-hex"),
            pytest.param(lambda t: t.replace('ui_badge = "blue"', 'ui_badge = "pink"'), id="badge"),
            pytest.param(lambda t: t + 'deck = "#12345"\n', id="short-hex"),
            pytest.param(lambda t: t + "[extra]\nx = 1\n", id="unknown-table"),
            pytest.param(lambda t: t.replace('"key_value"', '"cache"'), id="cache-claims-to-own"),
        ],
    )
    def test_a_malformed_file_fails_at_load(self, tmp_path: Path, edit) -> None:
        _write(tmp_path, "example", edit(VALID.format(id="example", order=1)))
        with pytest.raises(CatalogError, match="example.toml"):
            load_engine_catalog(tmp_path)

    def test_the_id_must_be_the_file_name(self, tmp_path: Path) -> None:
        _write(tmp_path, "example", VALID.format(id="other", order=1))
        with pytest.raises(CatalogError, match="does not match the file name"):
            load_engine_catalog(tmp_path)

    def test_display_order_is_unique(self, tmp_path: Path) -> None:
        _write(tmp_path, "one", VALID.format(id="one", order=1))
        _write(tmp_path, "two", VALID.format(id="two", order=1))
        with pytest.raises(CatalogError, match="display_order"):
            load_engine_catalog(tmp_path)

    def test_picker_order_is_unique(self, tmp_path: Path) -> None:
        _write(tmp_path, "one", VALID.format(id="one", order=1))
        two = VALID.format(id="two", order=2).replace("ui_picker_order = 2", "ui_picker_order = 1")
        _write(tmp_path, "two", two)
        with pytest.raises(CatalogError, match="ui_picker_order"):
            load_engine_catalog(tmp_path)

    def test_a_toml_syntax_error_names_the_file(self, tmp_path: Path) -> None:
        _write(tmp_path, "broken", 'id = "broken\n')
        with pytest.raises(CatalogError, match="broken.toml"):
            load_engine_catalog(tmp_path)

    def test_an_empty_directory_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(CatalogError, match="no engine files"):
            load_engine_catalog(tmp_path)

    def test_the_shipped_catalog_loads(self) -> None:
        assert load_engine_catalog() == catalog.engines()


class TestEveryKnownEngineHasACatalogFile:
    """Adding an engine anywhere in the pipeline without a catalog file fails here."""

    def test_contract_engine_enums(self) -> None:
        assert {m.value for m in analysis_input.TargetDatabase} <= IDS
        # The analysis output enum is the widest engine list a contract carries.
        assert {m.value for m in analysis_output.TargetDatabase} == IDS

    def test_contract_display_name_enum(self) -> None:
        names = {e.display_name for e in catalog.engines()}
        assert {m.value for m in referee_output.DatabaseService} <= names

    def test_analysis_agents_are_exactly_the_active_engines(self) -> None:
        assert triage.ANALYSIS_AGENTS == ACTIVE_IDS
        assert set(local_execution._ENGINES) == ACTIVE_IDS
        # The ATX orchestrator keys its table by a short name (``aurora_pg``); the engine
        # id it dispatches is ``target_database``.
        assert {e.target_database for e in atx_core._ANALYSIS_ENGINES.values()} == ACTIVE_IDS

    def test_triage_names_only_catalog_engines(self) -> None:
        assert triage.DEFERRED == catalog.engine_ids(status="future")
        assert triage.ALL_AGENTS <= IDS
        assert set(triage.SOURCE_ENGINE_TO_AURORA.values()) <= ACTIVE_IDS
        assert _triage_signal_targets() <= IDS

    def test_signal_overrides_name_catalog_engines(self) -> None:
        assert set(SIGNAL_ENGINE_OVERRIDES.values()) <= IDS


def _triage_signal_targets() -> set[str]:
    """Every engine a triage signal in triage.py targets.

    Signals are built as ``TriageSignal(..., targets=[...])`` or ``_signal("name", [...])``.
    """
    tree = ast.parse(Path(triage.__file__).read_text(encoding="utf-8"))
    lists: list[ast.List] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        lists += [
            k.value for k in node.keywords if k.arg == "targets" and isinstance(k.value, ast.List)
        ]
        if len(node.args) > 1 and isinstance(node.args[0], ast.Constant):
            if isinstance(node.args[1], ast.List):
                lists.append(node.args[1])
    found = {
        e.value
        for lst in lists
        for e in lst.elts
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    }
    assert found, "parsed no triage signal targets"
    return found


def _attribute_names(paths: list[Path]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Call):
                func = node.func
                callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if callee == "engine_ids":  # engine_ids(status=..., role=..., system_of_record=...)
                    names |= {k.arg for k in node.keywords if k.arg}
    return names


class TestEveryCatalogFieldIsUsed:
    def test_every_field_is_read_outside_the_model_definitions(self) -> None:
        consumers = [p for p in (ROOT / "src").rglob("*.py") if p.parent.name != "catalog"] + [
            RENDER_SCRIPT
        ]
        read = _attribute_names(consumers)
        fields = (set(EngineEntry.model_fields) - {"colors"}) | set(EngineColors.model_fields)
        # ``id`` and ``display_order`` are read by the loader itself (file-name check, sort).
        unused = sorted(fields - read - {"display_order"})
        assert not unused, f"catalog fields nothing reads (remove them or use them): {unused}"


class TestConstantsAreTheCatalogsView:
    """Each formerly hand-kept table, pinned to its pre-catalog value (zero behaviour change)."""

    def test_display_names(self) -> None:
        assert list(ENGINE_DISPLAY_NAMES.items()) == [
            ("dynamodb", "DynamoDB"),
            ("documentdb", "DocumentDB"),
            ("opensearch", "OpenSearch"),
            ("elasticache", "ElastiCache"),
            ("aurora_postgresql", "Aurora PostgreSQL"),
            ("aurora_mysql", "Aurora MySQL"),
            ("neptune", "Neptune"),
            ("keyspaces", "Keyspaces"),
            ("aurora", "Aurora"),
        ]

    def test_engine_sets(self) -> None:
        assert PLACEHOLDER_ENGINES == {"neptune", "keyspaces"}
        assert CACHE_ENGINES == {"elasticache"}
        assert KV_ENGINES == {"dynamodb"}
        assert SEARCH_ENGINES == {"opensearch"}
        assert DOCUMENT_ENGINES == {"documentdb"}
        assert RELATIONAL_ENGINES == {"aurora_mysql", "aurora_postgresql"}
        assert NON_OWNER_ENGINES == CACHE_ENGINES | SEARCH_ENGINES
        assert report_facts.SEARCH_ENGINES == {"opensearch"}
        assert report_facts.RELATIONAL_ENGINES == {"aurora", "aurora_mysql", "aurora_postgresql"}

    def test_decision_report_badges(self) -> None:
        from src.report import decision_report

        assert decision_report._ENGINE_BADGE == {
            "dynamodb": "#3b48cc",
            "documentdb": "#0c7838",
            "aurora_postgresql": "#9c5700",
            "aurora_mysql": "#9c5700",
            "elasticache": "#c62f25",
            "opensearch": "#0e7c86",
            "neptune": "#7048e8",
            "keyspaces": "#8250df",
        }

    def test_deck_accents(self) -> None:
        from src.report import pptx_report as deck

        assert deck.ENGINE_COLOR == {
            "aurora_postgresql": deck.BLUE,
            "elasticache": deck.PURPLE,
            "documentdb": deck.GREEN,
            "dynamodb": deck.YELLOW,
            "opensearch": deck.ORANGE,
            "aurora_mysql": deck.PINK,
        }
        assert set(deck.ENGINE_LABEL) == set(deck.ENGINE_COLOR)


class TestGeneratedUiModule:
    def test_the_committed_module_is_fresh(self) -> None:
        module = _render_module()
        expected = module.render_js(catalog.engines())
        assert module.JS_OUT.read_text(encoding="utf-8") == expected, (
            "src/ui/src/utils/engineCatalog.generated.js is stale. "
            "Run: uv run python scripts/render_catalog.py"
        )

    def test_check_mode_fails_on_a_stale_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _render_module()
        stale = tmp_path / "engineCatalog.generated.js"
        stale.write_text("// stale\n", encoding="utf-8")
        monkeypatch.setattr(module, "JS_OUT", stale)
        monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
        assert module.main(["--check"]) == 1
        assert module.main([]) == 0
        assert module.main(["--check"]) == 0
