"""ADR-031 Decision 8 guard: the rule catalog is complete for every engine (#639).

Decision 8 puts routing rules in one pattern-first, engine-neutral catalog
(``src/catalog/engines/*.toml``, ``src/catalog/patterns/*.toml``). Strict loading and the
generated constants are guarded by ``test_engine_catalog.py`` and
``test_pattern_catalog.py``. This file holds the two checks Decision 8 adds on top:

1. **Completeness.** Every pattern file has an ``[engines.<id>]`` entry for every engine
   except a ``legacy`` id (the generic ``aurora`` older reports carry): today that is every
   engine routing can target plus the future-enhancement placeholders (#495). Stated by
   exclusion, so an engine status added later is covered by default. Adding an engine or
   a pattern cannot silently default to "unsupported": it fails here with the exact list
   of missing decisions.
2. **No routing rule outside the catalog.** The constants already moved (slices 1 and 2
   of #492, then #1002 and #921) hold no engine or pattern literal any more. Everything that still could encode
   a rule is listed below, by file, with the reason it is still there: every module-level
   constant (any name case) or class attribute that names an engine id or a pattern type
   in a literal (enum members excepted: they are the contract vocabularies, pinned to the
   catalog by ``test_engine_catalog``), every score-weight table, every module-level
   numeric constant in the routing directories (``NUMERIC_DIRS``; class-attribute numbers
   are field defaults and not scanned), and every pattern-type literal in code and
   engine-id literal in routing code (``if engine == "dynamodb": ...``). It also checks
   engine-name keywords and display-name or concatenated-name branches in those routing
   directories and ``src/api/``. Numeric comparisons are checked in a function body nested
   under an explicit engine branch; unrelated numbers and numbers in the branch condition
   are out of scope. A new one fails until it either reads the catalog or is added here with
   its reason, so the remaining exceptions can only shrink
   on purpose. The inline-literal lists compare counts per file, so swapping one literal
   for another in the same file passes: they are a review prompt, not a proof.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

import pytest

from src.shared import catalog
from src.shared.catalog.patterns import (
    PatternEntry,
    load_pattern_catalog,
    missing_engine_entries,
    patterns,
)

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"

# The engines every pattern file must decide for: every engine except a legacy id (today
# the active ones plus the future-enhancement placeholders, whose decision is a written
# "not modelled yet", #495). Stated by exclusion, so a status added later is covered.
DECIDED = catalog.engine_ids() - catalog.engine_ids(status="legacy")
FUTURE = catalog.engine_ids(status="future")


class TestEveryPatternCoversEveryEngine:
    def test_the_shipped_catalog_is_complete(self) -> None:
        missing = missing_engine_entries(patterns(), DECIDED)
        assert not missing, (
            "These pattern files have no decision for an engine (every engine except a legacy id needs one). Write "
            "the engine's entry (kind, supported, why) instead of letting it default: "
            f"{missing}"
        )

    def test_a_future_engine_is_written_down_as_not_modelled(self) -> None:
        assert FUTURE == {"neptune", "keyspaces"}
        for p in patterns():
            for engine in FUTURE:
                support = p.engines[engine]
                assert (support.kind, support.supported, support.why) == (
                    "future",
                    False,
                    "Not modelled yet (future enhancement)",
                ), (p.id, engine)

    def test_no_pattern_file_decides_for_an_engine_it_should_not(self) -> None:
        extra = sorted(
            f"{p.id}.toml: [engines.{e}]" for p in patterns() for e in set(p.engines) - DECIDED
        )
        assert not extra

    def test_a_new_engine_fails_with_the_exact_missing_entries(self, tmp_path: Path) -> None:
        """On a synthetic catalog, so a real gap in the shipped one is reported once, by the
        test above, and not blamed on the new engine here."""
        engines = "".join(
            (
                f'\n[engines.{e}]\nkind = "unscored"\nsupported = true\nwhy = "No effect."\n'
                if e not in FUTURE
                else f'\n[engines.{e}]\nkind = "future"\nsupported = false\nwhy = "Not yet."\n'
            )
            for e in sorted(DECIDED)
        )
        for name in ("one", "two"):
            text = f'id = "{name}"\ndetectors = []\npenalty = 0\n' + engines
            (tmp_path / f"{name}.toml").write_text(text, encoding="utf-8")
        sample = load_pattern_catalog(tmp_path)
        assert missing_engine_entries(sample, DECIDED) == []
        assert missing_engine_entries(sample, DECIDED | {"newdb"}) == [
            "one.toml: [engines.newdb]",
            "two.toml: [engines.newdb]",
        ]

    def test_a_new_pattern_fails_with_the_exact_missing_entries(self, tmp_path: Path) -> None:
        text = (
            'id = "new-pattern"\ndetectors = []\npenalty = 0\n\n'
            '[engines.dynamodb]\nkind = "unscored"\nsupported = true\nwhy = "No effect."\n'
        )
        (tmp_path / "new-pattern.toml").write_text(text, encoding="utf-8")
        (entry,) = load_pattern_catalog(tmp_path)
        assert missing_engine_entries([entry], DECIDED) == sorted(
            f"new-pattern.toml: [engines.{e}]" for e in DECIDED - {"dynamodb"}
        )

    def test_every_pattern_type_an_analysis_emits_has_a_file(self) -> None:
        """Literal ``pattern_type=``/``anti_pattern_type=`` values in the analysis tools."""
        emitted: set[str] = set()
        for path in (SRC / "tools" / "analysis").glob("*_analysis_tools.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.keyword) and node.arg in (
                    "pattern_type",
                    "anti_pattern_type",
                ):
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                        emitted.add(node.value.value)
        assert emitted, "parsed no pattern types"
        assert emitted <= {p.id for p in patterns()}


# ---------------------------------------------------------------------------
# No routing rule outside the catalog
# ---------------------------------------------------------------------------

ENGINE_IDS = {e.id for e in catalog.engines()}
ENGINE_DISPLAY_NAMES = {e.display_name for e in catalog.engines()}
PATTERN_KEYS = {p.id for p in patterns()} | {a for p in patterns() for a in p.penalty_aliases}
SCORE_DIMENSIONS = {"pattern_match", "complexity", "performance", "cost"}
# Where routing happens: numeric module constants and engine-id literals in code are
# scanned here.
NUMERIC_DIRS = (
    "src/agents/referee/",
    "src/agents/analysis/",
    "src/tools/analysis/",
    "src/shared/",
)
API_DIRS = ("src/api/",)
# Calls that read the catalog: a literal passed to one of these is a lookup key, not a rule.
CATALOG_ACCESSORS = {
    "_guidance",
    "_hint",
    "apply_aurora_feature_adjustments",
    "cache_threshold",
    "capability_engines",
    "catalog_anti_patterns",
    "catalog_entries",
    "catalog_patterns",
    "engine_ids",
    "engine_threshold",
    "feature_complexity_bonus",
    "pattern",
    "profile_pattern_types",
    "score_weights",
    "signal_targets",
    "table_bonuses",
    "threshold",
}

_PINNED = (
    "engine list pinned to the catalog's active engines by "
    "test_engine_catalog.TestEveryKnownEngineHasACatalogFile"
)
_WORDING = "wording or display, not a routing rule"
_PLUMBING = "per-engine plumbing (dialects, schema fields, dispatch), not a routing rule"
_CONTRACT = (
    "a contract's engine vocabulary (Literal or field default; contract change, CONTRIBUTING.md)"
)

# Module-level constants (tables and single values) that name an engine id or a pattern
# type in a literal.   path: (names, why it is still there)
ENGINE_OR_PATTERN_TABLES: dict[str, tuple[frozenset[str], str]] = {
    "src/shared/capability_labels.py": (
        frozenset({"CAPABILITY_REDESIGN_HINTS"}),
        _WORDING + ": how to redesign a query a fallback rule placed (#729)",
    ),
    "src/agents/entrypoint.py": (
        frozenset({"ANALYSIS_AGENTS"}),
        "the agent types the container entrypoint dispatches to analysis: pinned to every "
        "catalog engine id by test_engine_catalog.TestEveryKnownEngineHasACatalogFile",
    ),
    "src/api/services/local_execution.py": (frozenset({"_ENGINES"}), _PINNED),
    "src/atx_orchestrator/core.py": (
        frozenset(
            {"_ANALYSIS_ENGINES", "_SAME_FAMILY", "IMPLEMENTED_SCHEMA_DESIGNERS", "_DESIGN_SHAPE"}
        ),
        _PLUMBING + "; _ANALYSIS_ENGINES is also " + _PINNED,
    ),
    "src/atx_orchestrator/subagents/schema.py": (frozenset({"VALID_TARGET_TYPES"}), _PLUMBING),
    "src/atx_orchestrator/tools.py": (frozenset({"_SCHEMA_ENGINES"}), _PLUMBING),
    "src/api/routes/agent_interaction.py": (frozenset({"_KNOWN_AGENT_FOLDERS"}), _PLUMBING),
    "src/agents/schema_design/group_merger.py": (frozenset({"ENGINE_LIST_FIELDS"}), _PLUMBING),
    "src/agents/schema_design/handler.py": (frozenset({"_NON_GROUPED_ENGINES"}), _PLUMBING),
    "src/agents/schema_design/scope.py": (
        frozenset({"_ENGINE_FIELDS", "_WARNING_FIELDS"}),
        _PLUMBING,
    ),
    "src/contracts/aurora_design_delta.py": (frozenset({"ENGINE_TYPE_RULES"}), _PLUMBING),
    "src/contracts/dynamodb_model_output.py": (
        frozenset({"UNSUPPORTED_PATTERN_TYPES", "DynamoDBModelOutputContract.target_engine"}),
        _CONTRACT,
    ),
    "src/contracts/elasticache_model_output.py": (
        frozenset({"USE_CASE_TYPE", "ElastiCacheModelOutputContract.target_engine"}),
        _CONTRACT,
    ),
    "src/contracts/aurora_mysql_model_output.py": (
        frozenset({"AuroraMySQLModelOutputContract.target_engine"}),
        _CONTRACT,
    ),
    "src/contracts/aurora_postgresql_model_output.py": (
        frozenset({"AuroraPostgresqlModelOutputContract.target_engine"}),
        _CONTRACT,
    ),
    "src/contracts/documentdb_model_output.py": (
        frozenset({"DocumentDBModelOutputContract.target_engine"}),
        _CONTRACT,
    ),
    "src/contracts/model_flag.py": (frozenset({"FlaggableEngine"}), _CONTRACT),
    "src/contracts/analysis_input.py": (
        frozenset({"AnalysisInput.model_config"}),
        "a JSON-schema example in the contract, not a rule",
    ),
    "src/contracts/referee_input.py": (
        frozenset({"RefereeInput.model_config"}),
        "a JSON-schema example in the contract, not a rule",
    ),
    "src/api/routes/settings.py": (
        frozenset({"_settings"}),
        "default UI settings (an AWS resource name field), not a rule",
    ),
    "src/shared/sql_comments.py": (frozenset({"_NESTING_DIALECTS"}), _PLUMBING),
    "src/tools/schema/aurora_common/constraint_translator.py": (
        frozenset({"PG_TARGET", "MYSQL_TARGET", "_ENGINE"}),
        _PLUMBING,
    ),
    "src/tools/schema/aurora_common/ddl_generator.py": (
        frozenset({"POSTGRES", "MYSQL"}),
        _PLUMBING,
    ),
    "src/tools/schema/aurora_common/delta_merge.py": (
        frozenset({"_GENERATORS", "_DIALECTS", "_OUTPUT_CONTRACTS"}),
        _PLUMBING,
    ),
    "src/tools/schema/aurora_common/source_family.py": (
        frozenset({"_PG_ALIASES", "_MYSQL_ALIASES", "AuroraTarget"}),
        _PLUMBING,
    ),
    "src/tools/schema/design_metrics.py": (frozenset({"METRIC_ENGINES"}), _PLUMBING),
    "src/agents/referee/migration_wave_builders.py": (frozenset({"_ALREADY_AURORA"}), _WORDING),
    "src/agents/referee/reality_check_summary.py": (frozenset({"_ENGINE_ALIASES"}), _WORDING),
    "src/agents/referee/synthesis_grounding.py": (frozenset({"_ENGINE_PATTERNS"}), _WORDING),
    "src/agents/referee/synthesis_report.py": (
        frozenset(
            {
                "AURORA_ENGINES",
                "_DYNAMODB_ALTERNATIVE_ANTI_PATTERNS",
                "_ENGINE_FUNCTION_PHRASE",
                "_OBJECT_NOUNS",
            }
        ),
        _WORDING,
    ),
    "src/report/waves.py": (frozenset({"_RELATIONAL_TO_SOURCE_ENGINE"}), _WORDING),
    "src/shared/signal_labels.py": (frozenset({"SIGNAL_NOUNS"}), _WORDING),
    "src/agents/referee/access_pattern.py": (
        frozenset({"TEXT_SEARCH"}),
        "an access-pattern class id from src/catalog/access_classes.toml (#728) that "
        "shares its spelling with the text_search pattern file",
    ),
    "src/tools/schema/aurora_common/bedrock_delta.py": (frozenset({"_ENGINE_LABELS"}), _WORDING),
}

# Module-level score-weight tables (keyed by the four score dimensions). None is left: the
# weights are the engine files' [score_weights] since #1002, so a new table fails here.
SCORE_WEIGHT_TABLES: dict[str, tuple[frozenset[str], str]] = {}

_DETECTION = "a detector threshold: detection stays in code (#491 facts module)"
_BUDGET = "an output or model budget, not a routing rule"
# Module-level numeric constants in the routing directories.
NUMERIC_CONSTANTS: dict[str, tuple[frozenset[str], str]] = {
    "src/agents/referee/access_pattern.py": (
        frozenset({"_LOW_CARDINALITY_SHARE", "SMALL_TABLE_MAX_ROWS", "WORKLOAD_KEY_LOOKUPS"}),
        _DETECTION + ": when an index's leading column is too coarse to be a key (#728), "
        "the most rows a small reference table holds (#920, shared with DynamoDB's "
        "metadata-config-store detector), and how many queries make a column the key of "
        "other access patterns on its table (#1061: each of them and at least one other)",
    ),
    "src/agents/referee/consolidation_validator.py": (
        frozenset({"MAX_QUERIES_PER_CALL"}),
        _BUDGET + ": the model batch size of a consolidation validation call",
    ),
    "src/agents/referee/transaction_shape.py": (
        frozenset({"TRANSACTION_MAX_ITEMS"}),
        _DETECTION + ": DynamoDB's 100-item transaction quota bounds the shape (#493)",
    ),
    "src/tools/analysis/aurora_common_pattern_catalog.py": (
        frozenset(
            {
                "STANDARD_CRUD_MIN_CPS",
                "HIGH_FREQUENCY_PK_LOOKUP_CPS",
                "CACHE_READ_CPS_THRESHOLD",
                "HIGH_VOLUME_TEXT_SEARCH_CPS",
                "TEXT_COLUMN_RATIO_THRESHOLD",
            }
        ),
        _DETECTION,
    ),
    "src/tools/analysis/dynamodb_analysis_tools.py": (
        frozenset(
            {
                "BOUNDED_PARENT_CHILD_RATIO_THRESHOLD",
                "CO_ACCESS_FREQUENCY_THRESHOLD",
                "GSI_FREQUENCY_THRESHOLD",
                "SECONDARY_INDEX_DOMINANCE_THRESHOLD",
            }
        ),
        _DETECTION,
    ),
    "src/tools/analysis/key_access.py": (
        frozenset({"READ_MOSTLY_RATIO"}),
        _DETECTION + ": reads per write that make a table read-mostly reference data (#920)",
    ),
    "src/tools/analysis/variant_attributes.py": (
        frozenset({"_ATTRIBUTE_TABLE_MAX_COLUMNS"}),
        _DETECTION + ": the widest an entity-attribute-value table can be (#920)",
    ),
    "src/tools/analysis/pattern_catalog_base.py": (
        frozenset({"HIGH_INGEST_CPS_THRESHOLD"}),
        _DETECTION + "; one value for OpenSearch's os-07 and Aurora's anti-04 (#493)",
    ),
    "src/agents/referee/routed_confidence.py": (frozenset({"LEAD_MIN_SHARE"}), _WORDING),
    "src/shared/collection_database_name.py": (
        frozenset({"_MAX_LENGTH"}),
        "the artifact-path name length limit (#420, #1096), not a routing rule",
    ),
    "src/shared/ranking.py": (frozenset({"PARTIAL_EVIDENCE_MIN_SHARE"}), _WORDING),
    "src/agents/referee/reality_check_request.py": (
        frozenset({"SQL_CHARS", "SQL_JSON_CHARS"}),
        _BUDGET,
    ),
    "src/agents/referee/summary_input_budget.py": (
        frozenset({"SUMMARY_INPUT_TOKEN_BUDGET", "SUMMARY_INPUT_LINE_BUDGET", "CHARS_PER_TOKEN"}),
        _BUDGET,
    ),
    "src/agents/referee/synthesis_grounding.py": (
        frozenset(
            {
                "_CUE_WINDOW",
                "_ALTERNATIVE_WINDOW",
                "_TOP_GROUPS_PER_ENGINE",
                "_MAX_RUN",
                "_ACCESS_WINDOW",
                "_MAX_CUE_GAP_WORDS",
                "_MAX_GROUPS_IN_SUMMARY",
            }
        ),
        _BUDGET,
    ),
    "src/agents/referee/migration_risks.py": (
        frozenset({"HOT_PARTITION_HIGH_PCT"}),
        "a risk-severity threshold for the report's risk register (#950), not a routing rule",
    ),
    "src/shared/migration_risk_rules.py": (
        frozenset({"GATE_IDS_NAMED", "LISTED_ITEMS"}),
        "two output budgets for the risk register's text (#950, moved in #1148 and #1149), "
        "not a routing rule",
    ),
    "src/agents/referee/synthesis_report.py": (
        frozenset({"_RELATIONAL_REASON_MAX_CHARS", "_SQL_EXCERPT_CHARS"}),
        _BUDGET,
    ),
    "src/shared/job_status.py": (frozenset({"_MAX_MESSAGE_LEN"}), _BUDGET),
    "src/agents/referee/co_dependency.py": (
        frozenset({"GROUP_SCORE_PLACES"}),
        "the precision a group's mean is compared at (#992 round 4): float noise in a "
        "weighted mean never decides between engines, an exact tie goes to tie_break",
    ),
    "src/agents/referee/pending_choices.py": (
        frozenset({"UNROUNDED_PLACES"}),
        "the places a pending engine choice's unrounded score is shown and compared at "
        "(#1003): it only orders a near-tie --auto decides, and it is the same for every "
        "engine",
    ),
    "src/agents/referee/write_groups.py": (
        frozenset({"CALL_WEIGHT_FLOOR_SHARE"}),
        "the floor weight of a write with no recorded calls in a call-weighted write group "
        "(maintainer decision on #992, 2026-10-17): it keeps the write in the mean without "
        "letting it outweigh real traffic; no engine reads it differently",
    ),
    "src/shared/bedrock_guardrail.py": (
        frozenset({"GUARDED_READ_TIMEOUT_SECONDS"}),
        "a transport read timeout for guarded streaming calls, not a routing rule (#1062)",
    ),
    "src/shared/models.py": (
        frozenset({"ADAPTIVE_MAX_TOKENS_FLOOR", "LEGACY_MAX_TOKENS_FLOOR"}),
        _BUDGET,
    ),
    "src/shared/query_trace.py": (frozenset({"_PLACES", "EXCERPT_CHARS"}), _BUDGET),
    "src/shared/report_facts.py": (
        frozenset(
            {
                "TRACE_FULL_CAP",
                "SHORT_ID_LENGTH",
                "CLOSE_CALL_IDS_SHOWN",
                "CLOSE_CALL_MARGIN",
                "UNSERVABLE_SHOWN",
                "REPLICA_READS_NAMED",
                "TABLE_SOURCES_SHOWN",
            }
        ),
        _BUDGET + "; CLOSE_CALL_MARGIN only decides which routed queries the report calls out",
    ),
    "src/tools/analysis/llm_advisor_base.py": (
        frozenset({"MAX_LLM_QUERIES", "MAX_RETRIES", "BASE_BACKOFF_SECONDS"}),
        _BUDGET,
    ),
}

_INLINE_DETECTION = "per-pattern detection inline in the analysis code (#491 facts module)"
# The per-pattern score adjustments are the pattern files' table_bonus (#921); what is left
# counts a table's join and multi-table-write queries for the relational baseline.
_JOIN_COUNTS = (
    _INLINE_DETECTION + ": the join and multi-table-write query counts the relational "
    "baseline score reads"
)
# String literals naming a pattern type inside function bodies (not in a module-level
# constant or a docstring), counted per file. path: (count, why)
INLINE_PATTERN_LITERALS: dict[str, tuple[int, str]] = {
    "src/tools/analysis/aurora_common_analysis_tools.py": (1, _INLINE_DETECTION),
    "src/tools/analysis/aurora_mysql_analysis_tools.py": (3, _JOIN_COUNTS),
    "src/tools/analysis/aurora_pg_analysis_tools.py": (3, _JOIN_COUNTS),
    "src/tools/analysis/documentdb_analysis_tools.py": (
        5,
        "the fit patterns the DocumentDB detectors report, looked up by type instead of by "
        "list position (#967)",
    ),
    "src/contracts/dynamodb_decision_trace.py": (
        4,
        "the DynamoDB denormalization subtypes the decision trace contract publishes "
        "(moved from the analysis tools, #973)",
    ),
    "src/tools/analysis/opensearch_analysis_tools.py": (
        6,
        _INLINE_DETECTION + ": the time-series classification's timestamp patterns, read twice",
    ),
    "src/tools/analysis/redis_analysis_tools.py": (
        11,
        "ElastiCache builds its patterns inline; test_pattern_catalog checks them against "
        "the catalog",
    ),
    "src/agents/schema_design/group_splitter.py": (
        3,
        "DynamoDB denormalization subtypes the group splitter keys on",
    ),
    "src/agents/referee/synthesis_report.py": (1, _WORDING),
    "src/agents/referee/triage.py": (1, "the text_search triage signal (same spelling)"),
}


# Engine-id string literals inside function bodies (not a module constant, class
# attribute, docstring or catalog lookup key), counted per file in NUMERIC_DIRS: the
# shape of a hard-coded routing branch (``if engine == "dynamodb": ...``).
# path: (count, why)
INLINE_ENGINE_LITERALS: dict[str, tuple[int, str]] = {
    "src/agents/referee/migration_wave_builders.py": (2, _WORDING),
    "src/agents/referee/reality_check_summary.py": (1, _WORDING),
    "src/agents/referee/synthesis_grounding.py": (3, _WORDING),
    "src/agents/referee/synthesis_handler.py": (3, _WORDING),
    "src/agents/referee/synthesis_report.py": (6, _WORDING),
    "src/agents/analysis/handler.py": (6, _PLUMBING + ": per-engine analysis dispatch"),
    "src/tools/analysis/dynamodb_analysis_tools.py": (
        1,
        _PLUMBING + ": the engine's own cost estimate reads its request prices (#494)",
    ),
    "src/tools/analysis/documentdb_analysis_tools.py": (
        1,
        _PLUMBING + ": the engine's own cost estimate reads its I/O price (#494)",
    ),
}


def _assignments(body: list[ast.stmt], prefix: str = ""):
    """``(name, value)`` for every single-name assignment in ``body``."""
    for node in body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            yield prefix + node.targets[0].id, node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            yield prefix + node.target.id, node.value


def _module_constants(tree: ast.Module):
    """Module-level assignments, whatever the name's case."""
    yield from _assignments(tree.body)


