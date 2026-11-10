// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// #615 (ADR-032): the one reader of report.json's `facts` block on the UI side.
// Synthesis computes every customer-facing figure once
// (src/shared/report_facts.py); the UI only formats it. The /results API fills
// `facts` in for a report synthesized before it existed (src/api/routes/results.py).

export function waveFacts(synthesis, wave) {
  const rows = (synthesis && synthesis.facts && synthesis.facts.waves) || [];
  return rows.find(r => r.wave === wave.wave) || null;
}

// The open-risk count every deliverable prints (facts.open_risk_count).
export function openRiskCountOf(synthesis, shownRisks) {
  const n = synthesis && synthesis.facts && synthesis.facts.open_risk_count;
  return typeof n === 'number' ? n : (shownRisks || []).length;
}

// #748: the other counts the deliverables print. Each reads the stored fact;
// the shown list is only the answer for a hand-built report with no `facts`
// block (the /results API fills one in for a real report).
function factsOf(synthesis) {
  return (synthesis && synthesis.facts) || null;
}

// "CRITICAL (3)": the open risks at one severity (facts.open_risks_by_severity
// omits a severity with none).
export function openRiskCountAt(synthesis, severity, shownAtSeverity) {
  const bySeverity = factsOf(synthesis) && factsOf(synthesis).open_risks_by_severity;
  if (bySeverity && typeof bySeverity === 'object') return bySeverity[severity] || 0;
  return (shownAtSeverity || []).length;
}

// "3 resolved by the assignment" (facts.resolved_risk_count).
export function resolvedRiskCountOf(synthesis, shownRisks) {
  const n = factsOf(synthesis) && factsOf(synthesis).resolved_risk_count;
  return typeof n === 'number' ? n : (shownRisks || []).length;
}

// #1020: "2 high, 4 medium", the resolved risks by severity
// (facts.resolved_risks_by_severity, every severity it lists, in its order: it omits
// a severity with none). Empty when the facts block has no such map.
export function resolvedRisksBySeverityText(synthesis, t) {
  const bySeverity = factsOf(synthesis) && factsOf(synthesis).resolved_risks_by_severity;
  if (!bySeverity || typeof bySeverity !== 'object') return '';
  return Object.entries(bySeverity)
    .map(([severity, count]) => t('analysis-results.risk-assessment.severity-count', { count, severity: severity.toLowerCase() }))
    .join(', ');
}

// #1876: the procedure-internal statements (facts.query_group_counts.
// procedure_internal_queries); 0 when the facts block has none.
export function procedureInternalQueryCountOf(synthesis) {
  const counts = factsOf(synthesis) && factsOf(synthesis).query_group_counts;
  const n = counts && counts.procedure_internal_queries;
  return typeof n === 'number' ? n : 0;
}

// #1891: the statements on temporary objects that follow their routine, per engine id
// (facts.query_group_counts.procedure_internal_routine_counts); {} when the facts block
// has none.
export function procedureInternalRoutineCountsOf(synthesis) {
  const counts = factsOf(synthesis) && factsOf(synthesis).query_group_counts;
  const byEngine = counts && counts.procedure_internal_routine_counts;
  return byEngine && typeof byEngine === 'object' ? byEngine : {};
}

// #1891: one engine's count of them; 0 when it has none.
export function procedureInternalRoutineCountOn(synthesis, engine) {
  const n = procedureInternalRoutineCountsOf(synthesis)[engine];
  return typeof n === 'number' ? n : 0;
}

// "Trade-offs (12)": the trade-offs that still apply (facts.trade_off_count).
export function tradeOffCountOf(synthesis, shownTradeOffs) {
  const n = factsOf(synthesis) && factsOf(synthesis).trade_off_count;
  return typeof n === 'number' ? n : (shownTradeOffs || []).length;
}

// "Table Mappings (22)" (facts.table_basis.mapped_tables).
export function mappedTableCountOf(synthesis, shownMappings) {
  const basis = factsOf(synthesis) && factsOf(synthesis).table_basis;
  return basis && typeof basis.mapped_tables === 'number' ? basis.mapped_tables : (shownMappings || []).length;
}

