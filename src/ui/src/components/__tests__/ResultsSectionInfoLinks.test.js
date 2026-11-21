// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2512: each results page section with a help topic has a Cloudscape "Info" link in
 * its header, on the real components and strings, and the explanation it points to is
 * no longer stated in the section. The topics themselves are in ResultsHelp.test.js.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import i18n from '../../i18n';
import en from '../../locales/en.json';
import CloseCallCallout from '../CloseCallCallout';
import MigrationWavePlan from '../MigrationWavePlan';
import QueryRoutingTable from '../QueryRoutingTable';
import TableOwnersCallout from '../TableOwnersCallout';
import { QueryDetailBody } from '../QueryDetailPanel';
import { ResultsHelp } from '../../hooks/useResultsDrawers';
import { buildQueryRoutingRows } from '../../utils/queryRouting';
import { WORKLOAD_FIT_DEFINITION } from '../../utils/workloadFitLabels';
import waves from '../../utils/__tests__/fixtures/waveHeadlines.json';

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

const t = i18n.t.bind(i18n);
let container;
let root;
let opened;

beforeEach(() => {
  opened = [];
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const render = node => act(() => {
  root.render(<ResultsHelp.Provider value={topic => opened.push(topic)}>{node}</ResultsHelp.Provider>);
});

// The section's Info link: in its header, labelled with the section, and it opens the topic.
function expectInfoLink(topic, title, header) {
  const link = header.querySelector(`[data-help-topic="${topic}"]`);
  expect(link).not.toBeNull();
  expect(link.getAttribute('aria-label')).toBe(`Info about the ${title} section`);
  act(() => link.click());
  expect(opened).toEqual([topic]);
}

const headerOf = selector => createWrapper(container.querySelector(selector)).findHeader().getElement();

test('the migration roadmap header has an Info link and the section no longer states the readiness rule', () => {
  const synthesis = JSON.parse(JSON.stringify(waves.synthesis));
  synthesis.facts.waves = synthesis.facts.waves.map(w => ({ ...w, readiness: 'suggested', suggested: true, readiness_line: 'Suggested: no check holds it.' }));
  render(<MigrationWavePlan synthesis={synthesis} t={t} />);
  expect(container.querySelector('[data-figure="wave-readiness"]')).not.toBeNull();
  expect(container.textContent).not.toContain(en['analysis-results.waves.readiness-rule']);
  expectInfoLink('migration-roadmap', 'Migration roadmap', headerOf('#migration-roadmap-section'));
});

test('the close calls header has an Info link and keeps its margin and ties', () => {
  const id = `q1${'a'.repeat(62)}`;
  const synthesis = {
    facts: {
      close_calls: {
        margin_points: 5,
        close_call_query_count: 1,
        settled_tie_count: 0,
        engine_pairs: [{ engine: 'dynamodb', alternative: 'aurora_mysql', pair_query_count: 1, exact_tie_count: 0, group_mean_count: 0, min_gap: 1, max_gap: 1, query_ids: [id], more_query_count: 0 }],
      },
    },
    query_traces: { close_calls: [{ query_id: id, engine: 'dynamodb', alternative: 'aurora_mysql', gap: 1, basis: 'query', settled_tie: false }] },
  };
  render(<CloseCallCallout synthesis={synthesis} jobId="job-1" onOpen={() => {}} queries={[]} t={t} />);
  const header = headerOf('#close-calls-section');
  expect(header.textContent).toContain('less than 5 points');
  expectInfoLink('close-calls', 'Close calls', header);
});

test('the table owners header has an Info link and the section states neither the owner rule nor the CDC sentence', () => {
  const facts = {
    table_owner_lines: {
      table_count: 1,
      owned_table_counts: { dynamodb: 1 },
      no_owner_table_count: 0,
      also_read_table_count: 1,
      cached_table_count: 0,
      cache_engines: [],
      unwritten_owner_rule: 'relational_step',
      table_lines: [{ table: 'app.carts', owner: 'dynamodb', owner_basis: 'writes', also_read_by: ['aurora_mysql'], cached_by: null, replica_read_count: 1 }],
    },
    table_sources: {
      written_table_count: 1,
      replicated_table_count: 1,
      split_write_table_count: 0,
      replicated_tables: [{ table: 'app.carts', source_of_truth: 'dynamodb', replica_engines: ['aurora_mysql'], replica_read_count: 1, risk: 'r', shares_replica_risk: true }],
      shared_risk_sentence: 'Every replicated table above has this risk.',
      more_replicated_count: 0,
    },
  };
  render(<TableOwnersCallout synthesis={{ facts }} t={t} />);
  // The header's Info link shows while the section is collapsed.
  const link = container.querySelector('[data-help-topic="table-owners"]');
  expect(link).not.toBeNull();
  act(() => container.querySelector('[aria-expanded="false"]').click());
  act(() => container.querySelector('[aria-label="Info about the consistency of replica reads"]').click());
  expect(container.textContent).toContain('Every replicated table above has this risk.');
  expect(container.textContent).not.toContain(en['analysis-results.table-owners.rule']);
  expect(container.textContent).not.toContain(en['analysis-results.sources-of-truth.intro']);
  expectInfoLink('table-owners', 'Table owners', container.querySelector('#table-owners-section'));
});

test('the query routing header has an Info link', () => {
  const journey = { query_id: `0001${'f'.repeat(60)}`, source: { query_text: 'SELECT 1 FROM shop.wp_posts', query_type: 'SELECT', tables_accessed: ['shop.wp_posts'] }, assignment: { assigned_engine: 'dynamodb' } };
  const rows = buildQueryRoutingRows([journey], new Map(), { dynamodb: 'DynamoDB' });
  render(
    <QueryRoutingTable t={t} rows={rows} loading={false} error={null} hasLedger truncated={false} selectedId={null} onSelect={() => {}} />,
  );
  expectInfoLink('query-routing', 'Query routing', headerOf('#query-routing-section'));
});

test('the query detail states its workload fit with an Info link to the definition, not the definition', () => {
  const id = `0452bed2${'z'.repeat(56)}`;
  const journey = {
    query_id: id,
    source: { query_text: 'SELECT 1', query_type: 'SELECT', tables_accessed: ['shop.t'], frequency_per_hour: 1, calls_per_second: 1 },
    assignment: { assigned_engine: 'dynamodb', confidence: 82, in_scope: true },
  };
  render(<QueryDetailBody queryId={id} journey={journey} loading={false} trace={{ data: null, error: null }} t={t} />);
  const text = container.textContent;
  expect(text).toContain('Workload fit');
  expect(text).toMatch(/Workload fit\s*82%/);
  expect(text).not.toContain(WORKLOAD_FIT_DEFINITION);
  expectInfoLink('query-routing', 'Query routing', container);
});
