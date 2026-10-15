/**
 * #822: the results page's query routing table, the schema-design-free way
 * into a query's journey and its "Why this engine" decision trace. Cloudscape
 * ships ESM this repo's Jest config cannot transform (see
 * AnalysisResultsSummaryFallback.test.js), so its components are plain DOM
 * stand-ins; the table component and the English strings are real. The
 * keyboard path through the real Cloudscape Link is covered by the UI smoke
 * test (tests/e2e/test_ui.py).
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import QueryRoutingTable from '../QueryRoutingTable';

jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/badge', () => ({
  __esModule: true,
  default: ({ children }) => <span className="mock-badge">{children}</span>,
}));
jest.mock('@cloudscape-design/components/container', () => ({
  __esModule: true,
  default: ({ header, children }) => <section>{header}{children}</section>,
}));
jest.mock('@cloudscape-design/components/header', () => ({
  __esModule: true,
  default: ({ counter, children, description }) => (
    <header><h2>{children}</h2><span className="mock-counter">{counter}</span><p>{description}</p></header>
  ),
}));
jest.mock('@cloudscape-design/components/status-indicator', () => ({
  __esModule: true,
  default: ({ type, children }) => <span data-status={type}>{children}</span>,
}));
jest.mock('@cloudscape-design/components/text-filter', () => ({
  __esModule: true,
  default: ({ filteringText, onChange, filteringAriaLabel }) => (
    <input
      className="mock-filter"
      aria-label={filteringAriaLabel}
      value={filteringText}
      onChange={e => onChange({ detail: { filteringText: e.target.value } })}
    />
  ),
}));
jest.mock('@cloudscape-design/components/segmented-control', () => ({
  __esModule: true,
  default: ({ options, selectedId, onChange }) => (
    <div className="mock-segments">
      {options.map(o => (
        <button key={o.id} data-id={o.id} aria-pressed={o.id === selectedId} onClick={() => onChange({ detail: { selectedId: o.id } })}>{o.text}</button>
      ))}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/pagination', () => ({
  __esModule: true,
  default: ({ currentPageIndex, pagesCount, onChange, ariaLabels }) => (
    <button
      className="mock-next"
      data-pages={pagesCount}
      aria-label={ariaLabels && ariaLabels.nextPageLabel}
      data-previous-label={ariaLabels && ariaLabels.previousPageLabel}
      onClick={() => onChange({ detail: { currentPageIndex: currentPageIndex + 1 } })}
    >next</button>
  ),
}));
jest.mock('@cloudscape-design/components/link', () => ({
  __esModule: true,
  default: ({ children, onFollow, ariaLabel }) => (
    <a role="button" tabIndex={0} aria-label={ariaLabel} onClick={e => onFollow({ preventDefault: () => {}, detail: {} })}>{children}</a>
  ),
}));
jest.mock('@cloudscape-design/components/table', () => ({
  __esModule: true,
  default: ({ items, columnDefinitions, empty, loading, ariaLabels }) => (
    <table data-loading={String(Boolean(loading))} aria-label={ariaLabels && ariaLabels.tableLabel}>
      <thead><tr>{columnDefinitions.map(c => <th key={c.id}>{c.header}</th>)}</tr></thead>
      <tbody>
        {items.length === 0 ? <tr><td>{empty}</td></tr> : items.map(item => (
          <tr key={item.queryId} data-query-id={item.queryId}>
            {columnDefinitions.map(c => <td key={c.id} data-col={c.id}>{c.cell(item)}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  ),
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

const row = (n, extra = {}) => ({
  queryId: `${String(n).padStart(4, '0')}${'f'.repeat(60)}`,
  excerpt: `SELECT ${n} FROM wp_posts`,
  tables: ['wp_posts'],
  engine: 'dynamodb',
  engineName: 'DynamoDB',
  kinds: null,
  ...extra,
});

const moved = row(2, {
  engine: 'aurora_mysql',
  engineName: 'Aurora MySQL',
  tables: ['wp_wc_product_meta_lookup'],
  kinds: { moved: true, tied: true, openTie: false, overridden: false, pinned: false },
});

const render = (props) => {
  act(() => root.render(<QueryRoutingTable t={t} rows={[row(1), moved]} hasLedger onOpen={() => {}} {...props} />));
};
const bodyRows = () => Array.from(container.querySelectorAll('tbody tr[data-query-id]'));

test('one row per query, each with a keyboard-reachable control that opens its trace', () => {
  const opened = [];
  render({ onOpen: id => opened.push(id) });
  expect(bodyRows().map(r => r.dataset.queryId)).toEqual([row(1).queryId, moved.queryId]);
  const open = container.querySelector(`tr[data-query-id="${moved.queryId}"] [role="button"]`);
  expect(open.getAttribute('tabindex')).toBe('0');
  expect(open.getAttribute('aria-label')).toBe(`Open the journey and decision trace for query ${moved.queryId}`);
  act(() => open.click());
  expect(opened).toEqual([moved.queryId]);
});

test('the routing column names why a listed query has a story, and nothing for the rest', () => {
  render();
  const routing = id => container.querySelector(`tr[data-query-id="${id}"] [data-col="routing"]`).textContent;
  expect(routing(moved.queryId)).toContain('Moved');
  expect(routing(moved.queryId)).toContain('Tie settled');
  expect(routing(row(1).queryId)).toBe('Kept its first assignment');
});

test('the routing-story view and the text filter narrow the rows', () => {
  render();
  act(() => container.querySelector('.mock-segments [data-id="listed"]').click());
  expect(bodyRows().map(r => r.dataset.queryId)).toEqual([moved.queryId]);
  act(() => container.querySelector('.mock-segments [data-id="all"]').click());
  const input = container.querySelector('.mock-filter');
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  act(() => {
    setter.call(input, 'product_meta');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  expect(bodyRows().map(r => r.dataset.queryId)).toEqual([moved.queryId]);
  expect(container.querySelector('.mock-counter').textContent).toBe('(1 of 2)');
});

test('pages of 25 queries, with named pager buttons and a named table', () => {
  const rows = Array.from({ length: 30 }, (_, i) => row(i));
  render({ rows });
  expect(bodyRows()).toHaveLength(25);
  const pager = container.querySelector('.mock-next');
  expect(pager.dataset.pages).toBe('2');
  expect(pager.getAttribute('aria-label')).toBe('Next page');
  expect(pager.dataset.previousLabel).toBe('Previous page');
  expect(container.querySelector('table').getAttribute('aria-label')).toBe('Query routing');
  act(() => container.querySelector('.mock-next').click());
  expect(bodyRows()).toHaveLength(5);
});

test('an error or an empty list says so instead of an empty table', () => {
  render({ rows: [], error: 'boom' });
  expect(container.textContent).toContain('The query list could not be loaded: boom');
  render({ rows: [] });
  expect(container.textContent).toContain('No queries match.');
});

test('without a move ledger, rows claim no routing history and the trace view wording explains why', () => {
  render({ hasLedger: false });
  const routing = id => container.querySelector(`tr[data-query-id="${id}"] [data-col="routing"]`).textContent;
  expect(routing(row(1).queryId)).toBe('—');
  expect(container.textContent).not.toContain('Kept its first assignment');
  expect(container.textContent).toContain(
    'This assessment predates the per-query decision record, so only the final engine is known.',
  );
});

test('a list cut at the page limit says so', () => {
  render({ truncated: true });
  expect(container.textContent).toContain('Only the first 2 queries are listed.');
  render({ truncated: false });
  expect(container.textContent).not.toContain('are listed.');
});