def _is_enum(node: ast.ClassDef) -> bool:
    names = {b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "") for b in node.bases}
    return bool(names & {"Enum", "StrEnum", "IntEnum"})


def _class_attributes(tree: ast.Module):
    """``Class.attr`` assignments in every class body, except enum members (the contract
    vocabularies, pinned to the catalog by ``test_engine_catalog``)."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and not _is_enum(node):
            yield from _assignments(node.body, prefix=f"{node.name}.")


def _named_literals(value: ast.AST) -> set[str]:
    """Engine ids and pattern keys written as literals in ``value``, except a lookup key
    passed to a catalog accessor."""
    lookups: set[int] = set()
    for node in ast.walk(value):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in CATALOG_ACCESSORS:
                lookups |= {id(n) for arg in node.args for n in ast.walk(arg)}
    named = ENGINE_IDS | ENGINE_DISPLAY_NAMES | PATTERN_KEYS
    return {
        text
        for n in ast.walk(value)
        if id(n) not in lookups
        if (text := _literal_string(n)) in named
    }


def _literal_string(node: ast.AST) -> str | None:
    """Return a string made only from string literals and ``+`` operations."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left)
        right = _literal_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _engine_branch_literals(tree: ast.AST) -> set[int]:
    """String expression nodes naming an engine in an ``if``/``elif`` test."""
    names = ENGINE_IDS | ENGINE_DISPLAY_NAMES
    return {
        id(value)
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        for value in ast.walk(node.test)
        if _literal_string(value) in names
    }


