// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** #2300: the link to an engine's part of the results page, and reading it back. */
import {
  resultsEngineHref,
  engineFromSearch,
  linkedEngineTarget,
  activeDesignEngine,
  awaitsApproval,
  synthesisRan,
  RESULTS_ENGINE_SECTION_ID,
} from '../resultsLinks';

test('the link names the job, the engine and the query table (#2446)', () => {
  expect(RESULTS_ENGINE_SECTION_ID).toBe('query-routing-section');
  expect(resultsEngineHref('job-1', 'dynamodb')).toBe('/analysis/results/job-1?engine=dynamodb#query-routing-section');
});

test('an engine id with odd characters is encoded', () => {
  expect(resultsEngineHref('job-1', 'a b&c')).toBe('/analysis/results/job-1?engine=a%20b%26c#query-routing-section');
});

test('without an engine the link is the results page section only', () => {
  expect(resultsEngineHref('job-1', '')).toBe('/analysis/results/job-1#query-routing-section');
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
  // #2446: the query table renders the section the link points at.
  const source = fs.readFileSync(path.join(__dirname, '..', '..', 'components', 'QueryRoutingTable.js'), 'utf8');

  test('has the section the link points at', () => {
    expect(source).toContain(`id="${RESULTS_ENGINE_SECTION_ID}"`);
  });
});

describe('where a linked engine lands on the results page', () => {
  // The engines whose schema design lists access patterns.
  const explorer = ['dynamodb', 'documentdb'];
  const designs = ['aurora_mysql', 'aurora_postgresql'];

  test('an engine with access patterns is its queries', () => {
    expect(linkedEngineTarget('dynamodb', explorer, designs)).toBe('queries');
  });

  test('a relational engine with a design is the Table design', () => {
    expect(linkedEngineTarget('aurora_postgresql', explorer, designs)).toBe('design');
  });

  test('an engine in neither is absent from the plan, so the page says so', () => {
    expect(linkedEngineTarget('opensearch', explorer, designs)).toBe('absent');
    expect(linkedEngineTarget('no-such-engine', [], [])).toBe('absent');
  });

  test('an engine with queries routed to it but no design yet is not absent: its design has not been made', () => {
    expect(linkedEngineTarget('dynamodb', [], designs, ['dynamodb'])).toBe('not-designed');
  });

  test('an engine in the plan with patterns or a design stays its queries or the design, routed or not', () => {
    expect(linkedEngineTarget('dynamodb', explorer, designs, ['dynamodb'])).toBe('queries');
    expect(linkedEngineTarget('aurora_mysql', explorer, designs, ['aurora_mysql'])).toBe('design');
  });

  test('a routed list that omits the engine leaves it absent', () => {
    expect(linkedEngineTarget('opensearch', [], designs, ['dynamodb'])).toBe('absent');
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

describe('results that can exist for a job', () => {
  const job = (...stages) => ({ progress: { stages: stages.map(([name, status]) => ({ name, status })) } });

  test('exist once synthesis has completed, also while a new decision is pending', () => {
    expect(synthesisRan(job(['RunRefereeSynthesis', 'completed']))).toBe(true);
    expect(synthesisRan(job(['LoadSynthesisOutput', 'completed']))).toBe(true);
    expect(synthesisRan(job(['LoadSynthesisOutput', 'completed'], ['WaitForAssignmentApproval', 'running']))).toBe(true);
  });

  test('cannot exist before: a first gate, a synthesis still running, or a job without stages', () => {
    expect(synthesisRan(job(['RunCollector', 'completed'], ['WaitForAssignmentApproval', 'running']))).toBe(false);
    expect(synthesisRan(job(['RunRefereeSynthesis', 'running']))).toBe(false);
    expect(synthesisRan({})).toBe(false);
    expect(synthesisRan(null)).toBe(false);
  });
});
