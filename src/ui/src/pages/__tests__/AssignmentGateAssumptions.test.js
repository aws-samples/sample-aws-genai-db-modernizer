// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #1990: the assignment gate page states the assumptions block, the lines the chat gate
 * prints (`assumptions` of the assignments response; the UI only formats them). Same
 * plain-DOM stand-ins as AssignmentGateCacheReads.test.js.
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
jest.mock('@cloudscape-design/components/flashbar', () => ({ __esModule: true, default: () => null }));
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
jest.mock('@cloudscape-design/components/select', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/SummaryFallbackNotice', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/ChartSankey', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/PendingEngineChoices', () => ({ __esModule: true, default: () => null }));
jest.mock('../../classes/ApiManager');
jest.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useNavigate: () => jest.fn(),
}));
// One stable `t` that fills in the default wording, so the page text can be read.
jest.mock('react-i18next', () => {
  const t = (key, opts = {}) => {
    const { defaultValue, ...vars } = opts;
    let text = defaultValue !== undefined ? defaultValue : key;
    Object.entries(vars).forEach(([k, v]) => { text = text.split(`{{${k}}}`).join(v); });
    return text;
  };
  const mockResult = { t };
  return { useTranslation: () => mockResult };
});

global.IS_REACT_ACT_ENVIRONMENT = true;

function payloads(assumptions) {
  return {
    job: { database_name: 'wordpress' },
    assignments: {
      success: true,
      cache_reads: null,
      assumptions,
      assignment: {
        version: 1,
        query_assignments: [
          {
            query_id: 'q1', assigned_engine: 'aurora_mysql', confidence: 80, assignment_reason: 'fit',
            source_tables: ['wp_options'], in_scope: true,
          },
        ],
      },
    },
    'reality-check': {
      success: true,
      before_distribution: { aurora_mysql: 1 },
      after_distribution: { aurora_mysql: 1 },
    },
    phases: { success: true },
    triage: { success: true },
    collector: { success: true, queries: { query_patterns: [{ query_id: 'q1', query_type: 'SELECT', query_text: 'SELECT 1' }] } },
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

async function render(assumptions) {
  const data = payloads(assumptions);
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => Object.fromEntries(requests.map((r) => [r.id, data[r.id]])),
  }));
  await act(async () => {
    root.render(<AssignmentGate />);
  });
  return container.querySelector('[data-figure="gate-assumptions"]');
}

test('states each assumption line, as the chat gate prints it', async () => {
  const list = await render([
    'Growth: 5× (default), stated as a separate scenario',
    'Peak: not measured (the collection has no CloudWatch metrics covering 7 to 14 days)',
  ]);
  expect([...list.querySelectorAll('li')].map((li) => li.textContent)).toEqual([
    'Growth: 5× (default), stated as a separate scenario.',
    'Peak: not measured (the collection has no CloudWatch metrics covering 7 to 14 days).',
  ]);
});

test('shows no block for a response written before the assumptions', async () => {
  expect(await render(undefined)).toBeNull();
});