// #1248: "Aurora MySQL · 20 tables in its design": the tables in one engine's
// schema design (facts.engines[].target_objects), a different set from the
// wave plan's "N source tables and views" and "the other N stay unchanged".
export function designTableCountOf(synthesis, engine, shownTables) {
  const rows = (factsOf(synthesis) && factsOf(synthesis).engines) || [];
  const row = Array.isArray(rows) ? rows.find(r => r && r.engine === engine) : null;
  return row && typeof row.target_objects === 'number' ? row.target_objects : (shownTables || []).length;
}

// #595: the reality-check trade-offs that still apply, shaped like a schema
// design's trade-off ({description, impact, query_ids}) so the trade-offs tab
// lists them next to the engines' own. A report synthesized before
// `facts.reality_check_decisions` existed lists its reality-check entries as-is.
export function realityCheckDecisions(synthesis) {
  const facts = synthesis && synthesis.facts;
  if (facts && Array.isArray(facts.reality_check_decisions)) {
    return facts.reality_check_decisions.map(d => ({
      description: d.title,
      impact: d.detail || '',
      source_tables: [],
      target_tables: [],
      query_ids: d.query_ids || [],
    }));
  }
  return ((synthesis && synthesis.trade_offs) || [])
    .filter(t => t && t.engine === 'reality-check')
    .map(t => ({ ...t, impact: t.impact === t.description ? '' : (t.impact || '') }));
}

// The report's trade-offs without the reality-check lines a later assignment
// edit reversed (#546): those no longer describe the architecture. A line an
// edit undid only in part reads as the facts layer restates it, for what
// stayed moved (#861, `description_now`).
export function currentTradeOffs(synthesis) {
  const all = (synthesis && synthesis.trade_offs) || [];
  const rc = (synthesis && synthesis.facts && synthesis.facts.reality_check_trade_offs) || [];
  const reversed = new Set(rc.filter(t => t.reversed).map(t => t.index));
  const restated = new Map(rc.filter(t => t.description_now && !t.reversed).map(t => [t.index, t.description_now]));
  return all
    .map((t, i) => (restated.has(i) ? { ...t, description: restated.get(i), impact: restated.get(i) } : t))
    .filter((t, i) => !reversed.has(i));
}

// The results page's "Trade-offs and design decisions" tabs (ADR-032 Decision 7):
// each engine's own trade-offs, plus a "reality-check" tab for the decisions the
// reality check made. An empty list gets no tab.
export const REALITY_CHECK_TAB = 'reality-check';

// #1574: an engine's tab lists the report's own trade-offs for it when synthesis wrote
// them (each long sentence stated once across the design's parts, as every deliverable
// states them); a design with none in the report keeps its own list.
export function tradeOffsByTab(designTradeOffs, synthesis) {
  const map = { ...designTradeOffs };
  const reported = (synthesis && synthesis.trade_offs) || [];
  Object.keys(map).forEach(engine => {
    const own = reported.filter(t => t && t.engine === engine);
    if (own.length > 0) map[engine] = own;
  });
  const rc = realityCheckDecisions(synthesis || {});
  if (rc.length > 0) map[REALITY_CHECK_TAB] = rc;
  return map;
}

// #1221: an engine tab's design trade-offs by topic (facts.trade_off_topics), each
// group's label, count and trade-offs (report.json's own, by position), the design
// review notes apart with their label and count (the schema agents' warnings stay a
// topic group, listed with the design decisions). null when the facts list no group for the
// engine (the reality-check tab, a report before the groups): the tab lists its
// trade-offs as before.
export const REVIEW_NOTE_TOPIC = 'review_note';

