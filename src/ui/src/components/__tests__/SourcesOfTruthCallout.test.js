/**
 * #992: the results page's "Sources of truth and replicas" callout. It lists
 * each written table read on a replica from report.json's facts.table_sources
 * (the same rows the engineering report and the PDF report state): the CDC
 * direction from the source of truth to its replicas and the eventual-
 * consistency risk. Cloudscape components are plain DOM stand-ins (see
 * UnservableQueriesCallout.test.js); the component and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import SourcesOfTruthCallout from '../SourcesOfTruthCallout';

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

const RISK = 'Eventually consistent: 2 reads on Aurora PostgreSQL (r1, r2) read a copy kept in sync from DynamoDB via CDC, so a read can miss a write until it replicates.';

const facts = {
  written_table_count: 2,
  replicated_table_count: 1,
  split_write_table_count: 0,
  replicated_tables: [
    {
      table: 'trailgear.Shipment',
      source_of_truth: 'dynamodb',
      replica_engines: ['aurora_postgresql'],
      write_query_count: 2,
      replica_read_query_ids: ['r1', 'r2'],
      replica_read_count: 2,
      risk: RISK,
    },
  ],
  more_replicated_count: 0,
  single_engine_tables: ['trailgear.Customer'],
  single_engine_table_count: 1,
};

function render(tableSources) {
  act(() => {
    root.render(<SourcesOfTruthCallout synthesis={{ facts: { table_sources: tableSources } }} t={t} />);
  });
}

describe('SourcesOfTruthCallout', () => {
  it('states each replicated table, its CDC direction and its risk', () => {
    render(facts);
    const text = container.textContent;
    expect(text).toContain('Sources of truth and replicas (2 written tables)');
    expect(text).toContain('1 written table runs every query on the source of truth, with no replica: trailgear.Customer.');
    expect(text).not.toContain('more than one engine');
    expect(text).toContain("Each table's writes land on one engine, its source of truth.");
    const row = container.querySelector('[data-table="trailgear.Shipment"]');
    expect(row.textContent).toBe('trailgear.Shipment CDC: DynamoDB → Aurora PostgreSQL');
    expect(text).toContain(RISK);
  });

  it('says how many more tables the reports list', () => {
    render({ ...facts, replicated_table_count: 4, more_replicated_count: 3 });
    expect(container.textContent).toContain('3 more replicated tables are listed in the Assessment Data (JSON), under table_sources.');
  });

  it('states the tables written on more than one engine, like the reports', () => {
    render({ ...facts, split_write_table_count: 2 });
    expect(container.textContent).toContain("2 tables have writes on more than one engine and no single source of truth; route each table's writes to one engine in the assignment review.");
  });

  it('still states the single-engine tables when no table has a replica', () => {
    render({ ...facts, replicated_table_count: 0, replicated_tables: [], written_table_count: 1 });
    expect(container.textContent).toContain('Sources of truth and replicas (1 written table)');
    expect(container.textContent).toContain('trailgear.Customer');
  });

  it('renders nothing for a report with no written table or no facts', () => {
    render({ ...facts, written_table_count: 0 });
    expect(container.textContent).toBe('');
    render(undefined);
    expect(container.textContent).toBe('');
  });
});
