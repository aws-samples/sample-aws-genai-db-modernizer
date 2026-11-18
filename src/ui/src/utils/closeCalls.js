// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { closeCallsOf } from './reportFacts';
import { displayEngine } from './engineNames';
import { queryExcerpt } from './queryRouting';

// #2318: the rows of the roadmap's close-calls table, one per query listed in
// report.json's query_traces.close_calls (the rows facts.close_calls groups by engine
// pair), with the statement from the query list when the page has loaded it. Rows only
// format what the report and the API hold: the gap is the report's, never recomputed.
// A report that lists no rows (written before the digest) shows the ids the facts name
// per pair, with no gap.

export const SETTLED_BY = ['score', 'group-score', 'tie-rule'];

// What decided the engine: its score on the query, its group's mean score (the query
// moves with the group), or the routing rule that settled an exact tie.
export function settledByOf(row) {
  if (row.settled_tie) return 'tie-rule';
  return row.basis === 'group_mean' ? 'group-score' : 'score';
}

function toRow(raw, statements) {
  const statement = statements.get(raw.queryId) || '';
  return {
    queryId: raw.queryId,
    statement: queryExcerpt(statement),
    engine: raw.engine,
    runnerUp: raw.runnerUp,
    gap: raw.gap,
    settledBy: raw.settledBy,
    tie: raw.settledBy === 'tie-rule',
    search: [raw.queryId, statement, displayEngine(raw.engine), displayEngine(raw.runnerUp)].join('\n').toLowerCase(),
  };
}

// `queries`: the query routing table's rows ({ queryId, statement }), for each close
// call's statement. Closest first, the settled ties last, then by id.
// Whether the rows are the report's own list (query_traces.close_calls). An older report
// has only the ids its facts name per engine pair, and says how many more it does not.
export function hasListedCloseCalls(synthesis) {
  const digest = synthesis && synthesis.query_traces && synthesis.query_traces.close_calls;
  return Array.isArray(digest) && digest.some(r => r && typeof r === 'object' && r.query_id && r.engine && r.alternative);
}

// For an older report: the pairs with close calls the facts name no id for.
export function unlistedCloseCalls(synthesis) {
  const cc = closeCallsOf(synthesis);
  if (!cc || hasListedCloseCalls(synthesis)) return [];
  return cc.engine_pairs.filter(p => p.more_query_count > 0);
}

export function closeCallRows(synthesis, queries) {
  const cc = closeCallsOf(synthesis);
  if (!cc) return [];
  const statements = new Map((queries || []).map(q => [q.queryId, q.statement || '']));
  const digest = synthesis.query_traces && synthesis.query_traces.close_calls;
  const listed = Array.isArray(digest)
    ? digest.filter(r => r && typeof r === 'object' && r.query_id && r.engine && r.alternative)
    : [];
  const raws = listed.length > 0
    ? listed.map(r => ({
      queryId: String(r.query_id),
      engine: r.engine,
      runnerUp: r.alternative,
      gap: typeof r.gap === 'number' ? r.gap : null,
      settledBy: settledByOf(r),
    }))
    : cc.engine_pairs.flatMap(p => (p.query_ids || []).map(id => ({
      queryId: String(id), engine: p.engine, runnerUp: p.alternative, gap: null, settledBy: null,
    })));
  return raws.map(r => toRow(r, statements)).sort((a, b) => {
    if (a.tie !== b.tie) return a.tie ? 1 : -1;
    if (a.gap !== b.gap) return a.gap - b.gap;
    return a.queryId < b.queryId ? -1 : 1;
  });
}

// The values the filter offers: the engines and runner-ups the rows hold, and the
// settled-by kinds present, never a count.
export function closeCallFilterOptions(rows, settledLabel) {
  const engines = new Set();
  const runnerUps = new Set();
  const settled = new Set();
  (rows || []).forEach((r) => {
    engines.add(r.engine);
    runnerUps.add(r.runnerUp);
    if (r.settledBy) settled.add(r.settledBy);
  });
  const named = (key, set) => [...set].sort((a, b) => displayEngine(a).localeCompare(displayEngine(b)))
    .map(value => ({ propertyKey: key, value, label: displayEngine(value) }));
  return [
    ...named('engine', engines),
    ...named('runnerUp', runnerUps),
    ...SETTLED_BY.filter(k => settled.has(k))
      .map(value => ({ propertyKey: 'settledBy', value, label: settledLabel(value) })),
  ];
}
