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
