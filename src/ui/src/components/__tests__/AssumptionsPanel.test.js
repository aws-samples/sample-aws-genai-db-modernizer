// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * (#2316: on the real Cloudscape components.) #1991: the results page's assumptions panel shows each assumption a customer can set
 * with the line the report states (value and origin), POSTs a change in the assumptions
 * file's own form, shows the server's reason when a change is refused or unavailable, and
 * reloads the results after one. It checks no range itself: the server does.
 */
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import AssumptionsPanel from '../AssumptionsPanel';
import ApiManager from '../../classes/ApiManager';
import { errorFromResponse } from '../../utils/apiErrors';

jest.mock('../../classes/ApiManager');

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

const t = (key, vars = {}) => Object.entries(vars).reduce((s, [k, v]) => `${s} ${k}=${v}`, key);

const ROWS = [
  {
    name: 'growth', key: 'growth_multiplier', min: 0, min_included: false, max: 100,
    reruns: ['synthesis', 'report'], value: 5, origin: 'default', origin_label: 'default',
    line: 'Growth: 5× (default), shown as a separate what-if scenario', available: true,
    unavailable_reason: null, refusal_kind: null, notes: [],
  },
  {
    name: 'peak', key: 'peak_to_avg_ratio', min: 1, min_included: true, max: 100,
    reruns: ['schema_design', 'synthesis', 'report'], value: null, origin: 'default',
    origin_label: 'default', line: 'Peak: not measured', available: false,
    unavailable_reason: 'The schema designs were written with a model. Nothing was changed.',
    refusal_kind: 'needs_model_designs', notes: [],
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
  const input = container.querySelector(`input[aria-label$="name.${name}"]`);
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

async function clickApply(name) {
  await act(async () => {
    [...container.querySelectorAll('button')].find((b) => b.textContent.includes('assumptions-panel.apply')).click();
  });
}

test('shows each assumption as a row: name, value and origin (#2316)', async () => {
  mockApi({ success: true });
  await render();
  const rows = [...container.querySelectorAll('tbody tr')];
  expect(rows).toHaveLength(2);
  expect(rows[0].textContent).toContain('analysis-results.assumptions-panel.name.growth');
  expect(container.querySelector('[data-figure="assumption-value"][data-assumption="growth"]').textContent).toBe('5×');
  expect(rows[0].textContent).toContain('analysis-results.assumptions-panel.origin.default');
  expect(container.querySelector('[data-figure="assumption-value"][data-assumption="peak"]').textContent)
    .toBe('analysis-results.assumptions-panel.value.not-measured');
});

test('the report line is behind an Info link', async () => {
  mockApi({ success: true });
  await render();
  expect(container.querySelector('[data-figure="assumption-line"]')).toBeNull();
  const info = container.querySelector('[aria-label^="analysis-results.assumptions-panel.info-label"]');
  await act(async () => { info.click(); });
  expect(container.querySelector('[data-figure="assumption-line"]').textContent)
    .toBe('Growth: 5× (default), shown as a separate what-if scenario.');
});

function mockRows(rows) {
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => Object.fromEntries(requests.map((r) => [r.id, { success: true, assumptions: rows }])),
  }));
}
const refusedAll = (kind) => ROWS.map((row) => ({ ...row, available: false, refusal_kind: kind, unavailable_reason: 'synthesis llm-mode none' }));

test('a setting this server can change has an input; a refused one says why, in fixed words', async () => {
  mockApi({ success: true });
  await render();
  expect(container.querySelectorAll('input')).toHaveLength(1);
  expect(container.querySelector('[data-figure="assumptions-fixed"]')).toBeNull();
  const note = container.querySelector('[data-figure="assumption-refused"]');
  expect(note.textContent).toBe('analysis-results.assumptions-panel.refused.needs_model_designs');
  expect(container.textContent).not.toContain('Nothing was changed');
});

test.each(['needs_model_summary', 'needs_model_designs', 'no_results', 'not_finished'])(
  'when nothing can change the note names the refusal kind %s in fixed words, with no server text',
  async (kind) => {
    mockRows(refusedAll(kind));
    await render();
    expect(container.querySelectorAll('[data-figure="assumptions-fixed"]')).toHaveLength(1);
    expect(container.querySelector('[data-figure="assumptions-fixed"]').textContent)
      .toBe(`analysis-results.assumptions-panel.fixed.${kind}`);
    expect(container.querySelectorAll('input')).toHaveLength(0);
    expect([...container.querySelectorAll('button')].some((b) => b.textContent.includes('apply'))).toBe(false);
    expect(container.textContent).not.toContain('synthesis');
    expect(container.textContent).not.toContain('llm-mode');
    expect(container.querySelector('[data-figure="assumption-refused"]')).toBeNull();
    expect(container.querySelectorAll('tbody tr')).toHaveLength(2);
  },
);

