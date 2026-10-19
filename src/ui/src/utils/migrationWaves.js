// #957 (ADR-032): the results page's migration roadmap reads report.json's
// waves (synthesis.migration_waves, which the /results API fills in for a
// report written before the field existed) and each wave's figures from
// synthesis.facts.waves, then formats them. It counts and totals nothing.
//
// waveHeadline prints the same headline as the decision and engineering
// reports (src/report/roadmap.py's _wave_headline_stats): the shared fixture
// utils/__tests__/fixtures/waveHeadlines.json is checked against both sides.

import { waveFacts } from './reportFacts';
import { displayEngine } from './engineNames';

// fmt_num(value, 1) from src/report/formatting.py: thousands separators, at
// most one decimal place, trailing zeros dropped, a small non-zero value as
// "<0.1".
export function formatShare(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return '0';
  if (Math.abs(n) < 0.05) return n > 0 ? '<0.1' : '>-0.1';
  const text = n.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: 1 });
  return text === '-0' ? '0' : text;
}

export function migrationWaves(synthesis) {
  const waves = (synthesis && synthesis.migration_waves) || [];
  return waves.filter(w => w && typeof w === 'object');
}

// "Migration roadmap (3 waves)": facts.migration_wave_count, or null when the
// report has no facts block to say it.
export function waveCount(synthesis) {
  const n = synthesis && synthesis.facts && synthesis.facts.migration_wave_count;
  return typeof n === 'number' ? n : null;
}

function viewsPhrase(t, wf) {
  if (!wf || !wf.views || typeof wf.tables_only !== 'number') return '';
  return t('analysis-results.waves.views-split', {
    tables: t('analysis-results.waves.table-count', { count: wf.tables_only }),
    views: t('analysis-results.waves.view-count', { count: wf.views }),
  });
}

function countPhrase(t, wave) {
  const n = Number(wave.query_count) || 0;
  const share = formatShare(wave.workload_share_percent);
  const engines = wave.engines || [];
  const cutover = wave.cutover_query_count;
  if (cutover !== undefined && cutover !== null && engines.length > 0) {
    return t('analysis-results.waves.cutover', {
      count: cutover, remaining: n, share, engine: displayEngine(engines[0]),
    });
  }
  const key = wave.share_basis === 'calls'
    ? 'analysis-results.waves.queries-share-calls'
    : 'analysis-results.waves.queries-share';
  return t(key, { count: n, share });
}

function tablePhrase(t, synthesis, wave) {
  const wf = waveFacts(synthesis, wave);
  const served = Number(wf ? wf.served_tables : wave.table_count) || 0;
  if (!served) return '';
  const engines = wave.engines || [];
  const cutover = wave.cutover_query_count;
  if (cutover !== undefined && cutover !== null && engines.length > 0) {
    return t('analysis-results.waves.tables-and-views', { count: served, views: viewsPhrase(t, wf) });
  }
  if (wf && wf.kept_in_sync_tables && typeof wf.moving_tables === 'number') {
    const kept = Number(wf.kept_in_sync_tables);
    return t('analysis-results.waves.served-move', {
      count: served,
      moving: wf.moving_tables,
      kept,
      owner: t('analysis-results.waves.kept-owner', { count: kept }),
    });
  }
  return t('analysis-results.waves.source-tables', { count: served });
}

// "all 107 queries at cutover; 65 (60.7%) remain on Aurora MySQL at the end,
// 50 source tables and views", "20 queries (83.4% of calls)",
// "1 query (0.9% of query patterns), 4 source tables: 3 to move, 1 kept in sync from its owner".
export function waveHeadline(t, synthesis, wave) {
  return countPhrase(t, wave) + tablePhrase(t, synthesis, wave);
}

export function waveTitle(wave) {
  return wave.title || (wave.engines || []).map(displayEngine).join(', ');
}
