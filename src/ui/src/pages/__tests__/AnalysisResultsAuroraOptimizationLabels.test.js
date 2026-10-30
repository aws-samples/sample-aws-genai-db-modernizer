// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #987: the results page's "Aurora Design" section names each optimization by
 * its readable category ("Read replica: Aurora cluster"), never the raw code
 * ("read_replica: Aurora cluster"). Only the retired HTML report did this
 * (#587 round 3); the results page took it over when that report went.
 *
 * The page itself cannot render under this repo's Jest config (Cloudscape ships
 * ESM it cannot transform; see AppRoutes.test.js), so this reads the page's
 * source for the optimization header and checks the helper it calls.
 */
import fs from 'fs';
import path from 'path';
import { optimizationLabel } from '../../utils/optimizationLabels';

const PAGE = fs.readFileSync(path.join(__dirname, '..', 'AnalysisResults.js'), 'utf8');

test('the optimization header formats the category with optimizationLabel', () => {
  const start = PAGE.indexOf('design.optimizations.map(');
  expect(start).toBeGreaterThan(-1);
  const block = PAGE.slice(start, PAGE.indexOf('</SpaceBetween>', start));
  expect(block).toContain('optimizationLabel(opt.category)');
  // The raw code is never printed on its own.
  expect(block).not.toMatch(/\$\{opt\.category\}/);
  expect(block).not.toMatch(/: opt\.category\}/);
});

test('every category the Aurora contracts allow has a readable label', () => {
  expect(optimizationLabel('read_replica')).toBe('Read replica');
  expect(optimizationLabel('io_optimized')).toBe('I/O-Optimized storage');
  expect(optimizationLabel('partitioning')).toBe('Partitioning');
  expect(optimizationLabel('index')).toBe('Index');
  expect(optimizationLabel('other')).toBe('Other');
});

test('an unknown category is shown as written, not dropped', () => {
  expect(optimizationLabel('future_category')).toBe('future_category');
});