test('a kind the page does not know falls back to a plain note', async () => {
  mockRows(refusedAll('something_new'));
  await render();
  expect(container.querySelector('[data-figure="assumptions-fixed"]').textContent)
    .toBe('analysis-results.assumptions-panel.fixed.other');
});

test('a job with no results or not finished says that before the model reason', async () => {
  mockRows([{ ...refusedAll('needs_model_designs')[0] }, { ...refusedAll('no_results')[1] }]);
  await render();
  expect(container.querySelector('[data-figure="assumptions-fixed"]').textContent)
    .toBe('analysis-results.assumptions-panel.fixed.no_results');
});

test('the Info link says whether its sentence is shown (#2316)', async () => {
  mockApi({ success: true });
  await render();
  const info = container.querySelector('[aria-label^="analysis-results.assumptions-panel.info-label"]');
  expect(info.getAttribute('aria-expanded')).toBe('false');
  expect(info.getAttribute('aria-controls')).toBe('assumption-line-growth');
  await act(async () => { info.click(); });
  expect(info.getAttribute('aria-expanded')).toBe('true');
  expect(container.querySelector('#assumption-line-growth [data-figure="assumption-line"]')).not.toBeNull();
});

const REPORT_ITEMS = [
  { key: 'growth', setting: 5, origin: 'default', line: 'Growth: 5×' },
  { key: 'rate_window', setting: null, origin: 'collected', basis: 'uptime', line: 'Rate window: the server uptime' },
];

test('the report assumptions show when the panel request fails, with the shared error message', async () => {
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => Object.fromEntries(requests.map((r) => [r.id, { success: false }])),
  }));
  await act(async () => { root.render(<AssumptionsPanel jobId="job-1" t={t} onChanged={jest.fn()} items={REPORT_ITEMS} />); });
  expect(container.querySelectorAll('tbody tr')).toHaveLength(2);
  expect(container.querySelector('[data-figure="assumption-value"][data-assumption="rate_window"]').textContent)
    .toContain('analysis-results.assumptions-panel.value.rate-uptime');
  expect(container.querySelector('[data-figure="assumptions-load-failed"]').textContent).toContain('errors.request.unreachable');
  expect(container.querySelectorAll('input')).toHaveLength(0);
});

test('the report items, when the page has them, are the rows (#2316)', async () => {
  mockApi({ success: true });
  const items = [
    { key: 'growth', setting: 5, origin: 'default', line: 'Growth: 5×' },
    { key: 'pricing', setting: 'us-east-1', origin: 'default', line: 'Region and prices: us-east-1' },
  ];
  await act(async () => { root.render(<AssumptionsPanel jobId="job-1" t={t} onChanged={jest.fn()} items={items} />); });
  expect(container.querySelectorAll('tbody tr')).toHaveLength(2);
  expect(container.querySelector('[data-figure="assumption-value"][data-assumption="pricing"]').textContent).toBe('us-east-1');
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
  mockApi({ success: false, status: 422, error: errorFromResponse(422, 'x', JSON.stringify({ detail: 'growth must be between 0 (excluded) and 100, got 500' })) });
  const onChanged = await render();
  await type('growth', '500');
  await clickApply('growth');
  expect(calls.find((c) => c.method === 'POST').params).toEqual({ growth: 500 });
  expect(container.querySelector('[data-figure="assumption-message"]').textContent)
    .toContain('growth must be between 0 (excluded) and 100');
  expect(onChanged).not.toHaveBeenCalled();
});

test.each([
  ['unsafe text', '<img src=x onerror=alert(1)> see https://10.0.0.5:8000/x'],
  ['a very long text', 'x'.repeat(400)],
])('%s as the reason falls back to the fixed message (#2346)', async (_, detail) => {
  mockApi({ success: false, status: 422, error: errorFromResponse(422, 'x', JSON.stringify({ detail })) });
  const onChanged = await render();
  await type('growth', '500');
  await clickApply('growth');
  expect(container.querySelector('[data-figure="assumption-message"]').textContent)
    .toBe('analysis-results.assumptions-panel.failed errors.request.rejected-form');
  expect(onChanged).not.toHaveBeenCalled();
});

test('with no report assumptions and a failed request, only the shared error shows', async () => {
  ApiManager.mockImplementation(() => ({
    execute: async (requests) => Object.fromEntries(requests.map((r) => [r.id, { success: false }])),
  }));
  await render();
  expect(container.querySelectorAll('tbody tr')).toHaveLength(0);
  expect(container.querySelector('[data-figure="assumptions-load-failed"]').textContent).toContain('errors.request.unreachable');
});

test('a successful request with no rows and no report assumptions shows nothing', async () => {
  mockRows([]);
  await render();
  expect(container.querySelector('#assumptions-panel')).toBeNull();
});