export function tradeOffTopicGroupsOf(synthesis, engine) {
  const groups = factsOf(synthesis) && factsOf(synthesis).trade_off_topics;
  if (!Array.isArray(groups)) return null;
  const mine = groups.filter(g => g && g.engine === engine);
  if (mine.length === 0) return null;
  const rows = (synthesis && synthesis.trade_offs) || [];
  const entries = g => (g.indexes || []).map(i => rows[i]).filter(t => t && typeof t === 'object');
  const notes = mine.find(g => g.topic === REVIEW_NOTE_TOPIC);
  return {
    topics: mine
      .filter(g => g.topic !== REVIEW_NOTE_TOPIC)
      .map(g => ({ topic: g.topic, label: g.label, count: g.trade_off_count, tradeOffs: entries(g) })),
    reviewNotes: notes ? entries(notes) : [],
    reviewNoteCount: notes ? notes.trade_off_count : 0,
    reviewNoteLabel: notes ? notes.label : '',
  };
}

export function tradeOffTabLabel(tab, count, t, engineLabels) {
  const name = tab === REALITY_CHECK_TAB
    ? t('analysis-results.tradeoffs.reality-check')
    : (engineLabels[tab] || tab);
  return `${name} (${count})`;
}

// #822: why each listed query's routing has a story (facts.query_trace's
// listed_queries: moved, tied, an open tie, a customer or design-review pick,
// a pinning rule), by query id. A query the facts do not list stayed where its
// first assignment put it. Empty for a report without the facts block.
export function routingKindsById(synthesis) {
  const qt = factsOf(synthesis) && factsOf(synthesis).query_trace;
  const listed = (qt && qt.listed_queries) || [];
  return new Map(listed.filter(q => q && q.query_id).map(q => [q.query_id, {
    moved: Boolean(q.moved),
    tied: Boolean(q.tied),
    openTie: Boolean(q.open_tie),
    overridden: Boolean(q.overridden),
    pinned: Boolean(q.pinned),
  }]));
}

// Whether the report records each query's routing steps (the move ledger):
// facts.query_trace.has_ledger. Without it the routing table claims nothing.
export function routingHistoryRecorded(synthesis) {
  const qt = factsOf(synthesis) && factsOf(synthesis).query_trace;
  return Boolean(qt && qt.has_ledger);
}

// #730: the queries an engine won by less than the close-call margin over
// another engine the plan keeps (facts.close_calls), or null when there are
// none or the report predates them. Every figure is the facts layer's.
export function closeCallsOf(synthesis) {
  const cc = factsOf(synthesis) && factsOf(synthesis).close_calls;
  if (!cc || !Array.isArray(cc.engine_pairs) || cc.engine_pairs.length === 0) return null;
  return cc;
}

// #729: the queries that landed on a fallback engine because no analyzed engine fit
// (facts.unservable), or null when there are none or the report predates them.
export function unservableOf(synthesis) {
  const u = factsOf(synthesis) && factsOf(synthesis).unservable;
  if (!u || !Array.isArray(u.unservable_queries) || u.unservable_queries.length === 0) return null;
  return u;
}

// #802: the DynamoDB group merge's unresolved conflicts (facts.dynamodb_merge_conflicts),
// each failure with the access patterns and queries the merged design does not serve,
// or null when the merge left none and for a report written before the fact.
export function dynamodbMergeConflictsOf(synthesis) {
  const f = factsOf(synthesis) && factsOf(synthesis).dynamodb_merge_conflicts;
  if (!f || !Array.isArray(f.conflicts) || f.conflicts.length === 0) return null;
  return f;
}

// #1780: the DynamoDB merge-review decisions (facts.dynamodb_merge_reviews): each source
// table's authoritative home and derived copies, with the sentences every deliverable
// states, or null when the merge chose none and for a report written before the fact.
export function dynamodbMergeReviewsOf(synthesis) {
  const f = factsOf(synthesis) && factsOf(synthesis).dynamodb_merge_reviews;
  if (!f || !Array.isArray(f.decisions) || f.decisions.length === 0) return null;
  return f;
}

// #992: each written table's source of truth and the replicas serving its reads
// (facts.table_sources), or null when no query writes a table or the report
// predates them. Every figure is the facts layer's.
export function tableSourcesOf(synthesis) {
  const ts = factsOf(synthesis) && factsOf(synthesis).table_sources;
  if (!ts || !ts.written_table_count) return null;
  return ts;
}

