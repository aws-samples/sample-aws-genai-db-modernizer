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
// edit reversed (#546): those no longer describe the architecture.
export function currentTradeOffs(synthesis) {
  const all = (synthesis && synthesis.trade_offs) || [];
  const rc = (synthesis && synthesis.facts && synthesis.facts.reality_check_trade_offs) || [];
  const reversed = new Set(rc.filter(t => t.reversed).map(t => t.index));
  return all.filter((t, i) => !reversed.has(i));
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
