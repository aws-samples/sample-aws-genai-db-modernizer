// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2358: the results page shows each engine's final query count, the figure synthesis counts once
 * into `facts.engine_query_counts`, never the reality check's own `after_distribution`. A later
 * assignment version (an `--auto` pick, a post-schema reroute) changes engines again, and the
 * reality check's counts are then older than the reports'.
 *
 * AnalysisResults.js cannot render under this Jest config (see AnalysisResultsSectionOrder.test.js),
 * so this checks the reader the page uses and reads the page's source for the one wiring that matters.
 */
const fs = require('fs');
const path = require('path');
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

test('the page reads the final counts, and names the reality check one as such', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'pages', 'AnalysisResults.js'), 'utf8');
  expect(source).toMatch(/ownerDistribution\(finalEngineCounts\(synthesis\)/);
  expect(source).not.toMatch(/ownerDistribution\(realityCheck\?\.after_distribution/);
});
