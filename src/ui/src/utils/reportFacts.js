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

export function tradeOffsByTab(designTradeOffs, synthesis) {
  const map = { ...designTradeOffs };
  const rc = realityCheckDecisions(synthesis || {});
  if (rc.length > 0) map[REALITY_CHECK_TAB] = rc;
  return map;
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

// #992: each written table's source of truth and the replicas serving its reads
// (facts.table_sources), or null when no query writes a table or the report
// predates them. Every figure is the facts layer's.
export function tableSourcesOf(synthesis) {
  const ts = factsOf(synthesis) && factsOf(synthesis).table_sources;
  if (!ts || !ts.written_table_count) return null;
  return ts;
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
