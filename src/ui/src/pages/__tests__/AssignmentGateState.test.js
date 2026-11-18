// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2326: the assignment gate page states where the decision stands (waiting, approved,
 * later version, done) and offers approval and overrides only while the assessment
 * waits at the gate. Real Cloudscape components; ChartSankey and the app header are
 * stubbed; `t` fills in each key's English default from en.json.
 */
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import AssignmentGate from '../AssignmentGate';
import ApiManager from '../../classes/ApiManager';

jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/ChartSankey', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/PendingEngineChoices', () => ({
  __esModule: true,
  default: ({ choices }) => (choices.length ? <div id="pending-engine-choices" /> : null),
}));
jest.mock('../../classes/ApiManager');
const mockNavigate = jest.fn();
jest.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useNavigate: () => mockNavigate,
}));
jest.mock('react-i18next', () => {
  const en = require('../../locales/en.json');
  const t = (key, opts = {}) => {
    const { defaultValue, count, ...vars } = opts;
    let text = (count !== undefined && count !== 1 && en[`${key}_other`]) || en[key] || defaultValue || key;
    Object.entries({ ...vars, count }).forEach(([k, v]) => { text = text.split(`{{${k}}}`).join(v); });
    return text;
  };
  const mockResult = { t };
  return { useTranslation: () => mockResult };
});

global.IS_REACT_ACT_ENVIRONMENT = true;

const stage = (status) => [
  { name: 'RunRefereeSynthesis', status: 'completed' },
  { name: 'WaitForAssignmentApproval', status },
];

function payloads({
  stages, version = 2, resultsVersion = null, results = true, db = 'wordpress',
  status, approvalRecorded, assignmentStatus, previousVersion, gateFacts,
}) {
  return {
    job: db ? { database_name: db, status, progress: { stages } } : {},
    assignments: {
      success: true,
      results_version: resultsVersion,
      approval_recorded: approvalRecorded,
      gate_facts: gateFacts,
      pending_choices: [{ choice_id: 'wp_options' }],
      assignment: {
        version,
        status: assignmentStatus,
        previous_version: previousVersion,
        query_assignments: [
          { query_id: 'q1aaaaaaaaaaaaaa', assigned_engine: 'aurora_mysql', confidence: 80, assignment_reason: 'fit', source_tables: ['wp_options'], in_scope: true },
        ],
      },
    },
    results: { success: results },
    'reality-check': { success: true, before_distribution: { aurora_mysql: 1 }, after_distribution: { aurora_mysql: 1 } },
    phases: { success: true },
    triage: { success: true },
    collector: { success: true, queries: { query_patterns: [{ query_id: 'q1aaaaaaaaaaaaaa', query_type: 'SELECT', query_text: 'SELECT 1' }] } },
  };
}

let container;
let root;
beforeEach(() => {
  mockNavigate.mockClear();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function render(options) {
  const data = payloads(options);
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => Object.fromEntries(requests.map((r) => [r.id, data[r.id]])),
  }));
  await act(async () => { root.render(<AssignmentGate />); });
}

const buttons = () => [...container.querySelectorAll('button')].map((b) => b.textContent.trim());
// The segmented control's segments (Cloudscape renders each as a button in a labelled group).
const viewButtons = () => [...container.querySelectorAll('[aria-label="View"] button, [aria-label="View"] [role="button"]')];
const stateAlert = () => container.querySelector('[data-gate-state]');

test('waiting at the gate: says so, and offers approval and the per-query review', async () => {
  await render({ stages: stage('in-progress') });
  expect(stateAlert().getAttribute('data-gate-state')).toBe('waiting');
  expect(stateAlert().textContent).toContain('Waiting for your approval');
  expect(stateAlert().textContent).toContain('version 2');
  expect(buttons()).toContain('Approve assignment');
});

test('approved with results: says which version, links the results, offers no approval', async () => {
  await render({ stages: stage('completed'), resultsVersion: 2 });
  expect(stateAlert().getAttribute('data-gate-state')).toBe('done');
  expect(stateAlert().textContent).toContain('Approved: assignment version 2');
  expect(buttons()).not.toContain('Approve assignment');
  expect(buttons().join('|')).not.toMatch(/Approve|Save overrides/);
  expect(stateAlert().querySelector('a, button').textContent).toContain('View results');
});

test('approved and still running: says the assessment is running', async () => {
  await render({ stages: stage('completed').slice(1), results: false });
  expect(stateAlert().getAttribute('data-gate-state')).toBe('approved');
  expect(stateAlert().textContent).toContain('Approved: assignment version 2');
  expect(buttons()).not.toContain('Approve assignment');
});

test('a version newer than the results is a later version and read-only', async () => {
  await render({ stages: stage('completed'), version: 3, resultsVersion: 2 });
  expect(stateAlert().getAttribute('data-gate-state')).toBe('later');
  expect(stateAlert().textContent).toContain('version 3');
  expect(stateAlert().textContent).toContain('version 2');
  expect(buttons()).not.toContain('Approve assignment');
});

test('an unknown job offers no approval', async () => {
  await render({ stages: [], db: '' });
  expect(buttons()).not.toContain('Approve assignment');
  expect(buttons().join('|')).not.toContain('Continue with recommendation');
});

