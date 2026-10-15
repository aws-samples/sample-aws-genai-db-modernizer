// #615 (ADR-032): the one reader of report.json's `facts` block on the UI side.
// Synthesis computes every customer-facing figure once
// (src/shared/report_facts.py); the UI only formats it. The /results API fills
// `facts` in for a report synthesized before it existed (src/api/routes/results.py).

export function waveFacts(synthesis, wave) {
  const rows = (synthesis && synthesis.facts && synthesis.facts.waves) || [];
  return rows.find(r => r.wave === wave.wave) || null;
}

// " · 312 source tables and views (311 tables, 1 view)", " · 4 served, 3 move",
// " · 12 source tables", or "" -- the same phrasing as the decision report.
// Only formats the wave's facts: the /results API and the report exporter
// fill `facts` in for an older report (#615), so there is no second formula
// here; without them it states the served count and nothing it would have to
// compute.
export function waveTableStats(synthesis, wave) {
  const wf = waveFacts(synthesis, wave);
  const served = wf ? wf.served_tables : (wave.table_count || 0);
  if (!served) return '';
  if (wf && wf.views && typeof wf.tables_only === 'number') {
    const t = wf.tables_only === 1 ? 'table' : 'tables';
    const v = wf.views === 1 ? 'view' : 'views';
    return ` · ${served} source tables and views (${wf.tables_only} ${t}, ${wf.views} ${v})`;
  }
  if (wf && wf.kept_in_sync_tables && typeof wf.moving_tables === 'number') {
    return ` · ${served} served, ${wf.moving_tables} move`;
  }
  return ` · ${served} source tables`;
}

// The open-risk count every deliverable prints (facts.open_risk_count).
export function openRiskCountOf(synthesis, shownRisks) {
  const n = synthesis && synthesis.facts && synthesis.facts.open_risk_count;
  return typeof n === 'number' ? n : (shownRisks || []).length;
}

// #748: the other counts the deliverables print. Each reads the stored fact;
// the shown list is only the answer for a hand-built report with no `facts`
// block (the /results API and the exporters fill one in for a real report).
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
    ? t('analysis-results-v2.tradeoffs.reality-check')
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
