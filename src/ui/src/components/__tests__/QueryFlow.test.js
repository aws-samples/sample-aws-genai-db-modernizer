// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2317: the query flow has a text alternative. The drawing is one image whose accessible
 * name states the split; "Show as a table" lists the same numbers; with a click handler,
 * each engine in the table is a button, the keyboard way to what a node click does.
 * Renders the real Cloudscape components and the real English strings.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import '../../i18n';
import QueryFlow from '../QueryFlow';
import { queryFlowLabel, queryFlowRows } from '../../utils/queryFlow';
import i18n from '../../i18n';

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };

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

const DATA = {
  nodes: [{ id: 'queries' }, { id: 'dynamodb' }, { id: 'aurora_mysql' }, { id: 'elasticache' }],
  links: [
    { source: 'queries', target: 'dynamodb', value: 60 },
    { source: 'queries', target: 'aurora_mysql', value: 41 },
    { source: 'dynamodb', target: 'elasticache', value: 20 },
  ],
};
const ONE = {
  nodes: [{ id: 'queries' }, { id: 'dynamodb' }],
  links: [{ source: 'queries', target: 'dynamodb', value: 1 }],
};

const draw = (props) => act(() => root.render(<QueryFlow height={280} data={DATA} total={101} {...props} />));
const expand = () => act(() => {
  container.querySelector('[aria-expanded]').click();
});

test('the drawing is a named image that states the split and the cache layer', () => {
  draw({});
  const img = container.querySelector('svg[role="img"]');
  expect(img.getAttribute('aria-label')).toBe(
    'Query flow of 101 queries, split as DynamoDB 60, Aurora MySQL 41. ElastiCache is a cache layer for 20 queries.',
  );
});

test('says "1 query" for one query', () => {
  draw({ data: ONE, total: 1 });
  expect(container.querySelector('svg').getAttribute('aria-label'))
    .toBe('Query flow of 1 query, split as DynamoDB 1.');
});

test('states no total when the report gives none, and formats thousands like the table', () => {
  draw({
    total: undefined,
    data: { ...ONE, links: [{ source: 'queries', target: 'dynamodb', value: 1234 }] },
  });
  expect(container.querySelector('svg').getAttribute('aria-label'))
    .toBe('Query flow, split as DynamoDB 1,234.');
});

test('"Show as a table" lists each engine and its queries, the cache layer marked', () => {
  draw({});
  expect(container.textContent).toContain('Show as a table');
  expand();
  const rows = Array.from(container.querySelectorAll('tbody tr')).map((tr) =>
    Array.from(tr.querySelectorAll('td')).map((td) => td.textContent));
  expect(rows).toEqual([
    ['DynamoDB', '60'],
    ['Aurora MySQL', '41'],
    ['ElastiCache (cache layer)', '20'],
  ]);
  expect(container.querySelector('table').getAttribute('aria-label')).toBe('Queries by target engine');
});

test('with a click handler an engine in the table is a button that opens its results', () => {
  const onNodeClick = jest.fn();
  draw({ onNodeClick });
  expand();
  const button = container.querySelector('button[aria-label="View results for Aurora MySQL"]');
  act(() => button.click());
  expect(onNodeClick).toHaveBeenCalledWith('aurora_mysql');
  expect(container.querySelector('button[aria-label^="View results for ElastiCache"]')).toBeNull();
});

test('without a click handler the table has no buttons', () => {
  draw({});
  expand();
  expect(container.querySelectorAll('tbody button')).toHaveLength(0);
});

test('the rows are the graph\'s own links, so the label and the table cannot differ', () => {
  expect(queryFlowRows(DATA)).toEqual({
    owners: [{ engine: 'dynamodb', queries: 60 }, { engine: 'aurora_mysql', queries: 41 }],
    cache: [{ engine: 'elasticache', queries: 20 }],
  });
  expect(queryFlowLabel(DATA, i18n.t.bind(i18n), 101)).toContain('101 queries');
});
