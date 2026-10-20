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

One of Decision 8's guards: the pattern files are guarded by ``test_pattern_catalog.py``,
their per-engine completeness and the "no routing rule outside the catalog" check by
``test_rule_catalog_complete.py`` (#639).
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
from src.shared.catalog import (
    CatalogError,
    EngineColors,
    EngineEntry,
    EnginePricing,
    load_engine_catalog,
)
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
    """)


def _write(directory: Path, engine_id: str, text: str) -> None:
    (directory / f"{engine_id}.toml").write_text(text, encoding="utf-8")


class TestLoaderIsStrict:
    def test_a_valid_file_loads(self, tmp_path: Path) -> None:
        _write(tmp_path, "example", VALID.format(id="example", order=1))
        (entry,) = load_engine_catalog(tmp_path)
        assert entry.id == "example"

    @pytest.mark.parametrize(
        "edit",
        [
            pytest.param(lambda t: t + 'unknown_field = "x"\n', id="unknown-field"),
            pytest.param(lambda t: t.replace('display_name = "Example"\n', ""), id="missing"),
            pytest.param(lambda t: t.replace('"active"', '"maybe"'), id="bad-status"),
            pytest.param(lambda t: t.replace('"key_value"', '"kv"'), id="bad-role"),
            pytest.param(lambda t: t.replace('"#123456"', '"blue"'), id="bad-hex"),
            # #1246: one colour per engine; the retired second and third palettes fail loudly.
            pytest.param(lambda t: t + 'ui_badge = "blue"\n', id="retired-ui-badge"),
            pytest.param(lambda t: t + 'deck = "#FF5C85"\n', id="retired-deck"),
            pytest.param(lambda t: t.replace('"#123456"', '"#12345"'), id="short-hex"),
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
        # The ATX orchestrator keys its table by the catalog id, which is also the
        # ``target_database`` it dispatches (#1179).
        assert set(atx_core._ANALYSIS_ENGINES) == ACTIVE_IDS
        assert all(k == e.target_database for k, e in atx_core._ANALYSIS_ENGINES.items())

    def test_the_container_entrypoint_accepts_every_catalog_engine(self) -> None:
        """``entrypoint.ANALYSIS_AGENTS`` (read by AST: importing the entrypoint builds an
        artifact store)."""
        source = (ROOT / "src" / "agents" / "entrypoint.py").read_text(encoding="utf-8")
        (value,) = [
            node.value
            for node in ast.parse(source).body
            if isinstance(node, ast.Assign)
            and [getattr(t, "id", None) for t in node.targets] == ["ANALYSIS_AGENTS"]
        ]
        assert set(ast.literal_eval(value)) == IDS

    def test_triage_names_only_catalog_engines(self) -> None:
        assert triage.DEFERRED == catalog.engine_ids(status="future")
        assert triage.ALL_AGENTS <= IDS
        assert set(triage.SOURCE_ENGINE_TO_AURORA.values()) <= ACTIVE_IDS
        assert _triage_signal_targets() <= IDS

    def test_signal_overrides_name_catalog_engines(self) -> None:
        assert set(SIGNAL_ENGINE_OVERRIDES.values()) <= IDS


def _triage_signal_targets() -> set[str]:
    """Every engine a triage signal targets: ``src/catalog/triage_signals.toml`` (#921).

    triage.py builds no ``targets=[...]`` list of its own any more; the AST check keeps
    one from coming back.
    """
    tree = ast.parse(Path(triage.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            assert not any(
                k.arg == "targets" and isinstance(k.value, ast.List) for k in node.keywords
            ), "triage.py lists signal targets in code: put them in triage_signals.toml"
    found = {t for signal in catalog.triage_signals().values() for t in signal.targets}
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
        fields = (
            (set(EngineEntry.model_fields) - {"colors", "pricing"})
            | set(EngineColors.model_fields)
            | set(EnginePricing.model_fields)
        )
        # ``id`` and ``display_order`` are read by the loader itself (file-name check, sort).
        unused = sorted(fields - read - {"display_order"})
        assert not unused, f"catalog fields nothing reads (remove them or use them): {unused}"


class TestPricingIsInTheCatalog:
    """The cost inputs (#494) are pinned to their pre-catalog values: no score changes."""

    def test_base_monthly_costs(self) -> None:
        got = {e.id: e.pricing.base_monthly for e in catalog.engines() if e.pricing}
        assert got == {
            "dynamodb": 0,
            "documentdb": 200,
            "opensearch": 150,
            "elasticache": 50,
            "aurora_postgresql": 250,
            "aurora_mysql": 250,
        }

    def test_every_routing_engine_states_the_burden_and_cutoff(self) -> None:
        for e in catalog.engines():
            if e.status == "active":
                assert e.pricing is not None, e.id
                assert e.pricing.extra_engine_burden_monthly == 300, e.id
                assert e.pricing.max_burden_per_unique_query == 100, e.id

    def test_unit_prices(self) -> None:
        pg, my = catalog.pricing("aurora_postgresql"), catalog.pricing("aurora_mysql")
        for p in (pg, my):
            assert (p.acu_price_per_hour, p.storage_price_per_gb_month) == (0.12, 0.10)
        assert pg.instance_hourly == {"db.r6g.large": 0.24, "db.r6g.xlarge": 0.48}
        assert my.instance_hourly == {"db.r6g.large": 0.22, "db.r6g.xlarge": 0.44}

    def test_request_unit_prices(self) -> None:
        """The per-request prices the per-query cost dimension reads (#494 slice 2): the
        same numbers the DynamoDB and DocumentDB cost estimates used."""
        got = {
            e.id: (
                e.pricing.read_unit_price_per_million,
                e.pricing.read_unit_bytes,
                e.pricing.write_unit_price_per_million,
                e.pricing.write_unit_bytes,
                e.pricing.reads_billed_on_cache_miss_only,
            )
            for e in catalog.engines()
            if e.pricing
        }
        assert got == {
            "dynamodb": (0.125, 4096, 0.625, 1024, False),
            "documentdb": (0.20, 8192, 0.20, 4096, True),
            "opensearch": (None, None, None, None, False),
            "elasticache": (None, None, None, None, False),
            "aurora_postgresql": (None, None, None, None, False),
            "aurora_mysql": (None, None, None, None, False),
        }

    def test_a_unit_price_needs_its_unit_size(self, tmp_path: Path) -> None:
        src = (ROOT / "src/catalog/engines/dynamodb.toml").read_text()
        (tmp_path / "dynamodb.toml").write_text(src.replace("read_unit_bytes = 4096\n", ""))
        with pytest.raises(CatalogError):
            load_engine_catalog(tmp_path)

    def test_the_cost_estimates_read_the_request_prices_from_the_catalog(self) -> None:
        for rel, needles in {
            "src/tools/analysis/dynamodb_analysis_tools.py": ["* 0.125", "* 0.625"],
            "src/tools/analysis/documentdb_analysis_tools.py": ["* 0.20"],
        }.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            for needle in needles:
                assert needle not in text, f"{rel} still holds a request price: {needle!r}"

    def test_an_engine_without_a_pricing_table_gets_the_defaults(self) -> None:
        assert catalog.pricing("neptune") == EnginePricing()
        assert catalog.pricing("not_an_engine").base_monthly == 100

    def test_a_negative_price_is_rejected(self, tmp_path: Path) -> None:
        src = (ROOT / "src/catalog/engines/dynamodb.toml").read_text()
        (tmp_path / "dynamodb.toml").write_text(
            src.replace("base_monthly = 0", "base_monthly = -1")
        )
        with pytest.raises(CatalogError):
            load_engine_catalog(tmp_path)

    def test_code_reads_the_catalog_not_literals(self) -> None:
        from src.agents.referee import reality_check_rules as rules
        from src.tools.analysis import aurora_common_analysis_tools as aurora_tools

        assert rules.ENGINE_BASE_COST["documentdb"] == catalog.pricing("documentdb").base_monthly
        assert not hasattr(rules, "EXTRA_ENGINE_BURDEN_MONTHLY")
        assert not hasattr(aurora_tools, "ACU_PRICE_PER_HOUR")
        assert not hasattr(aurora_tools, "INSTANCE_PRICES")

    def test_cost_literals_are_gone_from_the_code_that_used_them(self) -> None:
        forbidden = {
            "src/tools/analysis/scoring.py": ["ENGINE_BASE_COST", "PRICE_PER", "BURDEN"],
            "src/agents/referee/reality_check_rules.py": [
                "= 300",
                'dynamodb": 0',
                '"documentdb": 200',
            ],
            "src/agents/referee/reality_check_settle.py": ["rank_key > 100", ", 100)"],
            "src/agents/referee/reality_check_floors.py": [", 100)"],
            "src/tools/analysis/aurora_common_analysis_tools.py": [
                "= 0.12",
                "= 0.10",
                "0.24",
                "0.48",
            ],
        }
        for rel, needles in forbidden.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            for needle in needles:
                assert needle not in text, f"{rel} still holds a cost literal: {needle!r}"


DIMENSIONS = ("pattern_match", "complexity", "performance", "cost")
# The splits before #1002 (cost at 15% for DynamoDB and DocumentDB, 10% elsewhere).
SPLITS_BEFORE_1002 = {
    ("aurora_postgresql", "default"): (40, 30, 20, 10),
    ("aurora_mysql", "default"): (40, 30, 20, 10),
    ("dynamodb", "default"): (60, 20, 5, 15),
    ("documentdb", "default"): (45, 25, 15, 15),
    ("opensearch", "search"): (45, 25, 20, 10),
    ("opensearch", "time_series"): (40, 20, 30, 10),
}


def _split(weights: dict[str, float]) -> tuple[int, ...]:
    return tuple(round(weights[d] * 100) for d in DIMENSIONS)


def _with_weights(directory: Path, engine_id: str, order: int, weights: str) -> None:
    _write(directory, engine_id, VALID.format(id=engine_id, order=order) + weights)


WEIGHTS = (
    "\n[score_weights.default]\npattern_match = {}\ncomplexity = {}\nperformance = {}\ncost = {}\n"
)


class TestScoreWeightsAreInTheCatalog:
    """ADR-031 Decision 5 (maintainer decision, #1002): every engine weights cost the same."""

    def test_every_engine_and_profile_weights_cost_the_same(self) -> None:
        costs = {
            (e.id, name): weights["cost"]
            for e in catalog.engines()
            for name, weights in e.score_weights.items()
        }
        assert costs, "no engine file carries score weights"
        costs[("<default>", "default")] = catalog.default_score_weights()["cost"]
        assert set(costs.values()) == {0.10}, costs

    def test_the_weights_are_read_from_the_engine_files(self) -> None:
        got = {
            (e.id, name): _split(weights)
            for e in catalog.engines()
            for name, weights in e.score_weights.items()
        }
        assert got == {
            ("aurora_postgresql", "default"): (40, 30, 20, 10),
            ("aurora_mysql", "default"): (40, 30, 20, 10),
            ("dynamodb", "default"): (64, 21, 5, 10),
            ("documentdb", "default"): (48, 26, 16, 10),
            ("opensearch", "search"): (45, 25, 20, 10),
            ("opensearch", "time_series"): (40, 20, 30, 10),
        }

    def test_the_common_cost_weight_keeps_the_other_dimensions_in_balance(self) -> None:
        """Each engine's other three weights are its pre-#1002 ones scaled to fill what
        the common cost weight leaves, rounded to whole percent."""
        for (engine, profile), before in SPLITS_BEFORE_1002.items():
            after = _split(catalog.score_weights(engine, profile))
            scale = (100 - after[3]) / (100 - before[3])
            assert after[:3] == tuple(round(b * scale) for b in before[:3]), engine
            assert sum(after) == 100, engine

    def test_an_engine_without_weights_uses_the_default_split(self) -> None:
        assert _split(catalog.default_score_weights()) == (40, 30, 20, 10)
        assert catalog.score_weight_profiles("elasticache") == (catalog.default_score_weights(),)
        assert catalog.score_weight_profiles("not_an_engine") == (catalog.default_score_weights(),)

    def test_the_first_profile_is_the_engines_default(self) -> None:
        profiles = catalog.score_weight_profiles("opensearch")
        assert [_split(p) for p in profiles] == [(45, 25, 20, 10), (40, 20, 30, 10)]
        assert catalog.score_weights("opensearch") == profiles[0]
        assert catalog.score_weights("opensearch", "time_series") == profiles[1]

    def test_the_analysis_constants_and_the_router_read_the_catalog(self) -> None:
        from src.tools.analysis import scoring
        from src.tools.analysis.aurora_common_pattern_catalog import (
            AURORA_MYSQL_SCORE_WEIGHTS,
            AURORA_PG_SCORE_WEIGHTS,
        )
        from src.tools.analysis.documentdb_pattern_catalog import DOCUMENTDB_SCORE_WEIGHTS
        from src.tools.analysis.dynamodb_pattern_catalog import DYNAMODB_SCORE_WEIGHTS
        from src.tools.analysis.opensearch_pattern_catalog import (
            OPENSEARCH_SEARCH_WEIGHTS,
            OPENSEARCH_TIMESERIES_WEIGHTS,
        )
        from src.tools.analysis.score_weights import weight_profiles

        assert AURORA_PG_SCORE_WEIGHTS == catalog.score_weights("aurora_postgresql")
        assert AURORA_MYSQL_SCORE_WEIGHTS == catalog.score_weights("aurora_mysql")
        assert DYNAMODB_SCORE_WEIGHTS == catalog.score_weights("dynamodb")
        assert DOCUMENTDB_SCORE_WEIGHTS == catalog.score_weights("documentdb")
        assert OPENSEARCH_SEARCH_WEIGHTS == catalog.score_weights("opensearch", "search")
        assert OPENSEARCH_TIMESERIES_WEIGHTS == catalog.score_weights("opensearch", "time_series")
        assert scoring.DEFAULT_WEIGHTS == catalog.default_score_weights()
        for engine in IDS | {"not_an_engine"}:
            assert weight_profiles(engine) == catalog.score_weight_profiles(engine), engine

    def test_a_different_cost_weight_fails_at_load(self, tmp_path: Path) -> None:
        _with_weights(tmp_path, "one", 1, WEIGHTS.format(0.40, 0.30, 0.20, 0.10))
        _with_weights(tmp_path, "two", 2, WEIGHTS.format(0.60, 0.20, 0.05, 0.15))
        with pytest.raises(CatalogError, match="cost weight"):
            load_engine_catalog(tmp_path)

    def test_a_cost_weight_other_than_the_default_fails_even_when_all_agree(
        self, tmp_path: Path
    ) -> None:
        """An engine with no weights uses the default split, so it counts too."""
        _with_weights(tmp_path, "one", 1, WEIGHTS.format(0.60, 0.20, 0.05, 0.15))
        with pytest.raises(CatalogError, match="cost weight"):
            load_engine_catalog(tmp_path)

    def test_the_same_cost_weight_loads(self, tmp_path: Path) -> None:
        _with_weights(tmp_path, "one", 1, WEIGHTS.format(0.40, 0.30, 0.20, 0.10))
        _with_weights(tmp_path, "two", 2, WEIGHTS.format(0.64, 0.21, 0.05, 0.10))
        assert len(load_engine_catalog(tmp_path)) == 2

    @pytest.mark.parametrize(
        "weights",
        [
            pytest.param(WEIGHTS.format(0.40, 0.30, 0.20, 0.20), id="does-not-sum-to-one"),
            pytest.param(WEIGHTS.format(0.40, 0.30, 0.20, 0.10) + "speed = 0.0\n", id="extra"),
            pytest.param(
                WEIGHTS.format(0.40, 0.30, 0.20, 0.10).replace("complexity = 0.3\n", ""),
                id="missing",
            ),
            pytest.param(WEIGHTS.format(1.10, -0.20, 0.0, 0.10), id="negative"),
            pytest.param("\n[score_weights]\n", id="empty-table"),
            pytest.param(
                WEIGHTS.format(0.40, 0.30, 0.20, 0.10).replace("cost = 0.1", 'cost = "0.1"'),
                id="a-string",
            ),
            pytest.param(
                WEIGHTS.format(0.40, 0.30, 0.20, 0.10).replace("cost = 0.1", "cost = true"),
                id="a-boolean",
            ),
        ],
    )
    def test_malformed_weights_fail_at_load(self, tmp_path: Path, weights: str) -> None:
        _with_weights(tmp_path, "example", 1, weights)
        with pytest.raises(CatalogError, match="example.toml"):
            load_engine_catalog(tmp_path)

    @pytest.mark.parametrize("value", ['"0.1"', "true"])
    def test_a_weight_that_is_not_a_number_names_the_problem(
        self, tmp_path: Path, value: str
    ) -> None:
        """A string or a boolean is a catalog error naming the file and the profile, not a
        raw ``TypeError`` (a TOML ``true`` would otherwise count as 1)."""
        text = WEIGHTS.format(0.40, 0.30, 0.20, 0.10).replace("cost = 0.1", f"cost = {value}")
        _with_weights(tmp_path, "example", 1, text)
        with pytest.raises(
            CatalogError,
            match=r"(?s)example\.toml.*score_weights\.default: every weight is a number",
        ):
            load_engine_catalog(tmp_path)


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
        monkeypatch.setattr(module, "MD_OUT", tmp_path / "rule-catalog.md")
        monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
        assert module.main(["--check"]) == 1
        assert module.main([]) == 0
        assert module.main(["--check"]) == 0


RELATIONAL = VALID.replace('role = "key_value"', 'role = "relational"')


def _top(text: str, fields: str) -> str:
    """``text`` with top-level ``fields`` written before its first table."""
    return text.replace("\n[colors]", f"\n{fields}\n[colors]", 1)


class TestTheRoutingBranchesAreCatalogData:
    """#1081: the engine-specific routing rules are engine-file fields the loader checks."""

    def _relational(self, tmp_path: Path, a: str = "", b: str = "", n: int = 2) -> None:
        for i, extra in enumerate([a, b, ""][:n], start=1):
            name = f"rel_{i}"
            _write(tmp_path, name, _top(RELATIONAL.format(id=name, order=i), extra))

    def test_one_of_two_relational_engines_holds_the_tie_rule(self, tmp_path: Path) -> None:
        self._relational(tmp_path, b='exact_tie_preferred = true\nshort_name = "Two"\n')
        entries = {e.id: e for e in load_engine_catalog(tmp_path)}
        assert entries["rel_2"].exact_tie_preferred and entries["rel_2"].short_name == "Two"

    def test_two_relational_engines_need_the_tie_rule(self, tmp_path: Path) -> None:
        self._relational(tmp_path)
        with pytest.raises(CatalogError, match="must be set on exactly one"):
            load_engine_catalog(tmp_path)

    def test_the_tie_rule_is_on_one_engine_only(self, tmp_path: Path) -> None:
        flag = "exact_tie_preferred = true\n"
        self._relational(tmp_path, a=flag, b=flag)
        with pytest.raises(CatalogError, match="must be set on exactly one"):
            load_engine_catalog(tmp_path)

    def test_the_tie_rule_is_never_on_a_non_relational_engine(self, tmp_path: Path) -> None:
        _write(tmp_path, "kv", _top(VALID.format(id="kv", order=1), "exact_tie_preferred = true\n"))
        with pytest.raises(CatalogError, match="only an active relational engine"):
            load_engine_catalog(tmp_path)

    def test_a_third_relational_engine_fails_at_load(self, tmp_path: Path) -> None:
        self._relational(tmp_path, a="exact_tie_preferred = true\n", n=3)
        with pytest.raises(CatalogError, match="compares exactly two"):
            load_engine_catalog(tmp_path)

    @pytest.mark.passes_on_main
    def test_the_source_feature_direction_is_not_an_engine_flag(self, tmp_path: Path) -> None:
        self._relational(tmp_path, a="exact_tie_preferred = true\nsource_features_favour = true\n")
        with pytest.raises(CatalogError, match="source_features_favour"):
            load_engine_catalog(tmp_path)

    FLOOR = (
        '\n[justification_floor]\nkind = "search_depth"\nreason_code = "floor.x"\n'
        'why = "Earns its place."\n'
    )
    THRESHOLDS = "".join(
        f'\n[thresholds.{name}]\nvalue = 1.0\nwhy = "A floor."\n'
        for name in (
            "min_calls_per_second",
            "native_override_min_calls_per_second",
            "min_corpus_rows_avg",
        )
    )

    def test_a_floor_loads_with_its_thresholds(self, tmp_path: Path) -> None:
        _write(tmp_path, "s", VALID.format(id="s", order=1) + self.FLOOR + self.THRESHOLDS)
        (entry,) = load_engine_catalog(tmp_path)
        assert entry.justification_floor is not None
        assert entry.justification_floor.reason_code == "floor.x"

    def test_a_floor_needs_its_thresholds(self, tmp_path: Path) -> None:
        _write(tmp_path, "s", VALID.format(id="s", order=1) + self.FLOOR)
        with pytest.raises(CatalogError, match="needs thresholds"):
            load_engine_catalog(tmp_path)

    def test_two_engines_may_carry_a_floor_of_the_same_kind(self, tmp_path: Path) -> None:
        for i, name in enumerate(("s1", "s2"), start=1):
            _write(tmp_path, name, VALID.format(id=name, order=i) + self.FLOOR + self.THRESHOLDS)
        floors = [e.justification_floor for e in load_engine_catalog(tmp_path)]
        assert [f.kind for f in floors if f] == ["search_depth", "search_depth"]

    def test_an_unknown_floor_kind_fails(self, tmp_path: Path) -> None:
        text = VALID.format(id="s", order=1) + self.FLOOR.replace("search_depth", "vibes")
        _write(tmp_path, "s", text + self.THRESHOLDS)
        with pytest.raises(CatalogError, match="justification_floor.kind"):
            load_engine_catalog(tmp_path)

    def test_a_justified_override_needs_a_floor(self, tmp_path: Path) -> None:
        text = _top(VALID.format(id="s", order=1), "override_needs_justification = true\n")
        _write(tmp_path, "s", text)
        with pytest.raises(CatalogError, match="needs a justification_floor"):
            load_engine_catalog(tmp_path)

    def test_every_shipped_floor_reason_code_is_a_ledger_reason(self) -> None:
        from src.agents.referee.reality_check_floors import FLOOR_RULES
        from src.contracts.move_ledger import ReasonCode

        assert FLOOR_RULES and all(
            isinstance(r.reason_code, ReasonCode) for r in FLOOR_RULES.values()
        )


class TestTheHeterogeneousFeatures:
    """#1081: each source feature records the engine it favours (``routing.toml``)."""

    def test_every_feature_favours_an_active_relational_engine(self) -> None:
        relational = catalog.engine_ids(status=catalog.ACTIVE, role="relational")
        features = catalog.heterogeneous_features()
        assert features and all(f.favours in relational for f in features.values())

    def test_a_feature_favouring_another_engine_fails_at_load(self, tmp_path: Path) -> None:
        from src.shared.catalog.routing import ROUTING_FILE, load_routing_file

        text = ROUTING_FILE.read_text(encoding="utf-8").replace(
            'favours = "aurora_postgresql"\nbasis = "dialect"',
            'favours = "dynamodb"\nbasis = "dialect"',
        )
        bad = tmp_path / "routing.toml"
        bad.write_text(text, encoding="utf-8")
        with pytest.raises(CatalogError, match="not an active relational engine"):
            load_routing_file(bad)