// #212: each mapped table's owner, the engines that also read it and its cache
// (facts.table_owner_lines), or null when the report maps no table. Every figure
// is the facts layer's; the /results API fills it in for an older report.
export function tableOwnershipOf(synthesis) {
  const o = factsOf(synthesis) && factsOf(synthesis).table_owner_lines;
  if (!o || !o.table_count) return null;
  return o;
}

// #792: the access-pattern totals the results page shows
// (facts.access_pattern_totals: the patterns the owner engines serve, the cache
// layer's own apart). The explorer's live filter counts stay presentation. A
// hand-built report with no such fact returns the shown rows' own totals
// (`shownOwned`, `shownCacheLayer`); the /results API fills the fact in for a
// real report.
export function accessPatternTotals(synthesis, shownOwned, shownCacheLayer) {
  const totals = factsOf(synthesis) && factsOf(synthesis).access_pattern_totals;
  if (totals && typeof totals.owned_pattern_count === 'number') {
    return {
      owned: totals.owned_pattern_count,
      cacheLayer: totals.cache_layer_pattern_count || 0,
      listed: totals.listed_pattern_count || 0,
    };
  }
  return { owned: shownOwned || 0, cacheLayer: shownCacheLayer || 0, listed: (shownOwned || 0) + (shownCacheLayer || 0) };
}

// #1252: the cache layer's read figures, each labelled for what it counts
// (facts.cache_layer): the cache candidates routing put under the overlay and the
// designed cached reads the cache engine's design covers. Null for a report with no
// cache overlay. Shaped like a cache_overlay so formatCacheLayerLine can print it.
export function cacheLayerFactsOf(synthesis) {
  const c = factsOf(synthesis) && factsOf(synthesis).cache_layer;
  if (!c || typeof c.designed_read_count !== 'number') return null;
  return {
    candidates: c.candidate_read_count,
    candidateCallShare: c.candidate_call_share_percent,
    designed: c.designed_read_count,
    designedCallShare: c.designed_call_share_percent,
    ownerOnly: c.owner_only_read_count || 0,
    // #399: the cache layer as the assessment's recommendation, with its reason.
    roleSentence: typeof c.role_sentence === 'string' && c.role_sentence ? c.role_sentence : null,
  };
}

// #1146: the in-scope queries and the distinct query patterns they make
// (facts.query_pattern_counts), or null for a report written before the figure.
// Not the designers' access patterns (accessPatternTotals above).
export function queryPatternCountsOf(synthesis) {
  const c = factsOf(synthesis) && factsOf(synthesis).query_pattern_counts;
  if (!c || typeof c.query_count !== 'number' || typeof c.pattern_count !== 'number') return null;
  return { queries: c.query_count, patterns: c.pattern_count };
}

// #746: the reality-check moves no model reviewed (facts.unreviewed_move_count);
// 0 when none, or for a report written before the figure.
export function unreviewedMoveCountOf(synthesis) {
  const n = factsOf(synthesis) && factsOf(synthesis).unreviewed_move_count;
  return typeof n === 'number' ? n : 0;
}

// #1003: the pending engine choices (one per table) and the choices --auto decided,
// from facts.pending_choice_count / pending_choice_query_count /
// auto_decided_choice_count / auto_decided_query_count; 0 when none, or for a
// report written before the figures.
export function engineChoiceCountsOf(synthesis) {
  const f = factsOf(synthesis) || {};
  const n = (k) => (typeof f[k] === 'number' ? f[k] : 0);
  return {
    pendingTables: n('pending_choice_count'),
    pendingQueries: n('pending_choice_query_count'),
    decidedTables: n('auto_decided_choice_count'),
    decidedQueries: n('auto_decided_query_count'),
  };
}

