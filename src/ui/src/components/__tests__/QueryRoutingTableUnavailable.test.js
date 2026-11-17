// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2308: a query list that could not be read is a section-level alert with no count,
 * on the real Cloudscape components. A list that loaded and is empty says "No queries
 * match." with its true count.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import QueryRoutingTable from '../QueryRoutingTable';

const en = require('../../locales/en.json');
const t = (key, opts) => (en[key] || key).replace('{{count}}', opts && opts.count);

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

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

const render = (props) => act(() => {
  root.render(<QueryRoutingTable rows={[]} loading={false} error={null} hasLedger truncated={false} onOpen={() => {}} t={t} {...props} />);
});

test('an unreadable list shows the alert and no "(0)"', () => {
  render({ error: 'not-found' });
  expect(container.textContent).toContain("Query details aren't available for this assessment");
  expect(container.textContent).toContain('The service has no query list for this assessment.');
  expect(container.textContent).not.toContain('(0)');
  expect(container.textContent).not.toContain('not-found');
});

test('a server error and an unreachable API read differently from not found', () => {
  render({ error: 'server' });
  expect(container.textContent).toContain('The service returned an error.');
  render({ error: 'unreachable' });
  expect(container.textContent).toContain('The service could not be reached.');
  expect(container.textContent).not.toContain('returned an error');
});

test('a list that loaded and is empty keeps its true count', () => {
  render({});
  expect(container.textContent).toContain('(0)');
  expect(container.textContent).not.toContain("aren't available");
});

test('a list still loading shows no count', () => {
  render({ loading: true });
  expect(container.textContent).not.toContain('(0)');
});
