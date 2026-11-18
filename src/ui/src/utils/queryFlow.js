// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { isCacheEngine } from './cacheLayer';
import { displayEngine } from './engineNames';
import { formatCount } from './format';

const idOf = (end) => (typeof end === 'object' ? end.id : end);

/**
 * The numbers a query-flow drawing shows, as rows for its text alternative (#2317): the
 * links of the same `{ nodes, links }` graph the chart lays out, so the table, the
 * chart's accessible name and the drawing never disagree. `owners` are the engines the
 * queries flow to; `cache` is the cache layer(s) fed by their flows.
 */
export function queryFlowRows(data) {
  const owners = [];
  const cache = [];
  (data?.links || []).forEach((link) => {
    const engine = idOf(link.target);
    const target = isCacheEngine(engine) ? cache : owners;
    const row = target.find((r) => r.engine === engine);
    if (row) row.queries += link.value || 0;
    else target.push({ engine, queries: link.value || 0 });
  });
  return { owners, cache };
}

/**
 * The drawing's accessible name: the split of the queries, then the cache layer. `total` is
 * the report's own query total (a fact, ADR-032); without one (a running job has no report)
 * the name states the split alone.
 */
export function queryFlowLabel(data, t, total) {
  const { owners, cache } = queryFlowRows(data);
  const split = owners.map((r) => `${displayEngine(r.engine)} ${formatCount(r.queries)}`).join(', ');
  const cached = cache.map((r) => t('query-flow.chart-label-cache', { count: r.queries, formattedCount: formatCount(r.queries), engine: displayEngine(r.engine) }));
  const head = Number.isInteger(total)
    ? t('query-flow.chart-label', { count: total, formattedCount: formatCount(total), split })
    : t('query-flow.chart-label-split', { split });
  return [head, ...cached].join(' ');
}
