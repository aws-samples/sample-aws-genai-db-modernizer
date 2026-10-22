"""Guard against internal wording leaking into customer-facing text (#572).

Extends the #406 guard -- which only checked that a bare issue tag (``#167``)
never survives in a reality-check consolidation's ``reason`` -- to every
customer-facing string this pipeline writes: ``report.json``,
``assignment/v*/assignment.json`` (its per-query ``reason`` fields), the
reality-check output, and the rendered deliverables (decision/analysis HTML,
the engineering Markdown report, the PDF). It flags five classes of leak:

- a raw engine id (``dynamodb``) where a display name belongs -- the engine
  ids are read from ``src.shared.engine_names.ENGINE_DISPLAY_NAMES``'s keys,
  the single source of truth every renderer is supposed to go through
  (``display_engine``) before showing an engine name;
- an ``ADR-NNN`` reference -- internal architecture-decision-record numbering,
  meaningful only inside this repository;
- a snake_case reason code immediately followed by a colon (``tie_unresolved:``)
  -- an internal state name, not prose, leaking in place of a sentence. A
  known engine id followed by a colon (``aurora_mysql:``) is reported once,
  as an ``engine_id`` finding, not a second time here. #587 round 3's real-
  model run found that a real model will routinely write ordinary prose that
  also happens to look like this shape -- a column name followed by a colon
  ("ordered by meta_id: one Query per comment_id") is not a reason code
  leaking -- so this detector never trusts the shape alone (round 4):

  - inside a *deterministic* reason field this pipeline's own code writes
    (``REASON_CODE_FIELDS``/``REASON_CODE_FIELD_PATHS`` below -- never a
    model-written field such as an access pattern's ``description``, a
    summary or a design rationale), any snake_case-plus-colon token is still
    flagged, same as before -- these fields never carry free text with a
    coincidental column name, so the shape alone is reliable there;
  - everywhere else (every other JSON field, and the three rendered-text
    scanners below, which have no field path to check at all) the token
    must also be one of ``KNOWN_REASON_CODES`` -- the internal state names
    this pipeline's own code is actually capable of writing (tie kinds,
    ``EngineScore.gate_kind`` values, the sanity sweep's
    ``blocking_rule`` values, exclusion rule ids), imported from where each
    is defined rather than retyped here, so a new one is covered the moment
    it is added at the source;
- a known log-only phrase: today, exactly the ``description`` text of an
  ``ExclusionRule`` (``src.agents.referee.engine_exclusions``) -- its own
  module docstring documents ``description`` as "the technical reason, for
  logs and the decision trace only", with ``customer_message`` as the plain-
  language counterpart meant for customer-facing text;
- the old "confidence" wording for the routing fit score (bug: Deliverables
  present a fit score as a confidence percentage) -- "NN% confidence" reads as
  a success probability, when the figure is a workload-fit score
  (``src.shared.ranking``). The label is now "fit"/"workload fit"
  (``src.shared.workload_fit_labels``); this detector is a plain regex, not a
  field exclusion list, since the old wording must never appear in rendered
  text regardless of which field it came from. Triage's own classification
  confidence is a different metric this bug does not cover and has no
  adjacent percentage in its label, so it never matches.

``scan_json`` walks a parsed JSON document (``report.json``,
``assignment.json``, the reality-check output) and only scans STRING VALUES,
never a dict's own keys (``owners``, ``totals`` and similar maps are always
keyed by a raw id). A value is also skipped when its field is excluded --
by two different mechanisms, deliberately not by matching the value itself
("skip any string that happens to look like an id" would also hide a leak
inside a free-text field whose value coincidentally looks like one):

- ``ID_FIELDS`` -- a bare field name, excluded at any depth. Reserved for
  names distinctive enough that no other field in these contracts shares
  them (``assigned_engine``, ``cache_engine``, ``pattern_id``, ``query_text``, ...);
- ``ID_FIELD_PATHS`` -- a ``(immediate_parent_key, field_name)`` pair, for a
  field name generic enough that an unrelated field elsewhere could share it
  (``target``, ``database``, ``service``, ``owner``, ``engine``, ``names``):
  only a value whose own immediate container was reached through
  ``parent_key`` is excluded, so the same bare name under a different parent
  is still scanned. ``parent_key`` is the most recent dict/list key the walk
  passed through (a list's own key, not its numeric index), i.e. the schema
  path with every index collapsed -- not the bare field name alone.

Each entry in both sets names the contract field (``src/contracts/``) or the
dict-literal call site it corresponds to, so a renamed or newly-generic field
is easy to re-check.

``scan_markdown`` additionally strips a leading YAML frontmatter block (the
engineering report's own document metadata -- ``job_id``, ``source_artifact``,
``generated``, ...) before scanning: those are structured keys, the Markdown
analogue of an ``ID_FIELDS`` exclusion, not reason text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.agents.referee.consolidation_validator import BLOCKING_RULES
from src.agents.referee.engine_exclusions import EXCLUSION_RULES
from src.agents.referee.query_scoring import GATE_KINDS
from src.agents.referee.tie_break import TIE_KINDS
from src.shared.engine_names import ENGINE_DISPLAY_NAMES

# Fields distinctive enough that no other field in src/contracts/ shares their
# bare name -- excluded at any depth. See the module docstring for why a
# generic name instead goes in ID_FIELD_PATHS, qualified by its parent.
ID_FIELDS = frozenset(
    {
        # Engine ids.
        "assigned_engine",  # QueryAssignment.assigned_engine, TiedQuery.assigned_engine
        "cache_engine",  # QueryAssignment.cache_engine
        "from_engine",  # Consolidation.from_engine
        "to_engine",  # Consolidation.to_engine
        "recommended_database",  # TableMapping.recommended_database
        "primary_engine",  # TableAssignment.primary_engine
        "excluded_engine",  # ExclusionResult.excluded_engine (decision-trace shape)
        "source_engine",  # AuroraEngineChoice.source_engine, EVENT_SOURCING applies_to
        "replaces_engine",  # ServiceIntegrationRecommendation.replaces_engine
        "write_engine",  # CQRS pattern applies_to.write_engine
        "view_engine",  # Materialized View pattern applies_to.view_engine
        "fronts",  # MigrationWave.fronts
        "engines",  # QueryGroup.engines, MigrationWave.engines, Polyglot applies_to.engines
        "read_engines",  # CQRS pattern applies_to.read_engines
        "write_engines",  # TableAssignment / TableSource.write_engines (#992); CQRS applies_to
        "target_engines",  # EVENT_SOURCING pattern applies_to.target_engines
        "tied_engines",  # QueryAssignment.tied_engines, TiedQuery.tied_engines
        "final_engine",  # QueryTraceResponse.final_engine (query_trace.py, #339)
        "moves_from",  # MigrationWave.moves_from
        "serves_from",  # MigrationWave.serves_from
        "eliminated_engines",  # RealityCheckOutputContract.eliminated_engines
        "deciding_features",  # AuroraEngineChoice.deciding_features
        "designed_engines",  # ReportFacts.table_basis.designed_engines (#615)
        "pending_engines",  # ReportFacts.table_basis.pending_engines (#615)
        "moved_to",  # ReportFacts.removed_engines[].moved_to (#615)
        "engines_in_use",  # SynthesisOutputContract.engines_in_use: raw engine ids (#1076)
        "signal_overrides",  # ReportFacts.engines[].signal_overrides: raw signal ids (#584)
        # Rule/pattern ids (exclusion rule ids, generated pattern ids like
        # "relational-aurora_mysql-<query_id>"): internal identifiers, not prose.
        "rule_id",  # ExclusionResult.rule_id (decision-trace shape)
        "signal_override",  # QueryAssignment.signal_override
        "pattern_id",  # AccessPattern.pattern_id (synthesis_output.py:126)
        "anti_pattern_id",  # analysis contracts' anti_pattern_id
        "resolved_on",  # synthesis_report._resolved_risk's resolved_on
        "reattributed_from",  # synthesis_report's reattributed_from
        # Query/table ids and the customer's own SQL text (quoting a column named
        # ``like_count`` is not this pipeline's internal wording).
        "query_id",  # QueryAssignment.query_id, TiedQuery.query_id, ...
        "query_ids",  # AccessPattern.query_ids and similar
        "table_id",  # TableAssignment.table_id
        "source_table",  # TableMapping.source_table
        "source_tables",  # QueryAssignment.source_tables
        "tables_accessed",  # collector query shape
        "affected_tables",  # Risk.affected_tables
        "co_dependency_groups",  # Assignment.co_dependency_groups
        # #992: engine and query ids of each table's source of truth and replicas.
        "write_groups",  # Assignment.write_groups: {table_id: [query_id, ...]}
        "source_of_truth",  # TableAssignment / TableSource / ReplicatedTableFact
        "replica_engines",  # TableAssignment.replica_engines, ReplicatedTableFact
        "write_query_ids",  # TableSource.write_query_ids
        "read_query_ids",  # ReplicaRead.read_query_ids
        "replica_read_query_ids",  # ReplicatedTableFact.replica_read_query_ids
        "single_engine_tables",  # TableSourceFacts.single_engine_tables: table ids
        "query_text",  # collector query shape -- the customer's own SQL
        "source_query",  # OpenSearch UnsupportedPattern.source_query -- also the customer's own SQL
        "query_excerpt",  # QueryTraceEntry.query_excerpt (#339) -- the start of the customer's SQL
        # Job/database identifiers (also the Markdown frontmatter keys scan_markdown
        # strips before scanning -- listed here too for a caller that reads
        # report.json's own ``job_id``/``database_name`` fields directly).
        "job_id",
        "database_name",
    }
)

# (immediate parent key, field name): a generic field name excluded only when
# reached through that exact parent -- the same bare name under a different,
# unrelated parent is still scanned. "Immediate parent key" is the most recent
# dict/list key the walk passed through, i.e. the schema path with every list
# index collapsed (see the module docstring).
ID_FIELD_PATHS = frozenset(
    {
        ("ranking", "target"),  # EngineRanking.target (synthesis_output.py:219)
        ("cost_breakdown", "database"),  # CostBreakdown.database (synthesis_output.py:298)
        ("eliminated_engine_costs", "database"),  # same CostBreakdown shape, TCO's own list
        ("retired_engine_savings", "database"),  # same CostBreakdown shape (#949)
        ("considered_not_recommended", "engine"),  # ConsideredEngine.engine (#949)
        ("considered_not_recommended", "absorbed_by"),  # ConsideredEngine.absorbed_by (#949)
        ("databases", "service"),  # recommended_architecture.databases[].service
        ("access_patterns", "engine"),  # QueryGroup.access_patterns[].engine (extra field)
        ("cache_overlay", "engine"),  # CacheOverlaySummary.engine (assignment_models.py:203)
        ("aurora_engine_choice", "engine"),  # AuroraEngineChoice.engine (assignment_models.py:276)
        ("resolved_risks", "engine"),  # synthesis_report._resolved_risk's own "engine" (extra)
        ("trade_offs", "engine"),  # TradeOff.engine (extra field)
        ("table_owners", "owner"),  # TableOwner.owner (synthesis_output.py:429)
        # #212: each table's owner line, engine ids and a basis code, never prose; the
        # deliverables name the engines through display_engine.
        ("table_mappings", "owner"),  # TableMapping.owner
        ("table_mappings", "owner_basis"),  # TableMapping.owner_basis
        ("table_mappings", "also_read_by"),  # TableMapping.also_read_by
        ("table_mappings", "cached_by"),  # TableMapping.cached_by
        ("table_lines", "owner"),  # TableOwnershipRow.owner
        ("table_lines", "owner_basis"),  # TableOwnershipRow.owner_basis
        ("table_lines", "also_read_by"),  # TableOwnershipRow.also_read_by
        ("table_lines", "cached_by"),  # TableOwnershipRow.cached_by
        ("table_lines", "target_engine"),  # TableOwnershipRow.target_engine
        ("table_owner_lines", "cache_engines"),  # TableOwnershipFacts.cache_engines
        ("replicas", "engine"),  # TableSource.replicas[].engine (#992)
        ("table_sources", "table"),  # TableSource.table / ReplicatedTableFact.table (#992)
        ("replicated_tables", "table"),  # ReplicatedTableFact.table (#992)
        ("kept_in_sync", "owner"),  # ReportFacts.waves[].kept_in_sync: TableOwner (#615)
        ("shared_in_wave", "owner"),  # ReportFacts.waves[].shared_in_wave: TableOwner (#615)
        ("retired_in_use_engines", "engine"),  # ReportFacts.retired_in_use_engines (#1076)
        ("removed_engines", "engine"),  # ReportFacts.removed_engines[].engine (#615)
        ("reality_check_decisions", "engine"),  # ReportFacts.reality_check_decisions (#595)
        ("engine_pairs", "engine"),  # ReportFacts.close_calls.engine_pairs[].engine (#730)
        ("engine_pairs", "alternative"),  # ReportFacts.close_calls.engine_pairs[] (#730)
        ("unresolved_names", "names"),  # UnresolvedNames.names, report.json's own key
        ("unresolved_table_names", "names"),  # UnresolvedNames.names, assignment.json's own key
        # table_mappings and the per-engine schema_designs projection
        # (synthesis_handler._build_schema_summaries and friends), #572 round 2:
        # key/collection/index/data-stream templates and the engine alternatives
        # that TableMapping.alternatives (synthesis_output.py:287, extra="allow")
        # carries are id-holding, not prose, the same way their declared-field
        # siblings already are.
        ("table_mappings", "target_table"),  # TableMapping's own extra field
        ("alternatives", "database"),  # the alternative's engine id
        ("alternatives", "target_table"),  # the alternative's own target table/key
        ("tables", "table_name"),  # schema_designs[engine].tables[]: table/key/index/
        # collection/data-stream name or template (_build_*_schema_summary in
        # synthesis_handler.py always writes this key, whatever the engine)
        ("tables", "primary_key"),  # Aurora's table def: list of column names
        ("trade_offs", "target_tables"),  # TradeOff.target_tables (schema_design_output.py)
        ("optimizations", "target"),  # Aurora Optimization.target: table/column/cluster name(s)
        ("app_layer_notes", "feature"),  # Aurora AppLayerNote.feature: a short code, not prose
        ("migration_notes", "object_name"),  # MigrationNote.object_name: a stored-object name
        # RealityCheckOutputContract.flag_decisions (FlagDecision, #647): the
        # engine ids, the reason-category code and the co-dependency members
        # moved with the flag are structured fields; its ``reason`` (built with
        # display_engine) is still scanned.
        # report.json query_pattern_groups (QueryPatternGroup, #1146): each member's
        # assigned engine id, the operation code and the table and column names are
        # structured fields; no deliverable renders them as text.
        ("members", "engine"),
        ("query_pattern_groups", "operation"),
        ("query_pattern_groups", "tables"),
        ("query_pattern_groups", "returned_columns"),
        ("flag_decisions", "flagged_engine"),
        ("flag_decisions", "destination_engine"),
        ("flag_decisions", "reason_category"),
        ("flag_decisions", "moved_with_group"),
        # The model's own ``explanation`` (maintainer decision, 2026-10-14): model
        # evidence kept for audit, which no deliverable renders. Exempt only while
        # that holds -- tests/unit/tools/validation/test_customer_text_guard_flag_
        # decisions.py fails if a renderer under src/report or the UI ever reads
        # it, so this exemption is revisited before it reaches a customer.
        ("flag_decisions", "explanation"),
        # Assignment / report.json post_schema_reroutes (PostSchemaReroute, #629):
        # from_engine/to_engine are already ID_FIELDS; the reason-category code is
        # structured too. Its ``reason`` (built with display_engine) is scanned.
        ("post_schema_reroutes", "reason_category"),
        # Assignment.moves, the move ledger (src/contracts/move_ledger.py, #616):
        # a machine record with no prose. query_id, from_engine, to_engine and
        # tied_engines are already ID_FIELDS; scores are numbers, never scanned.
        # tests/unit/tools/validation/test_customer_text_guard_move_ledger.py
        # fails if a Move model gains a string field not listed here.
        ("moves", "pass"),  # Move.pass_ (MovePass code)
        ("moves", "reason_code"),  # Move.reason_code (ReasonCode rule id)
        ("moves", "group_key"),  # Move.group_key (12-hex hash)
        ("moves", "actor"),  # Move.actor (MoveActor code)
        ("runner_up", "engine"),  # Move.runner_up (EngineScore.engine)
        ("candidates", "engine"),  # MoveEvidence.candidates[] (CandidateScore.engine)
        ("candidates", "gate"),  # CandidateScore.gate (gate kind code)
        ("group_gates", "engine"),  # MoveEvidence.group_gates[] (GroupGate.engine, #787)
        ("group_gates", "gate"),  # GroupGate.gate (gate kind code)
        ("evidence", "signal"),  # MoveEvidence.signal (triage signal name)
        ("evidence", "replaced_engine"),  # MoveEvidence.replaced_engine
        ("evidence", "tie_rule"),  # MoveEvidence.tie_rule (ReasonCode rule id)
        ("evidence", "previous_tied_engines"),  # MoveEvidence.previous_tied_engines (engine ids)
        ("evidence", "choice_id"),  # MoveEvidence.choice_id: a table id (#1003)
        ("evidence", "choice_rule"),  # MoveEvidence.choice_rule: a decide_for_me rule code
        # Pending engine choices and --auto's decisions (#1003): ids and codes. Each
        # ``why_tied``/``trade_off``/``decide_for_me_reason``/``reason`` is the
        # customer-facing sentence (built with display_engine) and is still scanned.
        ("pending_engine_choices", "choice_id"),
        ("pending_engine_choices", "table"),
        ("pending_engine_choices", "tables"),
        ("pending_engine_choices", "current_engine"),
        ("pending_engine_choices", "current_engines"),
        ("pending_engine_choices", "decide_for_me_engine"),
        ("pending_engine_choices", "decide_for_me_rule"),
        ("pending_choices", "choice_id"),  # the API's AssignmentResponse.pending_choices
        ("pending_choices", "table"),
        ("pending_choices", "tables"),
        ("pending_choices", "current_engine"),
        ("pending_choices", "current_engines"),
        ("pending_choices", "decide_for_me_engine"),
        ("pending_choices", "decide_for_me_rule"),
        ("options", "engine"),  # PendingEngineChoice.options[] (ChoiceEngine.engine)
        ("auto_decided_choices", "choice_id"),
        ("auto_decided_choices", "table"),
        ("auto_decided_choices", "engine"),
        ("auto_decided_choices", "alternatives"),
        ("auto_decided_choices", "rule"),
        ("pending_choice_pairs", "tables"),  # ReportFacts.pending_choice_pairs[].tables
        ("auto_decided_by_engine", "engine"),  # ReportFacts.auto_decided_by_engine[].engine
        ("auto_decided_by_engine", "tables"),
        # report.json query_traces (QueryTraceDigest, src/contracts/query_trace.py,
        # #339): each trace's steps are Moves (the ("moves", ...) entries above
        # cover them), its first step is ``first_assignment``; the machine
        # fields of the runner-up and the outscored engines are codes. Every
        # ``explanation``/``summary``/``gate_text``/``rule``/``label`` is the
        # reason renderer's sentence and is still scanned.
        ("first_assignment", "pass"),  # TraceStep.pass_ (MovePass code)
        ("first_assignment", "reason_code"),  # TraceStep.reason_code (ReasonCode rule id)
        ("first_assignment", "group_key"),  # TraceStep.group_key (12-hex hash)
        ("first_assignment", "actor"),  # TraceStep.actor (MoveActor code)
        ("runner_up", "gate"),  # TraceRunnerUp.gate (gate kind code)
        ("runner_up", "basis"),  # TraceRunnerUp.basis (query / group_mean)
        ("outscored_by", "engine"),  # TraceOutscored.engine
        ("outscored_by", "basis"),  # TraceOutscored.basis
        ("outscored_by", "decided_by_rule"),  # TraceOutscored.decided_by_rule (ReasonCode)
        ("unlisted_rules", "rule"),  # TraceRuleCount.rule (ReasonCode rule id)
        # query_traces.close_calls (CloseCall, #730): engine ids and the basis code.
        ("close_calls", "engine"),  # CloseCall.engine
        ("close_calls", "alternative"),  # CloseCall.alternative
        ("close_calls", "basis"),  # CloseCall.basis (query / group_mean)
        # query_traces.unservable (UnservableQuery, #729) and facts.unservable:
        # engine ids, the kind, the rule and gate codes and the capability ids.
        # explanation, rule_label, gate_text and the redesign hints are scanned.
        ("unservable", "engine"),  # UnservableQuery.engine
        ("unservable", "kind"),  # UnservableQuery.kind
        ("unservable", "rule"),  # UnservableQuery.rule (ReasonCode rule id)
        ("unservable", "required_capabilities"),  # capability ids
        ("unservable_queries", "engine"),  # UnservableQueryFact.engine
        ("unservable_queries", "kind"),  # UnservableQueryFact.kind
        ("unservable_queries", "rule"),  # UnservableQueryFact.rule
        ("unservable_queries", "required_capabilities"),  # capability ids
        ("blocking_gates", "engine"),  # UnservableGate.engine / UnservableGateFact.engine
        ("blocking_gates", "gate"),  # gate kind code
        # facts.relational_gaps (RelationalGapFacts, #471): the target engine id and
        # each gap's evidence ids (tables, queries, objects). feature, reason and docs
        # are the catalog's sentences and are scanned.
        ("relational_gaps", "target_engine"),
        ("gaps", "tables"),
        ("gaps", "queries"),
        ("objects", "id"),
        # report.json flagged_unservable (FlaggedUnservableQuery, #888): engine
        # ids and the source, cause, reason-category and gate codes. Its
        # explanation, reason_label and gate_text are scanned.
        ("flagged_unservable", "engine"),
        ("flagged_unservable", "source"),
        ("flagged_unservable", "cause"),
        ("flagged_unservable", "reason_category"),
        ("flagged_unservable", "gate"),
        ("flagged_unservable", "required_capabilities"),
    }
)

# The opposite of ID_FIELDS/ID_FIELD_PATHS: fields this pipeline's own
# deterministic code writes (never an LLM) -- #587 round 4. The snake_case
# reason-code detector trusts the bare "snake_case_token:" shape ONLY inside
# one of these; everywhere else (including a field with the same bare name
# elsewhere, and the three rendered-text scanners, which carry no field path
# at all) it also requires the token to be a KNOWN_REASON_CODE.
REASON_CODE_FIELDS = frozenset(
    {
        "assignment_reason",  # QueryAssignment.assignment_reason (assignment_models.py:123)
        "cache_notes",  # Assignment.cache_notes (assignment_models.py:331)
        "cache_safety_net_notes",  # Assignment.cache_safety_net_notes (assignment_models.py:338)
    }
)
REASON_CODE_FIELD_PATHS = frozenset(
    {
        ("consolidations", "reason"),  # Consolidation.reason (reality_check_output.py:129)
        ("sweep_decisions", "reason"),  # SweepDecision.reason (reality_check_output.py:168)
    }
)

# Longest id first so "aurora_mysql" is matched whole, not as "aurora" plus a
# dangling "_mysql" (re's alternation takes the first branch that matches at a
# given position, not the longest).
_ENGINE_ID_RE = re.compile(
    r"\b("
    + "|".join(re.escape(k) for k in sorted(ENGINE_DISPLAY_NAMES, key=len, reverse=True))
    + r")\b"
)
_ADR_RE = re.compile(r"\bADR-\d+\b")
# A reason label reads "tie_unresolved: no routing rule decides" -- the colon is
# followed by whitespace, then prose. An identifier-shaped token that happens to
# contain an underscore and a colon never is: a key template
# ("wp_options:{option_name}", "wp_postmeta:{post_id}:meta") has the colon
# immediately followed by "{" or another identifier segment, never whitespace, and
# a bare table/column name has no colon at all. The lookahead (never consumed, so
# the finding's own text stays just the code and its colon) is what #572's second
# review round asked for: "only flag a snake_case code used as a reason label ...
# followed by prose", not any snake_case-plus-colon shape.
_SNAKE_REASON_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+:(?=\s+[A-Za-z])")
# Backtick-delimited Markdown code spans and <code>...</code> HTML spans: an
# identifier quoted this way (a table name, a key template) is customer-facing
# prose's way of saying "this exact string", not a reason sentence, whatever
# punctuation it happens to contain -- skipped for the snake_case detector only
# (not engine_id/adr_reference/log_only_phrase: a raw engine id is still a leak
# even quoted in backticks).
_CODE_SPAN_RE = re.compile(r"`[^`\n]+`|<code\b[^>]*>.*?</code>", re.IGNORECASE | re.DOTALL)

# A fit score presented as a confidence percentage (bug: Deliverables present a
# fit score as a confidence percentage) -- "78% confidence" reads as "we are 78%
# sure this will work", when the figure is actually a workload-fit score. The
# label everywhere is now "fit"/"workload fit" (`src.shared.workload_fit_labels`);
# this regression guard stops the old wording from coming back in rendered text.
# Triage's own classification confidence (a different metric this bug does not
# cover, e.g. "Triage confidence") has no adjacent percentage in its label, so it
# never matches this shape.
#
# The negative lookahead excludes the legitimate statistical phrases "NN%
# confidence interval"/"NN% confidence level" (and their plurals) -- a real,
# unrelated statistics term this pipeline could reasonably write about data
# quality or load testing, not a mislabelled fit score. Review round 1 found
# this guard flagging "95% confidence interval" as a violation.
_CONFIDENCE_WORDING_RE = re.compile(
    r"\d+%\s*confidence\b(?!\s+(?:interval|level)s?\b)", re.IGNORECASE
)

# The technical, log-only reason text of every exclusion rule (never meant for a
# customer -- see the module docstring above and engine_exclusions.py's own). Built
# from the rules themselves so a new rule is covered automatically.
LOG_ONLY_PHRASES: tuple[str, ...] = tuple(rule.description for rule in EXCLUSION_RULES)

# Every internal reason/state code this pipeline's code can write -- #587
# round 4. Imported from where each is actually defined (never retyped as a
# literal here), so a new tie kind, gate kind, blocking rule or exclusion
# rule id is covered the moment it is added at the source, with no second
# edit in this file. Used outside REASON_CODE_FIELDS/REASON_CODE_FIELD_PATHS
# (see above) to tell a genuine internal code apart from ordinary prose that
# merely has the same "snake_case_token:" shape -- a model-written sentence
# can mention a column name followed by a colon ("ordered by meta_id: one
# Query per comment_id"); it is never going to say "mixed" or
# "tie_unresolved" instead of a real sentence.
KNOWN_REASON_CODES: frozenset[str] = (
    frozenset(TIE_KINDS)
    | frozenset(GATE_KINDS)
    | frozenset(BLOCKING_RULES)
    | frozenset(rule.rule_id for rule in EXCLUSION_RULES)
)


@dataclass(frozen=True)
class Finding:
    kind: str  # "engine_id" | "adr_reference" | "snake_case_reason_code" | "log_only_phrase"
    # | "confidence_wording"
    path: str
    text: str
    excerpt: str


def _excerpt(text: str, start: int, end: int, radius: int = 40) -> str:
    return text[max(0, start - radius) : end + radius]


def scan_text(text: str, path: str = "", *, strict_reason_codes: bool = True) -> list[Finding]:
    """Run every detector over a plain-text (or already-detagged) string.

    ``strict_reason_codes`` (#587 round 4) gates the snake_case reason-code
    detector: when ``True`` (the default -- every caller except
    ``_walk_json`` scanning a ``REASON_CODE_FIELDS``/``REASON_CODE_FIELD_PATHS``
    value), a "snake_case_token:" match is only a finding when the token is
    also a ``KNOWN_REASON_CODE`` -- so ordinary prose that merely has the
    same shape (a column name before a colon) is never flagged. When
    ``False``, any match is a finding, same as before this round -- safe
    only inside a field this pipeline's own deterministic code writes, which
    never contains free text with a coincidental column name.
    """
    if not text:
        return []
    findings: list[Finding] = []
    for m in _ENGINE_ID_RE.finditer(text):
        findings.append(Finding("engine_id", path, m.group(0), _excerpt(text, m.start(), m.end())))
    for m in _ADR_RE.finditer(text):
        findings.append(
            Finding("adr_reference", path, m.group(0), _excerpt(text, m.start(), m.end()))
        )
    code_spans = [(m.start(), m.end()) for m in _CODE_SPAN_RE.finditer(text)]
    for m in _SNAKE_REASON_RE.finditer(text):
        token = m.group(0)[:-1]  # drop the trailing colon
        if token in ENGINE_DISPLAY_NAMES:
            continue  # already reported once as an engine_id finding, above
        if strict_reason_codes and token not in KNOWN_REASON_CODES:
            continue  # shaped like a reason code, but not one this pipeline writes
        if any(start <= m.start() < end for start, end in code_spans):
            continue  # quoted as a literal identifier, not stated as a reason
        findings.append(
            Finding("snake_case_reason_code", path, m.group(0), _excerpt(text, m.start(), m.end()))
        )
    for phrase in LOG_ONLY_PHRASES:
        i = text.find(phrase)
        if i != -1:
            findings.append(
                Finding("log_only_phrase", path, phrase, _excerpt(text, i, i + len(phrase)))
            )
    for m in _CONFIDENCE_WORDING_RE.finditer(text):
        findings.append(
            Finding("confidence_wording", path, m.group(0), _excerpt(text, m.start(), m.end()))
        )
    return findings


def scan_json(obj: Any, path: str = "$") -> list[Finding]:
    """Walk a parsed JSON document, scanning every string value not excluded by
    ``ID_FIELDS``/``ID_FIELD_PATHS``. Dict keys are never scanned (always an id,
    never prose)."""
    findings: list[Finding] = []
    _walk_json(obj, path, findings, parent_key=None)
    return findings


# ``Risk.description`` has no separate engine field (``src.shared.report_facts.
# risk_engine_and_body``'s own docstring: "Risks carry no explicit engine field; the
# engine is encoded as a leading [documentdb]/[elasticache] tag in the description").
# Every renderer parses this tag off before display (risk_engine_and_body,
# synthesis_grounding._ENGINE_TAG) -- it is the same kind of
# id-holding prefix ID_FIELDS excludes for a whole field, just embedded in one that
# also carries a real prose body. Stripped before scanning so a genuine leak in that
# body is still caught, without excluding the field (and its body) outright.
#
# Qualified by parent (#572 review item 5), the same way as ID_FIELD_PATHS: only
# ``risks[].description``/``resolved_risks[].description`` use this tag convention --
# an unrelated field also named "description" (``TradeOff.description``,
# ``ArchitecturalPattern.description``, ...) is never known to start with one, so
# stripping it there could silently hide a real leak that happens to start with "[".
_LEADING_ENGINE_TAG_RE = re.compile(r"^\[[^\]]+\]\s*")
_TAG_PREFIXED_FIELD_PATHS = frozenset(
    {
        ("risks", "description"),
        ("resolved_risks", "description"),
    }
)


def _walk_json(obj: Any, path: str, findings: list[Finding], parent_key: str | None) -> None:
    """``parent_key`` is the key (list or dict) through which ``obj`` itself was
    reached -- a list's own key, never a numeric index, so a schema path with
    every index collapsed. Used to qualify ``ID_FIELD_PATHS`` and the tag-prefixed
    description paths against the field's *immediate* container, not its bare name
    alone (see the module docstring)."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ID_FIELDS or (parent_key, key) in ID_FIELD_PATHS:
                continue
            if isinstance(value, str) and (parent_key, key) in _TAG_PREFIXED_FIELD_PATHS:
                stripped = _LEADING_ENGINE_TAG_RE.sub("", value)
                findings.extend(scan_text(stripped, f"{path}.{key}"))
                continue
            if key in REASON_CODE_FIELDS or (parent_key, key) in REASON_CODE_FIELD_PATHS:
                # A field this pipeline's own deterministic code writes (#587
                # round 4): the snake_case detector trusts the bare shape
                # here, with no KNOWN_REASON_CODES membership check (see
                # scan_text's docstring) -- a str value directly, or (e.g.
                # Assignment.cache_notes) a list of them.
                if isinstance(value, str):
                    findings.extend(scan_text(value, f"{path}.{key}", strict_reason_codes=False))
                elif isinstance(value, list):
                    for i, item in enumerate(value):
                        if isinstance(item, str):
                            findings.extend(
                                scan_text(item, f"{path}.{key}[{i}]", strict_reason_codes=False)
                            )
                continue
            _walk_json(value, f"{path}.{key}", findings, parent_key=key)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _walk_json(item, f"{path}[{i}]", findings, parent_key=parent_key)
    elif isinstance(obj, str):
        findings.extend(scan_text(obj, path))


# A Markdown deliverable's leading frontmatter block: "---\n...\n---\n", its own
# document metadata (title, database, job_id, generated, source_artifact) -- the
# Markdown analogue of an ID_FIELDS exclusion, not reason text.
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def scan_markdown(text: str, path: str = "") -> list[Finding]:
    """Scan a rendered Markdown deliverable, skipping its YAML frontmatter."""
    return scan_text(_FRONTMATTER_RE.sub("", text, count=1), path)


# Plain-prose style (#1034): audit fields that keep a model's text as received,
# or the reasons it was rejected, for the trace. No deliverable renders them
# (``tests/unit/report/test_plain_prose_fixtures.py`` checks the renderers never
# read them), so the style list does not apply to them.
STYLE_AUDIT_FIELDS = frozenset(
    {
        "summary_llm",  # SynthesisOutputContract.summary_llm
        "summary_validation_warnings",  # SynthesisOutputContract.summary_validation_warnings
        "executive_summary_llm",  # RealityCheckOutputContract.executive_summary_llm
        "executive_summary_validation_warnings",  # RealityCheckOutputContract (#236)
        # SynthesisOutputContract.prose_style_rejections (#1034). Since #1211 synthesis
        # writes it to the job's _logs; the name stays for reports written before, and
        # the assessment-data deliverable strips every field in this set.
        "prose_style_rejections",
    }
)

# Fields the style list does not apply to because they are not prose. Kept short:
# a model-written prose field is cleaned at its seam (synthesis_prose), never
# added here.
STYLE_SKIPPED_FIELDS = frozenset(
    {
        "key_condition",  # access pattern key condition: an expression (pk = :id AND sk BETWEEN ...)
        # query group label: a table-derived name, or for DynamoDB the model's
        # short pattern_group ("Post reads"); a label shown in the explorer, not a
        # sentence, so it is not held to the style list
        "group_name",
    }
)


@dataclass(frozen=True)
class StyleFinding:
    path: str
    kind: str
    term: str
    excerpt: str


def scan_json_style(
    obj: Any, path: str = "$", identifiers: frozenset[str] | set[str] = frozenset()
) -> list[StyleFinding]:
    """Every plain-prose style hit (``src.shared.prose_style``) in a parsed JSON
    document's string values, skipping the same id-holding fields as
    ``scan_json``, the ``STYLE_AUDIT_FIELDS`` and the ``STYLE_SKIPPED_FIELDS``.
    ``identifiers``: the collected table and column names (data, not words)."""
    from src.shared.prose_style import find_style_hits

    out: list[StyleFinding] = []

    def walk(node: Any, p: str, parent_key: str | None) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ID_FIELDS or (parent_key, key) in ID_FIELD_PATHS:
                    continue
                if key in STYLE_AUDIT_FIELDS or key in STYLE_SKIPPED_FIELDS:
                    continue
                walk(value, f"{p}.{key}", key)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{p}[{i}]", parent_key)
        elif isinstance(node, str):
            out.extend(
                StyleFinding(p, h.kind, h.term, h.excerpt)
                for h in find_style_hits(node, identifiers)
            )

    walk(obj, path, None)
    return out