def _inline_numeric_rule_literals(tree: ast.AST) -> set[int]:
    """Numbers compared inside a function's explicit engine branch.

    Requiring a nested body keeps ordinary numeric arguments, indexes, formatting precision,
    and compound conditions such as ``len(engines) == 2 and engine != ...`` out of scope.
    """
    found: set[int] = set()

    def numeric_comparisons(node: ast.AST) -> None:
        for candidate in ast.walk(node):
            if not isinstance(candidate, ast.Compare):
                continue
            for operand in (candidate.left, *candidate.comparators):
                number = operand.operand if isinstance(operand, ast.UnaryOp) else operand
                if isinstance(number, ast.Constant) and type(number.value) in (int, float):
                    found.add(id(number))

    def visit(node: ast.AST, in_function: bool = False) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in node.body:
                visit(child, True)
            return
        if isinstance(node, ast.If):
            names_engine = any(
                _literal_string(value) in ENGINE_IDS | ENGINE_DISPLAY_NAMES
                for value in ast.walk(node.test)
            )
            if in_function and names_engine:
                for child in node.body:
                    numeric_comparisons(child)
            for child in (*node.body, *node.orelse):
                visit(child, in_function)
            return
        for sub in ast.iter_child_nodes(node):
            visit(sub, in_function)

    visit(tree)
    return found