// #1329: the queries with no engine in the customer's engine scope, with the placed
// and total counts the headline names (facts.no_engine_in_scope_query_count,
// in_scope_query_count, query_total_count), or null when none or a report predates them.
export function noEngineInScopeOf(synthesis) {
  const f = factsOf(synthesis);
  if (!f || !f.no_engine_in_scope_query_count) return null;
  return {
    count: f.no_engine_in_scope_query_count,
    placed: f.in_scope_query_count,
    total: f.query_total_count,
  };
}

// #1195: what an engine's cost figure was sized for (facts.engines[].cost_sizing):
// the queries the assignment gives it, or the reads the cache layer fronts. Null for
// the analysis estimate and for a report written before.
export function costSizingOf(synthesis, engine) {
  const rows = (factsOf(synthesis) && factsOf(synthesis).engines) || [];
  const row = Array.isArray(rows) ? rows.find(r => r && r.engine === engine) : null;
  return (row && row.cost_sizing) || null;
}

// A number as the reports print it (fmt_num): thousands separators, at most two places.
function formatFigure(value) {
  const n = Number(value) || 0;
  return n.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 2 });
}

// "Sized for 67 queries, 96.51 calls/s" / "Sized for 20 cached reads, 185.07 calls/s",
// the reports' "Sized for" column; '' with no sizing.
export function costSizingText(t, sizing) {
  if (!sizing || !sizing.basis) return '';
  if (sizing.basis === 'no_in_scope_queries') return t('analysis-results.cost-breakdown.sized-nothing');
  const count = Number(sizing.sized_query_count) || 0;
  const rate = Number(sizing.calls_per_second) || 0;
  const what = t(
    sizing.basis === 'cache_overlay'
      ? 'analysis-results.cost-breakdown.sized-cached'
      : 'analysis-results.cost-breakdown.sized-queries',
    { count, formatted: formatFigure(count) },
  );
  const calls = t('analysis-results.cost-breakdown.sized-rate', { count: rate, formatted: formatFigure(rate) });
  const sized = t('analysis-results.cost-breakdown.sized-for', { what, rate: calls });
  // #1378: wave 1's Aurora engine also stores the tables no in-scope query reads.
  const unread = Number(sizing.unqueried_table_count) || 0;
  const text = unread
    ? sized + t('analysis-results.cost-breakdown.sized-unqueried', { count: unread, formatted: formatFigure(unread) })
    : sized;
  // The cache layer's reads stay billed on their owner engines too (the TCO assumption).
  return sizing.basis === 'cache_overlay' && count
    ? text + t('analysis-results.cost-breakdown.sized-also-on-owners')
    : text;
}

// #1474: what every cost figure is, and the one caveat printed where costs appear
// (facts.cost_scope_label, facts.cost_caveat). A report written before them gets the
// same words (the Python constants COST_SCOPE_LABEL and COST_CAVEAT; a test keeps them equal).
export const COST_SCOPE_LABEL = 'Full-modernization estimate: all waves done, every kept engine running.';
export const COST_CAVEAT = 'Estimates from public on-demand list prices for the stated Region. Check them with your own pricing in the AWS Pricing Calculator (https://calculator.aws/).';

export function costScopeLabelOf(synthesis) {
  const label = factsOf(synthesis) && factsOf(synthesis).cost_scope_label;
  return typeof label === 'string' && label ? label : COST_SCOPE_LABEL;
}

export function costCaveatOf(synthesis) {
  const caveat = factsOf(synthesis) && factsOf(synthesis).cost_caveat;
  return typeof caveat === 'string' && caveat ? caveat : COST_CAVEAT;
}

// #1195: the TCO's assumption lines (facts.cost_assumptions), as every deliverable
// states them; a hand-built report with no facts block shows its own list.
export function costAssumptionsOf(synthesis) {
  const lines = factsOf(synthesis) && factsOf(synthesis).cost_assumptions;
  if (Array.isArray(lines)) return lines;
  return (synthesis && synthesis.tco_analysis && synthesis.tco_analysis.assumptions) || [];
}