test('the two views are named Summary and Per query', async () => {
  await render({ stages: stage('in-progress') });
  expect(viewButtons().map((b) => b.textContent.trim())).toEqual(['Summary', 'Per query']);
  expect(container.textContent).not.toMatch(/Advanced review|Summary view|Review in detail|Back to summary/);
});

test('the per-query view is read-only unless the assessment waits', async () => {
  await render({ stages: stage('completed'), resultsVersion: 2 });
  await act(async () => { viewButtons()[1].click(); });
  expect(container.textContent).toContain('These are the query assignments of this version');
  expect(container.querySelector('.query-list-row button[aria-haspopup="listbox"]')).toBeNull();
  expect(container.querySelector('.query-list-row input[type="checkbox"]')).toBeNull();
});

test('while the assessment waits, the per-query view offers the engine pickers', async () => {
  await render({ stages: stage('in-progress') });
  await act(async () => { viewButtons()[1].click(); });
  expect(container.querySelector('.query-list-row button[aria-haspopup="listbox"]')).not.toBeNull();
  expect(container.querySelector('.query-list-row input[type="checkbox"]')).not.toBeNull();
  expect(buttons()).toContain('Approve assignment');
});

test('the engine choices are offered only while the assessment waits', async () => {
  await render({ stages: stage('in-progress') });
  expect(container.querySelector('#pending-engine-choices')).not.toBeNull();
  await act(async () => root.unmount());
  root = createRoot(container);
  await render({ stages: stage('completed'), resultsVersion: 2 });
  expect(container.querySelector('#pending-engine-choices')).toBeNull();
});

// A cloud stage history while the reality check runs: the gate is not reached yet.
const preGate = [
  { name: 'RunAssignment', status: 'completed' },
  { name: 'RunRealityCheck', status: 'in-progress' },
];

test('a job that has not reached the gate is not called approved', async () => {
  await render({ stages: preGate, status: 'RUNNING', version: 1, results: false });
  expect(stateAlert().getAttribute('data-gate-state')).toBe('running');
  expect(stateAlert().textContent).not.toMatch(/Approved/);
  expect(stateAlert().textContent).toContain('No approval is recorded');
  expect(buttons()).not.toContain('Approve assignment');
});

test('a failed job says so and links its progress', async () => {
  await render({ stages: stage('completed'), status: 'FAILED', results: false });
  expect(stateAlert().getAttribute('data-gate-state')).toBe('failed');
  expect(stateAlert().textContent).toContain('The assessment failed');
  expect(stateAlert().textContent).not.toMatch(/Approved/);
  expect(stateAlert().querySelector('a, button').textContent).toContain('View progress');
});

test('a local job past the gate is approved by its recorded approval', async () => {
  // Local mode drops the gate stage once it is passed.
  const stages = [{ name: 'RunRefereeSynthesis', status: 'completed' }];
  await render({ stages, approvalRecorded: true, resultsVersion: 2 });
  expect(stateAlert().getAttribute('data-gate-state')).toBe('done');
  expect(stateAlert().textContent).toContain('Approved: assignment version 2');
  await act(async () => root.unmount());
  root = createRoot(container);
  // A job saved before approvals were recorded: its results close the gate, as the API reads it.
  await render({ stages, approvalRecorded: false, resultsVersion: 2 });
  expect(stateAlert().getAttribute('data-gate-state')).toBe('done');
  await act(async () => root.unmount());
  root = createRoot(container);
  await render({ stages, approvalRecorded: false });
  expect(stateAlert().getAttribute('data-gate-state')).toBe('running');
});

test('a post-schema reroute reads as rerouted after schema design, never approved', async () => {
  await render({
    stages: stage('completed'), version: 3, previousVersion: 2, resultsVersion: 3,
    assignmentStatus: 'rerouted_after_approval',
  });
  expect(stateAlert().getAttribute('data-gate-state')).toBe('rerouted');
  expect(stateAlert().textContent).toContain('Assignment version 3 was rerouted after schema design');
  expect(stateAlert().textContent).toContain('You approved version 2');
  expect(stateAlert().textContent).not.toMatch(/Approved:/);
  expect(stateAlert().querySelector('a, button').textContent).toContain('View results');
});

test('the headline names the engines the figures keep', async () => {
  // The reality check's record says Aurora MySQL alone; the version's facts keep two.
  const gateFacts = {
    rows: [
      { engine: 'aurora_mysql', before: 1, after: 1, share_percent: 50, dropped: false },
      { engine: 'dynamodb', before: 0, after: 1, share_percent: 50, dropped: false },
    ],
    surviving: ['aurora_mysql', 'dynamodb'],
    total_after: 2,
    query_total_count: 2,
    source_table_count: 1,
    cache_candidates: null,
  };
  await render({ stages: stage('completed'), resultsVersion: 2, gateFacts });
  const headline = container.querySelector('.gate-hero__headline').textContent;
  expect(headline).toBe('Recommended architecture: Aurora MySQL and DynamoDB');
  const kept = [...container.querySelectorAll('[data-figure="gate-figures"] [data-engine]')]
    .map((el) => el.getAttribute('data-engine'));
  expect(kept).toEqual(['aurora_mysql', 'dynamodb']);
});