def _is_number(value: ast.AST) -> bool:
    if isinstance(value, ast.UnaryOp):
        value = value.operand
    return isinstance(value, ast.Constant) and type(value.value) in (int, float)


def _scan_sources(sources: Iterable[tuple[str, str]]) -> dict[str, dict]:
    tables: dict[str, set[str]] = defaultdict(set)
    weights: dict[str, set[str]] = defaultdict(set)
    numbers: dict[str, set[str]] = defaultdict(set)
    inline: dict[str, int] = defaultdict(int)
    inline_engines: dict[str, int] = defaultdict(int)
    inline_engine_keywords: dict[str, int] = defaultdict(int)
    inline_numbers: dict[str, int] = defaultdict(int)
    for rel, source in sources:
        if rel.startswith("src/shared/catalog/"):
            continue  # the loader itself
        tree = ast.parse(source)
        in_constants: set[int] = set()
        module_level = {name for name, _value in _module_constants(tree)}
        for name, value in [*_module_constants(tree), *_class_attributes(tree)]:
            in_constants |= {id(n) for n in ast.walk(value)}
            keys = {k.value for k in getattr(value, "keys", []) if isinstance(k, ast.Constant)}
            if isinstance(value, ast.Dict) and SCORE_DIMENSIONS <= keys:
                weights[rel].add(name)
            elif _named_literals(value):
                tables[rel].add(name)
            elif (
                # Class-attribute numbers are field defaults, not rules; not scanned.
                name in module_level
                and _is_number(value)
                and rel.startswith(NUMERIC_DIRS)
            ):
                numbers[rel].add(name)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        lookups = _accessor_arguments(tree)
        branch_literals = _engine_branch_literals(tree)
        if rel.startswith(NUMERIC_DIRS) or rel.startswith(API_DIRS):
            numeric_count = len(_inline_numeric_rule_literals(tree))
            if numeric_count:
                inline_numbers[rel] = numeric_count
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg in ENGINE_IDS
                and (rel.startswith(NUMERIC_DIRS) or rel.startswith(API_DIRS))
            ):
                inline_engine_keywords[rel] += 1
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in in_constants or id(node) in docstrings or id(node) in lookups:
                continue
            if node.value in PATTERN_KEYS:
                inline[rel] += 1
            elif node.value in ENGINE_IDS and rel.startswith(NUMERIC_DIRS):
                inline_engines[rel] += 1
            elif (
                node.value in ENGINE_IDS | ENGINE_DISPLAY_NAMES
                and id(node) in branch_literals
                and rel.startswith(NUMERIC_DIRS + API_DIRS)
            ):
                inline_engines[rel] += 1
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or id(node) not in branch_literals:
                continue
            if (
                rel.startswith(NUMERIC_DIRS + API_DIRS)
                and _literal_string(node) in ENGINE_IDS | ENGINE_DISPLAY_NAMES
            ):
                inline_engines[rel] += 1
    return {
        "tables": tables,
        "weights": weights,
        "numbers": numbers,
        "inline": inline,
        "inline_engines": inline_engines,
        "inline_engine_keywords": inline_engine_keywords,
        "inline_numbers": inline_numbers,
    }


