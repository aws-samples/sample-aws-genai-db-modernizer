// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2300: clicking an engine node in the Sankey chart goes to that engine's part of
 * the results page, but only when the job's results exist (the summary page's
 * "View results" condition: the job is complete; at the gate: the results endpoint
 * answers). On the summary page the click stays in the tab; at the gate it opens a
 * new tab, so the unsaved overrides are not lost. Without results the node is not a
 * link and says why. At the gate with a decision pending, results that exist belong
 * to an earlier version, and the page says so. It used to open the old
 * pattern-analysis page in a new tab, results or not.
 *
 * Renders both real pages with plain-DOM stand-ins for the Cloudscape components
 * (it ships ESM Jest cannot transform) and a ChartSankey stub that exposes one
 * button per node; `t` returns its key.
 */
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import AssignmentGate from '../AssignmentGate';
import JobMonitoringSummary from '../JobMonitoringSummary';
import ApiManager from '../../classes/ApiManager';

function mockPassThrough({ children, content }) {
  const React = require('react');
  return (
    <div>
      {React.isValidElement(content) ? content : null}
      {children}
    </div>
  );
}
const mockNothing = () => null;

jest.mock('@cloudscape-design/components/app-layout', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/side-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/space-between', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/header', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/container', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/box', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/column-layout', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/flashbar', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/status-indicator', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/badge', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/spinner', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/progress-bar', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/alert', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/table', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/property-filter', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/pagination', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/modal', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/tabs', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/expandable-section', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/checkbox', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/button', () => ({
  __esModule: true,
  default: ({ children, onClick }) => <button onClick={onClick}>{children}</button>,
}));
jest.mock('@cloudscape-design/components/select', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/SummaryFallbackNotice', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/ProgressState', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/UnservableAssignmentNotice', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/PendingEngineChoices', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/ChartSankey', () => ({
  __esModule: true,
  default: ({ data, onNodeClick, unavailableTitle }) => (
    <div>
      {data.nodes.map((n) =>
        onNodeClick ? (
          <button key={n.id} className="sankey-node" data-node={n.id} onClick={() => onNodeClick(n.id)} />
        ) : (
          <span key={n.id} className="sankey-node-off" data-node={n.id} title={unavailableTitle} />
        ),
      )}
    </div>
  ),
}));
jest.mock('../../classes/ApiManager');
const mockNavigate = jest.fn();
jest.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useNavigate: () => mockNavigate,
}));
// One stable `t`: the pages refetch when it changes identity.
jest.mock('react-i18next', () => {
  const mockResult = { t: (key) => key };
  return { useTranslation: () => mockResult };
});

global.IS_REACT_ACT_ENVIRONMENT = true;

const ASSIGNMENT = {
  success: true,
  assignment: {
    version: 2,
    query_assignments: [
      {
        query_id: 'scored000000bbbb',
        assigned_engine: 'dynamodb',
        confidence: 78,
        assignment_reason: 'highest fit for DynamoDB',
        source_tables: ['discourse.users'],
        in_scope: true,
      },
      {
        query_id: 'zeroscore000cccc',
        assigned_engine: 'aurora_postgresql',
        confidence: 80,
        assignment_reason: 'fit',
        source_tables: ['discourse.t'],
        in_scope: true,
      },
    ],
  },
};

const BASE = {
  job: {
    database_name: 'discourse',
    progress: { stages: [{ name: 'RunRealityCheck', status: 'completed' }] },
  },
  assignments: ASSIGNMENT,
  'reality-check': {
    success: true,
    after_distribution: { dynamodb: 1, aurora_postgresql: 1 },
  },
  phases: { success: true },
  triage: { success: true },
  collector: {
    success: true,
    metadata: { source_database: { engine: 'postgresql' } },
    queries: {
      query_patterns: [
        { query_id: 'scored000000bbbb', query_type: 'SELECT', query_text: 'SELECT 1' },
        { query_id: 'zeroscore000cccc', query_type: 'SELECT', query_text: 'SELECT 2' },
      ],
    },
  },
  'get-job-details': {
    success: true,
    database_name: 'discourse',
    progress: {
      stages: [
        { name: 'WaitForAssignmentApproval', status: 'completed' },
        { name: 'RunRefereeSynthesis', status: 'running' },
      ],
    },
  },
  'get-agent-details': { success: true },
  'get-execution-history': { success: true },
  'get-results': { success: true },
  results: { success: false },
};

