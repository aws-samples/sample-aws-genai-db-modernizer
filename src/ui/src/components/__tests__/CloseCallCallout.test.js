/**
 * #730: the results page's close-call callout. It states each engine pair
 * from report.json's facts.close_calls (the same figures the decision and
 * engineering reports print), opens a named query's "Why this engine"
 * decision trace, and links to the assignment review where the customer
 * moves the queries. Cloudscape components are plain DOM stand-ins (see
 * QueryRoutingTable.test.js); the component and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import CloseCallCallout from '../CloseCallCallout';

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
  margin_points: 5,
  close_call_query_count: 14,
  settled_tie_count: 2,
  engine_pairs: [
    {
      engine: 'dynamodb',
      alternative: 'aurora_mysql',
      pair_query_count: 13,
      exact_tie_count: 2,
      group_mean_count: 1,
      min_gap: 1,
      max_gap: 4,
      query_ids: ['q-01', 'q-02', 'q-03'],
      more_query_count: 10,
    },
    {
      engine: 'aurora_mysql',
      alternative: 'dynamodb',
      pair_query_count: 1,
      exact_tie_count: 0,
      group_mean_count: 0,
      min_gap: 1.5,
      max_gap: 1.5,
      query_ids: ['q-<b>'],
      more_query_count: 0,
    },
  ],
};
const synthesis = { facts: { close_calls: facts } };

const render = (props) => {
  act(() => root.render(<CloseCallCallout t={t} synthesis={synthesis} jobId="job-1" onOpen={() => {}} {...props} />));
};

test('states every pair from the facts: count, engines, margin, gap, ties and groups', () => {
  render();
  const text = container.textContent;
  expect(container.querySelector('[data-alert]').dataset.alert).toBe('info');
  expect(text).toContain('Close calls: 14 queries, plus 2 exact ties a rule settled');
  expect(text).toContain('less than 5 points');
  expect(text).toContain('13 queries went to DynamoDB by less than 5 points over Aurora MySQL (a gap of 1 to 4 points)');
  expect(text).toContain('DynamoDB also won 2 exact ties with Aurora MySQL that a routing rule settled');
  expect(text).toContain('1 is compared on its co-dependency group');
  expect(text).toContain('Aurora MySQL can serve every one of them.');
  expect(text).toContain('1 query went to Aurora MySQL by less than 5 points over DynamoDB (a gap of 1.5 points). DynamoDB can serve it.');
  expect(text).toContain('and 10 more');
});

test('a named query opens its Why this engine trace', () => {
  const opened = [];
  render({ onOpen: id => opened.push(id) });
  const open = container.querySelector('[role="button"][aria-label="Open the decision trace for query q-02"]');
  expect(open.getAttribute('tabindex')).toBe('0');
  act(() => open.click());
  expect(opened).toEqual(['q-02']);
});

test('links to the assignment review, where the customer moves the queries', () => {
  render();
  const link = container.querySelector('a.mock-href');
  expect(link.getAttribute('href')).toBe('/analysis/assignments/job-1');
  expect(link.textContent).toBe('Open the assignment review');
  expect(container.textContent).toContain('change its engine in the assignment review');
});

test('a query id is text, never markup', () => {
  render();
  expect(container.querySelector('b')).toBeNull();
  expect(container.textContent).toContain('q-<b>');
});

test('renders nothing without close calls or for a report written before them', () => {
  render({ synthesis: { facts: { close_calls: { ...facts, close_call_query_count: 0, engine_pairs: [] } } } });
  expect(container.innerHTML).toBe('');
  render({ synthesis: { facts: {} } });
  expect(container.innerHTML).toBe('');
  render({ synthesis: null });
  expect(container.innerHTML).toBe('');
});

test('one close call reads in the singular', () => {
  render({ synthesis: { facts: { close_calls: { ...facts, close_call_query_count: 1, settled_tie_count: 0, engine_pairs: [facts.engine_pairs[1]] } } } });
  expect(container.textContent).toContain('Close call: 1 query');
  expect(container.textContent).not.toContain('exact tie');
});

test('a pair with only settled ties says so', () => {
  const tiesOnly = { engine: 'aurora_mysql', alternative: 'documentdb', pair_query_count: 0, exact_tie_count: 1, group_mean_count: 0, min_gap: 0, max_gap: 0, query_ids: ['q-t'], more_query_count: 0 };
  render({ synthesis: { facts: { close_calls: { ...facts, close_call_query_count: 0, settled_tie_count: 1, engine_pairs: [tiesOnly] } } } });
  expect(container.textContent).toContain('Close calls: 0 queries, plus 1 exact tie a rule settled');
  expect(container.textContent).toContain('1 exact tie between Aurora MySQL and DocumentDB went to Aurora MySQL by a routing rule. DocumentDB can serve it.');
});
