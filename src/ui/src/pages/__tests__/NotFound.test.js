// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2393: the page for an unknown route. A heading, short plain text naming the path,
 * links to the dashboard and the assessments list, and a document title. The
 * Cloudscape components are the real ones; only the app layout and navigation
 * (page chrome, not under test) are stand-ins.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, useLocation } from 'react-router-dom';
import NotFound from '../NotFound';
import AppRoutes from '../../AppRoutes';

jest.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, opts) => {
      const text = jest.requireActual('../../locales/en.json')[key];
      return text === undefined ? key : text.replace('{{path}}', (opts && opts.path) || '');
    },
  }),
}));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('../../classes/ApiManager', () => ({
  __esModule: true,
  default: function ApiManager() {
    this.execute = () => new Promise(() => {});
  },
}));
jest.mock('@cloudscape-design/components/app-layout', () => ({
  __esModule: true,
  default: ({ content, breadcrumbs }) => <main>{breadcrumbs}{content}</main>,
}));
jest.mock('@cloudscape-design/components/side-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => ({
  __esModule: true,
  default: ({ items }) => <ol>{items.map((i) => <li key={i.text}>{i.text}</li>)}</ol>,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

let container;
let root;
let location;
function Probe() {
  location = useLocation();
  return null;
}

async function renderAt(path, tree = <NotFound />) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(<MemoryRouter initialEntries={[path]}>{tree}<Probe /></MemoryRouter>);
  });
}

beforeEach(() => {
  document.title = 'Database Modernizer Assessment Tool';
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test('has one h1, names the path and says what happened in plain words', async () => {
  await renderAt('/analysis/nope');
  const headings = container.querySelectorAll('h1');
  expect(headings).toHaveLength(1);
  expect(headings[0].textContent).toBe('Page not found');
  expect(container.textContent).toContain('No page lives at /analysis/nope.');
  expect(container.textContent).not.toMatch(/https?:|404|Failed to fetch/);
});

test('links to the dashboard and to the assessments section of it', async () => {
  await renderAt('/nope');
  const links = [...container.querySelectorAll('a')].map((a) => [a.textContent, a.getAttribute('href')]);
  expect(links).toContainEqual(['Go to the dashboard', '/dashboard']);
  expect(links).toContainEqual(['Open the assessments list', '/dashboard#assessments']);
});

test('the assessments link keeps the anchor when followed', async () => {
  await renderAt('/nope');
  const link = [...container.querySelectorAll('a')].find((a) => a.textContent === 'Open the assessments list');
  await act(async () => { link.click(); });
  expect(location.pathname).toBe('/dashboard');
  expect(location.hash).toBe('#assessments');
});

test('a link moves to the dashboard without a page load', async () => {
  await renderAt('/nope', <><NotFound /></>);
  const link = [...container.querySelectorAll('a')].find((a) => a.textContent === 'Go to the dashboard');
  await act(async () => { link.click(); });
  expect(location.pathname).toBe('/dashboard');
});

test('the breadcrumb ends on the page the reader is on', async () => {
  await renderAt('/nope');
  const crumbs = [...container.querySelectorAll('li')].map((li) => li.textContent);
  expect(crumbs[crumbs.length - 1]).toBe('Page not found');
});

test('sets the document title while shown and restores it after', async () => {
  await renderAt('/nope');
  expect(document.title).toBe('Page not found - Database Modernizer Assessment Tool');
  act(() => root.unmount());
  expect(document.title).toBe('Database Modernizer Assessment Tool');
  root = createRoot(container);
});

test('the real route table shows this page for an unknown nested route', async () => {
  await renderAt('/analysis/results', <AppRoutes />);
  expect(container.querySelector('h1').textContent).toBe('Page not found');
  expect(location.pathname).toBe('/analysis/results');
});
