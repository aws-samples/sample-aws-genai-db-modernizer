/**
 * #1329: the approval gate and the job summary show the queries no engine in the
 * customer's engine scope can serve (the assignment's `unservable_queries`), so a
 * customer never approves without seeing them. Cloudscape components are plain DOM
 * stand-ins (see UnservableQueriesCallout.test.js); the component and the English
 * strings are real.
 */
import { readFileSync } from 'fs';
import { join } from 'path';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import UnservableAssignmentNotice from '../UnservableAssignmentNotice';

jest.mock('@cloudscape-design/components/alert', () => ({
  __esModule: true,
  default: ({ header, children, type }) => <div data-alert={type}><h3>{header}</h3>{children}</div>,
}));
jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;
let t;

beforeAll(async () => {
  const i18n = createInstance();
  await i18n.init({
    resources: { en: { translation: en } },
    lng: 'en',
    fallbackLng: 'en',
    keySeparator: false,
    interpolation: { escapeValue: false },
  });
  t = i18n.t.bind(i18n);
});

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const row = (id) => ({
  query_id: id,
  rule: 'fallback.no_score',
  reason: "No engine in the customer's engine scope (DynamoDB) can serve this query.",
  closest_engine: 'aurora_mysql',
  gates: [],
});

test('names the count, each query and how to serve them', () => {
  act(() => root.render(<UnservableAssignmentNotice unservable={[row('q9'), row('q8')]} t={t} />));
  const text = container.textContent;
  expect(container.querySelector('[data-alert]').dataset.alert).toBe('warning');
  expect(text).toContain("2 queries have no engine in the customer's engine scope");
  expect(text).toContain(en['assignment-unservable.intro']);
  expect(text).toContain('To serve them, start a new assessment whose scope adds Aurora MySQL, or keep them on the source database.');
  expect([...container.querySelectorAll('[data-unservable-query]')].map(n => n.textContent)).toEqual(['q9', 'q8']);
  expect(text).toContain("q9: No engine in the customer's engine scope (DynamoDB) can serve this query.");
});

test('counts the ones past the first twenty', () => {
  const rows = Array.from({ length: 22 }, (_, i) => row(`q${i}`));
  act(() => root.render(<UnservableAssignmentNotice unservable={rows} t={t} />));
  expect(container.querySelectorAll('[data-unservable-query]').length).toBe(20);
  expect(container.textContent).toContain('2 more queries are listed in the assignment.');
});

test('shows nothing with no unservable query or an assignment written before them', () => {
  act(() => root.render(<UnservableAssignmentNotice unservable={[]} t={t} />));
  expect(container.textContent).toBe('');
  act(() => root.render(<UnservableAssignmentNotice unservable={undefined} t={t} />));
  expect(container.textContent).toBe('');
});

test('the approval gate and the job summary render it from the assignment', () => {
  const gate = readFileSync(join(__dirname, '../../pages/AssignmentGate.js'), 'utf8');
  expect(gate).toMatch(/<UnservableAssignmentNotice\s+unservable=\{assignmentData\?\.assignment\?\.unservable_queries\}/);
  const summary = readFileSync(join(__dirname, '../../pages/JobMonitoringSummary.js'), 'utf8');
  expect(summary).toContain('<UnservableAssignmentNotice unservable={unservableQueries} t={t} />');
  expect(summary).toContain('setUnservableQueries(res.assignments.assignment?.unservable_queries || []);');
});
