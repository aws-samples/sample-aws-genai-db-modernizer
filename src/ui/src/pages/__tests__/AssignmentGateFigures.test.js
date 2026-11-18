// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2326: the assignment review states the recommendation in one sentence, then its figures
 * (queries, source tables, engines kept, engines removed, cache candidates) as key-value
 * pairs, and the hottest cache candidates as a table. Every figure is the assignments API's
 * `gate_facts` (src/shared/gate_facts.py, the chat gate's source too); the page never
 * counts the reality check's distributions or the collector's tables. Real Cloudscape
 * components; ChartSankey and the app header are stubbed.
 */
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import AssignmentGate from '../AssignmentGate';
import ApiManager from '../../classes/ApiManager';

jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/ChartSankey', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/PendingEngineChoices', () => ({ __esModule: true, default: () => null }));
jest.mock('../../classes/ApiManager');
jest.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useNavigate: () => jest.fn(),
}));
jest.mock('react-i18next', () => {
  const en = require('../../locales/en.json');
  const t = (key, opts = {}) => {
    const { defaultValue, count, ...vars } = opts;
    let text = (count !== undefined && count !== 1 && en[`${key}_other`])
      || (count === 1 && en[`${key}_one`]) || en[key] || defaultValue || key;
    Object.entries({ ...vars, count }).forEach(([k, v]) => { text = text.split(`{{${k}}}`).join(v); });
    return text;
  };
  const mockResult = { t };
  return { useTranslation: () => mockResult };
});

global.IS_REACT_ACT_ENVIRONMENT = true;

// The facts deliberately differ from what counting the reality check (2 queries) or the
// collector (1 table) would give, so a page that counts shows the wrong figure.
const GATE_FACTS = {
  rows: [
    { engine: 'aurora_mysql', before: 59, after: 68, share_percent: 63.6, dropped: false },
    { engine: 'dynamodb', before: 38, after: 39, share_percent: 36.4, dropped: false },
    { engine: 'documentdb', before: 10, after: 0, share_percent: 0, dropped: true },
  ],
  surviving: ['aurora_mysql', 'dynamodb', 'elasticache'],
  cache_engine: 'elasticache',
  // #2326: Queries is the report's total, the in-scope queries plus the 3 with no engine in
  // the customer's scope, never the engines' total_after.
  total_after: 107,
  unservable_query_count: 3,
  query_total_count: 110,
  source_table_count: 50,
  cache_candidates: {
    read_count: 19,
    call_share_percent: 82.1,
    reads: [
      { query_id: 'q1', tables: ['wordpress.wp_options'], owner: 'dynamodb', pattern: 'point_lookup', calls_per_second: 75.8, call_share_percent: 34.2 },
      { query_id: 'q2', tables: ['wordpress.wp_postmeta'], owner: 'aurora_mysql', pattern: 'point_lookup', calls_per_second: 14.6, call_share_percent: 6.6 },
    ],
    more: 17,
  },
};

function payloads(gateFacts) {
  return {
    job: { database_name: 'wordpress', progress: { stages: [{ name: 'WaitForAssignmentApproval', status: 'in-progress' }] } },
    assignments: {
      success: true,
      gate_facts: gateFacts,
      assignment: {
        version: 1,
        query_assignments: [
          { query_id: 'q1', assigned_engine: 'aurora_mysql', confidence: 80, assignment_reason: 'fit', source_tables: ['wp_options'], in_scope: true },
        ],
      },
    },
    'reality-check': { success: true, before_distribution: { aurora_mysql: 2 }, after_distribution: { aurora_mysql: 2 } },
    phases: { success: true },
    triage: { success: true },
    collector: {
      success: true,
      database_schema: { tables: [{ name: 'wp_options' }] },
      queries: { query_patterns: [{ query_id: 'q1', query_type: 'SELECT', query_text: 'SELECT 1' }] },
    },
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

async function render(gateFacts) {
  const data = payloads(gateFacts);
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => Object.fromEntries(requests.map((r) => [r.id, data[r.id]])),
  }));
  await act(async () => { root.render(<AssignmentGate />); });
}

// Each key-value pair as "label: value text", in order.
const figures = () => {
  const block = container.querySelector('[data-figure="gate-figures"]');
  if (!block) return null;
  const labels = [...block.querySelectorAll('dt')].map((n) => n.textContent.trim());
  const values = [...block.querySelectorAll('dd')].map((n) => n.textContent.replace(/\s+/g, ' ').trim());
  return labels.map((l, i) => `${l}: ${values[i]}`);
};

test('one sentence, then the figures from the gate facts', async () => {
  await render(GATE_FACTS);
  const hero = container.querySelector('.gate-hero__summary').textContent;
  expect(hero).toBe('Your wordpress workload has 110 access patterns across 50 tables.');
  expect(figures()).toEqual([
    'Queries: 110',
    'Source tables: 50',
    'Engines kept: Aurora MySQL 68DynamoDB 39',
    'Engines removed: DocumentDB (10 queries before)',
    'Cache candidates: 1982.1% of calls',
  ]);
});

test('no engine removed and no cache layer: says none and leaves the cache figure out', async () => {
  await render({
    ...GATE_FACTS,
    rows: GATE_FACTS.rows.filter((r) => !r.dropped),
    surviving: ['aurora_mysql', 'dynamodb'],
    cache_candidates: null,
  });
  expect(figures()).toEqual([
    'Queries: 110',
    'Source tables: 50',
    'Engines kept: Aurora MySQL 68DynamoDB 39',
    'Engines removed: None',
  ]);
  expect(container.querySelector('[data-figure="gate-cache-candidates"]')).toBeNull();
});

test('the hottest cache candidates are a table, with how many more under it', async () => {
  await render(GATE_FACTS);
  const table = container.querySelector('[data-figure="gate-cache-candidates"]');
  expect(table.textContent).toContain('Hottest cache candidates');
  const headers = [...table.querySelectorAll('th')].map((th) => th.textContent.trim());
  expect(headers).toEqual(['Table', 'Owner', 'Pattern', 'Calls/s', 'Share of calls']);
  const rows = [...table.querySelectorAll('tbody tr')].map((tr) => [...tr.querySelectorAll('td')].map((td) => td.textContent.trim()));
  expect(rows).toEqual([
    ['wp_options', 'DynamoDB', 'point lookup', '75.8', '34.2%'],
    ['wp_postmeta', 'Aurora MySQL', 'point lookup', '14.6', '6.6%'],
  ]);
  expect(table.textContent).toContain('and 17 more');
  expect(container.querySelector('.cache-candidate-reads ul')).toBeNull();
});

test('an API without gate facts shows no figures and no counted sentence', async () => {
  await render(undefined);
  expect(figures()).toBeNull();
  expect(container.querySelector('.gate-hero__summary').textContent).not.toMatch(/access pattern/);
});
