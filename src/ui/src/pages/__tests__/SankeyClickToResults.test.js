// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2300: clicking an engine node in the Sankey chart (assignment gate and
 * job monitoring summary) goes to that engine's part of the results page in the
 * same tab. It used to open the old pattern-analysis page in a new tab.
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
  default: ({ data, onNodeClick }) => (
    <div>
      {data.nodes.map((n) => (
        <button key={n.id} className="sankey-node" data-node={n.id} onClick={() => onNodeClick(n.id)} />
      ))}
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

const DATA = {
  job: { database_name: 'discourse' },
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
      current_stage: 'WaitForAssignmentApproval',
      stages: [{ name: 'WaitForAssignmentApproval', status: 'completed' }],
    },
  },
  'get-agent-details': { success: true },
  'get-execution-history': { success: true },
  'get-results': { success: true },
};

let container;
let root;
let openSpy;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  mockNavigate.mockClear();
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

async function clickNode(Page, nodeId) {
  await act(async () => {
    root.render(<Page />);
  });
  const node = container.querySelector(`.sankey-node[data-node="${nodeId}"]`);
  expect(node).not.toBeNull();
  await act(async () => {
    node.click();
  });
}

describe.each([
  ['the assignment gate', AssignmentGate],
  ['the job monitoring summary', JobMonitoringSummary],
])('a Sankey engine click on %s', (_name, Page) => {
  test('goes to that engine on the results page, in the same tab', async () => {
    await clickNode(Page, 'dynamodb');
    expect(mockNavigate).toHaveBeenCalledTimes(1);
    expect(mockNavigate).toHaveBeenCalledWith('/analysis/results/job-1?engine=dynamodb#access-patterns-section');
    expect(openSpy).not.toHaveBeenCalled();
  });
});