// #1636: the sentence every deliverable prints when the source has no row count for
// some tables (facts.row_counts.statement): how many, and the sizing assumption.
// Null when every count is known, and for a report written before the fact.
export function rowCountStatementOf(synthesis) {
  const fact = factsOf(synthesis) && factsOf(synthesis).row_counts;
  const statement = fact && fact.statement;
  return typeof statement === 'string' && statement ? statement : null;
}

// #1893: the sentence every deliverable prints about the call rates
// (facts.rate_windows.statement): how many rates used each window basis, and that the
// costs assume each rate runs continuously. Null for a report written before the fact.
export function rateWindowStatementOf(synthesis) {
  const fact = factsOf(synthesis) && factsOf(synthesis).rate_windows;
  const statement = fact && fact.statement;
  return typeof statement === 'string' && statement ? statement : null;
}

// #1991: the label a rate window that is not representative puts on the costs and the
// risks (facts.rate_windows.scenario_label): a scenario at the measured rate, with a
// suggestion to collect over a longer window. Null for a representative window and for a
// report written before the check.
export function rateWindowScenarioOf(synthesis) {
  const fact = factsOf(synthesis) && factsOf(synthesis).rate_windows;
  const label = fact && fact.scenario_label;
  return typeof label === 'string' && label ? label : null;
}

// #1991: the short note the risks carry when the rate window is not representative
// (facts.rate_windows.scenario_risk_note); the full label stands with the costs. Null
// otherwise, and for a report written before the check.
export function rateWindowRiskNoteOf(synthesis) {
  const fact = factsOf(synthesis) && factsOf(synthesis).rate_windows;
  const note = fact && fact.scenario_risk_note;
  return typeof note === 'string' && note ? note : null;
}

// #1990: the assumptions block's lines (growth, peak, rate window, region and prices,
// each with its origin), then the growth scenario's (cost and busiest partition key value
// at 1x, 5x and 10x), as the facts state them. Empty for a report written before them.
export function assumptionLinesOf(synthesis) {
  const facts = factsOf(synthesis);
  if (!facts) return [];
  const block = (facts.assumptions && facts.assumptions.lines) || [];
  const scenario = (facts.growth_scenarios && facts.growth_scenarios.lines) || [];
  return [...block, ...scenario].filter((line) => typeof line === 'string' && line);
}

// #1527: the stream sync of a DynamoDB design conflict's derived copies
// (facts.derived_copy_sync), printed under the risk it belongs to. The sentences are the
// facts' own; `basis` is the figures' assumptions, carried by the first sync only.
// Null for a risk with no sync and for a report written before the fact.
export function derivedCopySyncOf(synthesis, riskId) {
  const fact = factsOf(synthesis) && factsOf(synthesis).derived_copy_sync;
  const syncs = fact && Array.isArray(fact.syncs) ? fact.syncs : [];
  const at = riskId ? syncs.findIndex(s => s.risk_id === riskId) : -1;
  if (at < 0) return null;
  const assumptions = Array.isArray(fact.assumptions) ? fact.assumptions : [];
  return {
    lines: [syncs[at].path_line, syncs[at].cost_line].filter(Boolean),
    basis: at === 0 ? assumptions.join(' ') : '',
  };
}

// #1939: how the job's routines migrate (facts.routines, #1938): the headline,
// the to-do and each routine behind a routine risk with its status and why in
// plain words. Null when the report has none (no routines, or a report written
// before them). Read as stored; nothing is counted.
export function routinesOf(synthesis) {
  const routines = factsOf(synthesis) && factsOf(synthesis).routines;
  return routines && typeof routines === 'object' ? routines : null;
}

// "We could not translate 5 routines automatically; migrate them yourself
// before wave 1.": the facts' to-do as a sentence (src/report/routines.py
// to_do_text), or null when no routine needs it.
export function routineToDoText(routines) {
  const line = routines && routines.migrate_yourself_line;
  return line ? `${line.charAt(0).toUpperCase()}${line.slice(1)}.` : null;
}

// #1939: a routine's key, its kind and id: a function and a procedure may share a name.
export function routineKey(ref) {
  return `${ref.kind}:${ref.routine_id}`;
}
