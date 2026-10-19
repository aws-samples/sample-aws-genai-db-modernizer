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
  groups under ``"Table not identified by the collector"``, or under
  ``"Utility and session statements"`` when it is a real utility/session
  statement (``utility_statements.is_utility_statement``, already routed to
  the relational engine by the assignment: SHOW/SET/EXPLAIN, catalog
  introspection) -- never a literal "unknown". Every consumer that walks
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
  cache layer"); each wave's served, moving and kept-in-sync tables, moving
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
  way the wave plan does (owner outside the wave: kept in sync; owner in the same
  wave: moves to that owner). ``TableBasis.mapped_tables_by_move`` is the Migration
  map heading's split (migrate, kept in sync, cache layer, search read model, stays),
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
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.contracts.assignment_models import (  # re-exported, see docstring below
    AuroraEngineChoice,
    AutoDecidedChoice,
    PendingEngineChoice,
    PostSchemaReroute,
    UnresolvedNames,
)

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
    """Mapping of a source table to its target engine."""

    source_table: str = Field(..., description="Source table ID")
    recommended_database: str = Field(..., description="Primary target engine")
    confidence_score: int = Field(..., ge=0, le=100)
    alternatives: list[dict] | None = Field(None)

    model_config = ConfigDict(extra="allow")


class QueryGroup(BaseModel):
    """Group of queries organized by access pattern."""

    group_name: str = Field(..., description="Human-readable group name")
    engines: list[str] = Field(default_factory=list, description="Engines serving this group")
    access_patterns: list[dict] = Field(
        default_factory=list, description="Access patterns in this group"
    )

    model_config = ConfigDict(extra="allow")


class CostBreakdown(BaseModel):
    """Cost breakdown per engine."""

    database: str = Field(..., description="Engine name")
    monthly_cost_usd: float = Field(..., ge=0)

    model_config = ConfigDict(extra="allow")


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
    cost_breakdown: list[CostBreakdown] | None = Field(None)
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

    model_config = ConfigDict(extra="allow")


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
    kind: Literal["co_dependency", "independent"] | None = Field(
        None,
        description=(
            "Whether this group came from a co-dependency group or is the remainder. "
            "None for a wave written before this field existed."
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
    homogeneity: Literal["homogeneous", "cross_engine", "heterogeneous"] | None = Field(
        None,
        description=(
            "Wave 1 (Aurora) only (#321): 'homogeneous' when the source maps to a "
            "source-compatible Aurora engine, 'cross_engine' (#381) when the source has "
            "no Aurora dialect of its own but the assignment resolver still picked one of "
            "the two competing Aurora engines (named, with a share, same as 'homogeneous', "
            "but stated as a dialect-and-schema conversion, not a 1:1 carry-over), and "
            "'heterogeneous' when no Aurora engine was picked at all. A heterogeneous wave "
            "1 still runs -- it is not skipped -- it just names no Aurora engine (engines "
            "is then empty, the title says what the source moves off instead of to, and "
            "the gate flags it as a risk). None for every other wave, and for a wave "
            "written before this field existed"
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
            "migrates; a served-but-not-owned table stays on its real owner, kept in "
            "sync. None for a single-engine wave (derive it there as "
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
            "its migration wave owns, kept in sync from that owner (the wave plan's rule, "
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
    rationale: str = Field("", description="Why the engine is in the target (#152)")
    signal_overrides: list[str] = Field(
        default_factory=list,
        description="Triage signals that overrode queries onto this engine (raw ids, #584)",
    )


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
    served_tables: int = Field(..., ge=0, description="The wave's table_count")
    moving_tables: int | None = Field(
        None,
        ge=0,
        description=(
            "Tables one of the wave's engines durably owns, the sum of "
            "owned_counts_by_engine for the merged wave (#581); for a read-model wave, "
            "the served tables not kept in sync from an owner. None for wave 1"
        ),
    )
    kept_in_sync_tables: int | None = Field(
        None,
        ge=0,
        description="Tables owned outside the wave and kept in sync from it. None for wave 1",
    )
    moving_by_engine: dict[str, int] | None = Field(None)
    served_by_engine: dict[str, int] | None = Field(None)
    kept_in_sync: list[TableOwner] = Field(
        default_factory=list,
        description="One entry per kept-in-sync table, naming its owner outside the wave",
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
    engine: str = Field(..., description="The engine it ends on (id)")
    kind: str = Field(..., description="fallback, gated_everywhere, flagged (#888) or held (#1033)")
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


class ReplicaRead(BaseModel):
    """One replica of a written table and the reads it serves (#992)."""

    engine: str = Field(..., description="The replica engine (id)")
    read_query_ids: list[str] = Field(
        default_factory=list, description="The table's reads this engine serves, sorted"
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
    source_of_truth: str = Field(..., description="The engine its writes land on (id)")
    replica_engines: list[str] = Field(
        default_factory=list, description="The replica engines (ids); CDC runs to each"
    )
    write_query_count: int = Field(0, ge=0)
    replica_read_query_ids: list[str] = Field(
        default_factory=list, description="The reads a replica serves, sorted"
    )
    replica_read_count: int = Field(0, ge=0, description="How many reads a replica serves")
    risk: str = Field("", description="The eventual-consistency risk, naming those reads")


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

    kind: Literal["column", "view", "procedure", "function", "trigger"]
    id: str = Field(..., description="The object id; a column is <table_id>.<column_name>")


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
        default_factory=list, description="Columns, views, routines and triggers it was found in"
    )


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
    gaps: list[RelationalGap] = Field(
        default_factory=list, description="Most severe impact first, then by rule id"
    )
    gaps_by_impact: dict[str, int] = Field(
        default_factory=dict, description="Gaps per impact, every impact listed"
    )


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
    workload_total_percent: float = Field(0.0, ge=0)
    engine_cost_total_usd: float = Field(0.0, ge=0)
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
    access_pattern_totals: AccessPatternFacts | None = Field(
        None,
        description=(
            "The access-pattern totals the results page shows (#792); None when the report "
            "has no query_groups"
        ),
    )
    table_sources: TableSourceFacts | None = Field(
        None,
        description=(
            "Each written table's source of truth, replicas, CDC direction and "
            "eventual-consistency risk (#992); None when the report has no table_sources"
        ),
    )
    relational_gaps: RelationalGapFacts | None = Field(
        None,
        description=(
            "The relational move's version and feature gaps against its Aurora target (#471); "
            "None when there is no Aurora target, no collection, or the report predates 1.35"
        ),
    )


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
        default="1.45",
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
            "they used an item of the plain-prose style list (#1034); audit only"
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
        default_factory=dict, description="Per-engine schema design summaries"
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

    table_sources: list[TableSource] | None = Field(
        None,
        description=(
            "Each written table's source of truth, writes and replicas (#992, ADR-031 "
            "Amendment 9), from the effective assignment's write_groups. None for a report "
            "synthesized before this field existed."
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