def _scan() -> dict[str, dict]:
    return _scan_sources(
        (path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted(SRC.rglob("*.py"))
    )


def _accessor_arguments(tree: ast.AST) -> set[int]:
    """Node ids of every literal passed to a catalog accessor (a lookup key, not a rule)."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in CATALOG_ACCESSORS:
                found |= {id(n) for arg in node.args for n in ast.walk(arg)}
    return found


@pytest.fixture(scope="module")
def scan() -> dict[str, dict]:
    return _scan()


def _drift(found: dict[str, set[str]], pinned: dict[str, tuple[frozenset[str], str]]):
    new = {
        path: sorted(names - pinned.get(path, (frozenset(), ""))[0])
        for path, names in found.items()
        if names - pinned.get(path, (frozenset(), ""))[0]
    }
    gone = {
        path: sorted(names - found.get(path, set()))
        for path, (names, _why) in pinned.items()
        if names - found.get(path, set())
    }
    return new, gone


def _count_drift(found: dict[str, int], pinned: dict[str, int]) -> dict[str, tuple[int, int]]:
    return {
        path: (found.get(path, 0), pinned.get(path, 0))
        for path in sorted(set(found) | set(pinned))
        if found.get(path, 0) != pinned.get(path, 0)
    }


# The routing decision code: every module of src/agents/referee/ but the ones whose engine
# literals INLINE_ENGINE_LITERALS lists as wording (the synthesis and wave text). It names
# no engine at all (#1081): no engine id or display name in a string literal anywhere in
# it (a module constant or a catalog lookup key included; docstrings excepted), and no
# attribute named after an engine id (an enum member such as a per-engine reason code).
# An engine-specific rule is an engine-file field or a role lookup instead.
ROUTING_DIR = "src/agents/referee/"
# Attributes named after an engine the routing code may still read, with why.
# path: (attribute names, why)
ENGINE_NAMED_ATTRIBUTES: dict[str, tuple[frozenset[str], str]] = {
    "src/agents/referee/assignment_resolver.py": (
        frozenset({"AURORA_FALLBACK"}),
        "the move-ledger pass of the relational fallback (MovePass, a contract vocabulary): "
        "it names the pass, not the engine the fallback picks",
    ),
}


def _routing_files(sources: Iterable[tuple[str, str]]) -> Iterable[tuple[str, str]]:
    wording = set(INLINE_ENGINE_LITERALS)
    for rel, source in sources:
        if rel.startswith(ROUTING_DIR) and rel not in wording:
            yield rel, source


def _engine_names_in_routing(sources: Iterable[tuple[str, str]]) -> dict[str, list[str]]:
    """``path -> ["line: name", ...]`` for every engine an engine-neutral routing module
    names in a string literal or an attribute (see ``ROUTING_DIR``)."""
    names = ENGINE_IDS | ENGINE_DISPLAY_NAMES
    words = {e.upper() for e in ENGINE_IDS}
    found: dict[str, list[str]] = defaultdict(list)
    for rel, source in _routing_files(sources):
        tree = ast.parse(source)
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        allowed = ENGINE_NAMED_ATTRIBUTES.get(rel, (frozenset(), ""))[0]
        for node in ast.walk(tree):
            if id(node) in docstrings:
                continue
            text = _literal_string(node) if isinstance(node, (ast.Constant, ast.BinOp)) else None
            line = getattr(node, "lineno", 0)
            if text in names:
                found[rel].append(f"{line}: {text!r}")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr not in allowed
                and any(f"_{w}_" in f"_{node.attr}_" for w in words)
            ):
                found[rel].append(f"{line}: .{node.attr}")
    return dict(found)


class TestRoutingCodeNamesNoEngine:
    """#1081: the routing decision code reads every engine-specific rule from the catalog."""

    def test_no_engine_is_named_in_the_routing_code(self) -> None:
        sources = [
            (path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8"))
            for path in sorted((SRC / "agents" / "referee").glob("*.py"))
        ]
        found = _engine_names_in_routing(sources)
        assert not found, (
            "Routing code names an engine. Read the rule from the catalog instead (an "
            "engine-file field, a role lookup, a catalog accessor keyed on a catalog-derived "
            f"engine): {found}"
        )

    @pytest.mark.passes_on_main
    def test_only_wording_modules_keep_engine_literals(self) -> None:
        routing = {p for p in INLINE_ENGINE_LITERALS if p.startswith(ROUTING_DIR)}
        assert all(INLINE_ENGINE_LITERALS[p][1].startswith(_WORDING) for p in routing), routing

    @pytest.mark.passes_on_main
    def test_every_listed_attribute_is_still_read(self) -> None:
        for rel, (attrs, why) in ENGINE_NAMED_ATTRIBUTES.items():
            source = (ROOT / rel).read_text(encoding="utf-8")
            assert why.strip() and all(f".{a}" in source for a in attrs), rel

    @pytest.mark.passes_on_main
    @pytest.mark.parametrize(
        "snippet",
        [
            'def f(engine):\n    return engine == "dynamodb"\n',
            'def f(engines):\n    return [e for e in engines if e in ("opensearch",)]\n',
            'X = catalog.engine_threshold("opensearch", "min_calls_per_second")\n',
            'def f():\n    return catalog.engine_threshold("opensearch", "x")\n',
            'def f(engine):\n    return engine == "Open" + "Search"\n',
            "def f(engine):\n    return ReasonCode.FLOOR_OPENSEARCH\n",
            "def f(engine):\n    return Pass.DYNAMODB\n",
        ],
    )
    def test_a_new_engine_name_in_routing_code_fails(self, snippet: str) -> None:
        found = _engine_names_in_routing([(ROUTING_DIR + "new_rule.py", snippet)])
        assert list(found) == [ROUTING_DIR + "new_rule.py"], snippet

    @pytest.mark.passes_on_main
    @pytest.mark.parametrize(
        "snippet",
        [
            '"""Docstrings may name DynamoDB and "opensearch"."""\n',
            "def f(engine):\n    return engine in catalog.denormalized_join_engines()\n",
            "def f(qa):\n    return qa.dynamodb_hint, ENGINE_LIST\n",
            "X = MovePass.AURORA_FALLBACK\n",
        ],
    )
    def test_engine_neutral_code_passes(self, snippet: str) -> None:
        rel = ROUTING_DIR + ("assignment_resolver.py" if "AURORA_FALLBACK" in snippet else "x.py")
        assert _engine_names_in_routing([(rel, snippet)]) == {}, snippet

    @pytest.mark.passes_on_main
    def test_a_wording_module_is_outside_this_check(self) -> None:
        wording = next(p for p in INLINE_ENGINE_LITERALS if p.startswith(ROUTING_DIR))
        assert _engine_names_in_routing([(wording, 'X = "dynamodb"\n')]) == {}


class TestNoRoutingRuleOutsideTheCatalog:
    _HOW = (
        "Build it from src/shared/catalog (a catalog file field) instead; if it really "
        "belongs in code, add it to this file's list with the reason."
    )

    def test_tables_that_name_an_engine_or_a_pattern(self, scan) -> None:
        new, gone = _drift(scan["tables"], ENGINE_OR_PATTERN_TABLES)
        assert not new, f"New constants name an engine id or pattern type. {self._HOW} {new}"
        assert not gone, f"These no longer name one: remove them from the list. {gone}"

    def test_score_weight_tables(self, scan) -> None:
        new, gone = _drift(scan["weights"], SCORE_WEIGHT_TABLES)
        assert not new, f"New score-weight tables. {self._HOW} {new}"
        assert not gone, f"Remove these from SCORE_WEIGHT_TABLES: {gone}"

    def test_numeric_constants_in_the_routing_code(self, scan) -> None:
        new, gone = _drift(scan["numbers"], NUMERIC_CONSTANTS)
        assert not new, f"New numeric constants where routing happens. {self._HOW} {new}"
        assert not gone, f"Remove these from NUMERIC_CONSTANTS: {gone}"

    def test_inline_pattern_type_literals(self, scan) -> None:
        found = dict(scan["inline"])
        pinned = {path: count for path, (count, _why) in INLINE_PATTERN_LITERALS.items()}
        assert found == pinned, (
            "A file gained or lost a pattern-type string literal in code. A new rule keyed on "
            "a pattern belongs in its pattern file; update INLINE_PATTERN_LITERALS only for "
            f"detection code. (found, pinned) per file: {_count_drift(found, pinned)}"
        )

    def test_inline_engine_id_literals(self, scan) -> None:
        found = dict(scan["inline_engines"])
        pinned = {path: count for path, (count, _why) in INLINE_ENGINE_LITERALS.items()}
        assert found == pinned, (
            "A file in the routing code gained or lost an engine-id string literal. An "
            "engine-specific branch (if engine == ...) is a routing rule: read it from the "
            "catalog instead; update INLINE_ENGINE_LITERALS only for wording or plumbing. "
            f"(found, pinned) per file: {_count_drift(found, pinned)}"
        )

    def test_engine_id_keyword_names_are_not_inline_rules(self, scan) -> None:
        assert not scan["inline_engine_keywords"], (
            "An engine id is used as a keyword name. Keep engine choices in the catalog: "
            f"{dict(scan['inline_engine_keywords'])}"
        )

    def test_numeric_comparisons_inside_engine_branches_are_not_inline_rules(self, scan) -> None:
        assert not scan["inline_numbers"], (
            "A numeric rule is compared inside a function body nested under an explicit "
            "engine branch. "
            f"Move the rule to the catalog or name it as a checked constant: {dict(scan['inline_numbers'])}"
        )

    def test_every_listed_exception_has_a_reason(self) -> None:
        for table in (ENGINE_OR_PATTERN_TABLES, SCORE_WEIGHT_TABLES, NUMERIC_CONSTANTS):
            for path, (names, why) in table.items():
                assert names and why.strip(), path
        for inline in (INLINE_PATTERN_LITERALS, INLINE_ENGINE_LITERALS):
            for path, (count, why) in inline.items():
                assert count and why.strip(), path

    def test_the_migrated_constants_hold_no_literal(self, scan) -> None:
        """The constants slices 1 and 2 moved read the catalog; none may regrow a literal."""
        migrated = {
            "src/shared/engine_names.py": {"ENGINE_DISPLAY_NAMES", "PLACEHOLDER_ENGINES"},
            "src/shared/cache_engines.py": {"CACHE_ENGINES"},
            "src/shared/migration_wave_engines.py": {
                "KV_ENGINES",
                "SEARCH_ENGINES",
                "DOCUMENT_ENGINES",
                "RELATIONAL_ENGINES",
                "NON_OWNER_ENGINES",
            },
            "src/shared/engine_capabilities.py": {
                "ACID_TRANSACTION_ENGINES",
                "TEXT_SEARCH_ENGINES",
                "FUZZY_SEARCH_ENGINES",
            },
            "src/tools/analysis/dynamodb_pattern_catalog.py": {"DYNAMODB_SCORE_WEIGHTS"},
            "src/tools/analysis/documentdb_pattern_catalog.py": {"DOCUMENTDB_SCORE_WEIGHTS"},
            "src/tools/analysis/scoring.py": {"DEFAULT_WEIGHTS"},
            "src/agents/referee/query_scoring.py": {
                "ANTI_PATTERN_PENALTIES",
                "CAPABILITY_SIGNAL_ENGINES",
                "CAPABILITY_REDESIGN_PENALTIES",
                "CAPABILITY_REDESIGN_NOTES",
                "CAPABILITY_EXEMPT_NOTES",
            },
            "src/tools/analysis/opensearch_pattern_catalog.py": {
                "OPENSEARCH_SEARCH_WEIGHTS",
                "OPENSEARCH_TIMESERIES_WEIGHTS",
                "OPENSEARCH_PATTERNS",
                "OPENSEARCH_ANTI_PATTERNS",
                "SEARCH_PATTERN_TYPES",
                "TIMESERIES_PATTERN_TYPES",
            },
            "src/tools/analysis/aurora_common_pattern_catalog.py": {
                "AURORA_PG_SCORE_WEIGHTS",
                "AURORA_MYSQL_SCORE_WEIGHTS",
                "AURORA_COMMON_PATTERNS",
                "AURORA_COMMON_ANTI_PATTERNS",
                "CATALOG_ENGINE_HINTS",
                "CATALOG_ENGINE_GUIDANCE",
            },
        }
        for path, names in migrated.items():
            tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
            defined = {name for name, _value in _module_constants(tree)}
            assert names <= defined, (path, sorted(names - defined))
            assert not names & scan["tables"].get(path, set()), path


def test_the_pattern_model_has_no_engine_specific_field() -> None:
    """Engine-neutral (Decision 8): per-engine facts live in ``engines``, never in a field
    named after an engine."""
    fields = set(PatternEntry.model_fields)
    assert not {f for f in fields if any(e in f for e in ENGINE_IDS)}


@pytest.mark.parametrize(
    ("source", "expected", "rel", "guard"),
    [
        (
            """
def choose():
    return dict(dynamodb=5)
""",
            "keyword",
            "src/agents/referee/planted.py",
            "test_engine_id_keyword_names_are_not_inline_rules",
        ),
        (
            """
def choose(engine):
    if engine == \"DynamoDB\":
        return True
    return False
""",
            "branch",
            "src/agents/referee/planted.py",
            "test_inline_engine_id_literals",
        ),
        (
            """
def choose(engine):
    if engine == \"dynamo\" + \"db\":
        return True
    return False
""",
            "branch",
            "src/agents/referee/planted.py",
            "test_inline_engine_id_literals",
        ),
        (
            """
def choose(engine, rows):
    if engine == \"dynamodb\":
        if rows > 100:
            return True
    return False
""",
            "number",
            "src/agents/referee/planted.py",
            "test_numeric_comparisons_inside_engine_branches_are_not_inline_rules",
        ),
        (
            """
def choose(engine):
    if engine == \"dynamodb\":
        return True
    return False
""",
            "branch",
            "src/api/planted.py",
            "test_inline_engine_id_literals",
        ),
    ],
    ids=("dict-keyword", "display-name", "concatenated-name", "inline-number", "api-branch"),
)
def test_engine_rule_shapes_are_detected(
    source: str, expected: str, rel: str, guard: str, scan
) -> None:
    """Each #900 report form reaches the real guard with a planted rule."""
    planted = _scan_sources([(rel, source)])
    combined = {key: dict(values) for key, values in scan.items()}
    for key, values in planted.items():
        combined[key].update(values)

    if expected == "keyword":
        assert planted["inline_engine_keywords"][rel] == 1
    elif expected == "number":
        assert planted["inline_numbers"][rel] == 1
    else:
        assert planted["inline_engines"][rel] == 1

    with pytest.raises(AssertionError):
        getattr(TestNoRoutingRuleOutsideTheCatalog(), guard)(combined)


def test_numeric_and_keyword_checks_ignore_unrelated_values() -> None:
    source = """
def format_result(score):
    if score > 100:
        return dict(retries=5)
    return None

def summarize(engines, primary):
    if len(engines) == 2 and primary != "dynamodb":
        return engines[0]
    return None
"""
    scan = _scan_sources([("src/api/unrelated.py", source)])

    assert scan["inline_numbers"] == {}
    assert scan["inline_engine_keywords"] == {}
