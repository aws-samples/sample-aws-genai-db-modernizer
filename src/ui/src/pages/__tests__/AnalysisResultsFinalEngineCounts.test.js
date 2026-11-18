// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2358: the results page shows each engine's final query count, the figure synthesis counts
 * once into `facts.engine_query_counts`, not the reality check's own `after_distribution`
 * (a later assignment version, an `--auto` pick or a post-schema reroute, leaves it behind).
 *
 * Renders the real page with plain-DOM stand-ins for the Cloudscape components (it ships ESM
 * Jest cannot transform) and a ChartSankey stub that lists each flow's value, with a report
 * whose final version differs from the reality check's.
 */
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import AnalysisResults from '../AnalysisResults';
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
jest.mock('@cloudscape-design/components/alert', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/anchor-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/app-layout', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/badge', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/box', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/button', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/code-editor', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/column-layout', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/container', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/copy-to-clipboard', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/expandable-section', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/flashbar', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/header', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/help-panel', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/icon', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/input', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/key-value-pairs', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/link', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/modal', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/multiselect', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/pagination', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/pie-chart', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/property-filter', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/segmented-control', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/select', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/side-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/space-between', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/spinner', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/status-indicator', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/table', () => ({ __esModule: true, default: () => null }));
// Only the open tab's content, as Cloudscape shows it.
jest.mock('@cloudscape-design/components/tabs', () => ({
  __esModule: true,
  default: ({ tabs, activeTabId }) => {
    const open = tabs.find((tab) => tab.id === activeTabId) || tabs[0];
    return <div>{open ? open.content : null}</div>;
  },
}));
jest.mock('@cloudscape-design/components/text-filter', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/toggle-button', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/top-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/global-styles', () => ({}));
jest.mock('../../components/ChartSankey', () => ({
  __esModule: true,
  default: ({ data }) => (
    <div>
      {data.links.map((l) => (
        <span key={l.target} className="sankey-link" data-engine={l.target} data-value={l.value} />
      ))}
    </div>
  ),
}));
jest.mock('ace-builds', () => ({}));
jest.mock('ace-builds/webpack-resolver', () => ({}));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('../../classes/ApiManager');
jest.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useLocation: () => ({ search: '', hash: '' }),
  useNavigate: () => jest.fn(),
}));
jest.mock('react-i18next', () => {
  const mockResult = { t: (key) => key };
  return { useTranslation: () => mockResult };
});

global.IS_REACT_ACT_ENVIRONMENT = true;

// The saved discourse run (job b5dd22c9): v4 is the final version, v2 the reality check's.
const FINAL = { aurora_postgresql: 797, dynamodb: 694, documentdb: 163 };
const REALITY_CHECK = { aurora_postgresql: 796, dynamodb: 707, documentdb: 151 };

const report = (facts) => ({
  success: true,
  synthesis: {
    database_name: 'discourse',
    facts,
    reality_check: { after_distribution: REALITY_CHECK },
  },
});

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

async function renderWith(results) {
  ApiManager.mockImplementation(() => ({
    execute: async () => ({ results, schemas: { success: false }, collector: { success: false } }),
  }));
  await act(async () => {
    root.render(<AnalysisResults />);
  });
}

const shown = () =>
  Object.fromEntries(
    [...container.querySelectorAll('.sankey-link')].map((n) => [n.dataset.engine, Number(n.dataset.value)]),
  );

test('the query flow shows the final version counts, not the reality check ones', async () => {
  await renderWith(report({ engine_query_counts: FINAL }));
  expect(shown()).toEqual(FINAL);
});

test('a report saved before the figure shows the reality check distribution', async () => {
  await renderWith(report({ engines: [] }));
  expect(shown()).toEqual(REALITY_CHECK);
});

// #2345: the target engines are one fact, facts.target_engines, which the assessments list reads too.
const badges = () => [...container.querySelectorAll('[data-engine-badge]')].map((n) => n.dataset.engineBadge);

test('the target engine badges are the facts target engines, cache layer included', async () => {
  const synthesis = report({ engine_query_counts: FINAL, target_engines: ['documentdb', 'elasticache'] });
  synthesis.synthesis.cache_overlay = { engine: 'elasticache' };
  await renderWith(synthesis);
  expect(badges().slice(0, 2)).toEqual(['documentdb', 'elasticache']);
  expect(container.textContent).toContain('cache-layer.engine-badge-suffix');
});

test('a report saved before the fact derives them from the counts, as before', async () => {
  await renderWith(report({ engine_query_counts: FINAL }));
  expect(badges().slice(0, 3)).toEqual(['aurora_postgresql', 'dynamodb', 'documentdb']);
});
