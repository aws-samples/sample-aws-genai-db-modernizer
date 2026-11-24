// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2519: a saved edit's feasibility findings show on the Assignment review page as one
 * warning, each finding's own message as the API wrote it; a save with none adds no
 * warning, from the per-table choices save and from the per-query overrides save.
 * Renders the real AssignmentGate page with the plain-DOM stand-ins of
 * AssignmentGatePinnedFit.test.js; the pending-choices panel is a button that saves
 * "decide for me", each engine picker a button per option, and `t` returns its key.
 */
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import AssignmentGate from '../AssignmentGate';
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

jest.mock('@cloudscape-design/components/app-layout', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/side-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/space-between', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/header', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/container', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/box', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/column-layout', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/flashbar', () => ({
  __esModule: true,
  default: ({ items }) => (
    <div>
      {items.map((item) => (
        <div key={item.id} className={`flash flash--${item.type}`}>
          <span className="flash-header">{item.header}</span>
          {item.content}
        </div>
      ))}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/status-indicator', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/badge', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/spinner', () => ({ __esModule: true, default: () => null }));
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
jest.mock('@cloudscape-design/components/select', () => ({
  __esModule: true,
  default: ({ options, onChange }) => (
    <div className="engine-picker">
      {(options || []).map((o) => (
        <button key={o.value} onClick={() => onChange && onChange({ detail: { selectedOption: o } })}>
          {`pick-${o.value}`}
        </button>
      ))}
    </div>
  ),
}));
jest.mock('../../components/SummaryFallbackNotice', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/ChartSankey', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/PendingEngineChoices', () => ({
  __esModule: true,
  default: ({ onSubmit }) => <button onClick={() => onSubmit({ decide_for_me: true })}>decide-for-me</button>,
}));
jest.mock('../../classes/ApiManager');
jest.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useNavigate: () => jest.fn(),
}));
// One stable `t`: the page refetches when it changes identity.
jest.mock('react-i18next', () => {
  const mockResult = { t: (key) => key };
  return { useTranslation: () => mockResult };
});

global.IS_REACT_ACT_ENVIRONMENT = true;

const ADVISORY = 'Table shop.users has its source of truth on Aurora PostgreSQL and a replica on DynamoDB.';
const STANDING = 'Co-dependent queries (q1, q2) share a significant JOIN but are split.';

function payloads(saved) {
  return {
    job: { database_name: 'shop', progress: { stages: [{ name: 'WaitForAssignmentApproval', status: 'in-progress' }] } },
    assignments: {
      success: true,
      assignment: {
        version: 1,
        query_assignments: [
          { query_id: 'q1', assigned_engine: 'aurora_postgresql', confidence: 80, assignment_reason: 'test', source_tables: ['shop.users'], in_scope: true },
        ],
      },
      pending_choices: [],
    },
    'reality-check': { success: true },
    phases: { success: true },
    triage: { success: true },
    collector: { success: true, queries: { query_patterns: [{ query_id: 'q1', query_type: 'SELECT', query_text: 'SELECT 1' }] } },
    choices: saved,
    save: saved,
  };
}

let container;
let root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function save(saved) {
  const data = payloads(saved);
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => Object.fromEntries(requests.map((r) => [r.id, data[r.id]])),
  }));
  await act(async () => {
    root.render(<AssignmentGate />);
  });
  const button = [...container.querySelectorAll('button')].find((b) => b.textContent === 'decide-for-me');
  await act(async () => {
    button.click();
  });
  return container.querySelector('.flash--warning');
}

test("a saved edit's findings show as one warning, each message in order", async () => {
  const warning = await save({
    success: true,
    feasibility_findings: [
      { kind: 'co_dependency_split', severity: 'blocking', engines: [], query_ids: ['q1', 'q2'], message: STANDING },
      { kind: 'read_write_split', severity: 'advisory', engines: [], query_ids: ['q1'], message: ADVISORY },
    ],
  });
  expect(warning.querySelector('.flash-header').textContent).toBe('assignment-gate.flash.feasibility-findings');
  expect([...warning.querySelectorAll('[data-testid="feasibility-findings"] li')].map((li) => li.textContent)).toEqual([
    STANDING,
    ADVISORY,
  ]);
});

test('a saved edit with no finding adds no warning', async () => {
  expect(await save({ success: true, feasibility_findings: [] })).toBeNull();
  expect(container.querySelector('.flash--success')).not.toBeNull();
});

test("the per-query overrides save shows the edit's findings too", async () => {
  const data = payloads({
    success: true,
    feasibility_findings: [{ kind: 'read_write_split', severity: 'advisory', engines: [], query_ids: ['q1'], message: ADVISORY }],
  });
  const calls = [];
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => {
      calls.push(...requests);
      return Object.fromEntries(requests.map((r) => [r.id, data[r.id]]));
    },
  }));
  await act(async () => {
    root.render(<AssignmentGate />);
  });
  const click = async (text) => {
    const button = [...container.querySelectorAll('button')].find((b) => b.textContent.startsWith(text));
    await act(async () => {
      button.click();
    });
  };
  await click('assignment-gate.actions.review-per-query');
  await click('pick-dynamodb');
  await click('assignment-gate.actions.save-overrides');
  const save = calls.find((c) => c.id === 'save');
  expect(save.params.overrides).toEqual([{ query_id: 'q1', assigned_engine: 'dynamodb' }]);
  const warning = container.querySelector('.flash--warning');
  expect([...warning.querySelectorAll('[data-testid="feasibility-findings"] li')].map((li) => li.textContent)).toEqual([ADVISORY]);
});
