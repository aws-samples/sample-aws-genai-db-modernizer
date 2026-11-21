// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { displayEngine } from './engineNames';

/**
 * #2521: the wording rules of "Queries that cannot be served as designed" that the alert
 * (UnservableQueriesCallout) and its help panel (resultsHelp.js) both follow. They read
 * report.json's facts.unservable and format it; they count nothing (ADR-032).
 */

// #1329: every listed query has no engine in the customer's engine scope. The alert then
// opens with its own intro and never points at the assignment review, which refuses to
// edit them; the same rule as src/report/unservable.py.
export function scopeOnly(u) {
  return (u.no_engine_in_scope_query_count || 0) > 0
    && !(u.no_fit_query_count || u.flagged_query_count || u.held_query_count);
}

function engineList(engines) {
  const names = engines.map(displayEngine);
  if (names.length < 2) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names.slice(-1)[0]}`;
}

/** The re-run steps: the assignment review, and a new assessment for a query with no engine in the scope. */
export function rerunLines(u, t) {
  const lines = [];
  if (!scopeOnly(u)) lines.push(t('analysis-results.unservable.rerun'));
  if ((u.no_engine_in_scope_query_count || 0) > 0) {
    const engines = u.no_engine_in_scope_closest_engines || [];
    lines.push(engines.length > 0
      ? t('analysis-results.unservable.rerun_scope', { engines: engineList(engines) })
      : t('analysis-results.unservable.rerun_scope_wider'));
  }
  return lines;
}
