// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** #2300: the link to an engine's part of the results page, and reading it back. */
import { resultsEngineHref, engineFromSearch, RESULTS_ENGINE_SECTION_ID } from '../resultsLinks';

test('the link names the job, the engine and the access pattern section', () => {
  expect(RESULTS_ENGINE_SECTION_ID).toBe('access-patterns-section');
  expect(resultsEngineHref('job-1', 'dynamodb')).toBe('/analysis/results/job-1?engine=dynamodb#access-patterns-section');
});

test('an engine id with odd characters is encoded', () => {
  expect(resultsEngineHref('job-1', 'a b&c')).toBe('/analysis/results/job-1?engine=a%20b%26c#access-patterns-section');
});

test('without an engine the link is the results page section only', () => {
  expect(resultsEngineHref('job-1', '')).toBe('/analysis/results/job-1#access-patterns-section');
});

test('the engine is read back from the search string', () => {
  expect(engineFromSearch('?engine=documentdb')).toBe('documentdb');
  expect(engineFromSearch('?x=1&engine=aurora_postgresql')).toBe('aurora_postgresql');
});

test('no engine, or an empty one, reads back as null', () => {
  expect(engineFromSearch('')).toBeNull();
  expect(engineFromSearch('?engine=')).toBeNull();
  expect(engineFromSearch('?target=dynamodb')).toBeNull();
});

// The results page cannot render under this repo's Jest config (see
// AnalysisResultsSectionOrder.test.js), so its use of the link is read from the
// source; the browser behaviour is checked by the UI smoke test (tests/e2e/test_ui.py).
describe('the results page', () => {
  const fs = require('fs');
  const path = require('path');
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'pages', 'AnalysisResults.js'), 'utf8');

  test('has the section the link points at', () => {
    expect(source).toContain(`id="${RESULTS_ENGINE_SECTION_ID}"`);
  });

  test('reads the engine from its URL and applies it to the explorer filter', () => {
    expect(source).toMatch(/engineFromSearch\(locationSearch\)/);
    expect(source).toMatch(/setEngineFilter\(\[\{ label: ENGINE_LABELS\[engine\] \|\| engine, value: engine \}\]\)/);
  });
});
