/**
 * #992: the results page's "Sources of truth and replicas" callout. It lists
 * each written table read on a replica from report.json's facts.table_sources
 * (the same rows the engineering report states): the CDC
 * direction from the source of truth to its replicas and the reads a replica
 * serves, with the eventual-consistency risk stated once (#1232, as the
 * engineering report does since #1170), in a section collapsed when the page
 * opens. Cloudscape components are plain DOM stand-ins (see
 * UnservableQueriesCallout.test.js); the component and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import SourcesOfTruthCallout from '../SourcesOfTruthCallout';
import oldWording from './fixtures/oldWordingTableSources.json';

jest.mock('@cloudscape-design/components/expandable-section', () => ({
  __esModule: true,
  default: ({ headerText, children, variant, defaultExpanded }) => (
    <section data-variant={variant} data-default-expanded={String(defaultExpanded)}>
      <h3>{headerText}</h3>
      {children}
    </section>
  ),
}));
// Cloudscape passes `data-*` attributes through to its root element.
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

// The stored per-table risk (src/shared/report_facts.replica_risk_text).
const RISK = 'Eventually consistent: 2 reads on Aurora PostgreSQL read a copy kept in sync from DynamoDB via CDC, so each sees a write only after CDC applies it, behind by the replication lag; a read that must see its own write should read from DynamoDB.';
// The same risk once for every table (src/shared/report_facts.shared_replica_risk_text).
const SHARED_DYNAMODB = 'Eventually consistent: a read on a replica reads a copy kept in sync from DynamoDB via CDC, so it sees a write only after CDC applies it, behind by the replication lag; a read that must see its own write should read from DynamoDB.';

const shipment = {
  table: 'trailgear.Shipment',
  source_of_truth: 'dynamodb',
  replica_engines: ['aurora_postgresql'],
  write_query_count: 2,
  replica_read_query_ids: ['r1', 'r2'],
  replica_read_count: 2,
  risk: RISK,
  shares_replica_risk: true,
};
const order = {
  table: 'trailgear.Order',
  source_of_truth: 'dynamodb',
  replica_engines: ['aurora_postgresql', 'documentdb'],
  write_query_count: 1,
  replica_read_query_ids: ['r3'],
  replica_read_count: 1,
  shares_replica_risk: true,
  risk: 'Eventually consistent: 1 read on Aurora PostgreSQL, DocumentDB read a copy kept in sync from DynamoDB via CDC, so each sees a write only after CDC applies it, behind by the replication lag; a read that must see its own write should read from DynamoDB.',
};

const facts = {
  written_table_count: 2,
  replicated_table_count: 1,
  split_write_table_count: 0,
  replicated_tables: [shipment],
  more_replicated_count: 0,
  single_engine_tables: ['trailgear.Customer'],
  single_engine_table_count: 1,
};

function count(text, part) {
  return text.split(part).length - 1;
}

function render(tableSources) {
  act(() => {
    root.render(<SourcesOfTruthCallout synthesis={{ facts: { table_sources: tableSources } }} t={t} />);
  });
}

describe('SourcesOfTruthCallout', () => {
  it('is a container section, collapsed when the page opens, headed by the written table count', () => {
    render(facts);
    const section = container.querySelector('#sources-of-truth-section > section');
    expect(section.getAttribute('data-variant')).toBe('container');
    expect(section.getAttribute('data-default-expanded')).toBe('false');
    expect(section.querySelector('h3').textContent).toBe('Sources of truth and replicas (2 written tables)');
  });

  it('states each replicated table with its CDC direction and replica read count', () => {
    render(facts);
    const text = container.textContent;
    expect(text).toContain('1 written table runs every query on the source of truth, with no replica: trailgear.Customer.');
    expect(text).not.toContain('more than one engine');
    expect(text).toContain("Each table's writes land on one engine, its source of truth.");
    const row = container.querySelector('[data-table="trailgear.Shipment"]');
    expect(row.textContent).toBe('trailgear.Shipment CDC: DynamoDB → Aurora PostgreSQL');
    expect(row.parentElement.querySelector('[data-figure="replica-reads"]').textContent).toBe('2 reads on a replica');
  });

  it('states the eventual-consistency risk once, not once per table', () => {
    render({ ...facts, replicated_table_count: 2, replicated_tables: [shipment, order] });
    const text = container.textContent;
    expect(count(text, 'Eventually consistent: ')).toBe(1);
    expect(container.querySelector('[data-figure="shared-risk"]').textContent).toBe(
      `Every table above has this risk. ${SHARED_DYNAMODB}`
    );
    expect(text).not.toContain(RISK);
    expect(text).not.toContain(order.risk);
    const reads = [...container.querySelectorAll('[data-figure="replica-reads"]')].map(n => n.textContent);
    expect(reads).toEqual(['2 reads on a replica', '1 read on a replica']);
  });

  it('names every source of truth in the shared risk, in row order, like the engineering report', () => {
    const fromDocdb = { ...order, table: 'trailgear.Review', source_of_truth: 'documentdb', replica_engines: ['opensearch'] };
    const fromPg = { ...order, table: 'trailgear.Item', source_of_truth: 'aurora_postgresql', replica_engines: ['opensearch'] };
    render({ ...facts, replicated_tables: [shipment, fromDocdb, order, fromPg] });
    expect(container.querySelector('[data-figure="shared-risk"]').textContent).toBe(
      'Every table above has this risk. Eventually consistent: a read on a replica reads a copy kept in sync from its source of truth (DynamoDB, DocumentDB or Aurora PostgreSQL) via CDC, so it sees a write only after CDC applies it, behind by the replication lag; a read that must see its own write should read from the source of truth.'
    );
  });

  it('keeps the stored risk of a row the facts say does not share it, never deciding from the prose', () => {
    const own = { ...order, table: 'trailgear.Legacy', shares_replica_risk: false, risk: 'Older wording of the risk.' };
    render({ ...facts, replicated_tables: [shipment, own] });
    const legacy = container.querySelector('[data-table="trailgear.Legacy"]').parentElement;
    expect(legacy.querySelector('[data-figure="own-risk"]').textContent).toBe('Older wording of the risk.');
    expect(legacy.querySelector('[data-figure="replica-reads"]').textContent).toBe('1 read on a replica');
    expect(container.querySelector('[data-figure="shared-risk"]').textContent).toBe(
      `Every other table above has this risk. ${SHARED_DYNAMODB}`
    );
    // A row with no flag at all (a hand-built block) keeps its stored risk too.
    const unflagged = { ...shipment };
    delete unflagged.shares_replica_risk;
    render({ ...facts, replicated_tables: [unflagged] });
    expect(container.querySelector('[data-figure="shared-risk"]')).toBeNull();
    expect(container.querySelector('[data-figure="own-risk"]').textContent).toBe(RISK);
  });

  it("states an older report's block as the engineering report does (shared fixture)", () => {
    render(oldWording.served);
    const { shared, own_risk: ownRisk } = oldWording.expected;
    expect(container.querySelector('[data-figure="shared-risk"]').textContent).toBe(shared);
    const legacy = container.querySelector(`[data-table="${ownRisk.table}"]`).parentElement;
    expect(legacy.querySelector('[data-figure="own-risk"]').textContent).toBe(ownRisk.risk);
    expect(count(container.textContent, 'Eventually consistent: ')).toBe(2);
  });

  it('says how many more tables the reports list', () => {
    render({ ...facts, replicated_table_count: 4, more_replicated_count: 3 });
    expect(container.textContent).toContain('3 more replicated tables are listed in the Assessment Data (JSON), under table_sources.');
  });

  it('states the tables written on more than one engine, like the reports', () => {
    render({ ...facts, split_write_table_count: 2 });
    expect(container.textContent).toContain("2 tables have writes on more than one engine and no single source of truth; route each table's writes to one engine in the assignment review.");
  });

  // #1329: tables whose writes have no engine in the customer's engine scope.
  it('states the tables the source database keeps writing, like the reports', () => {
    render({ ...facts, source_database_table_count: 24 });
    expect(container.querySelector('[data-figure="source-database-tables"]').textContent).toBe(
      "The source database stays the writer of 24 tables: no engine in the customer's engine scope can serve their writes. Each engine that reads one holds a replica kept in sync from the source database.",
    );
    render({ ...facts, source_database_table_count: 0 });
    expect(container.querySelector('[data-figure="source-database-tables"]')).toBeNull();
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
