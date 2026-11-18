// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2358: the results page shows each engine's final query count, the figure synthesis counts once
 * into `facts.engine_query_counts`, never the reality check's own `after_distribution`. A later
 * assignment version (an `--auto` pick, a post-schema reroute) changes engines again, and the
 * reality check's counts are then older than the reports'.
 *
 * This checks the reader the page uses; pages/__tests__/AnalysisResultsFinalEngineCounts.test.js
 * renders the page with it.
 */
const { finalEngineCounts } = require('../reportFacts');
const { ownerDistribution } = require('../cacheLayer');

// The saved discourse run (job b5dd22c9): v4 is the final version, v2 the reality check's.
const FINAL = { aurora_postgresql: 797, dynamodb: 694, documentdb: 163 };
const REALITY_CHECK = { aurora_postgresql: 796, dynamodb: 707, documentdb: 151 };

const report = (facts) => ({
  facts,
  reality_check: { after_distribution: REALITY_CHECK },
});

test('shows the final version counts, not the reality check ones', () => {
  expect(finalEngineCounts(report({ engine_query_counts: FINAL }))).toEqual(FINAL);
});

test('the cache layer is still stripped from the owners by the page', () => {
  const synthesis = report({ engine_query_counts: { ...FINAL, elasticache: 12 } });
  expect(ownerDistribution(finalEngineCounts(synthesis), true)).toEqual(FINAL);
});

test('a report saved before the figure falls back to the reality check distribution', () => {
  expect(finalEngineCounts(report({ engines: [] }))).toEqual(REALITY_CHECK);
  expect(finalEngineCounts({ reality_check: { after_distribution: REALITY_CHECK } })).toEqual(REALITY_CHECK);
});

test('a report with neither has no counts', () => {
  expect(finalEngineCounts({})).toEqual({});
  expect(finalEngineCounts(null)).toEqual({});
});

// #2345: the target engines are one fact the results page and the assessments list read.
const { targetEngineEntriesOf } = require('../reportFacts');

test('the target engines come from facts.target_engines, the cache layer marked', () => {
  const synthesis = { facts: { target_engines: ['dynamodb', 'elasticache'] }, cache_overlay: { engine: 'elasticache' } };
  expect(targetEngineEntriesOf(synthesis)).toEqual([
    { engine: 'dynamodb', isCacheLayer: false },
    { engine: 'elasticache', isCacheLayer: true },
  ]);
});

test('a report with no such fact has none, and an empty list is a list', () => {
  expect(targetEngineEntriesOf({ facts: {} })).toBeNull();
  expect(targetEngineEntriesOf(null)).toBeNull();
  expect(targetEngineEntriesOf({ facts: { target_engines: [] } })).toEqual([]);
});
