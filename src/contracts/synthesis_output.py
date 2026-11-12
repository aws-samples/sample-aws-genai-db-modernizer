# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Synthesis Output Contract

Data models for the synthesis agent output: the final modernization report
with ranking, architecture, table mappings, query groups, TCO, and risks.

Version History:
- 1.0: Initial synthesis output contract.
- 1.1 (2026-08-27): Added optional ``source`` provenance to ``AssignmentSummary``
  so a consumer can see which pipeline stage produced the consumed assignment
  version (ADR-028). Backward compatible — ``source`` defaults to ``None``.
- 1.2 (2026-10-04): Added optional ``cache_overlay`` (#296): the cache layer's
  queries and share of calls, which the owner distribution never counts, plus the
  notes of the post-schema safety net. A cache-layer ``ranking`` entry carries
  ``role: "cache_layer"``, ``cache_overlay_queries`` and
  ``cache_call_share_percent``. Backward compatible — defaults to ``None``.
- 1.3 (2026-10-04): Added optional ``routed_confidence`` to ``EngineRanking``
  (#152): the mean per-query fit of the queries the effective assignment routes
  to the engine (for the cache layer, of the reads it fronts; see
  ``routed_confidence_basis``), with ``routed_queries``, ``routed_tables`` (real
  source tables only), ``routed_confidence_evidence`` (``table`` / ``partial`` /
  ``signal_only``: whether a rated source table backs the fit),
  ``routed_queries_without_table_evidence``, ``routed_lead`` and
  ``routed_lead_count`` (the signal or cache pattern that leads the routed queries)
  and the engine's ``rationale``.
  ``analysis_confidence`` (= ``confidence_score``, the average over every analyzed
  table) and ``weight`` are declared as audit fields. The ranking is ordered by
  workload share. Backward compatible — every new field defaults to ``None``.
- 1.4 (2026-10-04): Added optional ``migration_waves`` (#225): the incremental
  roadmap synthesis computes deterministically from the assignment (one
  sequencing rule — cache, then key-value/point lookups, then search/analytics
  read models and document data, then whatever is retained on the
  source-compatible relational engine). Each wave carries its engines, the
  tables and queries it moves (and from/to which engine), its share of the
  workload or of calls, the ordering rationale and the gate before the next
  wave; the cache wave's fronted source engine (``fronts``, ``moves_from``
  always empty for the cache, the source engine rather than the end-state
  engine for every other wave); and a search/analytics read-model wave's
  per-table durable owner and sync pattern (``table_owners``).
  ``table_groups`` is typed ``list[TableGroup]`` (``tables``, ``query_count``
  as distinct DynamoDB-assigned query IDs, ``kind``: ``co_dependency`` or
  ``independent``), not untyped dicts. Also added optional
  ``unresolved_names`` (#316): a copy of the assignment's own
  ``unresolved_table_names`` -- the count and names of ``source_tables``
  entries that are not a table or view the collector saw (parser noise: a
  CTE alias, a system catalog, a sequence, a keyword, a column). Everything
  here is ``None``/empty for a report synthesized before this field existed;
  every deliverable falls back to deriving the same roadmap shape itself.
  Backward compatible.
- 1.5 (2026-10-05, revised after independent review of #321): Reordered the
  migration-waves sequence so the relational move comes first (#321): wave 1
  is now the whole source database moving to the source-compatible Aurora
  engine (schema carried over 1:1), skipped only when the source is already
  Aurora. Added optional ``homogeneity`` to ``MigrationWave`` (wave 1 only:
  ``"homogeneous"`` when the source maps to a compatible Aurora engine,
  ``"heterogeneous"`` when it does not). A heterogeneous wave 1 still runs
  -- it is not skipped -- it just names no Aurora engine (``engines`` is
  empty, the title says what the source moves *off* instead of *to*, and
  the gate flags it as a risk); only a missing source engine, or a source
  that is already Aurora, skips wave 1 outright. Added optional
  ``cutover_query_count`` to ``MigrationWave`` (wave 1, homogeneous only):
  every in-scope query, since the whole workload runs on Aurora the moment
  wave 1 finishes, before any later wave has moved its own share away;
  ``query_count``/``workload_share_percent`` stay the end-state share still
  on Aurora once every later wave has moved its own. ``moves_from`` and
  ``fronts`` now name the retained Aurora engine for every wave after wave 1
  (data has already reached Aurora by then), not the legacy source engine;
  wave 1 itself, and the cache wave whenever there is no retained engine to
  front instead (a heterogeneous source, or no source engine at all), still
  name the legacy source engine. ``table_count`` for wave 1 is the whole
  collected schema (tables and views); every later wave's is a subset of
  it, not an addition. DynamoDB and DocumentDB share one wave when both are
  routed (the product plan is "cache + DynamoDB or cache + DocumentDB", not
  a separate wave each), each keeping its own rationale and gate text
  inside the shared wave. Routing itself (owner distribution, shares, owned
  queries, the cache overlay) is unchanged -- only the wave order, fields
  and wording. Backward compatible: ``homogeneity``/``cutover_query_count``
  default to ``None`` for a wave written before they existed, and
  ``resolve_migration_waves``'s legacy fallback applies the same reorder.
- 1.6 (2026-10-06, #380): Added optional ``current_cost_known`` to
  ``TCOAnalysis`` (default ``True``): whether ``current_monthly_cost`` is a
  real baseline or 0.0 only because the collector reported no RDS instance
  metadata to estimate from -- a renderer shows "source cost not provided"
  instead of the bare figure when this is ``False``. Added optional
  ``eliminated_engine_costs`` (``list[CostBreakdown]``): engines the reality
  check eliminated, each with its own analysed cost before removal, so a
  saving a recommendation names for one is traceable here too, not only in
  prose. Backward compatible: both default to their pre-1.6 meaning
  (``current_cost_known`` true, ``eliminated_engine_costs`` absent) for a
  report written before this version.
- 1.7 (2026-10-07, #381 review): Added ``"cross_engine"`` to ``MigrationWave.
  homogeneity``'s ``Literal`` (independent review blocker: a heterogeneous
  source where the assignment resolver still picked one of the two competing
  Aurora engines wrote ``homogeneity="cross_engine"`` starting with #381, but
  the contract's ``Literal`` had not been updated to match, so synthesis
  failed ``SynthesisOutputContract`` validation for every such source).
  ``cutover_query_count``'s description corrected to say it is also set for a
  ``"cross_engine"`` wave 1, not ``"homogeneous"`` only (``migration_waves.py``
  always set it this way; only the description was wrong). Added optional
  ``aurora_engine_choice`` to ``AssignmentSummary`` (a copy of ``Assignment.
  aurora_engine_choice``, #381): the only way the resolver's heterogeneous
  engine choice reached ``report.json`` before this was buried inside wave 1's
  rationale text. Backward compatible: a pre-1.7 wave never had
  ``homogeneity="cross_engine"`` to begin with, and ``aurora_engine_choice``
  defaults to ``None``.
- 1.8 (2026-10-08, #424, #459 review): ``cache_overlay`` (``dict[str, Any]``,
  unstructured) now carries ``safety_net_notes``: the customer-facing subset
  of ``notes`` (which also carries customer-edit and legacy-migration notes,
  internal, kept for the engineering report only). Every other deliverable
  and the chat summary read ``safety_net_notes``. No model field added (the
  key is unstructured, like ``notes`` and ``dropped_query_ids`` before it);
  absent on a report with no cache overlay or no safety-net drop.
- 1.9 (2026-10-10, #478, independent review): ``query_groups``
  (``list[QueryGroup]``, each ``access_patterns`` entry already
  ``dict[str, Any]``) can now hold entries for an Aurora engine
  (``aurora_mysql``/``aurora_postgresql``). Aurora's schema-design contract
  has no ``access_patterns`` field (#157 adds real ones later), so without
  this an engine that can own half a typical workload never appeared in
  ``query_groups`` at all -- but a query is the atomic unit synthesis
  groups by, so an Aurora-routed query belongs here like any other, not in
  a separate field. Built from the assignment, not the schema design, so a
  group exists for Aurora's queries even when Aurora has no schema design
  yet (only the design-dependent fields -- DDL, table definitions,
  optimizations -- are unavailable then, not the groups).

  Each such entry's ``pattern_id`` is ``"relational-<engine>-<query_id>"``
  (the query's own id, not a truncated prefix of it -- a truncated id risks
  two different queries silently merging under a shared prefix on a
  non-cryptographic id scheme). It carries no ``description`` of its own;
  instead ``reason_index`` indexes into a new ``reasons: list[str]`` on its
  own ``QueryGroup`` -- a consolidation reason is written once per *event*,
  not per query, so the distinct text is a small fraction of the total (on
  one sample, 146 distinct reasons backed 1281 access patterns): storing it
  once per group and indexing into it, instead of copying the full string
  onto every access pattern, cut that report's size by roughly 5%.
  ``reason_text_for(group, access_pattern)`` (``synthesis_report.py``, and
  duplicated as ``_reason_text_for``/``reasonTextFor`` in
  ``src/report/engineering_report.py`` and ``src/ui/src/utils/auroraDesign.js`` --
  neither package imports the other, same as the two group-name constants
  below) resolves it, has the collector's ``"unknown"`` table placeholder
  replaced by a readable phrase (#483: some of those statements do have a
  real table the collector missed), and is capped at 180 characters on a
  word boundary with an ellipsis. ``table_name``/``key_condition`` are
  omitted (redundant with the group's own name, and always ``None``,
  respectively). A query with no real source table -- the collector's
  ``tables_accessed: ["unknown"]`` fallback, or only a parsed ``DUAL`` --
  groups under ``"No source table"`` (``"Table not identified by the collector"``
  in a report saved before #1536), or under
  ``"Utility and session statements"`` when it is a real utility/session
  statement (``utility_statements.is_utility_statement``, already routed to
  the relational engine by the assignment: SHOW/SET/EXPLAIN, catalog
  introspection), or under ``"Procedure-internal statements"`` when it is a SQL
  Server statement on temporary tables or table variables only
  (``utility_statements.is_procedure_internal_statement``, #1876) -- never a
  literal "unknown". Every consumer that walks
  ``query_groups`` generically (``access_pattern_scope``, the deterministic
  summary's query/group counts and "top query groups" sentence, the
  Bedrock executive-summary context, ``build_effective_architecture``'s
  per-engine ``top_query_groups``, the grounding fallback summary, the
  engineering report's generic "Query groups" table and its dedicated
  "Queries on Aurora by table" subsection) was updated so a relational
  engine's queries are counted as queries, not conflated with
  access-pattern counts, neither label is ever named as a busiest group,
  and a multi-table group name is shortened to its busiest table (by the
  calls/s each table's own groups carry, see ``_table_rps_weights``), not
  its alphabetically first one, before it is joined into a comma-separated
  list -- and two names that would otherwise shorten to the exact same
  table(s) grow until they no longer do. The synthesis LLM input's own
  compact form of each group (``prepare_synthesis_llm_input``) carries
  ``calls_per_second_by_engine`` (a dict), not one combined scalar --
  summing two engines' own figures (one engine's design RPS already
  includes peak/growth factors from its own analysis, another's is the
  collector's raw measured rate) invited ranking throughput across engines
  that are not on the same scale. Backward compatible: a report with no
  Aurora engine, or one still on an older synthesis build, has no such
  entries, same as today; an access pattern with no ``reason_index`` reads
  its reason from ``description`` directly, same as every non-relational
  engine's entry always has.
- 1.10 (2026-10-12, #530 review): Added optional ``source_table_count``:
  the collected schema's own tables-only total (no views). ``build_summary``'s
  "Analyzed N source tables ..." opening sentence already stated this number
  in prose, the only place it existed; a renderer that needed it without a
  completed schema design (``table_mappings`` stays empty then, #530) had to
  parse it back out of that sentence with a regex
  (``src.report.architecture._n_tables_mapped``'s ``_ANALYZED_SOURCE_TABLES``).
  It is now also this structured field, computed the same way
  (``synthesis_report.source_table_count``, which ``build_summary`` itself
  now calls) so the two can never disagree; the renderer reads this field
  first and only falls back to the regex, then to the migration-wave union,
  for a report synthesized before this field existed. Backward compatible --
  defaults to ``None``.
- 1.11 (2026-10-12, #503 review item 1): Added ``AssignmentSummary.
  tie_unresolved_count``: how many ``query_assignments`` carry a non-empty
  ``tied_engines`` -- an exact score tie no ADR-031 rule decided, listed
  under a deterministic placeholder engine pending the customer's own
  choice. Defaults to 0, so a report synthesized before this field existed
  reads as having no unresolved query ties.
- 1.12 (2026-10-12, #503): Added ``tied_queries``
  (``list[TiedQuery]``): the actual queries behind ``tie_unresolved_count``,
  each with its placeholder ``assigned_engine`` and its ``tied_engines``, so
  the decision report and the engineering report can list the same queries
  the approval gate already does from the assignment directly, instead of
  having nothing to render for the count alone. None for a report
  synthesized before this field existed.
- 1.13 (2026-10-12, #568 review of #562): Added optional ``table_counts_by_engine``
  and ``owned_counts_by_engine`` to ``MigrationWave``: for the merged DynamoDB/
  DocumentDB wave only, each engine's own served and durably-owned table counts.
  ``table_count`` on that wave is the union of both engines' served tables and
  cannot be split back into each engine's own share; a consumer that credited
  the whole union to every engine (``src.report.architecture._architecture_engines``,
  before this fix) double counted. Both default to ``None`` for a single-engine
  wave (derive the owned count there as ``table_count - len(table_owners or [])``)
  and for a wave written before this version. Backward compatible.
- 1.14 (2026-10-14, #629): Added ``post_schema_reroutes``
  (``list[PostSchemaReroute]``): the queries the post-schema router moved after
  the customer approved the assignment, copied from the final assignment, so
  the decision report, the engineering report and the deck can say which
  queries moved after approval, why, and how to override them. None for a
  report synthesized before this field existed.
- 1.15 (2026-10-14, #615, ADR-032): Added optional ``facts`` (``ReportFacts``):
  every customer-facing figure the deliverables print, computed once by
  ``src.shared.report_facts.compute_report_facts`` at synthesis -- each
  engine's role, workload, cached reads, source tables, tables that migrate
  and monthly cost; the totals (engines, tables migrate, workload, cost); the
  table basis behind the deck's "N tables mapped" bullet with its design
  coverage (#533) and its mapped tables per role (#258's "21 migrate, 1 to the
  cache layer"); each wave's served, moving and kept-current tables, moving
  from ``owned_counts_by_engine`` for the merged wave (#581), and wave 1's
  view count next to the tables-only ``source_table_count`` (#578); the open
  and resolved risk counts by severity, type and engine (and the one shared
  CRITICAL/HIGH root cause, if any); the tied-query
  count, the count of queries moved after approval (#629), and the
  migration-wave count. Renderers read these fields and only format them. Backward
  compatible -- ``None`` for a report synthesized before this field existed;
  ``src.report.facts.report_facts`` computes the same block for one.
- 1.16 (2026-10-14, #615 step 2, #584, #546): ``facts`` adds labels and
  reasons: each engine's ``signal_overrides`` (the triage signals that
  overrode queries onto it, from the assignment's ``signal_override`` fields,
  so the deck no longer parses reason text, #584); ``removed_engines`` (each
  engine the reality check removed that is not in the final architecture,
  with where its queries went and why -- an engine a customer edit moved
  queries back onto is never listed, #546); ``reality_check_trade_offs`` (each
  reality-check trade-off's engines and whether a later assignment edit
  ``reversed`` it, #546); ``trade_off_count`` (the trade-offs that still
  apply). Backward compatible: every new field defaults to empty.
- 1.17 (2026-10-14, #615 step 3, #595): ``facts.reality_check_decisions``:
  the reality-check trade-offs that still apply (consolidations, the sanity
  sweep's kept-engine verdicts, recommended patterns), each with its
  ``kind``, ``engine``, ``title`` and ``detail``, so the decision report, the
  deck and the UI show them the way the engineering report always did.
  Backward compatible: defaults to empty.
- 1.18 (2026-10-14, #762): Added optional ``summary_input_budget``
  (``SummaryInputBudget``): set only when the executive-summary request was
  over its token budget and its long lists were cut to fit (the same
  ``input_budget`` status the request itself carries, in both LLM modes):
  the budget, the estimated tokens before and after, the request's line count
  (as headless reads it), the per-list limit, and
  each cut list path with its full and kept item counts. None when nothing
  was cut, for ``--llm-mode none`` (no request is built), and for a report
  synthesized before this field existed. Backward compatible.
- 1.19 (2026-10-15, #339): Added optional ``query_traces``
  (``QueryTraceDigest``, ``src.contracts.query_trace``): the per-query
  decision traces from the move ledger, in full for the queries whose engine
  changed after their first assignment, that were tied at some step, were
  overridden or were pinned by a listed rule (at most
  ``report_facts.TRACE_FULL_CAP`` in full, the rest in ``more_listed``),
  each with its tables, an excerpt of its SQL, its own step numbers and its
  group mean, the rest counted by their first assignment's rule; and
  ``facts.query_trace`` (``QueryTraceFacts``), their figures. A group-mean
  runner-up's scores are on one scale, rounded to one decimal. None for a
  report synthesized before these fields existed. Backward compatible.
- 1.20 (2026-10-15, #787): The trace reason codes gain
  ``fallback.no_group_owner`` (a co-dependency group no analyzed engine can
  serve as a whole), from the move ledger 1.5 enum. Backward compatible.
- 1.21 (2026-10-15, #859): ``EngineRanking.routed_confidence_evidence`` gains
  ``pattern``: every query routed to a pattern-only engine (OpenSearch) matches
  one of that engine's detected patterns, which ``routed_confidence`` has
  produced since #475 (synthesis failed validation on it). Backward compatible.
- 1.22 (2026-10-15, #861): ``facts.reality_check_trade_offs`` entries gain
  ``queries_moved_back``, ``queries_still_moved`` and ``description_now``: a
  consolidation line is reversed per query (every query it moved is back on
  the engine it left), and a line an edit undid only in part is restated for
  what stayed moved. Backward compatible -- every new field has a default.
- 1.23 (2026-10-15, #730): ``query_traces.close_calls`` (``CloseCall``): every
  query whose runner-up -- a kept engine that can take it -- is less than
  ``report_facts.CLOSE_CALL_MARGIN`` points behind its final engine, with
  both scores, the gap, its basis and whether it is an exact tie a rule
  settled; and ``facts.close_calls`` (``CloseCallFacts``), the same queries
  by engine and alternative, the settled ties counted apart, so every
  deliverable says which queries went to an engine on a small margin and
  where the customer can move them. Backward compatible: ``close_calls``
  defaults to empty, ``facts.close_calls`` to None.
- 1.24 (2026-10-15, #729): ``query_traces.unservable`` (``UnservableQuery``):
  every query that landed on a fallback engine because no analyzed engine fit -- its
  engine decided by a fallback rule, or every candidate engine gated -- with
  the gate that ruled out each engine and the capabilities it needs; and
  ``facts.unservable`` (``UnservableFacts``), the same queries with a
  redesign hint each, so every deliverable flags them and points at the
  assignment review to re-run. Backward compatible: ``unservable`` defaults
  to empty, ``facts.unservable`` to None.
- 1.25 (2026-10-16, #949): Added optional ``TCOAnalysis.retired_engine_savings``
  (``list[CostBreakdown]``): engines the source runs today (the source engine, or
  one the collection records in ``metadata.engines_in_use``) that the plan
  retires, each with its analysed monthly cost -- the only retirement saving a
  deliverable may state. ``eliminated_engine_costs`` is no longer written: it
  priced every candidate engine the plan analysed and dropped as if the customer
  paid for it today. Added optional ``considered_not_recommended``
  (``list[ConsideredEngine]``): those dropped candidates, with the engine that
  absorbed their queries and no dollar figure. Backward compatible: both default
  to None; a report before 1.25 still loads, and its ``eliminated_engine_costs``
  rows read back as ``considered_not_recommended``, never as a saving
  (``src.shared.engines_in_use``).
- 1.26 (2026-10-17, #792, ADR-032): ``facts.access_pattern_totals``
  (``AccessPatternFacts``): the access-pattern figures the results page shows,
  computed once from ``query_groups`` -- the patterns the owner engines serve,
  the cache layer's own patterns apart (they never count as owned workload)
  and the two together (the explorer's header total). The explorer's live
  filter counts stay presentation. Backward
  compatible: ``None`` for a report with no ``query_groups`` and for a facts
  block written before 1.26 (the results API recomputes it from ``query_groups``).
- 1.27 (2026-10-17, #1028): ``migration_waves[].gate_parts`` (``GatePart``): the
  wave's ``gate`` as text runs, each followed by an optional item list (table
  names), so the results page collapses each list to an inline "(…)" without
  parsing the gate's prose. Joined back (``join_gate_parts``: each text, then
  " (a, b, c)" when it has items) the parts equal ``gate``; ``MigrationWave``
  rejects parts that do not. ``gate`` itself is unchanged, and the Markdown and
  PDF reports keep printing it. Backward compatible: ``None`` for a wave written
  before 1.27, which shows its gate string as it is.
- 1.28 (2026-10-17, #1020, ADR-032): ``facts.in_scope_query_count`` and
  ``facts.moving_query_count``: the in-scope queries the owner engines serve and
  how many of them move to a migration target or a search read model, the
  executive report's "Queries moved" total; and ``facts.resolved_risks_by_severity``,
  the risks the assignment resolved by severity, as ``open_risks_by_severity``
  counts the open ones. Backward compatible: the counts default to 0 and the map to
  empty, and a facts block written before 1.28 gains them from ``ranking`` and
  ``risk_assessment.resolved_risks`` when read (``src.report.facts.report_facts``
  and the results API).
- 1.31 (2026-10-18, #746): ``facts.unreviewed_move_count``: the reality-check
  moves no model reviewed (the moved queries of each consolidation review
  batch that got no verdict), from ``reality_check.validation_incomplete``,
  which synthesis now copies from the reality-check output. Every deliverable
  states it when it is not zero. Backward compatible: 0 for a report written
  before 1.31.
- 1.32 (2026-10-18, #992, ADR-031 Amendment 9): Added ``table_sources``
  (``TableSource``): each written table's source of truth (the one engine its
  writes land on), its write queries and its replicas (the other engines serving
  its reads, with those reads), from the assignment's ``write_groups``; and
  ``facts.table_sources`` (``TableSourceFacts``): per replicated table its source
  of truth, replica engines (CDC runs from the source of truth to them) and the
  eventual-consistency risk naming the replica-served reads; and
  ``facts.source_of_truth_reroute_count`` / ``design_review_reroute_count``: the
  queries moved after approval split by cause (a write re-routed to one source of
  truth, or a design review's flag). Backward compatible: the new fields default to
  None or 0 for a report synthesized before 1.32.
- 1.33 (2026-10-18, #1003, ADR-031 Decision 12 Amendment 10): Added
  ``pending_engine_choices`` (``PendingEngineChoice``): the open ties grouped per
  table, each with its queries, every tied engine's whole-point and unrounded
  score, why they tie, each engine's trade-off and the engine ``--auto`` would
  pick; and ``auto_decided_choices`` (``AutoDecidedChoice``): the choices
  ``--auto`` decided for the customer, still holding on the effective assignment;
  and ``facts.pending_choice_count``, ``pending_choice_query_count``,
  ``pending_choice_pairs``, ``auto_decided_choice_count``, ``auto_decided_query_count``
  and ``auto_decided_by_engine`` (``PendingChoicePair``, ``AutoDecidedEngine``). Backward compatible: None,
  0 or empty for a report synthesized before 1.33.
- 1.35 (2026-10-18, #471, ADR-032): ``facts.relational_gaps``
  (``RelationalGapFacts``): the relational move's version and feature gaps against
  wave 1's Aurora engine, each with its impact (blocks the move, needs a rewrite,
  behaves differently, informational), reason, documentation and evidence (tables,
  queries, objects), from the rules in ``src/catalog/gaps/``. Computed once at
  synthesis from the collection; no deliverable renders it yet. Backward compatible:
  None for a report written before 1.35.
- 1.37 (2026-10-18, #1034): ``prose_style_rejections``: the model-written
  schema-design fields synthesis sent back to deterministic text because they
  used an item of the plain-prose style list (``src.shared.prose_style``), one
  line each, for the audit trail; no deliverable renders it. Backward
  compatible: empty for a report written before 1.37.
- 1.38 (2026-10-19, #888): ``flagged_unservable`` (``FlaggedUnservableQuery``):
  the flagged queries no kept engine can take, which never reach the move
  ledger -- a reality-check model flag whose outcome is ``kept`` and a
  post-schema router terminal query -- each with its engine, the reason
  category, the review that flagged it and why it stays. ``facts.unservable``
  states them as a third kind, ``flagged`` (``flagged_query_count``), next to
  ``fallback`` and ``gated_everywhere`` (together ``no_fit_query_count``), with
  the reason category's redesign hint. Backward compatible:
  ``flagged_unservable`` defaults to empty, ``flagged_query_count`` and
  ``no_fit_query_count`` to 0.
- 1.39 (2026-10-19, #1033): ``FlaggedUnservableQuery.cause`` gains ``held_by_pin``:
  a query the post-schema router kept on its flagged engine because a customer pin
  holds its table's write group there (#992). ``facts.unservable`` states it as a
  fourth kind, ``held`` (``UnservableFacts.held_query_count``), with its own
  sentence in every deliverable. Backward compatible: ``held_query_count``
  defaults to 0.
- 1.40 (2026-10-18, #950, ADR-032): ``Risk.rule`` and ``Risk.wave``: the
  migration risks synthesis now raises from the facts it already computes
  (``src.agents.referee.migration_risks``: one sync risk and one close-call risk
  per wave, open ties, queries no engine fits, dropped cache reads, the cache
  layer's unsupported patterns, logic moved to the application, hot partitions
  and one review risk per engine for its design trade-offs) carry the rule that
  raised them (a key of ``migration_risks.RULES``) and the migration wave whose
  gate they must clear; ``Risk.details`` lists the design texts a risk covers in
  full. ``mitigation_strategies`` gains one line per wave naming
  those risks. Backward compatible: all three default to ``None``, the value every
  risk written before 1.40 (and every analysis risk) reads back as.
- 1.41 (2026-10-19, #1108): ``facts.query_trace.trace_groups``
  (``QueryTraceGroup``) and ``trace_group_count``: the listed queries with a
  full trace, grouped by what their trace says (same kinds, path, steps,
  scores, final engine and runner-up), each group with its query ids, 8-character
  short ids (unique among every query id in the report), count and open-tie
  count, so the engineering report's appendix states a story once and names
  the queries that share it, and its headings still add up to "Unresolved
  engine ties". ``listed_queries`` still lists every query. Backward
  compatible: both default to empty / 0, and a stored block without groups
  gets them from ``src.report.facts.report_facts``.
- 1.42 (2026-10-19, #1169): ``EngineFacts.kept_in_sync_tables`` and
  ``moves_to_tables`` split a migration target's served tables it does not own the
  way the wave plan does (owner outside the wave: kept current by CDC; owner in the same
  wave: moves to that owner). ``TableBasis.mapped_tables_by_move`` is the Migration
  map heading's split (migrate, kept current by CDC, cache layer, search read model, stays),
  with ``migrating_tables_not_mapped`` (and ``_names``), the moving tables the map has
  no row for, and ``mapped_tables_not_served``, the rows mapped to a migration target
  that no query routed there reads. Backward compatible: they default to None, empty
  and 0, and the facts reader fills them in for an older block.
- 1.43 (2026-10-19, #1148, ADR-032): ``Risk.rule`` gains ``routing.flagged``: one
  risk per engine for the flagged queries no other kept engine takes
  (``flagged_unservable``, the ``flagged`` and ``held`` kinds ``facts.unservable``
  counts), HIGH, or MEDIUM when a customer pin holds every one of them; raised by
  ``src.shared.migration_risk_rules`` from report.json's own fields, so the
  deliverables and the results payload raise it for a report written before 1.43
  (``backfill_report_risks``). ``FlaggedUnservableQuery.source_tables``: the tables
  the query reads, the risk's evidence. Backward compatible: ``source_tables``
  defaults to empty; an older report's risk takes its tables from its query groups.
- 1.44 (2026-10-19, #1149, ADR-032): ``Risk.rule`` gains ``relational.gaps``: one
  Wave 1 risk per impact of the relational move's gaps (``facts.relational_gaps``,
  1.35), HIGH for a gap that blocks the move, MEDIUM for one that needs a rewrite,
  LOW for one that behaves differently, none for an informational one, and only
  when the gap check's target engine is wave 1's engine; each gap's reason,
  documentation and objects are in ``Risk.details``. Raised by
  ``src.shared.migration_risk_rules`` from the facts block, so the deliverables and
  the results payload raise it for a report written before 1.44. No field changes.
- 1.45 (2026-10-20, #1076): engines_in_use and facts.retired_in_use_engines
  (RetiredInUseEngine): an engine the source runs today that the plan retires is stated
  once, with its counted saving or why none, where its queries went and the
  consolidation reason
- 1.46 (2026-10-20, #1211): `prose_style_rejections` is no longer written to
  `report.json`: its lines quote model text and name raw engine ids, so synthesis writes
  them to `_logs/prose_style_rejections-v<N>.json`. The field stays on the contract,
  empty by default, so an older report that has lines still loads with them.
- 1.47 (2026-10-20, #377): No field of its own. The query traces' rule values gain
  ``gate.key_scoped_count`` (move ledger): a key-scoped count placed on the engine its
  table's writes are on (ADR-031 Decision 2 Amendment 12).
- 1.48 (2026-10-20, #1120): A cross-engine wave 1 (SQL Server, Oracle or Db2 converted
  to the chosen Aurora engine) moves every source table, counted in full and once:
  ``facts.waves[].moving_tables`` / ``moving_by_engine`` are set for it, the chosen
  engine's ``engine_source_tables`` and ``tables_migrate`` are the source tables, and
  ``facts.tables_migrate`` is that count (every later wave moves a subset of them on).
  Added ``WaveFacts.heterogeneous_note``: the plain statement that wave 1 is not yet
  optimized for heterogeneous migrations, with the relational gap counts
  (``gaps_by_impact``), and ``WaveFacts.lists_relational_gaps``: the gap check targets
  wave 1's engine, so every deliverable lists ``facts.relational_gaps`` under wave 1.
  Backward compatible: None / False for every other wave; a stored facts block without
  them gets them, and the wave 1 counts, from the facts reader
  (``src.report.facts.report_facts``, ADR-032 Decision 4).
- 1.49 (2026-10-20, #1146): Added optional ``query_pattern_groups``
  (``QueryPatternGroup``: the in-scope queries grouped into distinct query patterns,
  each query kept with its id, frequency and engine) and ``facts.query_pattern_counts``
  (``QueryPatternFacts``: the 'N queries in M distinct query patterns' figures every
  deliverable prints, counted over the owner engines' in-scope queries). Routing never
  reads them. Backward compatible: both None for an older report, whose deliverables
  leave the line out.
- 1.50 (2026-10-20, #1232): facts.table_sources.replicated_tables[].shares_replica_risk:
  whether a row's stored risk is the standard sentence the deliverables state once
  (#1232); filled in for older reports
- 1.51 (2026-10-21, #1284): facts.waves[].moved_by_later_waves: how many of wave 1's
  tables and views a later wave moves off its Aurora engine, the figure wave 1's
  rationale now states instead of calling every mapped table a later-wave move.
- 1.52 (2026-10-22, #1327): Added optional ``RelationalGapFacts.feature_rules_checked``:
  the applicable rules that look at what the source uses, 0 for a conversion the gap
  catalog has no feature rules for; wave 1's note then says its feature gaps were not
  checked. None in a report written before.
- 1.53 (2026-10-22, #1328): Added optional ``facts.engine_scope``: the customer's engine
  scope (the target_databases setting triage recorded), in the order the deliverables
  name it; every deliverable states "Engines in scope: ... (customer's choice)". None
  for a job with no scope and for a report written before.
- 1.54 (2026-10-22, #212): Added optional TableMapping owner, owner_basis, also_read_by
  and cached_by, and facts.table_owner_lines (TableOwnershipFacts): each mapped table's
  owner (the engine that takes its writes; a table no query writes, the engine serving
  most of its queries; never a cache-only engine), the engines that also read it through
  CDC replicas and its cache, with their counts. The Migration map split counts each row
  under its owner. Filled in for older reports.
- 1.55 (2026-10-23, #1329): Added ``scope_unservable`` (``ScopeUnservableQuery``): the
  queries no engine in the customer's engine scope can serve, from the effective
  assignment. ``facts.unservable`` states them as a fifth kind, ``no_engine_in_scope``
  (``UnservableFacts.no_engine_in_scope_query_count``,
  ``UnservableQueryFact.closest_engine``, an empty ``engine``), and the risk rule
  ``routing.no_engine_in_scope`` raises one blocking risk for them. Defaults to empty
  and 0, so a report written before still loads.
- 1.56 (2026-10-23, #1195, #1196): ``tco_analysis.cost_breakdown[].sizing``
  (``CostSizing``) and ``.price_list``: each kept engine is priced for the in-scope
  queries the effective assignment gives it and the tables they touch (the cache layer
  for the reads it fronts), and the row states that basis and the price table it was
  priced from; ``facts.engines[].cost_sizing`` states it in every deliverable (basis
  ``assigned_queries``, ``cache_overlay``, or ``no_in_scope_queries`` at $0), and
  ``facts.cost_assumptions`` states the TCO's assumption lines, including that the owner
  engines are billed for the reads the cache fronts as misses. Backward compatible: None
  for an older report, whose figures were the analysis estimates. ``facts.price_list``
  (``PriceListFacts``): the source, region, pricing model and retrieval date of the
  price table (``src/catalog/prices.toml``) the TCO figures were estimated from, read
  from each analysis estimate (``engine_dates``; ``retrieved_on`` when they agree), so
  each deliverable states the price date. Backward compatible: defaults to None, the
  value an older report reads back as (its price date is unknown).
- 1.57 (2026-10-23, #1369): MigrationWave.homogeneity description: a wave 1 with no
  Aurora engine also covers a customer engine scope with no relational engine
- 1.58 (2026-10-23, #1361): Added optional query_traces[].key_candidates
  (TraceKeyCandidate, src.contracts.query_trace): each column the redesign considered as
  a key for the query, accepted or refused with every guard that refused it; a report
  written before has none
- 1.59 (2026-10-24, #1373): Added optional ``routed_pinned_queries`` on a ranking entry
  and ``pinned`` on a query trace (#1373): pins are left out of the mean fit and shown
  as 'pinned (utility)'.
- 1.60 (2026-10-24, #1403): Added optional ``facts.executive_brief``
  (``ExecutiveBriefFacts``): the source engine, whether a current cost was collected,
  what each wave does, the overall risk level, the one or two open risks driving it, the
  wave to approve, the whole-dollar figures the summary states (projected cost, source
  cost, retired savings) and each engine's share of all queries when some have no engine
  in the scope, so the executive summary leads with the recommendation, the risk and the
  decision from facts. Backward compatible: None for an older report.
- 1.61 (2026-10-24, #1252): Added optional facts.cache_layer (CacheLayerFacts): the
  cache layer's cache candidates, designed cached reads and the candidates that stay on
  their owner engine
- 1.62 (2026-10-24, #1383): Added MigrationWave.no_move_reason and WaveFacts.no_move: a
  wave 1 that moves nothing reads as no step
- 1.63 (2026-10-24, #1410): facts.query_group_counts: the engineering report's query
  group counts (#1410)
- 1.64 (2026-10-24, #1378): ``tco_analysis.unqueried_tables`` (``UnqueriedTables``:
  table count, storage, ``stored_on``) and ``CostSizing.unqueried_table_count``: the
  source tables no in-scope query reads are stored on wave 1's Aurora engine, priced
  with its own tables, and a TCO assumption line names the engine (or says no engine
  stores them). Defaults to None and 0, so a report written before still loads.
- 1.65 (2026-10-24, #1364): Added optional carried_writes
  (analysis_output.CarriedWrite): the write-group members of the effective assignment
  their engine serves poorly, with their intent and the target-native approach (#1362),
  for the detailed engineering report; None in a report written before
- 1.66 (2026-10-24, #1474): Added optional facts.cost_scope_label and facts.cost_caveat:
  the full-modernization label and the one caveat every deliverable prints where costs
  appear; None for a report written before
- 1.67 (2026-10-24, #1197): Added optional ``CostBreakdown`` fields stating the read I/O
  basis of an estimate that bills cache misses only (DocumentDB):
  ``read_io_hit_ratio_basis``, ``collection_hit_ratio_pct``,
  ``collection_hit_ratio_billed_pct``, ``source_memory_gb``, ``target_memory_gb`` and
  ``min_billed_miss_pct``; and ``ReportFacts.cost_basis_notes`` (that basis as one
  sentence per engine, printed next to the cost). Backward compatible: a report without
  them states no basis.
- 1.68 (2026-10-25, #226): facts.query_group_counts.ungrouped_queries: per owner engine,
  the in-scope queries no group of its own serves; query_groups[].cache_layer and
  source_queries[].cached_read: the cache layer's patterns make their own groups, whose
  queries are cached reads, so the owners' groups are a partition of the queries they
  serve
- 1.69 (2026-10-25, #1527): facts.derived_copy_sync: the DynamoDB derived copies' stream
  sync, listed and priced; None in an older report
- 1.70 (2026-10-27, #1398): MigrationWave.homogeneity gains the value no_move for a wave
  1 that moves nothing (it stored heterogeneous); BriefWave.kind gains no_move
- 1.71 (2026-10-28, #1636): Added optional facts.row_counts (RowCountFacts, #1636): the
  collection's row counts by basis (exact, estimate, unknown), the range of statistics
  refresh times, and the one sentence every deliverable prints for the sizing assumption
  that stands in for an unknown count. Backward compatible: None in an older report.
- 1.72 (2026-11-03, #802): Added optional ``facts.dynamodb_merge_conflicts``
  (DynamoDBMergeConflictFacts, #802): the DynamoDB group merge's unresolved conflicts,
  each with the access patterns and queries the merged design does not serve, and their
  distinct totals. Backward compatible: None in an older report.
- 1.73 (2026-11-03, #1780): Added optional ``facts.dynamodb_merge_reviews``
  (DynamoDBMergeReviewFacts, #1780): the DynamoDB merge-review decisions, each source
  table's authoritative home and derived copies with its design-conflict risk, their
  totals and the sentences every deliverable states. Backward compatible: None in an
  older report.
- 1.74 (2026-11-04, #1221): Added optional ``trade_offs[].topic`` and the pipeline-only
  ``trade_offs[].note_kind`` (``review``, ``warning``), and ``facts.trade_off_topics``:
  the design trade-offs grouped by engine and topic, then the ``warning`` and
  ``review_note`` groups, each with its label, ``trade_off_count`` and positions, so the
  engineering report and the results page list repeated trade-offs together.
- 1.75 (2026-11-04, #1078): Added the ``extension`` value to ``GapObject.kind``: a
  relational-move gap can name an installed extension as its evidence; and optional
  ``GapObject.note`` (the Aurora PostgreSQL version an extension is offered from).
- 1.76 (2026-11-05, #1876): Added optional
  ``QueryGroupFacts.procedure_internal_queries``: how many queries the
  procedure-internal group holds (SQL Server statements on temporary tables or table
  variables, pinned by the utility pin).
- 1.77 (2026-11-06, #1893): Added optional facts.rate_windows (RateWindowFacts): how
  many collected statements' call rates used each window basis (observed, uptime,
  stats_reset, assumed_24h) and the one sentence every deliverable prints about them.
  Backward compatible: defaults to None.
- 1.78 (2026-11-06, #1908): Added ``RateWindowFacts.sampled_count``,
  ``RateWindowFacts.sample_no_calls_count`` and ``RateWindowFacts.sample_fallback_count``
  (default 0): the rates over two snapshots of the counters, the sampled statements with
  no calls between them, and those that kept a single-snapshot rate. Backward
  compatible: all default to 0.
- 1.79 (2026-11-06, #1891): Added optional
  ``QueryGroupFacts.procedure_internal_routine_queries``: how many queries the group of
  procedure-internal statements that follow their routine holds (``"Procedure-internal
  statements, following their routine"``, a new blame group: SQL Server statements on
  temporary objects only, each on the engine its routine's other statements are on,
  #1891).
- 1.80 (2026-11-07, #1654): Added SynthesisOutputContract.routine_units
  (RoutineUnitFact: merged_by, query_ids, tables, engine, engines), each placement unit
  a procedure, function or trigger merged, and the routine value of TableGroup.kind, a
  wave table group from a routine unit (#1654). Backward compatible: routine_units
  defaults to an empty list.
- 1.81 (2026-11-08, #399): Added optional ``facts.cache_layer.role_sentence``: the cache
  layer stated as the assessment's recommendation, with its reason, that every
  deliverable prints (#399).
- 1.82 (2026-11-08, #1989): Added optional facts.hot_partitions (HotPartitionFacts):
  each DynamoDB design entry above the at-risk share of one partition key value's limit,
  with its total as an upper bound (never capped) and its load per key value estimated
  from the collection's distinct values, or not measured; and
  facts.busiest_partition_key, the busiest estimated key value over every entry.
  Backward compatible: defaults to None.
- 1.83 (2026-11-08, #1990): Added optional facts.assumptions (AssumptionsFacts: growth,
  peak source, rate window, region and prices, each with its origin, and the lines every
  deliverable prints) and facts.growth_scenarios (GrowthScenarioFacts: the cost and the
  busiest partition key value at 1x, 5x and 10x growth). Backward compatible: both
  default to None.
- 1.84 (2026-11-08, #1991): Added optional facts.rate_windows fields (shortest, longest
  and typical window seconds, window_start, window_end, window_clock,
  server_uptime_seconds, server_rate_per_second, measured_rate_per_second,
  representative, flags, window_statement, check_statement, scenario_label,
  scenario_risk_note): the rate window's length and dates and its representativeness
  check, and the scenario label and risk note a non-representative window puts on the
  costs and the rate-driven risks. Backward
  compatible: every new field defaults to None or empty.
- 1.85 (2026-11-08, #1993): Added optional HotPartitionFacts.per_key_measure (measured,
  lower_bound, upper_bound, not_measured) and per_key_upper_bound (PerKeyUpperBound):
  what each per-key figure is, and the upper bound set aside when it was over the limit,
  the evidence its per-key load is not measured. A report written before still loads.
- 1.86 (2026-11-08, #1991): facts.assumptions items and facts.growth_scenarios take
  origin flag (--growth) or file (the assumptions file); customer stays for other
  writers and older reports
- 1.87 (2026-11-09, #1991): facts.assumptions items and facts.growth_scenarios take
  origin ui (the results page's assumptions panel); a report written before still loads
- 1.88 (2026-11-09, #1992): Added optional facts.waves[] readiness fields (readiness:
  suggested, held, not_designed or not_reviewed; suggested; holds as WaveHold, with
  flagged_by for a re-attributed flag and the unsupported_queries check; next_steps as
  WaveNextStep; readiness_line; redesign_rounds and redesign_note),
  facts.wave_readiness_notes (WaveReadinessNote), ExecutiveBriefFacts.held_waves,
  BriefWave.suggested and readiness, and SynthesisOutputContract.wave_redesign
  (WaveRedesignLog: the assignment version's bounded redesign rounds, each with its
  failed checks, the engines' scores before it, the parts it redesigned and its outcome:
  fixed, improved, did_not_improve, failed or stopped), and ReviewWarning with
  WaveFacts.review_warnings and review_warning_count: a suggested wave's design review warnings, which hold
  nothing and which approving the wave accepts. decision_wave is now the first
  suggested wave. Backward compatible: every new field defaults to None, 0 or empty.
- 1.89 (2026-11-09, #2075): Added optional facts.waves[].readiness_line_short:
  readiness_line as the executive report prints it, with design review warnings as a
  count that points to the engineering report and a failed redesign round without its
  reason, so it names no collected table or column.
- 1.90 (2026-11-10, #2067): Added optional
  SynthesisOutputContract.wave_warning_decisions (WaveWarningDecision with
  DecidedWarning: the /wave-edit decisions on a version's design review warnings, each
  warning by its number and its own text); facts.waves[] accepted_warnings,
  accepted_warning_count, rejected_warnings, rejected_warning_count and
  open_warning_count; WaveRedesignCheck.warning; WaveRedesignRound.wave_edit,
  decision_version and reviewed_before, and its not_reviewed outcome. Backward
  compatible: every new field defaults to None, 0 or empty.
- 1.91 (2026-11-10, #1938): Added optional facts.routines (RoutineFacts with
  RoutineFactRow, RoutineRefFact and RoutineRuleFact): the routine-migration headline
  counts (carried as they are, rewritten, repaired, need repair, rebuilt around a
  literal, not checked, no code collected, not readable, with checks that could not
  run), the customer's to-do and its next step with their wave, the routines behind the
  routine risks with their class, severity and what set it, and the rules they name; ids,
  kinds, rule ids and reason codes only. Added facts.waves[].to_dos: the to-dos a wave's
  lines end with, which hold nothing, and Risk.to_do: the to-do a wave's gate lists
  instead of a condition. Backward compatible: defaults to None or empty.
- 1.92 (2026-11-10, #1939): Added optional facts.routines.others_line, to_do_routines
  and check_notes (RoutineNoteFact), and facts.routines.routines[].status_text, why and
  listed: what every deliverable prints for the routines (each listed row's status and
  why in plain words, the routines to migrate yourself, each rewrite and check that
  could not run once with its routines, the routines with no row). Backward compatible: default to
  None, False or empty.
- 1.93 (2026-11-10, #2081): WaveRedesignRound.reviewed_before marked legacy: no longer
  written or read; a redesign is kept only once its own review ran
- 1.94 (2026-11-10, #2107): Added optional facts.cutover_responsibility: the one
  sentence every wave plan says about the cutover
- 1.95 (2026-11-11, #2119): Added optional WaveRedesignCheck.parts: an open_review check
  on a DynamoDB design in parts names the parts whose last review held the design, with
  their statements in query_ids, so a redesign round redesigns only those parts.
- 1.96 (2026-11-11, #1130): Added optional facts.slice_plan (SlicePlanFacts: the
  relational step and the slice or cache steps with ordered stages a to d, move units
  that never split a write group, co-dependency group or routine unit and keep one
  writer per table, wave 1's table classes, the steps rolled up by wave (wave_phases),
  the dependency graph's edges and metrics, and the per-engine cost and risks the paths
  sum; #1129, #1698, ADR-033's slice_plan keyed by step_number) and facts.adoption
  (AdoptionFacts: the adoption paths with their replicated tables, those not offered and
  the one-step form of the full target; #1130, #473). Synthesis writes both empty until
  their computation lands, and the reader fills both for an older report. Backward
  compatible: both default to None.
- 1.97 (2026-11-12, #2165): Added optional
  schema_designs[engine].shared_sentence_groups: each group of sibling
  unsupported_patterns, served_by_waiver, migration_notes or app_layer_notes entries of
  one kind that share a long sentence, as {list, sentences, indexes}; the entries stay
  as the design wrote them and every renderer states the sentences once, then each
  entry. Backward compatible: a summary without it has no group.
"""

import re
from datetime import datetime
from typing import Any, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from src.contracts.assignment_models import (  # re-exported, see docstring below
    AuroraEngineChoice,
    AutoDecidedChoice,
    PendingEngineChoice,
    PostSchemaReroute,
    UnresolvedNames,
    UnservableQueryAssignment,
)

from . import schema_design_output
from .analysis_output import CarriedWrite
from .query_trace import QueryTraceDigest, QueryTraceListing
from .schema_design_output import TradeOff


class EngineRanking(BaseModel):
    """Ranking entry for a target engine."""

    target: str = Field(..., description="Engine name")
    confidence_score: int = Field(..., ge=0, le=100, description="Overall confidence")
    pattern_score: int | None = Field(None, ge=0, le=100)
    complexity_score: int | None = Field(None, ge=0, le=100)
    performance_score: int | None = Field(None, ge=0, le=100)
    cost_score: int | None = Field(None, ge=0, le=100)
    migration_complexity_avg: str | None = Field(None, description="LOW, MEDIUM, HIGH")
    assigned_queries: int | None = Field(None, ge=0)
    workload_percent: float | None = Field(None, ge=0, le=100)
    analysis_confidence: int | None = Field(
        None,
        ge=0,
        le=100,
        description="Audit: suitability averaged over every analyzed table (= confidence_score)",
    )
    routed_confidence: int | None = Field(
        None,
        ge=0,
        le=100,
        description=(
            "Mean per-query fit of the queries routed to the engine (cache layer: of the "
            "reads it fronts). None without an assignment or with no routed query (#152)"
        ),
    )
    routed_confidence_basis: Literal["owned_queries", "cached_reads"] | None = Field(None)
    routed_queries: int | None = Field(None, ge=0)
    routed_tables: int | None = Field(None, ge=0, description="Real source tables only")
    routed_confidence_evidence: Literal["table", "pattern", "partial", "signal_only"] | None = (
        Field(
            None,
            description=(
                "Whether a source table the engine's analysis rated backs the fit (for a "
                "pattern-only engine such as OpenSearch, whether the query matches one of "
                "its detected patterns: pattern); signal_only means the fit is the basic "
                "baseline plus the signal bonus"
            ),
        )
    )
    routed_queries_without_table_evidence: int | None = Field(None, ge=0)
    routed_pinned_queries: int | None = Field(
        None,
        ge=0,
        description=(
            "Routed queries pinned to the engine rather than placed by their score (a "
            "utility pin, QueryAssignment.pinned): left out of routed_confidence and "
            "routed_queries, and counted here (#1373)"
        ),
    )
    routed_lead: str | None = Field(
        None,
        description=(
            "Owners: the triage signal that is a plurality of the routed queries; cache "
            "layer: the most common cache_pattern. None without a clear plurality"
        ),
    )
    routed_lead_count: int | None = Field(None, ge=0)
    rationale: str | None = Field(
        None, description="Why the engine is in the target, from its routed workload (#152)"
    )
    weight: float | None = Field(
        None, description="Audit: the analysis weight that ordered the ranking before #152"
    )

    model_config = ConfigDict(extra="allow")


class TableMapping(BaseModel):
    """Mapping of a source table to its target engine.

    ``recommended_database`` is the schema design that maps the table with the highest
    fit, a summary label. The owner line (#212, ADR-031 Amendment 9) says who holds
    the table: ``owner``, the engine that takes its writes (never a cache-only engine),
    ``also_read_by``, the other engines whose queries read it through CDC replicas, and
    ``cached_by``, the cache in front of its reads
    (``src.shared.report_facts.table_owner_line``). None in a report written before.
    """

    source_table: str = Field(..., description="Source table ID")
    recommended_database: str = Field(..., description="Primary target engine")
    confidence_score: int = Field(..., ge=0, le=100)
    alternatives: list[dict] | None = Field(None)
    owner: str | None = Field(
        None,
        description=(
            "The table's owner as the wave plan states it: the engine that takes its writes "
            "(one writer per table); a table no query writes, or one whose writes are split: "
            "the owner engine serving most of its queries. Never a cache or search engine"
        ),
    )
    owner_basis: Literal["writes", "split_writes", "most_queries", "retained", "design"] | None = (
        Field(None, description="Which step of the owner rule decided the owner")
    )
    also_read_by: list[str] | None = Field(
        None,
        description=(
            "Every other engine with an in-scope query on the table, which reads a copy kept "
            "current from the owner via CDC; most queries first"
        ),
    )
    cached_by: str | None = Field(
        None, description="The cache engine fronting the table's reads, or keying it"
    )

    model_config = ConfigDict(extra="allow")


class QueryGroup(BaseModel):
    """Group of queries organized by access pattern.

    A cache-layer group (#226) carries ``cache_layer: true`` and only the cache
    engine's patterns; each of its ``source_queries`` carries ``cached_read: true``,
    a read its owner's group serves. The other groups never list a cache pattern.
    """

    group_name: str = Field(..., description="Human-readable group name")
    engines: list[str] = Field(default_factory=list, description="Engines serving this group")
    access_patterns: list[dict] = Field(
        default_factory=list, description="Access patterns in this group"
    )

    model_config = ConfigDict(extra="allow")


class CostSizing(BaseModel):
    """What a kept engine's TCO figure was sized for (#1195).

    ``assigned_queries``: the in-scope queries the effective assignment gives the engine
    (with any reads it also fronts) and the tables they touch; ``cache_overlay``: the
    reads the cache layer fronts; ``no_in_scope_queries``: the assignment has no query in
    scope, so the engine is sized to nothing ($0). Counts and rates follow the estimators: reads are
    SELECTs, writes INSERT, UPDATE, DELETE and REPLACE.
    """

    basis: Literal["assigned_queries", "cache_overlay", "no_in_scope_queries"]
    sized_query_count: int = Field(..., ge=0, description="Queries the figure was sized for")
    calls_per_second: float = Field(..., ge=0, description="Their calls a second")
    read_calls_per_second: float = Field(..., ge=0)
    write_calls_per_second: float = Field(..., ge=0)
    sized_table_count: int = Field(..., ge=0, description="Source tables those queries touch")
    unqueried_table_count: int = Field(
        0,
        ge=0,
        description=(
            "Source tables no in-scope query reads that this engine also stores (#1378): "
            "wave 1 moves them to it. Not in sized_table_count; their size is in storage_gb"
        ),
    )
    storage_gb: float = Field(..., ge=0, description="Those tables' source size, in GB")


class UnqueriedTables(BaseModel):
    """The source tables no in-scope query reads, and the engine that stores them (#1378).

    Wave 1 moves the whole source database, so they land on its Aurora engine, whose TCO
    figure is priced with them."""

    table_count: int = Field(..., ge=1, description="Source tables no in-scope query reads")
    storage_gb: float = Field(..., ge=0, description="Their source size, in GB")
    stored_on: str | None = Field(
        ...,
        description=(
            "Engine id of wave 1's Aurora engine, whose figure includes them; None when no "
            "kept engine is wave 1's, so no figure includes them"
        ),
    )


class CostBreakdown(BaseModel):
    """Cost breakdown per engine."""

    database: str = Field(..., description="Engine name")
    monthly_cost_usd: float = Field(..., ge=0)
    read_io_hit_ratio_basis: str | None = Field(
        None,
        description=(
            "For an estimate that bills a read only on a buffer-cache miss (DocumentDB): "
            "where the hit ratio came from, 'query', 'collection', 'none', a '+' join, or 'no_reads' when the "
            "estimate prices no reads (#1197)"
        ),
    )
    collection_hit_ratio_pct: float | None = Field(
        None,
        ge=0,
        le=100,
        description="The collection's instance-wide buffer-cache hit ratio, as collected (#1197)",
    )
    collection_hit_ratio_billed_pct: float | None = Field(
        None,
        ge=0,
        le=100,
        description=(
            "The bounded ratio a read that fell back to the collection's was billed with: "
            "scaled by target over source memory, at least min_billed_miss_pct missed (#1197)"
        ),
    )
    source_memory_gb: float | None = Field(
        None,
        gt=0,
        description="The source host's physical memory, in GiB, the ratio was scaled from",
    )
    target_memory_gb: float | None = Field(
        None, gt=0, description="The memory of the target instance the estimate priced, in GiB"
    )
    min_billed_miss_pct: float | None = Field(
        None,
        ge=0,
        le=100,
        description="The least share of every read billed as a miss, in percent (#1197)",
    )

    model_config = ConfigDict(extra="allow")


class TargetEngineCost(CostBreakdown):
    """One kept engine's TCO figure, with what it was sized for and priced from (#1195).

    A ``CostBreakdown`` passed in (a row built before these fields) is read as one with
    neither, so a caller that builds rows with the base model still validates."""

    @model_validator(mode="before")
    @classmethod
    def _from_a_breakdown_row(cls, value: Any) -> Any:
        if isinstance(value, CostBreakdown) and not isinstance(value, cls):
            return value.model_dump()
        return value

    sizing: CostSizing | None = Field(
        None,
        description=(
            "What the figure was sized for (#1195); None when it is the analysis estimate "
            "(sized for the whole collection: no assignment, or a report written before)"
        ),
    )
    price_list: dict[str, str] | None = Field(
        None,
        description=(
            "The price table the figure was priced from (#1196, price_basis()); None when "
            "its estimate recorded none"
        ),
    )


class TCOAnalysis(BaseModel):
    """Total cost of ownership analysis."""

    current_monthly_cost: float = Field(..., ge=0)
    # #380: whether ``current_monthly_cost`` is a real baseline or 0.0 only
    # because the collector never reported RDS instance metadata to estimate
    # from. Defaults ``True`` so a report written before this field existed
    # (where 0.0 almost always did mean "no instance metadata") still reads
    # as it always has.
    current_cost_known: bool = Field(True)
    projected_monthly_cost: float = Field(..., ge=0)
    savings_percent: float = Field(...)
    cost_breakdown: list[TargetEngineCost] | None = Field(None)
    # #380: engines the reality check eliminated, each with its own analysed
    # cost before removal. Not written since 1.25 (#949): it priced candidate
    # engines the source never ran. Kept so an older report still loads; never
    # read as a saving (``src.shared.engines_in_use.retired_engine_savings``).
    eliminated_engine_costs: list[CostBreakdown] | None = Field(None)
    # #949: engines the source runs today that the plan retires, each with its
    # analysed monthly cost -- the only retirement saving a deliverable states.
    retired_engine_savings: list[CostBreakdown] | None = Field(
        None,
        description=(
            "Engines the source runs today (the source engine, or one the collection "
            "records in metadata.engines_in_use) that the plan retires, with their "
            "analysed monthly cost. A candidate engine the plan dropped is never here."
        ),
    )
    unqueried_tables: UnqueriedTables | None = Field(
        None,
        description=(
            "The source tables no in-scope query reads and the engine that stores them "
            "(#1378); None when every table is read, with no assignment, and for a report "
            "written before"
        ),
    )
    assumptions: list[str] | None = Field(None)

    model_config = ConfigDict(extra="allow")


class ConsideredEngine(BaseModel):
    """A candidate engine the plan analysed and dropped (#949): never a saving."""

    engine: str = Field(..., description="Engine id")
    absorbed_by: str | None = Field(
        None, description="The engine that took most of its queries, if any"
    )

    model_config = ConfigDict(extra="allow")


class Risk(BaseModel):
    """A single migration risk."""

    severity: str = Field(..., description="LOW, MEDIUM, HIGH, CRITICAL")
    description: str = Field(...)
    rule: str | None = Field(
        None,
        description=(
            "The migration-risk rule that raised it (#950, a key of "
            "src.agents.referee.migration_risks.RULES); None for an analysis risk"
        ),
    )
    wave: int | None = Field(
        None, ge=1, description="The migration wave whose gate it must clear (#950)"
    )
    details: list[str] | None = Field(
        None,
        description=(
            "The design texts the risk covers, in full (#950: each trade-off, each "
            "unsupported cache pattern with its workaround); the engineering report and "
            "the results page list them, the executive report states only the count"
        ),
    )
    to_do: str | None = Field(
        None,
        description=(
            "The customer's to-do a wave's gate lists instead of a condition to close "
            "(#1938: 'migrate N routines yourself'), for a rule whose risks hold no wave; "
            "None otherwise (left out of the dump, so every other risk keeps the shape "
            "it had before 1.91). Counts only, no name"
        ),
    )

    model_config = ConfigDict(extra="allow")

    @model_serializer(mode="wrap")
    def _without_an_empty_to_do(self, handler: SerializerFunctionWrapHandler) -> Any:
        """A risk with no to-do dumps as it did before ``to_do`` existed (#1938): a
        report written before 1.91 renders the same risks as a new synthesis."""
        data = handler(self)
        if isinstance(data, dict) and data.get("to_do") is None:
            data.pop("to_do", None)
        return data


class RiskAssessment(BaseModel):
    """Risk assessment for the migration."""

    overall_risk_level: str = Field(..., description="LOW, MEDIUM, HIGH, CRITICAL")
    risks: list[Risk] = Field(...)
    mitigation_strategies: list[str] | None = Field(None)

    model_config = ConfigDict(extra="allow")


class AssignmentSummary(BaseModel):
    """Summary of the query-to-engine assignment used for synthesis."""

    version: int | None = Field(None, ge=1)
    status: str | None = Field(None)
    source: str | None = Field(
        None,
        description=(
            "Provenance of the assignment version this report was built from "
            "(assignment_resolution | reality_check | customer_gate). ADR-028."
        ),
    )
    query_count: int = Field(..., ge=0)
    in_scope_count: int = Field(..., ge=0)
    co_dependency_groups: int = Field(default=0, ge=0)
    tie_unresolved_count: int = Field(
        default=0,
        ge=0,
        description=(
            "How many query_assignments carry a non-empty tied_engines (#503): an "
            "exact score tie no ADR-031 rule decided, surfaced to the customer at the "
            "approval gate. 0 for a report synthesized before this field existed."
        ),
    )
    aurora_engine_choice: AuroraEngineChoice | None = Field(
        None,
        description=(
            "Copy of Assignment.aurora_engine_choice (#381): how the resolver picked "
            "one Aurora engine for a source with no Aurora dialect of its own. None "
            "for a homogeneous source and for a report synthesized before this field "
            "existed."
        ),
    )


class TiedQuery(BaseModel):
    """One query an exact score tie left unresolved (#503): no ADR-031 rule
    decided between ``tied_engines``, so
    ``assigned_engine`` is a deterministic placeholder, not a preference.
    Carried on the report so the decision report and the engineering report
    can show the SAME affected queries the approval gate already lists from
    ``QueryAssignment.tied_engines`` directly -- the "deliverables agree"
    invariant otherwise had nothing here to agree on.
    """

    query_id: str = Field(..., description="Query identifier from the collector")
    assigned_engine: str = Field(..., description="The placeholder engine the query sits under")
    tied_engines: list[str] = Field(
        ..., description="The engines this query's score ties between (length >= 2)"
    )


class TableGroup(BaseModel):
    """One DynamoDB table group within a wave (#225).

    Either a co-dependency group (queries sharing a significant JOIN,
    ``assignment.co_dependency_groups``) that touches at least one of this
    wave's tables, or the remainder of tables that belong to no such group
    (``kind: "independent"``).
    """

    tables: list[str] = Field(..., description="Tables in this group")
    query_count: int = Field(
        ...,
        ge=0,
        description=(
            "Distinct DynamoDB-assigned query IDs touching this group's tables "
            "(#225 — not every engine's query count, and not double-counted "
            "for a multi-table query)"
        ),
    )
    kind: Literal["co_dependency", "routine", "independent"] | None = Field(
        None,
        description=(
            "Whether this group came from a co-dependency group, a routine unit (a "
            "procedure's or trigger's statements, #1654, merged with the co-dependency "
            "groups it touches) or is the remainder. None for a wave written before this "
            "field existed."
        ),
    )


class TableOwner(BaseModel):
    """One table a search/analytics read-model wave serves, and its durable owner."""

    table: str = Field(..., description="Table identifier")
    owner: str = Field(
        ..., description="Engine that durably owns this table (never the read model)"
    )
    sync: Literal["zero-ETL", "OpenSearch Ingestion", "CDC"] = Field(
        ..., description="How the read model stays current with the owner"
    )


def join_gate_parts(parts: list[Any]) -> str:
    """The gate string a wave's ``gate_parts`` stand for (#1028): each part's
    text, then " (a, b, c)" when it has items. Takes ``GatePart`` models or
    their dicts."""
    out: list[str] = []
    for p in parts:
        text = p.get("text") if isinstance(p, dict) else p.text
        items = (p.get("items") if isinstance(p, dict) else p.items) or []
        out.append(str(text or ""))
        if items:
            out.append(f" ({', '.join(str(i) for i in items)})")
    return "".join(out)


class GatePart(BaseModel):
    """One run of a wave gate's text, followed by an optional item list (#1028).

    Joined back (``text``, then " (a, b, c)" when ``items`` is not empty), a
    wave's ``gate_parts`` are exactly its ``gate``. The results page collapses
    each list to an inline "(…)" without parsing the gate's prose.
    """

    text: str = Field(..., description="Text up to the list (or the rest of the gate)")
    items: list[str] = Field(
        default_factory=list, description="The list that follows the text, e.g. table names"
    )


class MigrationWave(BaseModel):
    """One step of the incremental migration roadmap (#225; relational-first
    reorder by #321).

    The sequence is one deterministic rule computed from the assignment, never
    a model's choice: the whole source database to the source-compatible
    Aurora engine first (schema carried over 1:1, #321), then cache (no data
    migration, reversible, fronting Aurora), then key-value and point-lookup
    queries to DynamoDB and/or document-shaped data to DocumentDB (one wave
    for both when both are routed), then any other direct migration target,
    then search/analytics read models (OpenSearch, synced, never system of
    record). A wave with nothing to move is omitted; the rest are numbered
    consecutively from 1. Every deliverable presents this as one suggested
    path, not the only one, alongside the direct "modernize in one step"
    option (the fully decomposed target architecture shown elsewhere in the
    same report).
    """

    wave: int = Field(..., ge=1, description="1-based position in the roadmap")
    title: str = Field(..., description="Short, human-readable name for the wave")
    engines: list[str] = Field(
        ...,
        description=(
            "The wave's target engine(s); two for the shared DynamoDB/DocumentDB wave "
            "when both are routed; empty for wave 1 when the source has no "
            "source-compatible Aurora engine (heterogeneous, #321)"
        ),
    )
    moves_from: list[str] = Field(
        default_factory=list,
        description=(
            "The engine(s) this wave's tables/queries move away from, if any (#321): "
            "the legacy source engine (e.g. 'mysql') for wave 1 itself, and for every "
            "later wave the retained Aurora engine once wave 1 has reached it -- never "
            "an end-state engine a later wave has not reached yet"
        ),
    )
    serves_from: list[str] = Field(
        default_factory=list,
        description=(
            "For a read-model wave only: the owner engine(s) it syncs from and never "
            "replaces as the system of record. Falls back to the retained/source engine "
            "when the indexed tables could not be resolved from the SQL (#225)"
        ),
    )
    fronts: str | None = Field(
        None,
        description=(
            "Cache wave only: the engine it fronts (#321) -- the retained Aurora engine "
            "once wave 1 has reached it, else the legacy source engine (e.g. 'mysql') "
            "when there is no retained engine to front instead (a heterogeneous source, "
            "or no source engine at all). The cache always moves no data (moves_from is "
            "empty); this is the fronted engine"
        ),
    )
    homogeneity: Literal["homogeneous", "cross_engine", "heterogeneous", "no_move"] | None = Field(
        None,
        description=(
            "Wave 1 (Aurora) only (#321): 'homogeneous' when the source maps to a "
            "source-compatible Aurora engine, 'cross_engine' (#381) when the source has "
            "no Aurora dialect of its own but the assignment resolver still picked one of "
            "the two competing Aurora engines (named, with a share, same as 'homogeneous', "
            "but stated as a dialect-and-schema conversion, not a 1:1 carry-over), and "
            "'heterogeneous' when no Aurora engine was picked at all, and 'no_move' (#1398) "
            "when the customer's engine scope has no Aurora engine or its Aurora engine "
            "serves no query (#1369), where the wave says no Aurora move is planned and "
            "the source is not necessarily heterogeneous (a report saved before keeps "
            "'heterogeneous' there; no_move_reason tells them apart). A heterogeneous wave "
            "1 still runs -- it is not skipped -- it just names no Aurora engine (engines "
            "is then empty, the title says what the source moves off instead of to, and "
            "the gate flags it as a risk). None for every other wave, and for a wave "
            "written before this field existed"
        ),
    )
    no_move_reason: Literal["no_relational_in_scope", "scope_choice_serves_nothing"] | None = Field(
        None,
        description=(
            "Wave 1 only (#1383): set when the wave moves nothing to Aurora, because the "
            "customer's engine scope has no Aurora engine ('no_relational_in_scope') or its "
            "Aurora engine serves no query ('scope_choice_serves_nothing', #1369). The wave "
            "moves nothing and names no engine; table_count and tables still hold the "
            "collected schema, but the renderers hide the wave's engine line, table count "
            "and tables, and no latency step follows it. None for every other wave, and "
            "for a wave written before this field existed"
        ),
    )
    tables: list[str] = Field(default_factory=list, description="Source tables this wave covers")
    table_count: int = Field(
        ...,
        ge=0,
        description=(
            "Number of source tables and views this wave covers. Wave 1 (Aurora) covers "
            "the whole collected schema (#321); every later wave's count is a subset of "
            "wave 1's tables that moves on from Aurora, not an addition to it"
        ),
    )
    table_groups: list[TableGroup] | None = Field(
        None,
        description="DynamoDB's own tables, grouped table group by table group (#225)",
    )
    table_owners: list[TableOwner] | None = Field(
        None,
        description=(
            "Each served-but-not-durably-owned table's real owner and sync pattern "
            "(#225, generalized from the search/analytics wave to every owner wave by "
            "#568 review): a table in ``tables`` that is absent here is this wave's own. "
            "For the merged DynamoDB/DocumentDB wave, deduped by (table, owner, sync) -- "
            "a table neither engine durably owns is one fact, not two identical entries."
        ),
    )
    table_counts_by_engine: dict[str, int] | None = Field(
        None,
        description=(
            "Per-engine table_count, for a wave with more than one engine (#568 review: "
            "the merged DynamoDB/DocumentDB wave). table_count itself is the union of "
            "both engines' served tables and cannot be split back into each engine's "
            "own share; crediting it to every engine double counts. None for a "
            "single-engine wave (table_count is already that engine's own) and for a "
            "wave written before this field existed."
        ),
    )
    owned_counts_by_engine: dict[str, int] | None = Field(
        None,
        description=(
            "Per-engine count of tables that engine durably owns -- table_counts_by_"
            "engine's count minus that engine's own table_owners entries -- for a wave "
            "with more than one engine (#568 review). Only the owned subset actually "
            "migrates; a served-but-not-owned table stays on its real owner, kept current "
            "by CDC. None for a single-engine wave (derive it there as "
            "table_count - len(table_owners or [])) and for a wave written before this "
            "field existed."
        ),
    )
    query_count: int = Field(..., ge=0, description="Number of queries this wave covers")
    cutover_query_count: int | None = Field(
        None,
        description=(
            "Wave 1 (Aurora), homogeneous or cross_engine only (#321 review, #381): every "
            "in-scope query, since the whole workload runs on Aurora the moment this wave "
            "finishes, before any later wave has moved its own share away. "
            "query_count/workload_share_percent stay the end-state share still on Aurora "
            "once every later wave has moved its own. None for every other wave, a "
            "heterogeneous wave 1 (no Aurora engine picked at all), and a wave written "
            "before this field existed"
        ),
    )
    workload_share_percent: float = Field(
        ..., ge=0, le=100, description="Share of the workload, or of calls for the cache wave"
    )
    share_basis: Literal["queries", "calls"] = Field(
        "queries", description="What workload_share_percent is a share of"
    )
    rationale: str = Field(..., description="Why this wave is sequenced where it is")
    gate: str = Field(..., description="What must be true before the next wave starts")
    gate_parts: list[GatePart] | None = Field(
        None,
        description=(
            "``gate`` as text runs and item lists (#1028), so a reader can collapse each "
            "list; joined back they are exactly ``gate``. None for a wave written before "
            "this field existed (the gate string alone)"
        ),
    )

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def _gate_parts_join_back_to_gate(self) -> "MigrationWave":
        # #1028: the parts are the gate, split; a reader collapsing the lists
        # must never show a different gate than the reports print.
        if self.gate_parts is not None and join_gate_parts(self.gate_parts) != self.gate:
            raise ValueError("gate_parts must join back to gate exactly")
        return self


class EngineFacts(BaseModel):
    """One engine of the target architecture, as every deliverable states it (#615)."""

    engine: str = Field(..., description="Engine id")
    role: Literal[
        "Retained",
        "Migration target",
        "Cache layer",
        "Search read model",
        "Evaluated",
        "Assessed",
    ] = Field(..., description="The engine's role in the target architecture")
    workload_percent: float | None = Field(
        None, description="Share of the owned workload; None for the cache layer (#296)"
    )
    cached_queries: int | None = Field(None, ge=0, description="Cache layer only: reads fronted")
    cached_call_share_percent: float | None = Field(
        None, description="Cache layer only: share of calls those reads carry"
    )
    engine_source_tables: int | None = Field(
        None,
        ge=0,
        description=(
            "Migration target only: source tables mapped to it (its schema design), or "
            "served by its migration wave when no design has run yet (#524)"
        ),
    )
    shared_source_tables: int | None = Field(
        None,
        ge=0,
        description=(
            "Source tables its schema design serves, when more than source_tables: "
            "includes tables also served by another engine (#257)"
        ),
    )
    target_objects: int | None = Field(
        None, ge=0, description="Target tables, indexes or key designs in its schema design"
    )
    tables_migrate: int = Field(
        0, ge=0, description="Source tables that move to it (owned, not only served; #568)"
    )
    kept_in_sync_tables: int | None = Field(
        None,
        ge=0,
        description=(
            "Migration target only: of engine_source_tables, the tables an engine outside "
            "its migration wave owns, kept current by CDC from that owner (the wave plan's rule, "
            "#581, #1169). None when it states no served count"
        ),
    )
    moves_to_tables: dict[str, int] | None = Field(
        None,
        description=(
            "Migration target only: owner -> its served tables another engine of the same "
            "wave owns; they move in that wave, to that owner (#1169). engine_source_tables "
            "= tables_migrate + these + kept_in_sync_tables"
        ),
    )
    monthly_cost_usd: float | None = Field(None, ge=0)
    cost_sizing: CostSizing | None = Field(
        None,
        description=(
            "What monthly_cost_usd was sized for (#1195), from tco_analysis.cost_breakdown; "
            "None for the analysis estimate and for a report written before"
        ),
    )
    rationale: str = Field("", description="Why the engine is in the target (#152)")
    signal_overrides: list[str] = Field(
        default_factory=list,
        description="Triage signals that overrode queries onto this engine (raw ids, #584)",
    )


class WaveHold(BaseModel):
    """One deterministic check that holds a wave (#1992, ``src.shared.wave_readiness``)."""

    check: Literal[
        "open_high_risk",
        "slower_and_pricier",
        "unserved_patterns",
        "unsupported_queries",
        "open_review",
        "earlier_wave",
    ] = Field(..., description="The check that holds the wave")
    engine: str | None = Field(None, description="The engine it is about; None for a wave 1 hold")
    risk_id: str | None = Field(None, description="The risk register entry it reads, if any")
    severity: str | None = Field(None, description="That risk's severity")
    risk_type: str | None = Field(None, description="That risk's type")
    flagged_by: str | None = Field(
        None,
        description=(
            "The engine whose analysis raised that risk, when another than the wave's "
            "(a re-attributed flag, which holds only when the wave's engine flags one of "
            "the same queries too)"
        ),
    )
    line: str = Field(..., description="The hold as every deliverable states it")


class WaveNextStep(BaseModel):
    """What to do next for a held wave (#1992)."""

    step: Literal["design", "revise_design", "review_again", "collect_longer", "earlier_wave"] = (
        Field(
            ...,
            description=(
                "Run the engine's schema design (it has none), revise its design and review "
                "it again, review it again (the review failed), collect over a longer window "
                "(a rate-driven risk on a window that failed its check), or wait for wave 1"
            ),
        )
    )
    engine: str | None = Field(None)
    line: str = Field(..., description="The step as every deliverable states it")


class ReviewWarning(BaseModel):
    """One design review change request below blocker severity (#1992)."""

    engine: str
    category: str = ""
    target: str = ""
    change: str = ""


class WaveRedesignCheck(BaseModel):
    """One failed check a redesign round sent back to the engine's design (#1992)."""

    check: str
    risk_id: str | None = None
    severity: str | None = None
    line: str | None = None
    detail: str | None = Field(None, description="The risk's own text, as the register states it")
    mitigation: str | None = None
    query_ids: list[str] = Field(default_factory=list)
    parts: list[int] | None = Field(
        None,
        description=(
            "An open_review check on a design in parts (#2119): the parts (DynamoDB group "
            "indexes) whose last review held the design, with their statements in "
            "query_ids; None for every other check"
        ),
    )
    warning: ReviewWarning | None = Field(
        None,
        description=(
            "A rejected_warning check (#2067): the design review warning the customer did "
            "not accept; None for every other check"
        ),
    )


class WaveRedesignRequest(BaseModel):
    """What one round sent back to one engine's design (#1992)."""

    engine: str
    waves: list[int] = Field(default_factory=list)
    checks: list[WaveRedesignCheck] = Field(default_factory=list)


class WaveRedesignRound(BaseModel):
    """One bounded redesign round (#1992): the engines sent back, what each was sent
    back with, and each one's outcome once the checks ran again."""

    round: int = Field(
        ..., ge=1, description="The round's place among every round of the version, both loops"
    )
    assignment_version: int = Field(0, ge=0)
    engines: list[str] = Field(default_factory=list)
    requests: list[WaveRedesignRequest] = Field(default_factory=list)
    outcome: (
        dict[
            str,
            Literal["fixed", "improved", "did_not_improve", "failed", "stopped", "not_reviewed"],
        ]
        | None
    ) = Field(
        None,
        description=(
            "Per engine; None while the round is running. not_reviewed (#2067): the new "
            "design's review did not run or failed, so the earlier design is kept"
        ),
    )
    reviewed_before: dict[str, bool] | None = Field(
        None,
        description=(
            "Legacy (#2067; not written or read since #2081): per engine, whether the "
            "design the round replaced had a review that ran. A new design is now kept "
            "only once its own review ran, whatever the replaced design had"
        ),
    )
    before: dict[str, list[int]] | None = Field(
        None,
        description=(
            "Per engine, its score before the round: [holds on its waves, assigned queries "
            "its design marks unsupported], plus, for a wave edit, the rejected warnings its "
            "review still lists; a round's design is kept only when strictly lower"
        ),
    )
    parts: dict[str, list[int] | None] | None = Field(
        None,
        description=(
            "Per engine, the design parts (DynamoDB groups) the round redesigned: the "
            "parts its checks name and every part sharing a placement group with one "
            "(#2119); None for the whole engine"
        ),
    )
    failed: dict[str, str] | None = Field(
        None, description="Per engine, why its redesign failed (a failed round)"
    )
    stopped: str | None = Field(
        None, description="Why the round did not run (a run ceiling); None when it ran"
    )
    wave_edit: int | None = Field(
        None,
        description=(
            "The wave a /wave-edit round redesigned (#2067); None for a round of the "
            "automatic loop"
        ),
    )
    decision_version: int | None = Field(
        None,
        ge=1,
        description=(
            "The wave edit decision (decisions/wave-edit/wave-<N>/v<k>.json) the round "
            "carries out; None for a round of the automatic loop"
        ),
    )


class WaveRedesignLog(BaseModel):
    """The redesign rounds of a run (#1992, ``wave-redesign/rounds.json``)."""

    max_rounds: int = Field(..., ge=0)
    rounds: list[WaveRedesignRound] = Field(default_factory=list)


class DecidedWarning(ReviewWarning):
    """A design review warning as the customer decided on it (#2067): its place in that
    review's list and its own text, never matched to another review's."""

    n: int = Field(..., ge=1, description="Its number in the list /wave-edit showed")


class WaveWarningDecision(BaseModel):
    """Which of a wave's design review warnings the customer accepted, and which they did
    not (#2067, ``/wave-edit``): recorded under ``decisions/wave-edit/`` and copied by
    synthesis into the report for the assignment version it was made on."""

    wave: int = Field(..., ge=0)
    decision_version: int = Field(
        ..., ge=1, description="The k of decisions/wave-edit/wave-<N>/v<k>.json"
    )
    assignment_version: int = Field(0, ge=0)
    accepted: list[DecidedWarning] = Field(default_factory=list)
    rejected: list[DecidedWarning] = Field(
        default_factory=list, description="Sent back to the design with the wave's blockers"
    )
    blockers: list[str] = Field(
        default_factory=list,
        description="The holds sent back with them, as the wave's readiness stated them",
    )
    auto: bool = Field(
        False, description="Recorded by an unattended run (--auto), which accepts every warning"
    )
    recorded_at: datetime | None = Field(None, description="When it was recorded (UTC)")


class WaveReadinessNote(BaseModel):
    """A wave the plan does not suggest, or a suggested one with design review warnings
    to accept, with its readiness line (#1992)."""

    wave: int = Field(..., ge=0)
    readiness: Literal["held", "not_designed", "not_reviewed", "suggested"]
    line: str


class WaveFacts(BaseModel):
    """The table figures one migration wave states (#615)."""

    wave: int = Field(..., ge=0)
    engines: list[str] = Field(default_factory=list)
    migrating: bool = Field(
        False,
        description=(
            "Moves tables off Aurora to a purpose-built engine (counts towards Tables "
            "migrate); wave 1, the cache and read-model waves do not"
        ),
    )
    no_move: bool = Field(
        False,
        description=(
            "The wave moves nothing to Aurora (its no_move_reason is set, #1383): renderers "
            "show no engine line or table count for it. False for a report written before "
            "this field existed"
        ),
    )
    served_tables: int = Field(..., ge=0, description="The wave's table_count")
    moving_tables: int | None = Field(
        None,
        ge=0,
        description=(
            "Tables one of the wave's engines durably owns, the sum of "
            "owned_counts_by_engine for the merged wave (#581); for a read-model wave, "
            "the served tables not kept current by CDC from an owner; for a cross-engine wave 1, "
            "every source table, views not included (#1120). None for a homogeneous or "
            "heterogeneous wave 1"
        ),
    )
    kept_in_sync_tables: int | None = Field(
        None,
        ge=0,
        description=(
            "Tables owned outside the wave and kept current by CDC from it. None for wave 1"
        ),
    )
    moving_by_engine: dict[str, int] | None = Field(None)
    served_by_engine: dict[str, int] | None = Field(None)
    kept_in_sync: list[TableOwner] = Field(
        default_factory=list,
        description="One entry per table kept current by CDC, naming its owner outside the wave",
    )
    shared_in_wave: list[TableOwner] = Field(
        default_factory=list,
        description=(
            "Tables one engine of this wave serves while another engine of the same wave "
            "owns them (#581): they move, with their owner"
        ),
    )
    views: int | None = Field(
        None,
        ge=0,
        description=(
            "Wave 1 only: how many of served_tables are views; the rest are the "
            "source_table_count tables (#578)"
        ),
    )
    tables_only: int | None = Field(
        None, ge=0, description="Wave 1 only: served_tables minus its views (#578)"
    )
    critical_high_risks: int = Field(
        0, ge=0, description="Open CRITICAL/HIGH risks on the wave's engines"
    )
    heterogeneous_note: str | None = Field(
        None,
        description=(
            "Cross-engine wave 1 only (#1120): wave 1 is not yet optimized for "
            "heterogeneous migrations (it converts every source table to the chosen "
            "Aurora engine with its current data model), with the relational gap counts. "
            "Every deliverable prints it under wave 1, followed by facts.relational_gaps "
            "when lists_relational_gaps is set"
        ),
    )
    moved_by_later_waves: int | None = Field(
        None,
        ge=0,
        description=(
            "Wave 1 only, when it names an Aurora engine (#1284): how many of its tables "
            "and views a later migrating wave moves off that engine (one of the wave's "
            "engines owns it). Wave 1's rationale states it next to the migration map's "
            "count. None for every other wave, a heterogeneous wave 1 and a block stored "
            "before the field (the facts reader fills it in)"
        ),
    )
    lists_relational_gaps: bool = Field(
        False,
        description=(
            "Cross-engine wave 1 only (#1120): facts.relational_gaps is the gap check for "
            "this wave's engine, so the deliverables list its gaps under this wave and the "
            "note states their counts. False when the gap check targets another engine, "
            "when there is none, and for every other wave"
        ),
    )
    readiness: Literal["suggested", "held", "not_designed", "not_reviewed"] | None = Field(
        None,
        description=(
            "The wave's readiness (#1992): suggested; held by a deterministic check "
            "(holds); not designed (an engine has no schema design) or not reviewed (its "
            "review failed), neither suggested nor held. None for a report written before "
            "the check"
        ),
    )
    suggested: bool | None = Field(
        None,
        description=(
            "True when readiness is suggested, False when held; None when not designed, "
            "not reviewed, or for a report written before the check"
        ),
    )
    holds: list[WaveHold] = Field(
        default_factory=list, description="The checks that hold the wave; empty when suggested"
    )
    next_steps: list[WaveNextStep] = Field(
        default_factory=list, description="What to do next for a held wave"
    )
    review_warnings: list[ReviewWarning] = Field(
        default_factory=list,
        description=(
            "A suggested wave's design review warnings (#1992): change requests below "
            "blocker severity, which hold nothing; approving the wave accepts them"
        ),
    )
    review_warning_count: int = Field(
        0, ge=0, description="How many review_warnings the wave lists (#1992)"
    )
    accepted_warnings: list[DecidedWarning] = Field(
        default_factory=list,
        description=(
            "The review_warnings the customer accepted with /wave-edit (#2067), as that "
            "decision recorded them; set only while the review the decision was made on is "
            "the wave's last (a kept redesign's review is a new list). Empty otherwise"
        ),
    )
    accepted_warning_count: int = Field(
        0, ge=0, description="How many accepted_warnings the wave lists (#2067)"
    )
    open_warning_count: int = Field(
        0,
        ge=0,
        description=(
            "How many of a suggested wave's review_warnings the customer has not decided on "
            "with /wave-edit: still to accept (#2067)"
        ),
    )
    rejected_warnings: list[DecidedWarning] = Field(
        default_factory=list,
        description=(
            "The review_warnings the customer did not accept with /wave-edit (#2067), as "
            "that decision recorded them, on the same terms as accepted_warnings"
        ),
    )
    rejected_warning_count: int = Field(
        0, ge=0, description="How many rejected_warnings the wave lists (#2067)"
    )
    redesign_rounds: int = Field(
        0,
        ge=0,
        description=(
            "How many bounded redesign rounds the wave's engines went through (#1992); 0 "
            "with none, and for a report written before them"
        ),
    )
    redesign_note: str | None = Field(
        None,
        description=(
            "Why the last redesign round kept no new design (#1992): it failed, did not "
            "improve the design, or a run ceiling stopped it; None otherwise"
        ),
    )
    readiness_line: str | None = Field(
        None,
        description=(
            "The wave's readiness as every deliverable prints it: suggested, or not "
            "suggested yet with its holds and next steps. None before the check"
        ),
    )
    readiness_line_short: str | None = Field(
        None,
        description=(
            "readiness_line as the executive report prints it (#2075): its design review "
            "warnings as a count that points to the engineering report, and a failed "
            "redesign round without its reason, so it names no collected table or column; "
            "the same as readiness_line otherwise. None before the check"
        ),
    )
    to_dos: list[str] = Field(
        default_factory=list,
        description=(
            "The customer's to-dos the wave's lines end with (#1938: the routines the "
            "pipeline could not translate automatically, facts.routines' "
            "migrate_yourself_line); they hold nothing. Counts only, no name"
        ),
    )


class NoMigrationFacts(BaseModel):
    """The engines that need no data migration: the retained core and the cache layer."""

    engines: list[str] = Field(default_factory=list)
    workload_percent: float = Field(
        0.0, ge=0, description="Workload they keep (cache layer excluded, it owns none)"
    )
    cached_call_share_percent: float = Field(0.0, ge=0)
    critical_high_risks: int = Field(0, ge=0)


class RemovedEngine(BaseModel):
    """An engine the reality check removed that is not in the final architecture (#547, #546)."""

    engine: str = Field(...)
    moved_to: list[str] = Field(default_factory=list, description="Where its queries went")
    reasons: list[str] = Field(default_factory=list, description="The reality check's own lines")


class RetiredInUseEngine(BaseModel):
    """An engine the customer runs today that the plan retires (#1076)."""

    engine: str = Field(...)
    monthly_cost_usd: float | None = Field(
        None, ge=0, description="The counted saving; None when the plan counts none"
    )
    moved_to: list[str] = Field(default_factory=list, description="Where its queries went")
    reason: str | None = Field(
        None, description="The reality check's reason for the first consolidation out of it"
    )
    no_saving_reason: str | None = Field(
        None, description="Why no saving is counted; None when monthly_cost_usd is set"
    )


class RealityCheckTradeOff(BaseModel):
    """One reality-check entry of ``trade_offs`` and whether it still applies (#546)."""

    index: int = Field(..., ge=0, description="Its position in the report's trade_offs")
    from_engine: str | None = Field(None, description="The engine a consolidation moved from")
    to_engine: str | None = Field(None, description="The engine it moved to")
    reversed: bool = Field(
        False,
        description=(
            "A later assignment edit moved queries back onto from_engine (every query the "
            "line moved, when the report records them per query): the line no longer "
            "describes the architecture and no deliverable states it as current"
        ),
    )
    queries_moved_back: int = Field(
        0, ge=0, description="Queries the line moved that a later edit put back on from_engine"
    )
    queries_still_moved: int | None = Field(
        None, ge=0, description="Queries the line moved that stay moved; None when not recorded"
    )
    description_now: str | None = Field(
        None,
        description=(
            "The line restated for what stayed moved, when a later edit undid only part "
            "of it; every deliverable shows this instead of the original line"
        ),
    )


class RealityCheckDecision(BaseModel):
    """A reality-check trade-off that still applies, as every deliverable shows it (#595)."""

    index: int = Field(..., ge=0, description="Its position in the report's trade_offs")
    kind: Literal["consolidation", "kept", "pattern", "other"] = Field(...)
    engine: str | None = Field(None, description="The engine it is about, if any")
    title: str = Field(..., description="Short heading, e.g. 'DocumentDB kept'")
    detail: str = Field("", description="The rest of the reality check's own line")
    query_ids: list[str] = Field(default_factory=list)
    with_removed_engine: bool = Field(
        False, description="A consolidation removed_engines already lists as its reason"
    )


class TradeOffTopicGroup(BaseModel):
    """One engine's design trade-offs on one topic, as every deliverable lists them (#1221)."""

    engine: str = Field(..., description="The engine whose design wrote them ('' when none)")
    topic: schema_design_output.TradeOffGroupTopic = Field(
        ..., description="The topic the trade-offs are filed under, or the pipeline's notes"
    )
    label: str = Field(..., description="The topic's heading, e.g. 'Item and value size'")
    trade_off_count: int = Field(..., ge=0, description="How many trade-offs the group lists")
    indexes: list[int] = Field(
        default_factory=list, description="Their positions in the report's trade_offs, in order"
    )


class TableBasis(BaseModel):
    """The figure behind "N tables mapped to a named target" (#530, #533)."""

    basis_table_count: int = Field(
        ..., ge=0, description='The "N tables mapped to a named target" figure itself'
    )
    mapped_tables: int = Field(0, ge=0, description="Tables a completed schema design mapped")
    design_coverage: Literal["complete", "partial", "none"] = Field(...)
    designed_engines: list[str] = Field(default_factory=list)
    pending_engines: list[str] = Field(
        default_factory=list, description="Migration targets with no completed schema design"
    )
    mapped_tables_by_role: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Mapped tables per role of their engine (#258: '21 migrate, 1 to the cache "
            "layer'); 'Other' for an engine with no role in the architecture"
        ),
    )
    mapped_tables_by_move: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Mapped tables by what the plan does with them, the Migration map heading's "
            "split (#1169): migrate (a migrating wave moves it, as counted by "
            "tables_migrate), kept_in_sync (a migrating wave serves it, another engine "
            "owns it), cache_layer, search_read_model, stays (on the source engine) "
            "and other"
        ),
    )
    migrating_tables_not_mapped: int = Field(
        0,
        ge=0,
        description=(
            "Tables a migration wave moves that no table_mappings row lists (#1169), so "
            "mapped_tables_by_move['migrate'] plus this is the waves' moving count"
        ),
    )
    migrating_tables_not_mapped_names: list[str] = Field(
        default_factory=list,
        description="Those tables by name: the schema design has no target table for them yet",
    )
    mapped_tables_not_served: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Engine -> tables the schema design maps to that migration target although no "
            "query routed to it reads them; they stay on the source engine (#1169)"
        ),
    )


class QueryTraceFactRow(QueryTraceListing):
    """One listed query, and whether report.json carries its full trace."""

    in_full: bool = Field(False, description="Its full trace is in query_traces.queries")


class TraceRuleFact(BaseModel):
    """How many of the queries not listed one first-assignment rule placed."""

    rule: str = Field(..., description="The rule (machine id)")
    label: str = Field(..., description="The rule as a short plain-English label")
    rule_query_count: int = Field(..., ge=0)


class QueryTraceGroup(BaseModel):
    """Listed queries whose full traces read the same (#1108): one appendix entry."""

    representative_id: str = Field(
        ..., description="The first of them, whose full trace the appendix renders once"
    )
    query_ids: list[str] = Field(
        default_factory=list, description="Every query of the group, most notable first"
    )
    short_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Each id's short form, in step with query_ids (8 characters, whole if another "
            "query id in the report starts the same)"
        ),
    )
    query_count: int = Field(..., ge=1, description="How many queries share the entry")
    open_tie_count: int = Field(
        0,
        ge=0,
        description="Those still in an unresolved tie; the headings add up to the open ties",
    )

    @model_validator(mode="after")
    def _counts_agree(self) -> "QueryTraceGroup":
        if not (len(self.query_ids) == len(self.short_ids) == self.query_count):
            raise ValueError("query_ids, short_ids and query_count must agree")
        if self.open_tie_count > self.query_count:
            raise ValueError("open_tie_count cannot exceed query_count")
        if self.representative_id not in self.query_ids:
            raise ValueError("representative_id must be one of query_ids")
        return self


class QueryTraceFacts(BaseModel):
    """The figures of the per-query decision traces (#339), from ``query_traces``."""

    has_ledger: bool = Field(
        False, description="False for an assignment written before the move ledger"
    )
    traced_query_count: int = Field(0, ge=0, description="Queries of the effective assignment")
    listed_query_count: int = Field(0, ge=0, description="Queries listed (in full or not)")
    full_trace_cap: int = Field(0, ge=0, description="At most this many carry their full trace")
    full_trace_count: int = Field(0, ge=0, description="Listed queries with their full trace")
    not_in_full_count: int = Field(0, ge=0, description="Listed queries beyond the cap")
    moved_query_count: int = Field(0, ge=0)
    ever_tied_query_count: int = Field(0, ge=0, description="Tied at some step")
    open_tie_query_count: int = Field(0, ge=0, description="Still tied at the end")
    overridden_query_count: int = Field(0, ge=0, description="Placed by a customer or a review")
    pinned_query_count: int = Field(0, ge=0, description="Pinned by a listed rule")
    unlisted_query_count: int = Field(
        0, ge=0, description="Queries that stayed where their first assignment put them"
    )
    listed_queries: list[QueryTraceFactRow] = Field(
        default_factory=list, description="Every listed query, most notable first"
    )
    trace_group_count: int = Field(
        0, ge=0, description="Entries in the appendix: groups of the queries with a full trace"
    )
    trace_groups: list[QueryTraceGroup] = Field(
        default_factory=list,
        description="The queries with a full trace, grouped by what their trace says (#1108)",
    )
    unlisted_rules: list[TraceRuleFact] = Field(
        default_factory=list, description="The queries not listed, by their first rule"
    )


class CloseCallPair(BaseModel):
    """The queries one engine won by less than the close-call margin over another (#730)."""

    engine: str = Field(..., description="The engine the queries went to (id)")
    alternative: str = Field(..., description="The kept engine that can take every one (id)")
    pair_query_count: int = Field(
        ..., ge=0, description="Close calls: queries won by more than 0 and less than the margin"
    )
    exact_tie_count: int = Field(
        0, ge=0, description="Exact ties a rule settled for the engine, apart from the close calls"
    )
    group_mean_count: int = Field(
        0, ge=0, description="Of them, compared on a co-dependency group's mean (move as a group)"
    )
    min_gap: float = Field(0.0, ge=0, description="The close calls' smallest gap, in points")
    max_gap: float = Field(0.0, ge=0, description="The close calls' largest gap, in points")
    query_ids: list[str] = Field(
        default_factory=list, description="The closest of them, at most CLOSE_CALL_IDS_SHOWN"
    )
    more_query_count: int = Field(0, ge=0, description="The ones query_ids does not name")


class CloseCallFacts(BaseModel):
    """Queries a kept engine could serve almost as well (#730), from ``query_traces.close_calls``."""

    margin_points: float = Field(
        ..., ge=0, description="A close call's runner-up is less than this many points behind"
    )
    close_call_query_count: int = Field(
        0, ge=0, description="Close calls with a gap above 0 (the settled ties are apart)"
    )
    settled_tie_count: int = Field(0, ge=0, description="Exact ties a routing rule settled")
    engine_pairs: list[CloseCallPair] = Field(
        default_factory=list, description="By engine and alternative, largest first"
    )


class UnservableGateFact(BaseModel):
    """One engine ruled out for a query on a fallback engine (#729)."""

    engine: str = Field(..., description="The engine (id)")
    gate: str = Field(..., description="The gate kind (machine id)")
    gate_text: str = Field("", description="The gate in plain words")


class UnservableQueryFact(BaseModel):
    """A query no kept engine can serve: on a fallback engine because no analyzed
    engine fit (#729), or flagged and kept where it is (#888)."""

    query_id: str
    engine: str = Field(..., description="The engine it ends on (id); empty when it has none")
    kind: str = Field(
        ...,
        description=(
            "fallback, gated_everywhere, flagged (#888), held (#1033) or no_engine_in_scope "
            "(#1329: no engine, so engine is empty)"
        ),
    )
    rule: str = Field("", description="The deciding step's rule (machine id)")
    explanation: str = Field("", description="Why it sits there, the trace's own sentence")
    blocking_gates: list[UnservableGateFact] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list, description="Capability ids")
    capability_labels: list[str] = Field(
        default_factory=list, description="The capabilities in plain words"
    )
    redesign_hints: list[str] = Field(
        default_factory=list, description="How to redesign it, one sentence each"
    )
    closest_engine: str = Field(
        "",
        description=(
            "For no_engine_in_scope (#1329): the engine it would fall back to, which the "
            "customer's engine scope leaves out; empty otherwise"
        ),
    )


class FlaggedUnservableQuery(BaseModel):
    """A flagged query no kept engine can take, so it stays on its engine (#888).

    Written by ``src.agents.referee.flagged_unservable``: a reality-check flag
    whose outcome is ``kept``, or a post-schema router terminal query, while
    the effective assignment keeps it in scope on the flagged engine.
    """

    query_id: str
    engine: str = Field(..., description="The flagged engine it stays on (id)")
    engine_name: str = Field(..., description="Its display name")
    source: Literal["reality_check", "design_review"] = Field(
        ..., description="The review that flagged it"
    )
    cause: Literal["no_kept_engine", "depth_limit", "held_by_pin"] = Field(
        ...,
        description=(
            "no_kept_engine: no other kept engine can take it; depth_limit: the "
            "post-schema routing cascade reached its last pass; held_by_pin: a customer "
            "pin keeps its table's write group on the flagged engine (#1033)"
        ),
    )
    reason_category: str = Field(..., description="The flag's reason category (FlagReason value)")
    reason_label: str = Field(..., description="The reason category in plain words")
    explanation: str = Field(..., description="Why it stays, in customer-facing words")
    gate: str = Field("model_flag", description="The gate that rules its engine out (kind)")
    gate_text: str = Field("", description="The gate in plain words")
    required_capabilities: list[str] = Field(
        default_factory=list, description="The hard capabilities triage found it needs (ids)"
    )
    source_tables: list[str] = Field(
        default_factory=list,
        description=(
            "The tables it reads, from the assignment (#1148: its migration risk's "
            "evidence); empty for a row written before 1.43"
        ),
    )


class ScopeUnservableQuery(UnservableQueryAssignment):
    """A query no engine in the customer's engine scope can serve (#1329): the
    assignment's ``unservable_queries`` entry, with the capabilities triage found it
    needs. It has no engine, is in no wave or design, and is a blocking risk
    (``routing.no_engine_in_scope``)."""

    required_capabilities: list[str] = Field(
        default_factory=list, description="The hard capabilities triage found it needs (ids)"
    )


class UnservableFacts(BaseModel):
    """Queries no kept engine can serve: the ones on a fallback engine (#729), from
    ``query_traces.unservable``, and the flagged ones that stay put (#888), from
    ``flagged_unservable``."""

    unservable_query_count: int = Field(0, ge=0)
    fallback_query_count: int = Field(0, ge=0, description="Placed by a fallback rule")
    gated_everywhere_query_count: int = Field(
        0, ge=0, description="Every candidate engine gated, no fallback rule"
    )
    flagged_query_count: int = Field(
        0,
        ge=0,
        description="A review flagged it and no other kept engine can take it (#888)",
    )
    held_query_count: int = Field(
        0,
        ge=0,
        description=(
            "A design review flagged it, and a customer pin keeps its table's writes "
            "on the flagged engine (#1033)"
        ),
    )
    no_fit_query_count: int = Field(
        0,
        ge=0,
        description=(
            "No analyzed engine fit it: placed by a fallback rule or gated everywhere "
            "(fallback_query_count + gated_everywhere_query_count, #888)"
        ),
    )
    no_engine_in_scope_query_count: int = Field(
        0,
        ge=0,
        description=(
            "No engine in the customer's engine scope can serve it, so it has no engine "
            "(#1329, scope_unservable): kept out of every engine's query count and cost"
        ),
    )
    no_engine_in_scope_closest_engines: list[str] = Field(
        default_factory=list,
        description=(
            "The engines those queries would fall back to, which the scope leaves out "
            "(#1329), sorted; the deliverables name them in how to serve the queries"
        ),
    )
    unservable_queries: list[UnservableQueryFact] = Field(
        default_factory=list, description="At most UNSERVABLE_SHOWN, by engine then query id"
    )
    more_unservable_count: int = Field(0, ge=0, description="The ones not named")


class AccessPatternFacts(BaseModel):
    """The access-pattern totals the results page shows (#792), from ``query_groups``."""

    owned_pattern_count: int = Field(
        0, ge=0, description="Access patterns the owner engines serve (the cache layer is apart)"
    )
    cache_layer_pattern_count: int = Field(
        0, ge=0, description="The cache layer's own patterns; never owned workload"
    )
    listed_pattern_count: int = Field(
        0, ge=0, description="Owned plus cache-layer patterns: the explorer's header total"
    )


class CacheLayerFacts(BaseModel):
    """The cache layer's read figures, each labelled for what it counts (#1252)."""

    engine: str | None = Field(None, description="The cache engine; None once nothing is cached")
    candidate_read_count: int = Field(
        0, ge=0, description="Cache candidates: reads routing put under the cache overlay"
    )
    candidate_call_share_percent: float | None = Field(
        None,
        ge=0,
        le=100,
        description="None when a report that recorded a drop did not store the candidates' share",
    )
    designed_read_count: int = Field(
        0, ge=0, description="Designed cached reads: what the cache engine's design covers"
    )
    designed_call_share_percent: float = Field(0.0, ge=0, le=100)
    owner_only_read_count: int = Field(
        0, ge=0, description="Candidates the design does not cover: they stay on their owner"
    )
    role_sentence: str | None = Field(
        None,
        description=(
            "The cache layer stated as the assessment's recommendation, with its reason "
            "(#399); None for a report written before it"
        ),
    )


class DerivedCopyFacts(BaseModel):
    """One derived copy's share of a stream sync (#1527)."""

    table: str = Field(..., description="The derived copy's DynamoDB table")
    write_units_per_month: float = Field(0.0, ge=0, description="Write request units it applies")
    cost_usd: float = Field(0.0, ge=0, description="Their on-demand cost a month")


class DynamoDBMergeConflictRow(BaseModel):
    """One unresolved DynamoDB merge failure and what it leaves unserved (#802)."""

    failure: str = Field(..., description="The merge failure, as the schema design states it")
    status: Literal["unserved", "at_risk", "none_named", "not_recorded"] = Field(
        "unserved",
        description=(
            "What pattern_ids are: unserved, at risk until the conflict is resolved, none "
            "named by the failure, or not recorded (a design merged before they were)"
        ),
    )
    table_name: str | None = Field(
        None, description="The merged table the listed access patterns run on, if one"
    )
    pattern_ids: list[str] = Field(
        default_factory=list, description="The access patterns the merged design does not serve"
    )
    query_ids: list[str] = Field(
        default_factory=list, description="The queries those access patterns serve"
    )
    repeated_pattern_ids: list[str] = Field(
        default_factory=list, description="Of pattern_ids, those an earlier conflict lists"
    )


class DynamoDBMergeConflictFacts(BaseModel):
    """The DynamoDB group merge's unresolved conflicts and their totals (#802)."""

    conflicts: list[DynamoDBMergeConflictRow] = Field(default_factory=list)
    conflict_count: int = Field(0, ge=0)
    unserved_pattern_count: int = Field(
        0, ge=0, description="Distinct access patterns the merged design does not serve"
    )
    unserved_query_count: int = Field(
        0, ge=0, description="Distinct queries those access patterns serve"
    )
    at_risk_pattern_count: int = Field(
        0, ge=0, description="Distinct access patterns at risk and not already unserved"
    )
    at_risk_query_count: int = Field(
        0, ge=0, description="Distinct queries of those, not already unserved"
    )
    unrecorded_conflict_count: int = Field(
        0, ge=0, description="Conflicts whose unserved access patterns were not recorded"
    )
    summary_line: str = Field(
        "",
        description="The conflicts and their totals in one sentence, as every deliverable states it",
    )


class DynamoDBMergeReviewRow(BaseModel):
    """One DynamoDB merge-review decision: the home the merge chose for a source table
    two or more design groups each gave its own (#1780)."""

    source_tables: list[str] = Field(
        default_factory=list, description="The source tables the design groups each modelled"
    )
    authoritative_home: str = Field(..., description="The table that takes every write to them")
    derived_copies: list[str] = Field(
        default_factory=list,
        description="The tables kept current from the home's DynamoDB Streams",
    )
    risk_id: str | None = Field(
        None, description="The open design-conflict risk of the overlap, if the register has it"
    )
    reconstructed: bool = Field(
        False,
        description=(
            "True when the design was merged before the merge recorded its choice: the home "
            "was ranked again from the design's access patterns as they are now"
        ),
    )
    line: str = Field("", description="The decision as the engineering report lists it")


class DynamoDBMergeReviewFacts(BaseModel):
    """The DynamoDB merge-review decisions and their totals (#1780)."""

    decisions: list[DynamoDBMergeReviewRow] = Field(default_factory=list)
    decision_count: int = Field(0, ge=0)
    source_table_count: int = Field(
        0, ge=0, description="Distinct source tables the decisions give one home"
    )
    derived_copy_count: int = Field(0, ge=0, description="Derived copies across the decisions")
    reconstructed_count: int = Field(
        0, ge=0, description="Decisions reconstructed for a design merged before they were recorded"
    )
    risk_ids: list[str] = Field(
        default_factory=list, description="The decisions' design-conflict risks, in order"
    )
    summary_line: str = Field(
        "", description="The decisions in totals, naming no table, as every deliverable states it"
    )
    review_line: str = Field(
        "",
        description="Why each home was chosen and what to review, as every deliverable states it",
    )


class DerivedCopySyncRow(BaseModel):
    """One source table's stream sync: the authoritative home to its derived copies (#1527)."""

    risk_id: str = Field(..., description="The design-conflict risk the deliverables print it by")
    source_tables: list[str] = Field(default_factory=list)
    authoritative_home: str
    derived: list[DerivedCopyFacts] = Field(default_factory=list)
    replayed_writes_per_second: float = Field(
        0.0, ge=0, description="Writes a second on the source rows replayed to every copy"
    )
    stream_read_units_per_month: int = Field(0, ge=0)
    stream_read_cost_usd: float = Field(0.0, ge=0)
    copy_write_cost_usd: float = Field(0.0, ge=0)
    monthly_cost_usd: float = Field(0.0, ge=0, description="Stream reads plus copy writes")
    path_line: str = Field("", description="The sync path, as the engineering report states it")
    cost_line: str = Field("", description="The sync cost, as the engineering report states it")
    summary_line: str = Field(
        "", description="The sync in one sentence naming no table (the executive report)"
    )


class DerivedCopyPrices(BaseModel):
    streams_read_unit_price_per_million: float = Field(..., ge=0)
    write_unit_price_per_million: float = Field(..., ge=0)


class DerivedCopySyncFacts(BaseModel):
    """The stream sync of DynamoDB derived copies, listed and priced (#1527)."""

    syncs: list[DerivedCopySyncRow] = Field(default_factory=list)
    total_monthly_cost_usd: float = Field(0.0, ge=0)
    prices: DerivedCopyPrices
    assumptions: list[str] = Field(default_factory=list)


class RowCountFacts(BaseModel):
    """The collection's row counts by basis, and the sizing assumption for the unknown
    ones (#1636), from ``src.shared.row_counts.row_count_facts``."""

    table_count: int = Field(0, ge=0, description="The collected source tables")
    unknown_count: int = Field(
        0, ge=0, description="Of those, the tables the source has no row count for"
    )
    ddl_table_count: int = Field(
        0,
        ge=0,
        description=(
            "Of the unknown ones, the tables read from DDL alone: routing treats them as "
            "small, an interim rule (#1646)"
        ),
    )
    no_size_count: int = Field(
        0,
        ge=0,
        description=(
            "Of the unknown ones, the tables with no collected size either (a DDL-only "
            "collection): sizing uses the fixed stand-in and no row-count rule reads them"
        ),
    )
    exact_count: int = Field(0, ge=0, description="Of those, the tables with an exact count")
    estimate_count: int = Field(
        0, ge=0, description="Of those, the tables whose count is a statistics estimate"
    )
    stats_oldest_at: str | None = Field(
        None, description="The oldest statistics refresh time the source reported (ISO 8601)"
    )
    stats_newest_at: str | None = Field(
        None, description="The newest statistics refresh time the source reported (ISO 8601)"
    )
    statement: str | None = Field(
        None,
        description=(
            "The one sentence every deliverable prints when a count is unknown: how many, "
            "and the sizing assumption that stands in for them; None when every count is known"
        ),
    )


class PerKeyUpperBound(BaseModel):
    """An upper bound on the average per key value, over the limit (#1993): the total spread
    over the values counted for part of the key, which takes at least that many."""

    at_least_key_values: int = Field(
        ..., ge=1, description="The values counted for part of the key: a lower bound"
    )
    units_per_second: float = Field(..., ge=0, description="The total over them")
    pct_of_limit: float = Field(
        ..., ge=0, description="units_per_second / partition_limit × 100, never capped"
    )


class HotPartitionFacts(BaseModel):
    """One DynamoDB design entry above the at-risk share of one partition key value's
    limit, with its load per key value estimated (#1989), from
    ``src.shared.partition_load.hot_partition_rows``."""

    engine: str = Field(..., description="Canonical engine id of the design")
    table_name: str = Field(..., description="The design table")
    gsi_name: str | None = Field(None, description="The GSI, or None for the base table")
    operation: Literal["read", "write"]
    partition_limit: float = Field(
        ..., description="One partition key value's limit: 3,000 read or 1,000 write units/s"
    )
    total_units_per_second: float = Field(
        ...,
        ge=0,
        description=(
            "The design's load on the whole table or index at the design rate: an upper "
            "bound for one key value, never the severity"
        ),
    )
    total_pct_of_key_limit: float = Field(
        ..., ge=0, description="total_units_per_second / partition_limit × 100, never capped"
    )
    total_is_upper_bound: bool = Field(
        True,
        description=(
            "Always true: the total is an upper bound for one key value, reached only if "
            "every request used the same value; it never sets the severity"
        ),
    )
    distinct_key_bound: Literal["collected", "at_most"] | None = Field(
        None,
        description=(
            "collected: distinct_key_values is the collection's count (or a lower bound "
            "on a wider key); at_most: a ``% n`` key transform, an access pattern's literal "
            "key or IN list, or the product or sum of every counted part of the key bounds "
            "it; None when not measured"
        ),
    )
    per_key_source: Literal["key_values", "access_pattern", "static_entity"] | None = Field(
        None,
        description=(
            "What set the per-key estimate: the key's distinct values, an access pattern "
            "whose calls reach at most distinct_key_values values (its share of the load over "
            "them), or a static-key entity's share of the load on its one key value; None "
            "when not measured"
        ),
    )
    distinct_key_values: int | None = Field(
        None,
        ge=1,
        description=(
            "The partition key's distinct values from the collection (1 for a key every "
            "item shares), or None when not collected"
        ),
    )
    per_key_units_per_second: float | None = Field(
        None,
        ge=0,
        description=(
            "The estimated load on the busiest key value: the total over "
            "distinct_key_values; None when not measured"
        ),
    )
    per_key_pct_of_limit: float | None = Field(
        None, ge=0, description="per_key_units_per_second / partition_limit × 100, never capped"
    )
    per_key_basis: Literal["single_key", "distinct_values", "not_measured"] = Field(
        ...,
        description=(
            "single_key: every request uses one key value; distinct_values: the total "
            "spread evenly over the key's distinct values; not_measured: per-key load not "
            "measured"
        ),
    )
    per_key_measure: Literal["measured", "lower_bound", "upper_bound", "not_measured"] | None = (
        Field(
            None,
            description=(
                "What the per-key figure is (#1993): measured (every request, or a measured "
                "share, on one key value), lower_bound (the total over a count the key's "
                "values cannot exceed, every part counted: the busiest value carries at "
                "least it), upper_bound (some part of the key is not counted, so the figure "
                "bounds only the average per key value, from above; never sets the "
                "severity) or not_measured; None in a report written before #1993"
            ),
        )
    )
    per_key_upper_bound: PerKeyUpperBound | None = Field(
        None,
        description=(
            "An upper bound on the average per key value set aside because it was over the "
            "limit (#1993): the evidence the busiest value is not measured; None otherwise"
        ),
    )
    query_ids: list[str] = Field(
        default_factory=list, description="The entry's contributing queries"
    )


class AssumptionItem(BaseModel):
    """One assumption the assessment rests on (#1990, ``src.shared.assumptions``)."""

    key: str = Field(
        ..., description="growth, peak, rate_window, rate_window_check (#1991) or pricing"
    )
    setting: float | str | None = Field(
        None, description="Its value (a growth or peak multiple, a region), when it has one"
    )
    origin: Literal["default", "collected", "customer", "flag", "file", "ui"] = Field(
        ...,
        description=(
            "Where the value comes from: flag (--growth), file (the assumptions file) or "
            "ui (the results page's assumptions panel, #1991); customer for any other "
            "writer and every report written before #1991"
        ),
    )
    basis: str | None = Field(
        None, description="For the peak: cloudwatch, sampled, not_measured or customer"
    )
    line: str = Field(..., description="The sentence every deliverable and the gate print")


class AssumptionsFacts(BaseModel):
    """The one assumptions block (#1990): its items and the lines the deliverables print."""

    assumptions: list[AssumptionItem] = Field(default_factory=list)
    lines: list[str] = Field(default_factory=list)


class GrowthScenarioRow(BaseModel):
    """The growth-dependent figures at one growth multiple (#1990)."""

    growth: float = Field(..., gt=0)
    monthly_cost_usd: float = Field(
        ..., ge=0, description="The headline's arithmetic at the scaled rates (1×: the headline)"
    )
    busiest_key_pct_of_limit: float | None = Field(
        None,
        ge=0,
        description=(
            "The busiest estimated partition key value's share of its limit at this growth; "
            "None when no key value is measured"
        ),
    )


class GrowthScenarioFacts(BaseModel):
    """Growth as a separate scenario (#1990): never in the headline cost or the risks."""

    growth: float = Field(..., gt=0, description="The job's growth: 5× unless the customer set it")
    origin: Literal["default", "customer", "flag", "file", "ui"]
    scenarios: list[GrowthScenarioRow] = Field(default_factory=list)
    busiest_key: dict[str, Any] | None = Field(
        None, description="engine, table_name, gsi_name and operation of the busiest key value"
    )
    lines: list[str] = Field(default_factory=list)


class RateWindowFacts(BaseModel):
    """How many collected statements' call rates used each window basis (#1893), from
    ``src.shared.rate_window.rate_window_facts``."""

    statement_count: int = Field(0, ge=0, description="The collected statements")
    sampled_count: int = Field(
        0,
        ge=0,
        description=(
            "Of those, the rates over the time between two snapshots of the counters "
            "(#1908): the change in the count over the interval"
        ),
    )
    observed_count: int = Field(
        0,
        ge=0,
        description="Of those, the rates over the statement's own first to last seen window",
    )
    uptime_count: int = Field(0, ge=0, description="Of those, the rates over the server's uptime")
    stats_reset_count: int = Field(
        0,
        ge=0,
        description=(
            "Of those, the rates over the time since PostgreSQL's pg_stat_statements was " "reset"
        ),
    )
    assumed_24h_count: int = Field(
        0,
        ge=0,
        description="Of those, the rates that assume the execution count covers 24 hours",
    )
    unrecorded_count: int = Field(
        0,
        ge=0,
        description=(
            "Of those, the statements with no basis recorded (a collector output written "
            "before #1893, or a Performance Insights statement)"
        ),
    )
    sample_fallback_count: int = Field(
        0,
        ge=0,
        description=(
            "Of those, the statements of a sampled collection (#1908) that kept a "
            "single-snapshot rate: a count that went down, a counter reset, a measured "
            "interval too short, a snapshot that came back incomplete, or an interval "
            "that was not measured"
        ),
    )
    sample_no_calls_count: int = Field(
        0,
        ge=0,
        description=(
            "Of those, the statements of a sampled collection (#1908) with no calls "
            "between the snapshots, rated at the lower of their single-snapshot rate and "
            "one call over the interval"
        ),
    )
    statement: str | None = Field(
        None,
        description=(
            "The one sentence every deliverable prints after 'Call rates:': how many rates "
            "used each basis, why sampled statements kept a single-snapshot rate, and that "
            "the costs assume each rate runs continuously; None for a collection with no "
            "statements"
        ),
    )
    # #1991: the window's length and dates, and whether it is representative.
    shortest_window_seconds: float | None = Field(
        None, ge=0, description="The shortest window a statement's count was divided by (#1991)"
    )
    longest_window_seconds: float | None = Field(
        None, ge=0, description="The longest window a statement's count was divided by (#1991)"
    )
    typical_window_seconds: float | None = Field(
        None,
        ge=0,
        description=(
            "The window half the calls were counted over or less (the call-weighted median "
            "of the statements' windows), the one the representativeness check reads (#1991)"
        ),
    )
    window_start: str | None = Field(
        None, description="The first moment the counts cover, YYYY-MM-DD HH:MM (#1991)"
    )
    window_end: str | None = Field(
        None, description="The last moment the counts cover, YYYY-MM-DD HH:MM (#1991)"
    )
    window_clock: Literal["server", "utc"] | None = Field(
        None,
        description=(
            "The clock the dates read: the server's (the statements' first and last seen) "
            "or UTC (the time the counters were read, less each window)"
        ),
    )
    server_uptime_seconds: float | None = Field(
        None, ge=0, description="The source server's uptime when collected (#1991)"
    )
    server_rate_per_second: float | None = Field(
        None,
        ge=0,
        description=(
            "The server's own statement rate (its count since it started over an uptime of "
            "at least 7 days, MySQL and MariaDB Questions), None when not collected (#1991)"
        ),
    )
    measured_rate_per_second: float | None = Field(
        None, ge=0, description="The collected statements' rates, summed (#1991)"
    )
    representative: bool | None = Field(
        None,
        description=(
            "Whether the window passed every representativeness check (#1991); None for a "
            "collection with no statements"
        ),
    )
    flags: list[
        Literal["short_window", "short_against_uptime", "after_reset", "above_source_rate"]
    ] = Field(
        default_factory=list,
        description=(
            "The checks the window failed (#1991): under a day, short against the server's "
            "uptime, counted from a reset (statements first seen together), or a measured "
            "rate 3x or more the server's own"
        ),
    )
    window_statement: str | None = Field(
        None,
        description=(
            "The window's length and dates and the divisor reminder, as the assumptions "
            "block prints them (#1991)"
        ),
    )
    check_statement: str | None = Field(
        None, description="The representativeness check, as the assumptions block prints it"
    )
    scenario_label: str | None = Field(
        None,
        description=(
            "When the window is not representative, the label every deliverable prints by "
            "the costs and the risks: a scenario at the measured rate, with a suggestion to "
            "collect over a longer window (#1991); None otherwise"
        ),
    )
    scenario_risk_note: str | None = Field(
        None,
        description=(
            "When the window is not representative, the short note every deliverable "
            "prints with the risks (the full label stands with the costs, #1991); None "
            "otherwise"
        ),
    )


class RelationalGroupRow(BaseModel):
    """One "Queries on Aurora by table" row (#1410): a relational engine's own queries
    in one query group."""

    engine: str = Field(..., description="The relational engine (id)")
    group_index: int = Field(..., ge=0, description="The group's index in query_groups")
    query_count: int = Field(0, ge=0, description="The engine's distinct queries in the group")
    calls_per_second: float = Field(0.0, ge=0, description="Their design calls/s, summed")


class UngroupedQueryRow(BaseModel):
    """An owner engine's in-scope queries that no query group of its own serves (#226)."""

    engine: str = Field(..., description="The owner engine (id)")
    query_count: int = Field(0, ge=0, description="Its in-scope queries in no group")
    schema_design_available: bool = Field(
        False, description="False: the engine has no schema design, so no access patterns yet"
    )


class QueryGroupFacts(BaseModel):
    """The engineering report's query group figures (#1410), from ``query_groups``."""

    group_count: int = Field(0, ge=0, description="Every query group")
    listed_group_count: int = Field(
        0, ge=0, description="Groups a non-relational engine serves: the Query groups table"
    )
    relational_only_group_count: int = Field(
        0, ge=0, description="Groups only relational engines serve, listed per table instead"
    )
    relational_only_engines: list[str] = Field(
        default_factory=list, description="The relational engines (ids) those groups run on"
    )
    listed_group_indexes: list[int] = Field(
        default_factory=list, description="The listed groups' indexes in query_groups, in order"
    )
    relational_rows: list[RelationalGroupRow] = Field(
        default_factory=list,
        description="Queries on Aurora by table: per engine, busiest first, blame groups last",
    )
    relational_query_counts: dict[str, int] = Field(
        default_factory=dict, description="Each relational engine's queries over its rows"
    )
    ungrouped_queries: list[UngroupedQueryRow] = Field(
        default_factory=list,
        description=(
            "Per owner engine, the in-scope queries no group of its own serves (#226); "
            "empty when every one is grouped"
        ),
    )
    procedure_internal_queries: int = Field(
        0,
        ge=0,
        description=(
            "Queries in the procedure-internal group: SQL Server statements on temporary "
            "tables or table variables only, kept with the utility statements (#1876)"
        ),
    )
    procedure_internal_routine_queries: int = Field(
        0,
        ge=0,
        description=(
            "How many statements follow their routine (the report's routine_followers): "
            "SQL Server statements on temporary tables or table variables only, each on "
            "the engine the other statements of the routine the collection names for it "
            "are on, and not scored on its own (#1891)"
        ),
    )
    procedure_internal_routine_counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "procedure_internal_routine_queries per engine id (#1891): each engine's "
            "section states its own; engines with none are left out"
        ),
    )


class QueryPatternMember(BaseModel):
    """One query of a query pattern (#1146): its id, frequency and engine, as routed."""

    query_id: str
    frequency_per_hour: float = Field(0.0, ge=0)
    engine: str | None = Field(None, description="The engine the query is assigned to")


class QueryPatternGroup(BaseModel):
    """The in-scope queries that are one query pattern (#1146): the same tables, predicate
    shape, order, limit class and operation; the columns they return may differ."""

    pattern_id: str = Field(..., description="PAT-<n>, in assignment order")
    operation: str = Field(
        ...,
        description=(
            "read, aggregate, or a write's kind (insert, update, delete, upsert); unread when "
            "the SQL could not be read"
        ),
    )
    tables: list[str] = Field(default_factory=list, description="The tables it reads or writes")
    returned_columns: list[str] = Field(
        default_factory=list, description="The union of the columns its queries return; * first"
    )
    frequency_per_hour: float = Field(0.0, ge=0, description="Its queries' frequency, summed")
    members: list[QueryPatternMember] = Field(default_factory=list)


class QueryPatternFacts(BaseModel):
    """The "N queries in M distinct query patterns" figures (#1146), from ``query_pattern_groups``,
    over the queries the owner engines serve (N is ``in_scope_query_count``)."""

    query_count: int = Field(0, ge=0, description="The queries counted: the owners' in-scope ones")
    pattern_count: int = Field(0, ge=0, description="The distinct query patterns they make")
    repeated_pattern_count: int = Field(
        0, ge=0, description="Of those, the patterns more than one query makes"
    )
    repeated_query_count: int = Field(0, ge=0, description="The queries in those patterns")
    split_pattern_count: int = Field(
        0, ge=0, description="Patterns whose queries routing put on more than one engine"
    )


class ReplicaRead(BaseModel):
    """One replica of a written table and the reads it serves (#992)."""

    engine: str = Field(..., description="The replica engine (id)")
    read_query_ids: list[str] = Field(
        default_factory=list, description="The table's reads this engine serves, sorted"
    )


class RoutineFollower(BaseModel):
    """A statement on temporary objects that follows its routine (#1891)."""

    query_id: str = Field(..., description="The statement on temporary objects")
    routine_id: str = Field(..., description="The routine it runs in, '<schema>.<name>'")
    engine: str = Field(..., description="The engine its routine's other statements are on")


class RoutineUnitMerge(BaseModel):
    """A routine behind a routine unit's merge (#1654)."""

    kind: Literal["procedure", "function", "trigger"] = Field(
        ..., description="What the routine is, as the collection's schema lists it"
    )
    id: str = Field(..., description="The routine, '<schema>.<name>'")


class RoutineUnitFact(BaseModel):
    """A placement unit one or more routines merged (#1654, ADR-031 Decision 4
    Amendment 21): its statements move as one, because on the source they run together
    in the routine (in one transaction where the routine opens one)."""

    merged_by: list[RoutineUnitMerge] = Field(
        default_factory=list, description="The procedures and triggers behind the merge"
    )
    query_ids: list[str] = Field(
        default_factory=list, description="The unit's in-scope statements, in placement order"
    )
    tables: list[str] = Field(
        default_factory=list, description="The source tables those statements read or write"
    )
    engine: str | None = Field(
        None, description="The one engine the unit is on; None when an edit split it"
    )
    engines: list[str] = Field(
        default_factory=list, description="Every engine the unit's statements are on"
    )


class TableSource(BaseModel):
    """A written table's source of truth and replicas (#992, ADR-031 Amendment 9)."""

    table: str = Field(..., description="The source table")
    source_of_truth: str | None = Field(
        None, description="The engine all its writes land on; None if they sit on several"
    )
    write_engines: list[str] = Field(
        default_factory=list, description="The engines its writes land on (one, by the rule)"
    )
    write_query_ids: list[str] = Field(
        default_factory=list, description="Its writes and locking reads, sorted"
    )
    replicas: list[ReplicaRead] = Field(
        default_factory=list,
        description="The other engines serving its reads, fed by CDC from the source of truth",
    )


class ReplicatedTableFact(BaseModel):
    """A written table whose reads run on a replica (#992)."""

    table: str
    source_of_truth: str = Field(
        ...,
        description=(
            "The engine its writes land on (id), or ``source_database`` when no engine in "
            "the customer's engine scope can serve them (#1329)"
        ),
    )
    replica_engines: list[str] = Field(
        default_factory=list, description="The replica engines (ids); CDC runs to each"
    )
    write_query_count: int = Field(0, ge=0)
    replica_read_query_ids: list[str] = Field(
        default_factory=list, description="The reads a replica serves, sorted"
    )
    replica_read_count: int = Field(0, ge=0, description="How many reads a replica serves")
    risk: str = Field("", description="The eventual-consistency risk, naming those reads")
    shares_replica_risk: bool = Field(
        False,
        description=(
            "Its risk is the standard sentence, so the deliverables state it once for every "
            "such table (#1232); False keeps the stored risk for this table"
        ),
    )


class TableSourceFacts(BaseModel):
    """Sources of truth and replicas (#992), from ``table_sources``."""

    written_table_count: int = Field(0, ge=0, description="Tables some query writes")
    replicated_table_count: int = Field(
        0, ge=0, description="Written tables with a read on another engine"
    )
    split_write_table_count: int = Field(
        0, ge=0, description="Written tables whose writes sit on several engines (0 by the rule)"
    )
    replicated_tables: list[ReplicatedTableFact] = Field(
        default_factory=list,
        description="At most TABLE_SOURCES_SHOWN, most replica reads first, then by table",
    )
    more_replicated_count: int = Field(0, ge=0, description="The replicated tables not listed")
    single_engine_tables: list[str] = Field(
        default_factory=list,
        description="Written tables every query of which runs on the source of truth",
    )
    single_engine_table_count: int = Field(0, ge=0)
    source_database_table_count: int = Field(
        0,
        ge=0,
        description=(
            "Written tables whose every write has no engine in the customer's engine scope "
            "(#1329): the source database stays their writer (source_of_truth "
            "``source_database``), and each engine that reads one holds a replica kept current "
            "by CDC from it"
        ),
    )


class TableOwnershipRow(BaseModel):
    """One mapped source table's owner line (#212)."""

    table: str = Field(..., description="Source table ID")
    owner: str | None = Field(None, description="Its owner (the wave plan's); None when none")
    owner_basis: Literal["writes", "split_writes", "most_queries", "retained", "design"] | None = (
        Field(None)
    )
    also_read_by: list[str] = Field(default_factory=list, description="Most queries first")
    cached_by: str | None = Field(None)
    target_engine: str | None = Field(
        None, description="The schema design target_table comes from: the owner's when it maps it"
    )
    target_table: str | None = Field(None)
    aggregate_pattern: str | None = Field(None)
    confidence_score: float | None = Field(None, description="target_engine's fit for the table")


class TableOwnershipFacts(BaseModel):
    """Each mapped source table's owner, the engines that also read it and its cache
    (#212, ADR-031 Amendment 9), from ``table_mappings``."""

    table_count: int = Field(0, ge=0, description="Mapped source tables")
    owned_table_counts: dict[str, int] = Field(
        default_factory=dict, description="engine -> mapped tables it owns, most first"
    )
    no_owner_table_count: int = Field(0, ge=0, description="Tables with no owner engine")
    also_read_table_count: int = Field(
        0, ge=0, description="Tables another engine also reads through a CDC replica"
    )
    cached_table_count: int = Field(0, ge=0, description="Tables a cache engine fronts")
    cache_engines: list[str] = Field(
        default_factory=list, description="The cache engines named in cached_by, sorted"
    )
    table_lines: list[TableOwnershipRow] = Field(
        default_factory=list, description="Sorted by table"
    )


class PendingChoicePair(BaseModel):
    """The pending engine choices that tie between the same engines (#1003)."""

    engines: list[str] = Field(default_factory=list, description="The tied engines, sorted")
    choice_count: int = Field(0, ge=0, description="Choices (tables) in the group")
    query_count: int = Field(0, ge=0, description="Queries those choices cover")
    tables: list[str] = Field(default_factory=list, description="Their tables, sorted")


class AutoDecidedEngine(BaseModel):
    """The choices ``--auto`` placed on one engine (#1003)."""

    engine: str = Field(..., description="The engine --auto placed them on")
    choice_count: int = Field(0, ge=0, description="Choices (tables) placed there")
    query_count: int = Field(0, ge=0, description="Queries those choices cover")
    tables: list[str] = Field(default_factory=list, description="Their tables, sorted")


GapImpactLevel = Literal["blocks_move", "needs_rewrite", "behaves_differently", "informational"]


class GapObject(BaseModel):
    """A schema object a relational-move gap was found in (#471)."""

    kind: Literal["column", "view", "procedure", "function", "trigger", "extension"]
    id: str = Field(
        ...,
        description="The object id; a column is <table_id>.<column_name>, an extension its name",
    )
    note: str | None = Field(
        None,
        description=(
            "What the gap says about this object beyond its id: the Aurora PostgreSQL "
            "version an extension is offered from (#1078)"
        ),
    )


class RelationalGap(BaseModel):
    """One version or feature gap for the relational move (#471), from one catalog rule."""

    rule_id: str = Field(..., description="The rule's file in src/catalog/gaps/")
    feature: str = Field(..., description="What the rule found, by name")
    impact: GapImpactLevel
    reason: str = Field(..., description="One plain sentence on what to do about it")
    docs: str = Field(..., description="The public AWS documentation the rule rests on")
    tables: list[str] = Field(default_factory=list, description="Table ids it touches")
    queries: list[str] = Field(default_factory=list, description="Query ids it was found in")
    objects: list[GapObject] = Field(
        default_factory=list,
        description="Columns, views, routines, triggers and extensions it was found in",
    )


class PriceListFacts(BaseModel):
    """The list prices behind every cost figure (#1196): ``src.shared.catalog.prices``."""

    source: str = Field(..., description='Where the prices come from ("AWS Price List")')
    region: str = Field(..., description="The AWS Region the prices are for")
    pricing_model: str = Field(..., description='The purchase option ("on-demand")')
    retrieved_on: str | None = Field(
        None,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description=(
            "The day the price table was read, when every dated estimate used the same "
            "table; None when they differ (see engine_dates)"
        ),
    )
    engine_dates: dict[str, str] = Field(
        default_factory=dict,
        description="Each priced engine's own price-table date, from its analysis estimate",
    )
    undated_engines: list[str] = Field(
        default_factory=list,
        description="Priced engines whose estimate recorded no date (written before #1196)",
    )


RoutineCategory = Literal[
    "as_is",
    "rewritten",
    "repaired",
    "needs_repair",
    "not_exact",
    "not_checked",
    "opted_out",
    "not_readable",
]
RoutineRiskClass = Literal[
    "migrate_yourself",
    "behaves_differently",
    "opted_out",
    "carried_review",
]


class RoutineRefFact(BaseModel):
    """A routine by its kind and id (``routines/v<N>/routine_migration.json``'s key)."""

    kind: str = Field(..., description="function, procedure, trigger or event")
    routine_id: str = Field(..., min_length=1, description="The routine's routine_id")


class RoutineRuleFact(BaseModel):
    """A routine check or rewrite rule a row names, as the rule catalog states it."""

    rule_id: str = Field(..., min_length=1)
    rule: Literal["check", "rewrite"] = Field(..., description="A check or a rewrite rule")
    impact: str | None = Field(None, description="A check rule's impact")
    reason: str = Field(..., description="The catalog's reason")
    fix_hint: str | None = Field(None, description="A check rule's fix hint")


class RoutineFactRow(BaseModel):
    """One routine that carries a risk (#1938): ids, kinds, rule ids and reason codes
    only, never its text, a diff of it or what a check found in it."""

    routine_id: str = Field(..., min_length=1)
    kind: str = Field(..., description="function, procedure, trigger or event")
    status: str = Field(..., description="The routine-migration artifact's status")
    category: RoutineCategory = Field(..., description="The headline count it is in")
    risk_classes: list[RoutineRiskClass] = Field(
        ..., min_length=1, description="The routine risks it is listed under, most severe first"
    )
    severity: str = Field(..., description="Its most severe class's severity")
    severity_basis: list[str] = Field(
        ...,
        min_length=1,
        description=(
            "What set the severity (the trace): the failed rule ids, the reason codes, "
            "rewritten, or the rule ids that could not be checked"
        ),
    )
    reasons: list[str] = Field(
        default_factory=list, description="The artifact's reasons (failed rule ids, reason codes)"
    )
    blocked_by: list[RoutineRefFact] = Field(
        default_factory=list, description="The routines it names that need attention"
    )
    rewrites: list[str] = Field(default_factory=list, description="The rewrite rule ids applied")
    unknown_checks: list[str] = Field(
        default_factory=list, description="The check rule ids that could not be checked"
    )
    notes: list[str] = Field(default_factory=list, description="The artifact's notes")
    tables: list[str] = Field(
        default_factory=list, description="The collected tables it names (what it touches)"
    )
    deploy_order: int = Field(..., ge=1)
    status_text: str | None = Field(
        None,
        description=(
            "Its status in plain words, as every deliverable prints it (#1939): 'Carried "
            "as it is', 'Not carried: needs repair', ...; None in a block stored before it"
        ),
    )
    why: list[str] = Field(
        default_factory=list,
        description=(
            "Why a routine that is not carried is in its class, in plain words (#1939): each "
            "failed check with the catalog's reason and fix hint, and each reason; never a "
            "code, an id (the routines it names are blocked_by) or its text. Rewrites and "
            "checks that could not run are in check_notes, once; empty for a carried routine"
        ),
    )
    listed: bool = Field(
        False,
        description=(
            "Whether the deliverables list it one by one (#1939): a routine to migrate "
            "yourself, one that would behave differently, or one with no code collected; "
            "the carried routines to check roll up into check_notes"
        ),
    )


class RoutineNoteFact(BaseModel):
    """A note routines share (#1939, the LOW roll-up): a rewrite rule or a check that
    could not run, stated once with the routines it applies to."""

    note: Literal["rewrite", "check_not_run"] = Field(..., description="Its kind")
    rule_id: str = Field(..., min_length=1, description="The rewrite or check rule")
    label: str = Field(..., description="Its label in plain words")
    text: str = Field("", description="The catalog's reason, once")
    note_count: int = Field(..., ge=1, description="The routines it applies to")
    routines: list[RoutineRefFact] = Field(
        default_factory=list, description="The first of them by kind and id, in deploy order"
    )
    more_count: int = Field(0, ge=0, description="How many more than routines lists")


class RoutineFacts(BaseModel):
    """The routine-migration headline and the routines behind its risks (#1938, ADR-034
    Decision 7), computed once from the newest ``routines/v<N>/routine_migration.json``
    at or below the report's assignment version (``src.shared.routine_facts``).

    The ``*_count`` categories split ``routine_count`` (each routine in one): ``as_is``,
    ``rewritten`` and ``repaired`` are carried; ``needs_repair`` failed a check or a
    dependency; ``not_exact`` was checked on a rebuilt ``CREATE`` that is not exact;
    ``not_checked`` has no exact ``CREATE``; ``opted_out`` has none because the customer
    turned body collection off; ``not_readable``'s body could not be read.
    ``unknown_checks_count`` and ``rewritten_carried_count`` overlap them: carried
    routines with a check that could not run, and with a header rewrite. The class
    counts (``migrate_yourself_count``, ``behaves_differently_count``,
    ``carried_review_count``) count each routine by its most severe class. ``proof``
    says what the verdicts rest on."""

    assignment_version: int = Field(..., ge=1, description="The artifact's version read")
    source_engine: str = Field(...)
    target_engine: str = Field(...)
    wave: int | None = Field(
        None, ge=1, description="The first wave that brings in target_engine; None: none"
    )
    routine_count: int = Field(..., ge=0, description="The routines in the artifact")
    carried_count: int = Field(..., ge=0, description="as_is + rewritten + repaired")
    as_is_count: int = Field(..., ge=0)
    rewritten_count: int = Field(..., ge=0)
    repaired_count: int = Field(..., ge=0)
    needs_repair_count: int = Field(..., ge=0)
    not_exact_count: int = Field(..., ge=0)
    not_checked_count: int = Field(..., ge=0)
    opted_out_count: int = Field(..., ge=0)
    not_readable_count: int = Field(..., ge=0)
    unknown_checks_count: int = Field(
        ..., ge=0, description="Carried routines with a check that could not run"
    )
    rewritten_carried_count: int = Field(
        ..., ge=0, description="Carried routines with a header rewrite"
    )
    migrate_yourself_count: int = Field(
        ..., ge=0, description="Routines the pipeline could not translate automatically"
    )
    behaves_differently_count: int = Field(..., ge=0)
    carried_review_count: int = Field(
        ..., ge=0, description="Carried routines rewritten or with a check that could not run"
    )
    migrate_yourself_line: str | None = Field(
        None,
        description=(
            "The to-do the risk and the wave line state: 'we could not translate N routines "
            "automatically; migrate them yourself before wave N'; None with no such routine"
        ),
    )
    migrate_yourself_step: str | None = Field(
        None,
        description=(
            "Its next step: 'migrate these routines yourself before wave N, then check them "
            "again'; None with no such routine"
        ),
    )
    counts_by_kind: dict[str, int] = Field(default_factory=dict, description="kind -> routines")
    reason_counts: dict[str, int] = Field(
        default_factory=dict, description="A reason (rule id or reason code) -> routines"
    )
    proof: Literal["checked, not load-tested"] = Field(
        "checked, not load-tested",
        description="What the verdicts rest on: the static checks, no load-and-run proof yet",
    )
    line: str = Field(..., description="The headline sentence, with no routine or table name")
    routines: list[RoutineFactRow] = Field(
        default_factory=list, description="Every routine with a risk class, in deploy order"
    )
    others_line: str | None = Field(
        None,
        description=(
            "The routines with no row, once (#1939): each is carried and every check on it "
            "passed; None when every routine has a row"
        ),
    )
    to_do_routines: list[RoutineRefFact] = Field(
        default_factory=list,
        description="The routines to migrate yourself (#1939), by kind and id, in deploy order",
    )
    check_notes: list[RoutineNoteFact] = Field(
        default_factory=list,
        description=(
            "What routines share, rolled up (#1939): each rewrite and each check that could "
            "not run once, with its count and the first routine ids"
        ),
    )
    rules: list[RoutineRuleFact] = Field(
        default_factory=list, description="Every rule the rows name, by id"
    )

    @model_validator(mode="after")
    def _counts_split_the_total(self) -> "RoutineFacts":
        parts = (
            self.as_is_count,
            self.rewritten_count,
            self.repaired_count,
            self.needs_repair_count,
            self.not_exact_count,
            self.not_checked_count,
            self.opted_out_count,
            self.not_readable_count,
        )
        if sum(parts) != self.routine_count:
            raise ValueError("the routine categories do not add up to the total")
        if self.carried_count != sum(parts[:3]):
            raise ValueError("carried_count is not as_is + rewritten + repaired")
        return self


class RelationalGapFacts(BaseModel):
    """The relational move's version and feature gaps against its Aurora target (#471),
    computed once at synthesis by ``src.tools.analysis.relational_gaps``."""

    source_engine: str
    source_version: str | None = None
    source_line: str | None = Field(
        None, description="The source's major version line (8.0, 16), when compared"
    )
    target_engine: str = Field(..., description="Wave 1's Aurora engine")
    target_lines: list[str] = Field(
        default_factory=list, description="The Aurora major version lines compared against"
    )
    version_checked: bool = Field(
        False, description="Whether the source's version was compared (homogeneous sources)"
    )
    rules_checked: int = Field(0, ge=0, description="Catalog rules that apply to this move")
    feature_rules_checked: int | None = Field(
        None,
        ge=0,
        description=(
            "Of rules_checked, the ones that look at what the source uses rather than its "
            "version (#1327); 0 means the catalog has no feature rules for this conversion. "
            "None in a report written before the count."
        ),
    )
    gaps: list[RelationalGap] = Field(
        default_factory=list, description="Most severe impact first, then by rule id"
    )
    gaps_by_impact: dict[str, int] = Field(
        default_factory=dict, description="Gaps per impact, every impact listed"
    )


class BriefExposure(BaseModel):
    """One open risk the executive summary names as driving the overall risk (#1403)."""

    risk_id: str | None = Field(None, description="The risk register entry it is")
    severity: str = Field(..., description="CRITICAL, HIGH, MEDIUM, LOW or UNRATED")
    risk_type: str | None = Field(None)
    rule: str | None = Field(None, description="The migration-risk rule that raised it, if any")
    engine: str | None = Field(None, description="The engine it is raised for")
    wave: int | None = Field(None, ge=0, description="The wave to act before; None if none")


class BriefWave(BaseModel):
    """What one wave does, as the executive summary's recommendation words it (#1403)."""

    wave: int = Field(..., ge=0)
    engines: list[str] = Field(default_factory=list)
    kind: Literal[
        "homogeneous",
        "cross_engine",
        "heterogeneous",
        "no_move",
        "cache",
        "migration",
        "read_model",
    ] = Field(
        ...,
        description=(
            "Wave 1's homogeneity (homogeneous, cross_engine, heterogeneous, no_move), a cache wave, "
            "a migration wave (moves tables off Aurora) or a read-model wave"
        ),
    )
    suggested: bool | None = Field(
        None,
        description="The wave's facts.waves[].suggested (#1992); None before the check",
    )
    readiness: Literal["suggested", "held", "not_designed", "not_reviewed"] | None = Field(
        None, description="The wave's facts.waves[].readiness (#1992); None before the check"
    )


class ExecutiveBriefFacts(BaseModel):
    """What the executive summary states beyond the headline figures (#1403).

    Built by ``src.shared.report_facts.executive_brief_facts``: the summary leads with
    the recommendation (engines, query shares, wave order, projected cost with its price
    basis), the risk (the overall level and the one or two exposures driving it) and the
    decision (the wave to approve next). Its cost, shares and price basis are the facts'
    own fields; this block adds the rest.
    """

    projected_monthly_cost_whole_usd: int = Field(
        0,
        ge=0,
        description=(
            "projected_monthly_cost_usd rounded half up to the nearest dollar: the figure "
            "the executive summary states ($505); cost tables keep the cents"
        ),
    )
    source_engine: str | None = Field(None, description="The source engine the plan moves off")
    source_cost_known: bool = Field(
        False, description="Whether a current cost was collected for the source database"
    )
    source_monthly_cost_usd: float | None = Field(
        None, description="The source's estimated monthly cost; None when not collected"
    )
    source_monthly_cost_whole_usd: int | None = Field(
        None, ge=0, description="source_monthly_cost_usd rounded half up, as the summary states it"
    )
    savings_percent: float | None = Field(
        None, description="The saving against it; None when no current cost was collected"
    )
    saving_counted: bool = Field(
        False,
        description=(
            "Whether a deliverable states any saving: a known current cost, or a retired "
            "engine the customer runs today with a counted saving. False: claim none"
        ),
    )
    retired_saving_whole_usd: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Each retired engine the customer runs today with a counted saving, that saving "
            "rounded half up to whole dollars, as the summary states it"
        ),
    )
    waves: list[BriefWave] = Field(default_factory=list)
    share_of_all_queries_percent: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "With queries no engine in the customer's engine scope serves (#1329): each owner "
            "engine's share of every collected query, which the summary states instead of "
            "its workload share. Empty when every query has an engine"
        ),
    )
    overall_risk_level: str = Field(
        "LOW", description="The highest open severity (CRITICAL, HIGH, MEDIUM, LOW)"
    )
    exposures: list[BriefExposure] = Field(
        default_factory=list, description="The one or two open risks driving the level"
    )
    decision_wave: int | None = Field(
        None,
        ge=0,
        description=(
            "The wave the reader is asked to approve next: the first suggested wave (#1992), "
            "never one held, not designed or not reviewed; None when no wave is suggested"
        ),
    )
    held_waves: list[int] = Field(
        default_factory=list,
        description=(
            "The waves a check holds (#1992): the Decision, rendered from the facts, says "
            "so with each one's reason and next step"
        ),
    )


# --- Slice plan and adoption paths (#1128: #1129, #1130, #1698) -------------------------
#
# The contract for what the slice plan computes. Everything here is computed
# deterministically in synthesis from the assignment and the collection (``src/shared/
# adoption_facts.py``); a model may explain it, never decide it. Step numbers and slice
# keys are a public key: the transformation contract (ADR-033 Decision 1) writes one file
# per step, ``transformation-contract/v<N>/step-<k>.json``, keyed on ``step_number``.
#
# Field names are owned by the renderers-only-format guards by their bare name (ADR-032
# Decision 2), so the lists are ``plan_steps`` and ``step_stages``: ``steps`` and
# ``stages`` already name the UI's own walkthrough and job-progress lists.

SliceStepKind = Literal["relational_step", "cache", "slice", "engine_wave"]
SliceStageKind = Literal[
    "whole_database",
    "cache_reads",
    "replica_reads",
    "self_contained",
    "with_reverse_replica",
    "risk_held",
]
MoveUnitKind = Literal["whole_database", "owner", "replica_reads", "cache_reads", "read_model"]
KeptWholeKind = Literal["write_group", "co_dependency", "routine_unit"]
UnitDependencyKind = Literal[
    "replica_read", "reverse_replica", "shared_table", "foreign_key", "cache_fronts"
]
OneStepRule = Literal[
    "no_open_critical_high_risk",
    "at_most_one_purpose_built_target",
    "no_reverse_replica_stage",
]

# A stage's letter inside its step ("3b"), fixed by its kind so a letter always means
# the same kind of work (an empty stage is skipped, never relettered), and the order the
# stages run in. The relational step's one stage is the whole step ("1").
STAGE_LETTERS: dict[str, str] = {
    "whole_database": "",
    "cache_reads": "a",
    "replica_reads": "a",
    "self_contained": "b",
    "with_reverse_replica": "c",
    "risk_held": "d",
}
STAGE_RANKS: dict[str, int] = {
    "whole_database": 0,
    "cache_reads": 1,
    "replica_reads": 1,
    "self_contained": 2,
    "with_reverse_replica": 3,
    "risk_held": 4,
}
# The unit kinds each stage kind may hold.
STAGE_UNIT_KINDS: dict[str, frozenset[str]] = {
    "whole_database": frozenset({"whole_database"}),
    "cache_reads": frozenset({"cache_reads"}),
    "replica_reads": frozenset({"replica_reads", "read_model"}),
    "self_contained": frozenset({"owner"}),
    "with_reverse_replica": frozenset({"owner"}),
    "risk_held": frozenset({"owner", "replica_reads", "read_model"}),
}
# The unit kinds that move queries to their engine. A query sits in at most one of
# them, and they keep write groups, co-dependency groups and routine units whole. The
# whole-database unit carries every query to Aurora first, and the cache only fronts
# reads that still run on their owner, so neither splits a group.
MOVING_UNIT_KINDS: frozenset[str] = frozenset({"owner", "replica_reads", "read_model"})
# The step kinds that move queries off the relational engine, and the ones that leave
# every query where the relational step put it (the relational-only path's steps).
MOVING_STEP_KINDS: frozenset[str] = frozenset({"slice", "engine_wave"})
NO_MOVE_STEP_KINDS: frozenset[str] = frozenset({"relational_step", "cache"})
PATH_ID_RE = re.compile(r"^(?:relational_only|full_target|through_step_([1-9][0-9]*))$")


def _count_matches(count: int, items: list[Any], what: str) -> None:
    if count != len(items):
        raise ValueError(f"{what} is {count} but lists {len(items)}")


def _unique(items: list[str], what: str) -> None:
    if len(set(items)) != len(items):
        raise ValueError(f"{what} lists a value twice")


class ReplicatedInTable(BaseModel):
    """A table a move unit reads but does not own: a CDC replica of its one writer
    (ADR-031 Amendment 9)."""

    table: str = Field(..., description="Source table id")
    source_of_truth: str = Field(..., description="The engine that writes it")


class ReverseReplica(BaseModel):
    """Reads on another engine that keep reading tables the unit owns, through a CDC copy
    fed from the unit's engine once the unit's writes move."""

    engine: str = Field(..., description="The engine whose reads stay")
    tables: list[str] = Field(..., min_length=1, description="The owned tables those reads read")
    query_ids: list[str] = Field(default_factory=list, description="Those reads")
    read_count: int = Field(0, ge=0, description="len(query_ids)")

    @model_validator(mode="after")
    def _counts(self) -> "ReverseReplica":
        _count_matches(self.read_count, self.query_ids, "read_count")
        _unique(self.tables, "reverse replica tables")
        return self


class UnitMetrics(BaseModel):
    """A move unit's dependency-graph metrics (#1698), rounded, computed in our code."""

    gravity: float = Field(
        ..., ge=0, le=1, description="How tightly the unit is tied to the rest (0 to 1)"
    )
    blast_radius: int = Field(
        ..., ge=0, description="What the move touches: its queries plus its reverse-replica reads"
    )
    hub_score: float = Field(
        ..., ge=0, le=1, description="How many other parts depend on it (betweenness, 0 to 1)"
    )


class MoveUnit(BaseModel):
    """The smallest set of queries and tables that moves to one engine on its own (#1129).

    A unit never splits a write group, a co-dependency group or a routine unit, and a
    table has one writer (ADR-031 Amendment 9): ``SlicePlanFacts`` checks both against
    ``kept_whole``.
    """

    key: str = Field(..., description="Stable key: the engine and the first table id")
    engine: str = Field(..., description="The engine the unit moves to")
    kind: MoveUnitKind = Field(
        ...,
        description=(
            "whole_database: the relational step, every query and table at once; owner: "
            "moves the writes of the tables it owns; replica_reads: reads only tables "
            "another engine owns; cache_reads: reads the cache fronts; read_model: a "
            "search read model's queries"
        ),
    )
    query_ids: list[str] = Field(
        default_factory=list,
        description=(
            "The queries it moves; empty for the whole-database unit, which is every "
            "in-scope query (query_count says how many)"
        ),
    )
    query_count: int = Field(..., ge=0)
    write_query_ids: list[str] = Field(
        default_factory=list, description="Of query_ids, the writes and locking reads"
    )
    owned_tables: list[str] = Field(
        default_factory=list, description="Tables whose one writer moves with the unit"
    )
    owned_table_count: int = Field(0, ge=0)
    replicated_in: list[ReplicatedInTable] = Field(default_factory=list)
    reverse_replicas: list[ReverseReplica] = Field(default_factory=list)
    reverse_read_count: int = Field(
        0, ge=0, description="The reverse replicas' reads, summed over engines"
    )
    kept_whole_ids: list[str] = Field(
        default_factory=list, description="The kept_whole groups the unit holds, by group_id"
    )
    call_share_percent: float = Field(
        0.0, ge=0, le=100, description="The unit's calls per second over the in-scope total"
    )
    holding_risk_ids: list[str] = Field(
        default_factory=list,
        description="Open critical or high risks on its queries or tables; they hold it in stage d",
    )
    pilot: bool = Field(False, description="The first unit of its step's first stage, named")
    metrics: UnitMetrics | None = Field(None, description="None until the graph (#1698) runs")

    @model_validator(mode="after")
    def _consistent(self) -> "MoveUnit":
        _unique(self.query_ids, "query_ids")
        _unique(self.owned_tables, "owned_tables")
        if self.kind == "whole_database":
            if self.query_ids or self.write_query_ids:
                raise ValueError("the whole-database unit lists no query ids")
        else:
            _count_matches(self.query_count, self.query_ids, "query_count")
        if not set(self.write_query_ids) <= set(self.query_ids):
            raise ValueError("write_query_ids must be a subset of query_ids")
        _count_matches(self.owned_table_count, self.owned_tables, "owned_table_count")
        if self.reverse_read_count != sum(r.read_count for r in self.reverse_replicas):
            raise ValueError("reverse_read_count must sum the reverse replicas' reads")
        if any(not set(r.tables) <= set(self.owned_tables) for r in self.reverse_replicas):
            raise ValueError("a reverse replica copies only tables the unit owns")
        replicated = [r.table for r in self.replicated_in]
        _unique(replicated, "replicated_in")
        if set(replicated) & set(self.owned_tables):
            raise ValueError("a table is owned or replicated in, never both")
        if self.kind in ("replica_reads", "cache_reads", "read_model") and self.owned_tables:
            raise ValueError(f"a {self.kind} unit owns no table")
        if self.holding_risk_ids and self.kind not in MOVING_UNIT_KINDS:
            raise ValueError("only a moving unit is held by a risk")
        return self


class KeptWholeGroup(BaseModel):
    """A group of queries routing placed as one, so a move unit holds all of it or none
    of it (ADR-031 Amendment 9: ``write_groups.placement_groups``' sources). A write
    group names its table: the unit that holds it is the table's one writer."""

    group_id: str = Field(..., description="Stable id, e.g. 'write:wp_posts' or 'co_dependency:1'")
    kind: KeptWholeKind
    query_ids: list[str] = Field(..., min_length=1)
    table: str | None = Field(None, description="A write group's table; None for the others")

    @model_validator(mode="after")
    def _table(self) -> "KeptWholeGroup":
        if (self.kind == "write_group") != (self.table is not None):
            raise ValueError("a write group, and only it, names its table")
        return self


class UnitDependency(BaseModel):
    """An edge of the dependency graph (#1698): ``unit_key`` needs ``depends_on`` in place
    first, so ``depends_on`` always runs earlier: in an earlier step or stage, or before it
    in the same stage. The graph is therefore acyclic."""

    unit_key: str
    depends_on: str
    kinds: list[UnitDependencyKind] = Field(..., min_length=1)
    edge_weight: float = Field(..., ge=0, description="How strongly the two are tied")
    tables: list[str] = Field(default_factory=list, description="The tables the edge runs through")

    @model_validator(mode="after")
    def _distinct(self) -> "UnitDependency":
        if self.unit_key == self.depends_on:
            raise ValueError("a unit does not depend on itself")
        _unique(list(self.kinds), "kinds")
        return self


class CutCost(BaseModel):
    """What cutting a slice off the rest costs (#1698)."""

    replica_table_count: int = Field(
        ..., ge=0, description="Tables that need a CDC copy across the cut, either way"
    )
    write_site_count: int = Field(..., ge=0, description="Write statements that change engine")
    transactions_split: int | None = Field(
        None, ge=0, description="Captured transactions the cut splits; None until #1682 lands"
    )


class SliceMetrics(BaseModel):
    """The figures a slice step's order rests on (#1698): value against cut cost, hub-heavy
    slices last. Recorded so every order decision states its reasons."""

    gravity: float = Field(..., ge=0, le=1)
    hub_score: float = Field(..., ge=0, le=1)
    cut_cost: CutCost
    value_call_share_percent: float = Field(..., ge=0, le=100)
    value_monthly_cost_usd: float | None = Field(
        None, ge=0, description="The cost the slice moves; None when not priced"
    )


class SliceStage(BaseModel):
    """One ordered stage of a step (#1129), with its own gate: a, reads on a replica; b,
    self-contained ownership; c, ownership with a reverse replica; d, held by a risk."""

    label: str = Field(
        ..., description="Step number and stage letter, e.g. '3b'; '1' for the relational step"
    )
    kind: SliceStageKind
    unit_keys: list[str] = Field(
        ..., min_length=1, description="Its move units in run order: blast radius, call share, key"
    )
    unit_count: int = Field(..., ge=1)
    query_count: int = Field(..., ge=0, description="The sum of its units' query_count")
    owned_table_count: int = Field(0, ge=0, description="The sum of its units' owned_table_count")
    call_share_percent: float = Field(0.0, ge=0, le=100)
    holding_risk_ids: list[str] = Field(
        default_factory=list, description="Stage d: its units' holding risks, sorted"
    )
    gate: str = Field(..., description="The condition before the next stage, a filled template")
    stage_gate_parts: list[GatePart] | None = Field(
        None,
        description=(
            "``gate`` as text runs and item lists, as a wave's gate_parts (#1028); joined "
            "back, ``gate``"
        ),
    )

    @model_validator(mode="after")
    def _consistent(self) -> "SliceStage":
        _unique(self.unit_keys, "unit_keys")
        _count_matches(self.unit_count, self.unit_keys, "unit_count")
        if (
            self.stage_gate_parts is not None
            and join_gate_parts(self.stage_gate_parts) != self.gate
        ):
            raise ValueError("stage_gate_parts must join back to gate exactly")
        if self.holding_risk_ids and self.kind != "risk_held":
            raise ValueError("only a risk_held stage lists holding risks")
        return self


class LeavingTable(BaseModel):
    """A table the relational step carries to Aurora that leaves it later, and the stage
    it leaves in."""

    table: str
    stage_label: str


class RelationalTableClasses(BaseModel):
    """Which of the tables the relational step carries matter, and how (#1129): each
    collected table is in exactly one class. The classes are
    ``transformation_contract.TableClass``'s values, one list each."""

    stays: list[str] = Field(
        default_factory=list,
        description="Aurora stays their one writer and no other engine reads them",
    )
    stays_count: int = Field(0, ge=0)
    leaves: list[LeavingTable] = Field(
        default_factory=list, description="Owned by a later step's unit"
    )
    leaves_count: int = Field(0, ge=0)
    replicated_out: list[str] = Field(
        default_factory=list, description="Aurora stays their writer; another engine reads a copy"
    )
    replicated_out_count: int = Field(0, ge=0)
    unread: list[str] = Field(default_factory=list, description="No collected query reads them")
    unread_count: int = Field(0, ge=0)

    @model_validator(mode="after")
    def _partition(self) -> "RelationalTableClasses":
        leaving = [t.table for t in self.leaves]
        _count_matches(self.stays_count, self.stays, "stays_count")
        _count_matches(self.leaves_count, leaving, "leaves_count")
        _count_matches(self.replicated_out_count, self.replicated_out, "replicated_out_count")
        _count_matches(self.unread_count, self.unread, "unread_count")
        every = [*self.stays, *leaving, *self.replicated_out, *self.unread]
        if len(set(every)) != len(every):
            raise ValueError("a table is in exactly one class")
        return self


class SliceStep(BaseModel):
    """One step of the plan (#1698, ADR-033 Decision 1): step 1 is the relational step
    (wave 1); each later step moves one slice, or the cache's reads, to one engine
    through its stages."""

    step_number: int = Field(..., ge=1, description="k: the public key a step's files use")
    kind: SliceStepKind = Field(
        ...,
        description=(
            "relational_step: wave 1; cache: the cache's fronted reads; slice: one slice "
            "of move units; engine_wave: one engine's whole wave, when the plan follows "
            "the migration waves (no slice plan yet)"
        ),
    )
    engine: str = Field(..., description="The one engine the step moves to")
    new_engine: bool = Field(
        ...,
        description=(
            "An engine the customer does not run yet, introduced by this step: only the "
            "first step on an engine introduces it, so a step introduces at most one"
        ),
    )
    movement: Literal["homogeneous", "heterogeneous"] = Field(
        ...,
        description=(
            "ADR-033 Decision 1: homogeneous when the destination is the same engine "
            "family on Aurora (a whole-table mirror), heterogeneous otherwise (contract rules)"
        ),
    )
    phase_wave: int | None = Field(
        None,
        ge=1,
        description=(
            "The migration_waves[].wave whose engine phase the step rolls up into (the "
            "executive view); None when no wave names its engine"
        ),
    )
    slice_key: str | None = Field(None, description="A slice step's stable slice key")
    step_stages: list[SliceStage] = Field(..., min_length=1)
    stage_count: int = Field(..., ge=1)
    unit_count: int = Field(..., ge=1)
    query_count: int = Field(..., ge=0, description="The sum of its stages' query_count")
    owned_table_count: int = Field(0, ge=0, description="The sum of its stages' owned_table_count")
    call_share_percent: float = Field(0.0, ge=0, le=100)
    metrics: SliceMetrics | None = Field(None, description="A slice step's order metrics")
    table_classes: RelationalTableClasses | None = Field(
        None, description="The relational step only"
    )

    @model_validator(mode="after")
    def _consistent(self) -> "SliceStep":
        _count_matches(self.stage_count, self.step_stages, "stage_count")
        if self.unit_count != sum(s.unit_count for s in self.step_stages):
            raise ValueError("unit_count must sum the stages' unit_count")
        if self.query_count != sum(s.query_count for s in self.step_stages):
            raise ValueError("query_count must sum the stages' query_count")
        if self.owned_table_count != sum(s.owned_table_count for s in self.step_stages):
            raise ValueError("owned_table_count must sum the stages' owned_table_count")
        ranks = [STAGE_RANKS[s.kind] for s in self.step_stages]
        if ranks != sorted(set(ranks)):
            raise ValueError("stages run a, b, c, d, each kind at most once")
        for stage in self.step_stages:
            if stage.label != f"{self.step_number}{STAGE_LETTERS[stage.kind]}":
                raise ValueError(f"stage {stage.label} must be labelled by its step and kind")
        kinds = {s.kind for s in self.step_stages}
        if self.kind == "relational_step":
            if (
                self.step_number != 1
                or kinds != {"whole_database"}
                or self.step_stages[0].unit_count != 1
            ):
                raise ValueError(
                    "the relational step is step 1: one whole_database stage, one unit"
                )
        elif "whole_database" in kinds:
            raise ValueError("only the relational step has a whole_database stage")
        if (self.kind == "cache") != (kinds == {"cache_reads"}):
            raise ValueError("the cache step, and only it, is one cache_reads stage")
        if (self.kind == "slice") != (self.slice_key is not None):
            raise ValueError("a slice step, and only it, has a slice_key")
        if self.metrics is not None and self.kind != "slice":
            raise ValueError("only a slice step carries slice metrics")
        if self.table_classes is not None and self.kind != "relational_step":
            raise ValueError("only the relational step carries table classes")
        return self


class PhaseFacts(BaseModel):
    """One engine phase: a migration wave's steps rolled up (the executive view, #1133;
    the engineering report's per-wave stages, #1131), so no renderer counts them."""

    phase_wave: int = Field(..., ge=1, description="The migration_waves[].wave")
    step_numbers: list[int] = Field(..., min_length=1, description="Its steps, in order")
    engines: list[str] = Field(..., min_length=1, description="Its steps' engines, in order")
    stage_labels: list[str] = Field(..., min_length=1, description="Its steps' stages, in order")
    stage_kinds: list[SliceStageKind] = Field(
        ..., min_length=1, description="The distinct stage kinds, in run order"
    )
    stage_count: int = Field(..., ge=1)
    query_count: int = Field(..., ge=0, description="The sum of its steps' query_count")


def _stage_position(step: SliceStep, stage: SliceStage, index: int) -> tuple[int, int, int]:
    return (step.step_number, STAGE_RANKS[stage.kind], index)


def phase_facts(steps: list[SliceStep]) -> list[PhaseFacts]:
    """The phases ``steps`` roll up into, by wave: the one function that computes them."""
    by_wave: dict[int, list[SliceStep]] = {}
    for step in steps:
        if step.phase_wave is not None:
            by_wave.setdefault(step.phase_wave, []).append(step)
    out = []
    for wave in sorted(by_wave):
        members = by_wave[wave]
        stages = [s for step in members for s in step.step_stages]
        kinds = sorted({s.kind for s in stages}, key=lambda k: (STAGE_RANKS[k], k))
        out.append(
            PhaseFacts(
                phase_wave=wave,
                step_numbers=[s.step_number for s in members],
                engines=[s.engine for s in members],
                stage_labels=[s.label for s in stages],
                stage_kinds=kinds,  # type: ignore[arg-type]
                stage_count=len(stages),
                query_count=sum(s.query_count for s in members),
            )
        )
    return out


class SlicePlanFacts(BaseModel):
    """The plan of steps, stages and move units, and the dependency graph behind its
    order (#1129, #1698; ADR-033's ``slice_plan``). ``migration_waves`` stays the engine
    roll-up the deliverables show today; every step names its phase wave, and ``wave_phases``
    rolls the steps up by it."""

    step_basis: Literal["slices", "engine_waves"] = Field(
        ...,
        description=(
            "slices: later steps are slices from the dependency graph; engine_waves: one "
            "step per migration wave, numbered as the wave (before #1698, and the "
            "engine-first scenario lever)"
        ),
    )
    slice_basis: Literal["community"] | None = Field(
        None,
        description=(
            "How slices were found: deterministic community detection over the graph; "
            "None for engine_waves. Module seeds wait for application evidence (#1692)"
        ),
    )
    plan_steps: list[SliceStep] = Field(default_factory=list)
    step_count: int = Field(0, ge=0)
    wave_phases: list[PhaseFacts] = Field(
        default_factory=list, description="The steps rolled up by phase wave (phase_facts)"
    )
    move_units: list[MoveUnit] = Field(default_factory=list)
    dependencies: list[UnitDependency] = Field(default_factory=list)
    kept_whole: list[KeptWholeGroup] = Field(
        default_factory=list, description="The groups no move unit splits"
    )
    engine_monthly_cost_usd: dict[str, float] = Field(
        default_factory=dict,
        description="Each step engine's cost_breakdown estimate, the one the paths sum",
    )
    engine_critical_high_risks: dict[str, int] = Field(
        default_factory=dict,
        description="Each step engine's open critical and high risks, the ones the paths sum",
    )

    @model_validator(mode="after")
    def _consistent(self) -> "SlicePlanFacts":
        _count_matches(self.step_count, self.plan_steps, "step_count")
        if [s.step_number for s in self.plan_steps] != list(range(1, len(self.plan_steps) + 1)):
            raise ValueError("steps are numbered 1, 2, ... in order")
        if (self.slice_basis is None) == (self.step_basis == "slices"):
            raise ValueError("slice_basis is set exactly when step_basis is slices")
        wrong_kind = "engine_wave" if self.step_basis == "slices" else "slice"
        if any(s.kind == wrong_kind for s in self.plan_steps):
            raise ValueError(f"a {self.step_basis} plan has no {wrong_kind} step")
        if self.step_basis == "engine_waves" and any(
            s.phase_wave != s.step_number for s in self.plan_steps
        ):
            # The data movement plans number their steps by wave (plan._steps): one
            # numbering, so step k's files are the same step everywhere.
            raise ValueError("an engine_waves step is numbered as its wave")
        _unique([s.slice_key for s in self.plan_steps if s.slice_key], "slice keys")
        seen: set[str] = set()
        for step in self.plan_steps:
            if step.new_engine and step.engine in seen:
                raise ValueError(f"step {step.step_number} reintroduces {step.engine}")
            seen.add(step.engine)
        if [p.model_dump() for p in self.wave_phases] != [
            p.model_dump() for p in phase_facts(self.plan_steps)
        ]:
            raise ValueError("wave_phases must roll the steps up by phase wave (phase_facts)")
        units = {u.key: u for u in self.move_units}
        if len(units) != len(self.move_units):
            raise ValueError("move unit keys are unique")
        position, stage_of = _place_units(self.plan_steps, units)
        missing = sorted(set(units) - set(position))
        if missing:
            raise ValueError(f"units in no stage: {missing}")
        _check_queries_and_groups(self.move_units, self.kept_whole)
        _check_one_writer(self.move_units, self.kept_whole)
        for dep in self.dependencies:
            for key in (dep.unit_key, dep.depends_on):
                if key not in units:
                    raise ValueError(f"dependency names unknown unit {key}")
            if position[dep.depends_on] >= position[dep.unit_key]:
                raise ValueError(
                    f"{dep.unit_key} depends on {dep.depends_on}, which does not run before it"
                )
        _check_table_classes(self.plan_steps, units, stage_of)
        engines = {s.engine for s in self.plan_steps}
        for name, figures in (
            ("engine_monthly_cost_usd", self.engine_monthly_cost_usd),
            ("engine_critical_high_risks", self.engine_critical_high_risks),
        ):
            if set(figures) != engines:
                raise ValueError(f"{name} names exactly the steps' engines")
        return self


def _place_units(
    steps: list[SliceStep], units: dict[str, MoveUnit]
) -> tuple[dict[str, tuple[int, int, int]], dict[str, SliceStage]]:
    """Each unit's run position (step, stage, place in the stage) and stage, with the
    per-stage and per-step checks that need the units."""
    position: dict[str, tuple[int, int, int]] = {}
    stage_of: dict[str, SliceStage] = {}
    for step in steps:
        pilots = 0
        for i, stage in enumerate(step.step_stages):
            members = []
            for j, key in enumerate(stage.unit_keys):
                if key not in units:
                    raise ValueError(f"stage {stage.label} names unknown unit {key}")
                if key in position:
                    raise ValueError(f"unit {key} is in more than one stage")
                unit = units[key]
                position[key], stage_of[key] = _stage_position(step, stage, j), stage
                members.append(unit)
                if unit.engine != step.engine:
                    raise ValueError(f"unit {key} moves to {unit.engine}, not {step.engine}")
                if unit.kind not in STAGE_UNIT_KINDS[stage.kind]:
                    raise ValueError(f"a {stage.kind} stage holds no {unit.kind} unit")
                if unit.pilot:
                    pilots += 1
                    if (i, j) != (0, 0):
                        raise ValueError("the pilot is the first unit of the first stage")
                _check_stage_shape(stage, unit)
            if stage.query_count != sum(u.query_count for u in members):
                raise ValueError(f"stage {stage.label}: query_count must sum its units'")
            if stage.owned_table_count != sum(u.owned_table_count for u in members):
                raise ValueError(f"stage {stage.label}: owned_table_count must sum its units'")
            risks = sorted({r for u in members for r in u.holding_risk_ids})
            if stage.holding_risk_ids != risks:
                raise ValueError(f"stage {stage.label}: holding_risk_ids are its units' risks")
        if step.kind == "slice" and step.unit_count > 1 and pilots == 0:
            raise ValueError(f"slice step {step.step_number} names no pilot")
    return position, stage_of


def _check_stage_shape(stage: SliceStage, unit: MoveUnit) -> None:
    """The stage rules of #1129, first that applies: a risk holds its unit; a unit that
    owns no table reads on a replica (STAGE_UNIT_KINDS and MoveUnit already keep an owner
    out of stage a); one with no copy either way is self-contained."""
    if unit.kind not in MOVING_UNIT_KINDS:
        return
    if (stage.kind == "risk_held") != bool(unit.holding_risk_ids):
        raise ValueError(f"unit {unit.key}: a risk holds its unit in stage d, and only there")
    copies = bool(unit.replicated_in or unit.reverse_replicas)
    if stage.kind in ("self_contained", "with_reverse_replica") and not unit.owned_tables:
        raise ValueError(f"unit {unit.key} owns no table")
    if stage.kind == "self_contained" and copies:
        raise ValueError(f"unit {unit.key} needs a replica, so it is not self-contained")
    if stage.kind == "with_reverse_replica" and not copies:
        raise ValueError(f"unit {unit.key} needs no replica, so it is self-contained")


def _check_queries_and_groups(units: list[MoveUnit], groups: list[KeptWholeGroup]) -> None:
    """A query sits in at most one moving unit (and one cache unit); a kept-whole group is
    in one moving unit entirely, or in none (ADR-031 Amendment 9)."""
    holder = _query_holders(units)
    cached: set[str] = set()
    for unit in units:
        if unit.kind == "cache_reads":
            if cached & set(unit.query_ids):
                raise ValueError(f"unit {unit.key}: a cached read is in two cache units")
            cached |= set(unit.query_ids)
    _unique([g.group_id for g in groups], "kept_whole group ids")
    by_key = {u.key: u for u in units}
    known = {g.group_id for g in groups}
    for unit in units:
        stray = set(unit.kept_whole_ids) - known
        if stray:
            raise ValueError(f"unit {unit.key} names unknown groups {sorted(stray)}")
    for group in groups:
        holders = {holder[q] for q in group.query_ids if q in holder}
        if len(holders) > 1:
            raise ValueError(f"group {group.group_id} is split across units {sorted(holders)}")
        if holders:
            (key,) = holders
            if not set(group.query_ids) <= set(by_key[key].query_ids):
                raise ValueError(f"unit {key} moves part of group {group.group_id}")
        claimed = {u.key for u in units if group.group_id in u.kept_whole_ids}
        if claimed != holders:
            raise ValueError(
                f"group {group.group_id} is held by {sorted(holders)}, not {sorted(claimed)}"
            )


def _query_holders(units: list[MoveUnit]) -> dict[str, str]:
    holder: dict[str, str] = {}
    for unit in units:
        if unit.kind not in MOVING_UNIT_KINDS:
            continue
        for qid in unit.query_ids:
            if qid in holder:
                raise ValueError(f"query {qid} is in units {holder[qid]} and {unit.key}")
            holder[qid] = unit.key
    return holder


def _check_one_writer(units: list[MoveUnit], groups: list[KeptWholeGroup]) -> None:
    """A table has one writer (ADR-031 Amendment 9): at most one moving unit owns it, and
    a unit owns a table exactly when it holds the table's write group."""
    owner: dict[str, str] = {}
    for unit in units:
        if unit.kind not in MOVING_UNIT_KINDS:
            continue
        for table in unit.owned_tables:
            if table in owner:
                raise ValueError(f"table {table} is owned by units {owner[table]} and {unit.key}")
            owner[table] = unit.key
    held = {g: u.key for u in units if u.kind in MOVING_UNIT_KINDS for g in u.kept_whole_ids}
    for group in groups:
        if group.kind != "write_group" or group.table is None:
            continue
        holder, table_owner = held.get(group.group_id), owner.get(group.table)
        if holder is not None and table_owner != holder:
            raise ValueError(f"unit {holder} moves {group.table}'s writes but does not own it")
        if table_owner is not None and holder != table_owner:
            raise ValueError(f"unit {table_owner} owns {group.table} without its write group")


def _check_table_classes(
    steps: list[SliceStep], units: dict[str, MoveUnit], stage_of: dict[str, SliceStage]
) -> None:
    """Wave 1's table classes agree with the units: every table a later unit owns leaves
    in that unit's stage, and every table replicated out is one a later unit reads from
    the relational engine."""
    relational = next((s for s in steps if s.kind == "relational_step"), None)
    if relational is None or relational.table_classes is None:
        return
    classes = relational.table_classes
    whole = units[relational.step_stages[0].unit_keys[0]]
    classified = [*classes.stays, *(t.table for t in classes.leaves), *classes.replicated_out]
    if set(classified + classes.unread) != set(whole.owned_tables):
        raise ValueError("the table classes sort exactly the tables the relational step carries")
    owned = {
        t: stage_of[k].label
        for k, u in units.items()
        if u.kind in MOVING_UNIT_KINDS
        for t in u.owned_tables
    }
    leaving = {t.table: t.stage_label for t in classes.leaves}
    if leaving != owned:
        raise ValueError("leaves must be every table a later unit owns, with that unit's stage")
    read_from_relational = {
        r.table
        for u in units.values()
        for r in u.replicated_in
        if r.source_of_truth == relational.engine
    }
    if set(classes.replicated_out) != read_from_relational - set(owned):
        raise ValueError("replicated_out must be the relational tables a later unit reads")


class OneStepCheck(BaseModel):
    """One rule of "suits one step" (#473, adoption-paths design 8.3), as a stated fact."""

    rule: OneStepRule
    observed: int = Field(..., ge=0, description="What the plan has")
    limit: int = Field(..., ge=0, description="The most the rule allows")
    passed: bool

    @model_validator(mode="after")
    def _passed(self) -> "OneStepCheck":
        if self.passed != (self.observed <= self.limit):
            raise ValueError("passed means observed is at most limit")
        return self


class OneStepFacts(BaseModel):
    """The full target as one step instead of staged (#473, "modernize in one step"): the
    same end state, so the same cost, engines, queries and tables, in one step whose gate
    holds every stage gate of the staged form."""

    step_count: int = Field(1, ge=1, le=1, description="One step")
    staged_step_count: int = Field(..., ge=0, description="The staged form's steps")
    staged_stage_count: int = Field(..., ge=0, description="The staged form's stages")
    gate_count: int = Field(..., ge=0, description="The stage gates the one step's gate holds")
    temporary_stream_count: int = Field(0, ge=0, le=0, description="One step needs none")
    staged_temporary_stream_count: int = Field(
        ..., ge=0, description="The reverse streams the staged form's stage c needs"
    )
    suits_one_step: bool
    one_step_checks: list[OneStepCheck] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _suits(self) -> "OneStepFacts":
        rules = [c.rule for c in self.one_step_checks]
        _unique(list(rules), "one_step_checks rules")
        if set(rules) != set(get_args(OneStepRule)):
            raise ValueError("one_step_checks states every rule once")
        if self.suits_one_step != all(c.passed for c in self.one_step_checks):
            raise ValueError("suits_one_step means every check passed")
        return self


def _path_step(path_id: str) -> int | None:
    match = PATH_ID_RE.match(path_id)
    if match is None:
        raise ValueError(
            f"path id {path_id!r} is not relational_only, through_step_<k> or full_target"
        )
    return int(match.group(1)) if match.group(1) else None


class AdoptionPathFacts(BaseModel):
    """One offered adoption path (#1130): the plan stopped after a step, as a projection
    of the assignment (no Move, no re-scoring). Every figure a deliverable prints."""

    path_id: str = Field(..., description="relational_only, through_step_<k> or full_target")
    engines: list[str] = Field(..., min_length=1, description="The engines at its end")
    stops_after_step: int = Field(..., ge=1, description="The last step it takes")
    step_count: int = Field(..., ge=1)
    stage_count: int = Field(..., ge=1)
    gate_count: int = Field(..., ge=0)
    queries_moved: int = Field(..., ge=0, description="In-scope queries it leaves off Aurora")
    queries_by_engine: dict[str, int] = Field(default_factory=dict)
    tables_migrate: int = Field(..., ge=0, description="Tables whose writer it moves off Aurora")
    monthly_cost_usd: float = Field(..., ge=0, description="Its engines' cost_breakdown, summed")
    cost_delta_vs_relational_usd: float = Field(...)
    cost_delta_vs_current_usd: float | None = Field(
        None, description="None when the current cost is not known"
    )
    cost_is_upper_bound: bool = Field(
        True,
        description="Aurora stays sized for the whole workload, so its cost is an upper bound",
    )
    critical_high_risks: int = Field(..., ge=0)
    replicated_tables: list[str] = Field(
        default_factory=list,
        description="The tables it reads through a CDC copy, either way, sorted (#1131 caps them)",
    )
    replicated_table_count: int = Field(..., ge=0)
    temporary_stream_count: int = Field(..., ge=0)
    first_gate: str | None = Field(None)

    @model_validator(mode="after")
    def _consistent(self) -> "AdoptionPathFacts":
        _unique(self.engines, "engines")
        step = _path_step(self.path_id)
        if step is not None and step != self.stops_after_step:
            raise ValueError(f"{self.path_id} stops after step {step}")
        if self.step_count != self.stops_after_step:
            raise ValueError("a path takes steps 1 to stops_after_step")
        if set(self.queries_by_engine) - set(self.engines):
            raise ValueError("queries_by_engine names only the path's engines")
        _count_matches(
            self.replicated_table_count, self.replicated_tables, "replicated_table_count"
        )
        if self.replicated_tables != sorted(set(self.replicated_tables)):
            raise ValueError("replicated_tables are sorted, each once")
        return self


class NotOfferedPath(BaseModel):
    """A path not offered, and why (#1130): its closure is offered instead when there is one."""

    path_id: str
    reason: Literal["no_relational_engine", "fails_relational_gates"]
    failing_query_ids: list[str] = Field(default_factory=list)
    failing_query_count: int = Field(0, ge=0)
    serving_engines: list[str] = Field(
        default_factory=list, description="The engines the failing queries need"
    )
    offered_instead: str | None = Field(None, description="The closure offered in its place")

    @model_validator(mode="after")
    def _counts(self) -> "NotOfferedPath":
        _path_step(self.path_id)
        _count_matches(self.failing_query_count, self.failing_query_ids, "failing_query_count")
        return self


class AdoptionFacts(BaseModel):
    """The menu of adoption paths and the one-step form of the full target (#1130, #473).
    Nothing ranks the paths: the staged full target is the default, as today."""

    default_path_id: str | None = Field(None, description="full_target, when paths exist")
    paths: list[AdoptionPathFacts] = Field(default_factory=list)
    path_count: int = Field(0, ge=0)
    not_offered: list[NotOfferedPath] = Field(default_factory=list)
    one_step: OneStepFacts | None = Field(None)

    @model_validator(mode="after")
    def _consistent(self) -> "AdoptionFacts":
        _count_matches(self.path_count, self.paths, "path_count")
        ids = [p.path_id for p in self.paths]
        _unique(ids, "path ids")
        if "full_target" in ids and ids[-1] != "full_target":
            raise ValueError("full_target goes last")
        if "relational_only" in ids and ids[0] != "relational_only":
            raise ValueError("relational_only goes first")
        stops = [p.stops_after_step for p in self.paths]
        if stops != sorted(set(stops)):
            raise ValueError("paths are listed by the step they stop after, one per step")
        if (self.default_path_id is None) != (not self.paths):
            raise ValueError("a default path is named exactly when paths exist")
        if self.default_path_id is not None and self.default_path_id not in ids:
            raise ValueError("the default path is an offered path")
        if set(ids) & {n.path_id for n in self.not_offered}:
            raise ValueError("a path is offered or not offered, not both")
        for n in self.not_offered:
            if n.offered_instead is not None and n.offered_instead not in ids:
                raise ValueError(f"{n.path_id}'s closure {n.offered_instead} is not offered")
        return self


def _moving_units(plan: SlicePlanFacts, steps: list[SliceStep]) -> list[tuple[str, MoveUnit]]:
    units = {u.key: u for u in plan.move_units}
    return [
        (stage.kind, units[k])
        for step in steps
        for stage in step.step_stages
        for k in stage.unit_keys
        if units[k].kind in MOVING_UNIT_KINDS
    ]


def _streams(plan: SlicePlanFacts, steps: list[SliceStep]) -> int:
    return sum(
        len(u.reverse_replicas)
        for kind, u in _moving_units(plan, steps)
        if kind == "with_reverse_replica"
    )


def _expected_path(plan: SlicePlanFacts, stop: int) -> dict[str, Any]:
    """Every figure of the path that stops after step ``stop``, from the plan alone."""
    steps = plan.plan_steps
    taken = steps[:stop]
    moving = [s for s in taken if s.kind in MOVING_STEP_KINDS]
    moved = sum(s.query_count for s in moving)
    by_engine: dict[str, int] = {}
    for s in moving:
        by_engine[s.engine] = by_engine.get(s.engine, 0) + s.query_count
    relational = steps[0] if steps and steps[0].kind == "relational_step" else None
    if relational is not None:
        by_engine[relational.engine] = relational.query_count - moved
    engines = list(dict.fromkeys(s.engine for s in taken))
    baseline = [s.engine for s in steps if s.kind in NO_MOVE_STEP_KINDS]
    try:
        cost = round(sum(plan.engine_monthly_cost_usd[e] for e in engines), 2)
        relational_cost = round(sum(plan.engine_monthly_cost_usd[e] for e in set(baseline)), 2)
        risks = sum(plan.engine_critical_high_risks[e] for e in engines)
    except KeyError as missing:
        raise ValueError(f"the plan states no cost or risks for {missing}") from None
    replicated = {
        t
        for _, u in _moving_units(plan, taken)
        for t in [
            *(r.table for r in u.replicated_in),
            *(t for r in u.reverse_replicas for t in r.tables),
        ]
    }
    return {
        "engines": set(engines),
        "stage_count": sum(s.stage_count for s in taken),
        "queries_moved": moved,
        "queries_by_engine": by_engine,
        "tables_migrate": sum(s.owned_table_count for s in moving),
        "monthly_cost_usd": cost,
        "cost_delta_vs_relational_usd": round(cost - relational_cost, 2),
        "critical_high_risks": risks,
        "replicated_tables": sorted(replicated),
        "temporary_stream_count": _streams(plan, taken),
    }


def check_adoption_against_plan(adoption: AdoptionFacts, plan: SlicePlanFacts) -> None:
    """The paths are the plan stopped after a step (#1130): every figure a path states is
    the one its steps 1 to k give, its id names where it stops, and the one-step form
    states the plan's own counts."""
    steps = plan.plan_steps
    lead = 0
    while lead < len(steps) and steps[lead].kind in NO_MOVE_STEP_KINDS:
        lead += 1
    for path in adoption.paths:
        if path.stops_after_step > len(steps):
            raise ValueError(f"{path.path_id} stops after a step the plan does not have")
        if path.path_id == "relational_only" and path.stops_after_step != lead:
            raise ValueError("relational_only stops after the steps that move no query")
        if path.path_id == "full_target" and path.stops_after_step != len(steps):
            raise ValueError("full_target takes every step")
        if path.path_id.startswith("through_step_") and not (
            lead < path.stops_after_step < len(steps)
        ):
            raise ValueError(f"{path.path_id} must stop between relational_only and full_target")
        expected = _expected_path(plan, path.stops_after_step)
        stated = path.model_dump()
        stated["engines"] = set(path.engines)
        for name, value in expected.items():
            if stated[name] != value:
                raise ValueError(
                    f"{path.path_id}: {name} is {stated[name]}, the steps give {value}"
                )
        if path.gate_count != path.stage_count:
            raise ValueError(f"{path.path_id}: every stage has one gate")
        if path.first_gate is not None and path.first_gate != steps[0].step_stages[0].gate:
            raise ValueError(f"{path.path_id}: first_gate is its first stage's gate")
    one = adoption.one_step
    if one is None:
        return
    stages = sum(s.stage_count for s in steps)
    if (one.staged_step_count, one.staged_stage_count, one.gate_count) != (
        len(steps),
        stages,
        stages,
    ):
        raise ValueError("the one-step form states the plan's step, stage and gate counts")
    if one.staged_temporary_stream_count != _streams(plan, steps):
        raise ValueError("the staged form's temporary streams are the plan's stage c streams")
    observed = {c.rule: c.observed for c in one.one_step_checks}
    expected_observed = {
        "no_open_critical_high_risk": sum(plan.engine_critical_high_risks.values()),
        "at_most_one_purpose_built_target": len(
            {s.engine for s in steps if s.kind in MOVING_STEP_KINDS}
        ),
        "no_reverse_replica_stage": sum(
            1 for s in steps for st in s.step_stages if st.kind == "with_reverse_replica"
        ),
    }
    if observed != expected_observed:
        raise ValueError("the one-step checks state the plan's own figures")


def check_plan_against_facts(
    plan: SlicePlanFacts,
    adoption: AdoptionFacts | None,
    engine_costs: dict[str, float | None],
    risks_by_engine: dict[str, int],
    no_migration_engines: list[str],
) -> None:
    """The plan's per-engine figures are the report's (``facts.engines``' costs and
    ``critical_high_risks_by_engine``), and the relational-only path ends on the engines
    that need no data migration (``facts.no_migration``)."""
    for engine, cost in plan.engine_monthly_cost_usd.items():
        if engine_costs.get(engine) != cost:
            raise ValueError(f"{engine}'s cost is {engine_costs.get(engine)} in facts.engines")
    for engine, count in plan.engine_critical_high_risks.items():
        if risks_by_engine.get(engine, 0) != count:
            raise ValueError(f"{engine}'s critical and high risks differ from the facts'")
    path = next(
        (p for p in (adoption.paths if adoption else []) if p.path_id == "relational_only"), None
    )
    if path is not None and set(path.engines) != set(no_migration_engines):
        raise ValueError("relational_only ends on facts.no_migration's engines")


class ReportFacts(BaseModel):
    """Every customer-facing figure, computed once at synthesis (#615, ADR-032).

    Built by ``src.shared.report_facts.compute_report_facts``; every
    deliverable reads it through ``src.report.facts.report_facts`` and only
    formats it.
    """

    projected_monthly_cost_usd: float = Field(
        0.0, ge=0, description="The headline projected monthly cost (#358)"
    )
    source_table_count: int | None = Field(None, ge=0)
    source_view_count: int | None = Field(
        None, ge=0, description="Views wave 1 carries on top of the tables (#578)"
    )
    engine_count: int = Field(..., ge=0)
    migration_wave_count: int = Field(
        0, ge=0, description="The 'N migration waves' figure: the roadmap's waves"
    )
    engines: list[EngineFacts] = Field(default_factory=list)
    tables_migrate: int = Field(..., ge=0, description='The "Tables migrate" figure')
    in_scope_query_count: int = Field(
        0, ge=0, description="In-scope queries the owner engines serve; the cache layer owns none"
    )
    moving_query_count: int = Field(
        0,
        ge=0,
        description=(
            "Of them, the queries on a migration target or a search read model: they leave "
            "the source engine (#1020)"
        ),
    )
    no_engine_in_scope_query_count: int = Field(
        0,
        ge=0,
        description=(
            "Queries no engine in the customer's engine scope can serve (#1329): in no "
            "engine's count, workload share or cost"
        ),
    )
    query_total_count: int | None = Field(
        None,
        ge=0,
        description=(
            "in_scope_query_count plus no_engine_in_scope_query_count: the denominator the "
            "headline names when some queries have no engine (#1329). None in a report "
            "written before it"
        ),
    )
    workload_total_percent: float = Field(0.0, ge=0)
    engine_cost_total_usd: float = Field(0.0, ge=0)
    cost_basis_notes: list[str] = Field(
        default_factory=list,
        description=(
            "How each priced engine's estimate billed its reads where that rests on an "
            "assumption (DocumentDB's buffer-cache basis, #1197); printed next to the cost"
        ),
    )
    table_basis: TableBasis
    waves: list[WaveFacts] = Field(default_factory=list)
    no_migration: NoMigrationFacts = Field(default_factory=NoMigrationFacts)
    removed_engines: list[RemovedEngine] = Field(default_factory=list)
    retired_in_use_engines: list[RetiredInUseEngine] = Field(
        default_factory=list,
        description=(
            "Removed engines the customer runs today (#1076), each stated once with its "
            "counted saving; a dropped candidate the source never ran is not here"
        ),
    )
    reality_check_trade_offs: list[RealityCheckTradeOff] = Field(default_factory=list)
    reality_check_decisions: list[RealityCheckDecision] = Field(default_factory=list)
    trade_off_topics: list[TradeOffTopicGroup] = Field(
        default_factory=list,
        description=(
            "The schema designs' trade-offs grouped by engine and topic (#1221), in the "
            "order the deliverables list them; the reality check's own lines are not here"
        ),
    )
    trade_off_count: int = Field(
        0, ge=0, description="Trade-offs that still apply (reversed reality-check lines excluded)"
    )
    open_risk_count: int = Field(0, ge=0)
    open_risks_by_severity: dict[str, int] = Field(default_factory=dict)
    critical_high_risk_count: int = Field(0, ge=0)
    critical_high_risks_by_engine: dict[str, int] = Field(default_factory=dict)
    critical_high_risk_types: list[str] = Field(
        default_factory=list, description="The distinct risk_type of the CRITICAL/HIGH risks"
    )
    critical_high_root_cause: str | None = Field(
        None, description="The one risk_type every CRITICAL/HIGH risk shares, if they share one"
    )
    open_risks_by_type: dict[str, int] = Field(default_factory=dict)
    resolved_risk_count: int = Field(0, ge=0)
    resolved_risks_by_severity: dict[str, int] = Field(
        default_factory=dict,
        description="The resolved risks by severity (#1020), as open_risks_by_severity",
    )
    tied_query_count: int = Field(0, ge=0)
    pending_choice_count: int = Field(
        0, ge=0, description="Pending engine choices, one per table (#1003)"
    )
    pending_choice_query_count: int = Field(
        0, ge=0, description="The tied queries those choices cover (#1003)"
    )
    pending_choice_pairs: list[PendingChoicePair] = Field(
        default_factory=list,
        description="The pending choices grouped by their tied engines (#1003), sorted",
    )
    auto_decided_choice_count: int = Field(
        0, ge=0, description="Choices --auto decided for the customer, still holding (#1003)"
    )
    auto_decided_query_count: int = Field(
        0, ge=0, description="The queries those decided choices placed (#1003)"
    )
    auto_decided_by_engine: list[AutoDecidedEngine] = Field(
        default_factory=list,
        description="The decided choices grouped by the engine --auto placed them on (#1003)",
    )
    unreviewed_move_count: int = Field(
        0,
        ge=0,
        description=(
            "Reality-check moves no model reviewed: the moved queries of every consolidation "
            "review batch that got no verdict (reality_check.validation_incomplete, #746)"
        ),
    )
    post_schema_reroute_count: int = Field(
        0, ge=0, description="Queries moved after approval (#629, post_schema_reroutes)"
    )
    source_of_truth_reroute_count: int = Field(
        0,
        ge=0,
        description=(
            "Of those, writes moved after approval to one source of truth per table (#992, "
            "reason_category source_of_truth)"
        ),
    )
    design_review_reroute_count: int = Field(
        0, ge=0, description="Of those, queries a design review's flag moved (#629)"
    )
    query_trace: QueryTraceFacts | None = Field(
        None,
        description=(
            "The per-query decision traces' figures (#339); None when the report has no "
            "query_traces"
        ),
    )
    close_calls: CloseCallFacts | None = Field(
        None,
        description=(
            "Queries whose runner-up is a kept engine less than the margin behind (#730); "
            "None when query_traces predates close_calls"
        ),
    )
    unservable: UnservableFacts | None = Field(
        None,
        description=(
            "Queries that landed on a fallback engine because no analyzed engine fit (#729); None "
            "when query_traces predates unservable"
        ),
    )
    cache_layer: CacheLayerFacts | None = Field(
        None,
        description=(
            "The cache layer's cache candidates and designed cached reads (#1252); None "
            "when the report has no cache_overlay"
        ),
    )
    busiest_partition_key: HotPartitionFacts | None = Field(
        None,
        description=(
            "The design entry whose estimated partition key value is the busiest share of "
            "its limit, over every entry (#1989): the one figure the hot-partition risk and "
            "the growth scenario state; None when none is measured"
        ),
    )
    assumptions: AssumptionsFacts | None = Field(
        None,
        description=(
            "The one assumptions block (#1990): growth, peak source, rate window, region and "
            "prices, each with its origin; None in a report rebuilt later, or written before"
        ),
    )
    growth_scenarios: GrowthScenarioFacts | None = Field(
        None,
        description=(
            "The cost and the busiest partition key value at 1×, 5× and 10× growth (#1990); "
            "None in a report rebuilt later, or written before"
        ),
    )
    hot_partitions: list[HotPartitionFacts] | None = Field(
        None,
        description=(
            "The DynamoDB design entries above the at-risk share of one partition key "
            "value's limit, each with its total as an upper bound and its estimated load "
            "per key value (#1989); None in a report rebuilt later, or written before"
        ),
    )
    rate_windows: RateWindowFacts | None = Field(
        None,
        description=(
            "How many collected statements' call rates used each window basis, stated in "
            "every deliverable (#1893); None in a report rebuilt without the collection, or "
            "written before"
        ),
    )
    row_counts: RowCountFacts | None = Field(
        None,
        description=(
            "The collection's row counts by basis and the sizing assumption for the unknown "
            "ones (#1636); None in a report rebuilt without the collection, or written before"
        ),
    )
    dynamodb_merge_conflicts: DynamoDBMergeConflictFacts | None = Field(
        None,
        description=(
            "The DynamoDB group merge's unresolved conflicts (#802): each failure with the "
            "access patterns and queries the merged design does not serve; None when the "
            "merge left none (and in a report written before it)"
        ),
    )
    dynamodb_merge_reviews: DynamoDBMergeReviewFacts | None = Field(
        None,
        description=(
            "The DynamoDB merge-review decisions (#1780): per overlap, the authoritative "
            "home the merge chose and its derived copies; None when the merge chose none "
            "(and in a report written before it)"
        ),
    )
    derived_copy_sync: DerivedCopySyncFacts | None = Field(
        None,
        description=(
            "The stream sync of the DynamoDB derived copies (#1527): each copy's source "
            "table, authoritative home and replayed writes, with the sync's cost; None "
            "when the design has no derived copy (and in a report written before it)"
        ),
    )
    access_pattern_totals: AccessPatternFacts | None = Field(
        None,
        description=(
            "The access-pattern totals the results page shows (#792); None when the report "
            "has no query_groups"
        ),
    )
    query_group_counts: QueryGroupFacts | None = Field(
        None,
        description=(
            "The query groups the engineering report lists and the relational-only rest "
            "(#1410); None when the report has no query_groups"
        ),
    )
    table_sources: TableSourceFacts | None = Field(
        None,
        description=(
            "Each written table's source of truth, replicas, CDC direction and "
            "eventual-consistency risk (#992); None when the report has no table_sources"
        ),
    )
    table_owner_lines: TableOwnershipFacts | None = Field(
        None,
        description=(
            "Each mapped table's owner, the engines that also read it and its cache (#212); "
            "None when the report has no table_mappings"
        ),
    )
    relational_gaps: RelationalGapFacts | None = Field(
        None,
        description=(
            "The relational move's version and feature gaps against its Aurora target (#471); "
            "None when there is no Aurora target, no collection, or the report predates 1.35"
        ),
    )
    routines: RoutineFacts | None = Field(
        None,
        description=(
            "How the job's routines migrate (#1938): the headline counts and the routines "
            "behind the routine risks; None when the job has no routine-migration artifact "
            "(no routines, or a report written before it)"
        ),
    )
    query_pattern_counts: QueryPatternFacts | None = Field(
        None,
        description=(
            "The queries and the distinct query patterns they make (#1146); None when the report has "
            "no query_pattern_groups (written before #1146)"
        ),
    )
    engine_scope: list[str] | None = Field(
        None,
        description=(
            "The customer's engine scope (#1328, the target_databases setting triage "
            "recorded), engine ids in the order the deliverables name them (by display "
            "name); every deliverable states 'Engines in scope: ... (customer's choice)'. "
            "None for a job with no scope, and for a report written before"
        ),
    )
    cost_assumptions: list[str] = Field(
        default_factory=list,
        description=(
            "The TCO's assumption lines (tco_analysis.assumptions), as every deliverable's "
            "cost section states them (#1195)"
        ),
    )
    cost_scope_label: str | None = Field(
        None,
        description=(
            "What every cost figure is: the full-modernization estimate, all waves done "
            "(#1474); None for a report written before it"
        ),
    )
    cost_caveat: str | None = Field(
        None,
        description=(
            "The one caveat every deliverable prints where costs appear: public on-demand "
            "list prices, to check against the customer's own pricing (#1474); None for a "
            "report written before it"
        ),
    )
    cutover_responsibility: str | None = Field(
        None,
        description=(
            "The one sentence every deliverable's wave plan says about the cutover: it is "
            "the customer's (#2107); None for a report written before it"
        ),
    )
    price_list: PriceListFacts | None = Field(
        None,
        description=(
            "The source, region and date of the list prices behind every cost figure "
            "(#1196); None for a report written before it"
        ),
    )
    executive_brief: ExecutiveBriefFacts | None = Field(
        None,
        description=(
            "What the executive summary states beyond the headline figures: the risk "
            "level and its drivers, the wave to approve (#1403); None for a report "
            "written before it"
        ),
    )
    wave_readiness_notes: list[WaveReadinessNote] | None = Field(
        None,
        description=(
            "Each wave the plan does not suggest (held, not designed or not reviewed), with "
            "its readiness line, as the chat summary lists them (#1992); None for a report "
            "written before the check"
        ),
    )
    slice_plan: SlicePlanFacts | None = Field(
        None,
        description=(
            "The steps, their ordered stages and move units, and the dependency graph "
            "behind their order (#1129, #1698; ADR-033's slice_plan, keyed by step_number); "
            "None for a report written before it"
        ),
    )
    adoption: AdoptionFacts | None = Field(
        None,
        description=(
            "The adoption paths, those not offered and the one-step form of the full "
            "target (#1130, #473); None for a report written before it"
        ),
    )

    @model_validator(mode="after")
    def _adoption_follows_the_plan(self) -> "ReportFacts":
        if self.adoption is not None and self.slice_plan is not None:
            check_adoption_against_plan(self.adoption, self.slice_plan)
        if self.slice_plan is not None and self.slice_plan.plan_steps:
            check_plan_against_facts(
                self.slice_plan,
                self.adoption,
                {e.engine: e.monthly_cost_usd for e in self.engines},
                self.critical_high_risks_by_engine,
                self.no_migration.engines,
            )
        return self


class CappedList(BaseModel):
    """One list path the executive-summary request's token budget cut (#762)."""

    path: str = Field(
        ..., description="Payload path, e.g. query_groups or migration_waves[].tables"
    )
    lists: int = Field(..., ge=1, description="How many lists at this path were cut")
    items_total: int = Field(..., ge=0, description="Their items before the cut")
    items_kept: int = Field(..., ge=0, description="Their items after the cut")
    omitted_query_count: int | None = Field(
        None,
        ge=0,
        description="query_groups only: the queries the dropped groups carry (sum of query_count)",
    )


class SummaryInputBudget(BaseModel):
    """The executive-summary request's token budget, when it had to cut (#762)."""

    budget_tokens: int = Field(..., ge=0, description="The request's token budget")
    estimated_tokens_uncapped: int = Field(
        ..., ge=0, description="Estimated tokens of the request before any cut"
    )
    estimated_tokens: int = Field(..., ge=0, description="Estimated tokens after the cut")
    lines: int = Field(..., ge=0, description="Lines of the cut request as headless reads it")
    list_limit: int = Field(..., ge=0, description="Items kept per cut list")
    within_budget: bool = Field(..., description="Whether the cut request fits the budget")
    capped_lists: list[CappedList] = Field(default_factory=list)


class SynthesisOutputContract(BaseModel):
    """Output contract for the synthesis agent.

    The final modernization report combining all pipeline artifacts into
    a comprehensive assessment with architecture, TCO, and risk analysis.

    Consumed by:
    - The UI (results page — architecture view, query groups, table mappings)
    - Step Functions (needs_deeper_analysis flag for the analysis loop)
    """

    contract_version: str = Field(
        default="1.97",
        pattern=r"^\d+\.\d+$",
        description="Contract version (MAJOR.MINOR format)",
    )
    job_id: str = Field(..., description="Job identifier")
    database_name: str = Field(..., description="Source database name")
    agent_type: str = Field(default="referee-synthesis", description="Agent identifier")
    status: str = Field(default="completed", description="Pipeline status")
    timestamp: datetime = Field(..., description="When synthesis was run")
    needs_deeper_analysis: bool = Field(
        default=False, description="Whether any engine needs further analysis"
    )
    ranking: list[EngineRanking] = Field(
        ..., description="Engines by workload share, owners first, then the cache layer (#152)"
    )
    summary: str = Field(..., description="Executive summary text")
    summary_deterministic: str = Field(..., description="Deterministic summary (no LLM)")
    summary_source: str = Field(
        default="deterministic",
        description=(
            "Which text ``summary`` holds: llm, deterministic, or deterministic_fallback "
            "(the LLM summary failed the table-assignment post-check)"
        ),
    )
    summary_llm: str | None = Field(
        None, description="LLM-written summary as received, kept for audit even when rejected"
    )
    summary_validation_warnings: list[str] = Field(
        default_factory=list,
        description="Post-check findings: summary sentences naming a table under the wrong engine",
    )
    prose_style_rejections: list[str] = Field(
        default_factory=list,
        description=(
            "Model-written schema-design fields replaced by deterministic text because "
            "they used an item of the plain-prose style list (#1034). Legacy: written to "
            "report.json up to 1.41; since then it lives in the job's _logs (#1211)"
        ),
    )
    recommended_architecture: dict[str, Any] = Field(
        ..., description="Architecture recommendation with type, databases, integrations"
    )
    table_mappings: list[TableMapping] = Field(
        ..., description="Source table to target engine mappings"
    )
    query_groups: list[QueryGroup] = Field(..., description="Queries grouped by access pattern")
    tco_analysis: TCOAnalysis = Field(..., description="Total cost of ownership analysis")
    risk_assessment: RiskAssessment = Field(..., description="Migration risk assessment")
    schema_designs: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Per-engine schema design summaries; a summary's optional "
            "shared_sentence_groups records the sentences sibling entries share (#2165)"
        ),
    )
    trade_offs: list[TradeOff] = Field(default_factory=list, description="Collected trade-offs")
    assignment_summary: AssignmentSummary | None = Field(
        None, description="Summary of the assignment version used"
    )
    cache_overlay: dict[str, Any] | None = Field(
        None,
        description=(
            "Cache layer view (#296): engine, query_count, calls_per_second, "
            "call_share_percent, owners, patterns, dropped_query_ids and notes. The cache "
            "owns no query, so it is never part of the owner workload share."
        ),
    )
    engines_in_use: list[str] | None = Field(
        None,
        description=(
            "Engines the source runs today (#1076): its own engine and those the collection "
            "declares in metadata.engines_in_use. None for a report synthesized before this field existed."
        ),
    )
    considered_not_recommended: list[ConsideredEngine] | None = Field(
        None,
        description=(
            "Candidate engines the plan analysed and then dropped (#949), each with the "
            "engine that absorbed its queries. The source does not run them, so dropping "
            "one saves nothing; no dollar figure is attached. None for a report "
            "synthesized before this field existed."
        ),
    )
    migration_waves: list[MigrationWave] | None = Field(
        None,
        description=(
            "Incremental migration roadmap (#225), computed deterministically from the "
            "assignment. None for a report synthesized before this field existed; every "
            "deliverable then falls back to deriving the same shape itself."
        ),
    )
    unresolved_names: UnresolvedNames | None = Field(
        None,
        description=(
            "Count and names of source_tables entries that did not resolve to a table "
            "or view the collector saw -- a CTE alias, a system catalog, a sequence, a "
            "keyword, a column the SQL parser mistook for a table (#316). Copied "
            "unchanged from the assignment's own unresolved_table_names; None for a "
            "report synthesized before this field existed."
        ),
    )
    source_table_count: int | None = Field(
        None,
        ge=0,
        description=(
            "The collected schema's own tables-only total (no views, #530 review): the "
            "same figure summary_deterministic's 'Analyzed N source tables ...' opening "
            "sentence already states, as a structured field. None for a report "
            "synthesized before this field existed."
        ),
    )
    tied_queries: list[TiedQuery] | None = Field(
        None,
        description=(
            "Queries an exact score tie left unresolved (#503), copied from the final "
            "assignment's own QueryAssignment.tied_engines "
            "so the decision report and the engineering report can list the same "
            "queries the approval gate does. Empty list when none; None for a report "
            "synthesized before this field existed."
        ),
    )
    query_traces: QueryTraceDigest | None = Field(
        None,
        description=(
            "Per-query decision traces from the move ledger (#339): in full for the "
            "queries whose engine changed after their first assignment, that were left "
            "tied or were overridden; the rest counted by their first rule. None for a "
            "report synthesized before this field existed."
        ),
    )
    facts: ReportFacts | None = Field(
        None,
        description=(
            "Every customer-facing figure, computed once (#615, ADR-032); the "
            "deliverables only format it. None for a report synthesized before this "
            "field existed."
        ),
    )

    routine_followers: list[RoutineFollower] = Field(
        default_factory=list,
        description=(
            "The SQL Server statements on temporary objects that follow the routine they "
            "run in (#1891): each on the engine its routine's other statements are on, not "
            "scored on its own. Empty when none does (a collection that names no routine)."
        ),
    )
    routine_units: list[RoutineUnitFact] = Field(
        default_factory=list,
        description=(
            "Each placement unit a procedure or trigger merged (#1654): which routines "
            "merged it, its statements, tables and engine. Empty when none did (a "
            "collection that names no routine)."
        ),
    )
    table_sources: list[TableSource] | None = Field(
        None,
        description=(
            "Each written table's source of truth, writes and replicas (#992, ADR-031 "
            "Amendment 9), from the effective assignment's write_groups. None for a report "
            "synthesized before this field existed."
        ),
    )
    query_pattern_groups: list[QueryPatternGroup] | None = Field(
        None,
        description=(
            "The in-scope queries grouped into distinct query patterns, each query under its pattern "
            "(#1146); routing never reads it. None for a report synthesized before #1146."
        ),
    )
    pending_engine_choices: list[PendingEngineChoice] | None = Field(
        None,
        description=(
            "The open ties grouped per table (#1003): one customer choice per table, its "
            "queries listed under it. Empty list when none; None for a report synthesized "
            "before this field existed."
        ),
    )
    auto_decided_choices: list[AutoDecidedChoice] | None = Field(
        None,
        description=(
            "Pending engine choices --auto decided for the customer (#1003) that still "
            "hold on the effective assignment: decided automatically, review if needed. "
            "Empty list when none; None for a report synthesized before this field existed."
        ),
    )
    flagged_unservable: list[FlaggedUnservableQuery] = Field(
        default_factory=list,
        description=(
            "Flagged queries no kept engine can take (#888): a reality-check flag it "
            "kept and a post-schema router terminal query, by engine and query id"
        ),
    )
    scope_unservable: list[ScopeUnservableQuery] = Field(
        default_factory=list,
        description=(
            "Queries no engine in the customer's engine scope can serve (#1329), from the "
            "effective assignment's unservable_queries, by query id. Empty with no scope "
            "and in a report written before them"
        ),
    )
    carried_writes: list[CarriedWrite] | None = Field(
        None,
        description=(
            "The write-group members of the effective assignment their engine serves "
            "poorly (#1362), each with its intent and the target-native approach, as the "
            "design.carried_writes risk counts them; the detailed engineering report lists "
            "them under their write group (#1364). Empty list when none; None for a report "
            "synthesized before this field existed."
        ),
    )
    wave_warning_decisions: list[WaveWarningDecision] | None = Field(
        None,
        description=(
            "The assignment version's /wave-edit decisions (#2067): which design review "
            "warnings the customer accepted per wave, in the order recorded; None with "
            "none, and for a report written before them"
        ),
    )
    wave_redesign: WaveRedesignLog | None = Field(
        None,
        description=(
            "The bounded redesign rounds of held waves (#1992): each round's engines, the "
            "failed checks sent back and each outcome; None with no round, and for a "
            "report written before them"
        ),
    )
    post_schema_reroutes: list[PostSchemaReroute] | None = Field(
        None,
        description=(
            "Queries the post-schema router moved after the customer approved the "
            "assignment (#629), copied from Assignment.post_schema_reroutes. Empty "
            "list when none; None for a report synthesized before this field existed."
        ),
    )
    summary_input_budget: SummaryInputBudget | None = Field(
        None,
        description=(
            "Set only when the executive-summary request was over its token budget and "
            "its long lists were cut to fit (#762); None otherwise."
        ),
    )

    model_config = ConfigDict(extra="allow")
