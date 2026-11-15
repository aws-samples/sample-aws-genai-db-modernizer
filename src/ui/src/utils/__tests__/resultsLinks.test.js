// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** #2300: the link to an engine's part of the results page, and reading it back. */
import {
  resultsEngineHref,
  engineFromSearch,
  linkedEngineTarget,
  activeDesignEngine,
  awaitsApproval,
  RESULTS_ENGINE_SECTION_ID,
} from '../resultsLinks';

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
// AnalysisResultsSectionOrder.test.js), so only the id the link points at is read from
// its source; what the page does with the link is checked in the browser by the UI
// smoke test (tests/e2e/test_ui.py).
describe('the results page', () => {
  const fs = require('fs');
  const path = require('path');
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'pages', 'AnalysisResults.js'), 'utf8');

  test('has the section the link points at', () => {
    expect(source).toContain(`id="${RESULTS_ENGINE_SECTION_ID}"`);
  });
});

describe('where a linked engine lands on the results page', () => {
  const explorer = ['dynamodb', 'documentdb'];
  const designs = ['aurora_mysql', 'aurora_postgresql'];

  test('an engine with explorer rows is the explorer', () => {
    expect(linkedEngineTarget('dynamodb', explorer, designs)).toBe('explorer');
  });

  test('a relational engine with a design is the Table design', () => {
    expect(linkedEngineTarget('aurora_postgresql', explorer, designs)).toBe('design');
  });

  test('an engine in neither is absent from the plan, so the page says so', () => {
    expect(linkedEngineTarget('opensearch', explorer, designs)).toBe('absent');
    expect(linkedEngineTarget('no-such-engine', [], [])).toBe('absent');
  });

  test('a link without an engine asks for nothing', () => {
    expect(linkedEngineTarget(null, explorer, designs)).toBe('none');
  });
});

describe('the Table design tab the page opens on', () => {
  const designs = ['aurora_mysql', 'aurora_postgresql'];

  test('the linked engine, even when it is the second design (the tabs are controlled from the first render)', () => {
    expect(activeDesignEngine('aurora_postgresql', designs)).toBe('aurora_postgresql');
  });

  test('the first design when nothing, or an engine without a design, is linked', () => {
    expect(activeDesignEngine(null, designs)).toBe('aurora_mysql');
    expect(activeDesignEngine('dynamodb', designs)).toBe('aurora_mysql');
  });

  test('no designs, no tab', () => {
    expect(activeDesignEngine('aurora_mysql', [])).toBeUndefined();
  });
});

describe('a decision pending at the gate', () => {
  const job = (name, status) => ({ progress: { stages: [{ name: 'RunCollector', status: 'completed' }, { name, status }] } });

  test('is the approval stage not yet completed', () => {
    expect(awaitsApproval(job('WaitForAssignmentApproval', 'running'))).toBe(true);
  });

  test('is not an approved gate, another stage or a job without stages', () => {
    expect(awaitsApproval(job('WaitForAssignmentApproval', 'completed'))).toBe(false);
    expect(awaitsApproval(job('RunSchemaDesign', 'running'))).toBe(false);
    expect(awaitsApproval({})).toBe(false);
    expect(awaitsApproval(null)).toBe(false);
  });
});
