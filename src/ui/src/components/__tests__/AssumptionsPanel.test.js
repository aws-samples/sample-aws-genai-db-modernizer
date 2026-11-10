// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #1991: the results page's assumptions panel shows each assumption a customer can set
 * with the line the report states (value and origin), POSTs a change in the assumptions
 * file's own form, shows the server's reason when a change is refused or unavailable, and
 * reloads the results after one. It checks no range itself: the server does.
 */
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import AssumptionsPanel from '../AssumptionsPanel';
import ApiManager from '../../classes/ApiManager';

function mockPassThrough({ children, header, ...props }) {
  const data = Object.fromEntries(Object.entries(props).filter(([k]) => k.startsWith('data-')));
  return <div {...data}>{header}{children}</div>;
}

jest.mock('@cloudscape-design/components/box', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/container', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/header', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/space-between', () => ({ __esModule: true, default: mockPassThrough }));
jest.mock('@cloudscape-design/components/button', () => ({
  __esModule: true,
  default: ({ children, onClick, disabled }) => <button onClick={onClick} disabled={disabled}>{children}</button>,
}));
jest.mock('@cloudscape-design/components/input', () => ({
  __esModule: true,
  default: ({ value, onChange, ariaLabel }) => (
    <input aria-label={ariaLabel} value={value} onChange={(e) => onChange({ detail: { value: e.target.value } })} />
  ),
}));
jest.mock('../../classes/ApiManager');

global.IS_REACT_ACT_ENVIRONMENT = true;

const t = (key, vars = {}) => Object.entries(vars).reduce((s, [k, v]) => `${s} ${k}=${v}`, key);

const ROWS = [
  {
    name: 'growth', key: 'growth_multiplier', min: 0, min_included: false, max: 100,
    reruns: ['synthesis', 'report'], value: 5, origin: 'default', origin_label: 'default',
    line: 'Growth: 5× (default), shown as a separate what-if scenario', available: true,
    unavailable_reason: null, notes: [],
  },
  {
    name: 'peak', key: 'peak_to_avg_ratio', min: 1, min_included: true, max: 100,
    reruns: ['schema_design', 'synthesis', 'report'], value: null, origin: 'default',
    origin_label: 'default', line: 'Peak: not measured', available: false,
    unavailable_reason: 'The schema designs were written with a model. Nothing was changed.', notes: [],
  },
];

let container;
let root;
let calls;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  calls = [];
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function mockApi(postResult) {
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => Object.fromEntries(requests.map((r) => {
      calls.push(r);
      if (r.method === 'GET') return [r.id, { success: true, assumptions: ROWS }];
      return [r.id, postResult];
    })),
  }));
}

async function render(onChanged = jest.fn()) {
  await act(async () => {
    root.render(<AssumptionsPanel jobId="job-1" t={t} onChanged={onChanged} />);
  });
  return onChanged;
}

async function type(name, value) {
  const input = container.querySelector(`[data-assumption="${name}"] input`);
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

async function clickApply(name) {
  await act(async () => {
    container.querySelector(`[data-assumption="${name}"] button`).click();
  });
}

test('states each assumption with the line the report states', async () => {
  mockApi({ success: true });
  await render();
  const lines = [...container.querySelectorAll('[data-figure="assumption-line"]')].map((n) => n.textContent);
  expect(lines).toEqual(['Growth: 5× (default), shown as a separate what-if scenario.', 'Peak: not measured.']);
});

test('a change unavailable here shows the server reason and offers no input', async () => {
  mockApi({ success: true });
  await render();
  const peak = container.querySelector('[data-assumption="peak"]');
  expect(peak.querySelector('input')).toBeNull();
  expect(peak.querySelector('[data-figure="assumption-unavailable"]').textContent).toContain('Nothing was changed');
});

test('posts the change in the assumptions file form and reloads the results', async () => {
  mockApi({ success: true, reran: ['synthesis', 'report'], notes: [] });
  const onChanged = await render();
  await type('growth', '3');
  await clickApply('growth');
  const post = calls.find((c) => c.method === 'POST');
  expect(post.path).toBe('assessments/job-1/assumptions');
  expect(post.params).toEqual({ growth: 3 });
  expect(onChanged).toHaveBeenCalledTimes(1);
  expect(container.querySelector('[data-figure="assumption-message"]').textContent).toContain('applied');
});

test('a value out of range is sent as typed and the server reason is shown', async () => {
  mockApi({ success: false, error: new Error('growth must be between 0 (excluded) and 100, got 500') });
  const onChanged = await render();
  await type('growth', '500');
  await clickApply('growth');
  expect(calls.find((c) => c.method === 'POST').params).toEqual({ growth: 500 });
  expect(container.querySelector('[data-figure="assumption-message"]').textContent)
    .toContain('growth must be between 0 (excluded) and 100');
  expect(onChanged).not.toHaveBeenCalled();
});

test('shows nothing when the job has no assumptions to set', async () => {
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => Object.fromEntries(requests.map((r) => [r.id, { success: false }])),
  }));
  await render();
  expect(container.querySelector('#assumptions-panel')).toBeNull();
});
