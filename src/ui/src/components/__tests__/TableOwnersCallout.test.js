/**
 * #212: the results page's "Table owners" callout. It lists each mapped source
 * table from report.json's facts.table_owner_lines (the lines the engineering
 * report's Migration map states): its owner, the engines that also read it
 * through CDC replicas and its cache, with the counts the facts layer computed,
 * in a section collapsed when the page opens. Cloudscape components are plain
 * DOM stand-ins (see SourcesOfTruthCallout.test.js); the component and the
 * English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import TableOwnersCallout from '../TableOwnersCallout';

jest.mock('@cloudscape-design/components/expandable-section', () => ({
  __esModule: true,
  default: ({ headerText, children, variant, defaultExpanded }) => (
    <section data-variant={variant} data-default-expanded={String(defaultExpanded)}>
      <h3>{headerText}</h3>
      {children}
    </section>
  ),
}));
jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children, ...props }) => (
    <div {...Object.fromEntries(Object.entries(props).filter(([k]) => k.startsWith('data-')))}>{children}</div>
  ),
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/table', () => ({
  __esModule: true,
  default: ({ columnDefinitions, items }) => (
    <table>
      <thead><tr>{columnDefinitions.map(c => <th key={c.id}>{c.header}</th>)}</tr></thead>
      <tbody>
        {items.map(item => (
          <tr key={item.table}>{columnDefinitions.map(c => <td key={c.id}>{c.cell(item)}</td>)}</tr>
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

const ownership = {
  table_count: 3,
  owned_table_counts: { dynamodb: 2, aurora_mysql: 1 },
  no_owner_table_count: 0,
  also_read_table_count: 2,
  cached_table_count: 1,
  cache_engines: ['elasticache'],
  table_lines: [
    { table: 'wordpress.wp_postmeta', owner: 'dynamodb', owner_basis: 'writes', also_read_by: ['aurora_mysql'], cached_by: 'elasticache' },
    { table: 'wordpress.wp_usermeta', owner: 'aurora_mysql', owner_basis: 'most_queries', also_read_by: ['dynamodb'], cached_by: null },
    { table: 'wordpress.wp_commentmeta', owner: 'dynamodb', owner_basis: 'most_queries', also_read_by: [], cached_by: null },
    { table: 'wordpress.wp_split', owner: 'aurora_mysql', owner_basis: 'split_writes', also_read_by: ['dynamodb'], cached_by: null },
  ],
};

function render(facts) {
  act(() => root.render(<TableOwnersCallout synthesis={{ facts }} t={t} />));
}

function rowOf(table) {
  return container.querySelector(`[data-table="${table}"]`).closest('tr');
}

test('states one line per table: owner, also read by, cached by', () => {
  render({ table_owner_lines: ownership });
  const section = container.querySelector('section');
  expect(section.getAttribute('data-default-expanded')).toBe('false');
  expect(section.querySelector('h3').textContent).toBe('Table owners (3 mapped tables)');
  const postmeta = rowOf('wordpress.wp_postmeta');
  expect(postmeta.querySelector('[data-cell="owner"]').textContent).toBe('DynamoDB');
  expect(postmeta.querySelector('[data-cell="also-read-by"]').textContent).toBe('Aurora MySQL');
  expect(postmeta.querySelector('[data-cell="cached-by"]').textContent).toBe('ElastiCache');
  const usermeta = rowOf('wordpress.wp_usermeta');
  expect(usermeta.querySelector('[data-cell="owner"]').textContent).toBe('Aurora MySQL');
  expect(usermeta.querySelector('[data-cell="cached-by"]').textContent).toBe('—');
  expect(rowOf('wordpress.wp_commentmeta').querySelector('[data-cell="also-read-by"]').textContent).toBe('—');
  // Writes on two engines: the wave plan's owner, marked as having no single writer.
  expect(rowOf('wordpress.wp_split').querySelector('[data-cell="owner"]').textContent)
    .toBe('Aurora MySQL (no single writer)');
});

test('states the facts layer counts as stored, never counting the rows', () => {
  // Counts that differ from the rows: the callout prints the facts, not a recount.
  render({ table_owner_lines: { ...ownership, also_read_table_count: 7, cached_table_count: 5 } });
  const owned = [...container.querySelectorAll('[data-figure="owned-tables"] [data-engine]')].map(d => d.textContent);
  expect(owned).toEqual(['DynamoDB owns 2', 'Aurora MySQL owns 1']);
  expect(container.querySelector('[data-figure="also-read-tables"]').textContent)
    .toBe('7 tables are also read by another engine through CDC replicas');
  expect(container.querySelector('[data-figure="cached-tables"]').textContent)
    .toBe('5 tables are cached by ElastiCache');
  expect(container.querySelector('[data-figure="no-owner-tables"]')).toBeNull();
});

test('says how many tables only a cache design maps', () => {
  render({ table_owner_lines: { ...ownership, no_owner_table_count: 1 } });
  expect(container.querySelector('[data-figure="no-owner-tables"]').textContent)
    .toBe('1 table has no owner engine');
});

test('renders nothing for a report that maps no table', () => {
  render({ table_owner_lines: null });
  expect(container.innerHTML).toBe('');
  render({ table_owner_lines: { ...ownership, table_count: 0, table_lines: [] } });
  expect(container.innerHTML).toBe('');
  render({});
  expect(container.innerHTML).toBe('');
});
