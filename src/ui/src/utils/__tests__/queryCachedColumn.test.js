// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2319: the query table's "Cached" column comes from the cache design's patterns, while
 * the deliverables' cached-read figure comes from report.json's facts (ADR-032). On the
 * saved real runs the two agree, so the table and the reports tell one story.
 */
import fs from 'fs';
import path from 'path';
import zlib from 'zlib';
import { cacheLayerAccessPatterns } from '../cacheLayer';
import { cacheLayerFactsOf } from '../reportFacts';
import { queryPatternLinks } from '../queryRouting';

const RUNS = path.resolve(__dirname, '../../../../../tests/fixtures/real_runs');

function loadRun(name) {
  const dir = `${RUNS}/${name}`;
  const gz = `${dir}/report.json.gz`;
  const report = JSON.parse(
    fs.existsSync(gz) ? zlib.gunzipSync(fs.readFileSync(gz)).toString('utf8') : fs.readFileSync(`${dir}/report.json`, 'utf8'),
  );
  const versions = fs.readdirSync(`${dir}/job/schema-elasticache`)
    .filter(v => /^v\d+$/.test(v)).sort((a, b) => Number(a.slice(1)) - Number(b.slice(1)));
  const design = JSON.parse(fs.readFileSync(`${dir}/job/schema-elasticache/${versions.pop()}/schema_output.json`, 'utf8'));
  return { report, design };
}

describe.each(['wordpress-both', 'discourse-both'])('%s', (name) => {
  test('the queries marked cached are the report\'s designed cached reads', () => {
    const { report, design } = loadRun(name);
    const overlay = report.cache_overlay;
    const patterns = cacheLayerAccessPatterns([{ target_type: overlay.engine, content: design }], overlay)
      .map(ap => ({ ...ap, queryIds: ap.queryIds || ap.source_query_ids || [] }));
    const { cacheEngineById } = queryPatternLinks([], patterns);
    expect(cacheLayerFactsOf(report).designed).toBeGreaterThan(0);
    expect(cacheEngineById.size).toBe(cacheLayerFactsOf(report).designed);
  });
});