// The job is complete when its last stage, the review, is completed.
const COMPLETE = {
  progress: {
    stages: [
      { name: 'WaitForAssignmentApproval', status: 'completed' },
      { name: 'LoadSynthesisOutput', status: 'completed' },
    ],
  },
};

let DATA = BASE;

let container;
let root;
let openSpy;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  mockNavigate.mockClear();
  DATA = BASE;
  openSpy = jest.spyOn(window, 'open').mockImplementation(() => null);
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => Object.fromEntries(requests.map((r) => [r.id, DATA[r.id]])),
  }));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  openSpy.mockRestore();
});

async function renderPage(Page) {
  await act(async () => {
    root.render(<Page />);
  });
}

async function clickNode(Page, nodeId) {
  await renderPage(Page);
  const node = container.querySelector(`.sankey-node[data-node="${nodeId}"]`);
  expect(node).not.toBeNull();
  await act(async () => {
    node.click();
  });
}

const withData = (overrides) => {
  DATA = { ...BASE, ...overrides };
};

describe('the job monitoring summary', () => {
  test('a complete job: a Sankey engine click goes to that engine on the results page, in this tab', async () => {
    withData({ 'get-job-details': { ...BASE['get-job-details'], ...COMPLETE } });
    await clickNode(JobMonitoringSummary, 'dynamodb');
    expect(mockNavigate).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith('/analysis/results/job-1?engine=dynamodb#access-patterns-section');
    expect(openSpy).not.toHaveBeenCalled();
  });

  test('a job still running: no node is a link, and each says the details come with the completed assessment', async () => {
    await renderPage(JobMonitoringSummary);
    expect(container.querySelectorAll('.sankey-node')).toHaveLength(0);
    const off = container.querySelector('.sankey-node-off[data-node="dynamodb"]');
    expect(off.getAttribute('title')).toBe('job-summary.query-flow.details-after-completion');
    expect(mockNavigate).not.toHaveBeenCalled();
    expect(openSpy).not.toHaveBeenCalled();
  });
});

describe('the assignment gate', () => {
  test('with results: a click opens that engine on the results page in a new tab, never this one', async () => {
    withData({ results: { success: true, synthesis: {} } });
    await clickNode(AssignmentGate, 'dynamodb');
    expect(openSpy).toHaveBeenCalledTimes(1);
    expect(openSpy).toHaveBeenCalledWith(
      '/analysis/results/job-1?engine=dynamodb#access-patterns-section', '_blank', 'noopener',
    );
    expect(mockNavigate).not.toHaveBeenCalled();
    expect(container.textContent).not.toContain('assignment-gate.query-flow.older-version-note');
  });

  test('without results: no node is a link, and each says the details come with the completed assessment', async () => {
    await renderPage(AssignmentGate);
    expect(container.querySelectorAll('.sankey-node')).toHaveLength(0);
    const off = container.querySelector('.sankey-node-off[data-node="dynamodb"]');
    expect(off.getAttribute('title')).toBe('assignment-gate.query-flow.details-after-completion');
    expect(openSpy).not.toHaveBeenCalled();
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  test('a decision pending while results exist: the link opens, and the page says they are an earlier version', async () => {
    withData({
      results: { success: true, synthesis: {} },
      job: {
        database_name: 'discourse',
        progress: { stages: [{ name: 'WaitForAssignmentApproval', status: 'running' }] },
      },
    });
    await clickNode(AssignmentGate, 'dynamodb');
    expect(openSpy).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain('assignment-gate.query-flow.older-version-note');
  });
});
