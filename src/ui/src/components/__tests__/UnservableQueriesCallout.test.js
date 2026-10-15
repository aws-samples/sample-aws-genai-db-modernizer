/**
 * #729: the results page's callout for queries on a fallback engine. It
 * lists each query from report.json's facts.unservable (the same list the
 * decision and engineering reports flag) with the gates, the redesign hint and
 * the re-run path, opens a query's "Why this engine" decision trace, and links
 * to the assignment review. Cloudscape components are plain DOM stand-ins (see
 * QueryRoutingTable.test.js); the component and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import UnservableQueriesCallout from '../UnservableQueriesCallout';

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
jest.mock('@cloudscape-design/components/link', () => ({
  __esModule: true,
  default: ({ children, onFollow, ariaLabel, href }) => (href
    ? <a className="mock-href" href={href}>{children}</a>
    : <a role="button" tabIndex={0} aria-label={ariaLabel} onClick={() => onFollow({ preventDefault: () => {}, detail: {} })}>{children}</a>),
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

const facts = {
  unservable_query_count: 2,
  fallback_query_count: 2,
  gated_everywhere_query_count: 0,
  unservable_queries: [
    {
      query_id: 'q-count',
      engine: 'aurora_mysql',
      kind: 'fallback',
      rule: 'fallback.no_score',
      explanation: 'No engine could score this query, so it falls back to Aurora MySQL.',
      blocking_gates: [{ engine: 'dynamodb', gate: 'capability', gate_text: 'missing a capability the query needs' }],
      required_capabilities: ['aggregation'],
      capability_labels: ['aggregation'],
      redesign_hints: ['Maintain the aggregate as the data changes.'],
    },
    {
      query_id: 'q-<i>',
      engine: 'aurora_mysql',
      kind: 'fallback',
      rule: 'fallback.no_owner',
      explanation: 'No analyzed engine can own this query, so it falls back to Aurora MySQL.',
      blocking_gates: [],
      required_capabilities: [],
      capability_labels: [],
      redesign_hints: ['Rewrite the query so a kept engine can serve it.'],
    },
  ],
  more_unservable_count: 3,
};
const synthesis = { facts: { unservable: facts } };

const render = (props) => {
  act(() => root.render(<UnservableQueriesCallout t={t} synthesis={synthesis} jobId="job-1" onOpen={() => {}} {...props} />));
};

test('flags each query with its engine, why, the gates and a redesign', () => {
  render();
  const text = container.textContent;
  expect(container.querySelector('[data-alert]').dataset.alert).toBe('warning');
  expect(text).toContain('2 queries on a fallback engine');
  expect(text).not.toContain('no kept engine');
  expect(text).toContain('does not bring back an engine it removed');
  expect(text).toContain('landed on Aurora MySQL');
  expect(text).toContain('No engine could score this query, so it falls back to Aurora MySQL.');
  expect(text).toContain('Ruled out: DynamoDB (missing a capability the query needs).');
  expect(text).toContain('Keep it on Aurora MySQL, or redesign it. Maintain the aggregate as the data changes.');
  expect(text).toContain('3 more queries are listed in the Engineering Report.');
});

test('a query opens its Why this engine trace', () => {
  const opened = [];
  render({ onOpen: id => opened.push(id) });
  act(() => container.querySelector('[role="button"][aria-label="Open the decision trace for query q-count"]').click());
  expect(opened).toEqual(['q-count']);
});

test('points at the assignment review to re-run', () => {
  render();
  const link = container.querySelector('a.mock-href');
  expect(link.getAttribute('href')).toBe('/analysis/assignments/job-1');
  expect(container.textContent).toContain('To re-run, open the assignment review');
});

test('a query id is text, never markup, and a query without gates states none', () => {
  render();
  expect(container.querySelector('i')).toBeNull();
  expect(container.textContent).toContain('q-<i>');
  expect(container.textContent.match(/Ruled out:/g)).toHaveLength(1);
});

test('renders nothing without any, or for a report written before them', () => {
  render({ synthesis: { facts: { unservable: { ...facts, unservable_query_count: 0, unservable_queries: [] } } } });
  expect(container.innerHTML).toBe('');
  render({ synthesis: { facts: {} } });
  expect(container.innerHTML).toBe('');
});

test('one query reads in the singular', () => {
  render({ synthesis: { facts: { unservable: { ...facts, unservable_query_count: 1, unservable_queries: [facts.unservable_queries[0]], more_unservable_count: 0 } } } });
  expect(container.textContent).toContain('1 query on a fallback engine');
  expect(container.textContent).not.toContain('more quer');
});
