/**
 * #330: the assignment Sankey (ChartSankey-01.js) omitted the ElastiCache
 * cache layer entirely -- it was only ever mentioned in a caption below the
 * diagram. These tests cover the cache-layer-specific rendering added here:
 * the cache node (fed by its owner engines' flows, never a flow source) is
 * present, styled as a dashed overlay, and never clickable, while owner
 * nodes/links are unaffected.
 *
 * Named ChartSankeyCacheLayer (not ChartSankey) to avoid colliding with the
 * unrelated open PR #397 (ChartSankey.test.js, engine display-name labels).
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import ChartSankey from '../ChartSankey-01';

global.IS_REACT_ACT_ENVIRONMENT = true;

let container, root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const render = (data, onNodeClick) => {
  act(() => root.render(<ChartSankey width={900} height={400} data={data} onNodeClick={onNodeClick} />));
};

const textLabels = () => Array.from(container.querySelectorAll('text')).map((n) => n.textContent);

const nodeGroupForLabel = (label) =>
  Array.from(container.querySelectorAll('text'))
    .find((n) => n.textContent === label)
    ?.closest('g');

// A sankey with an owner layer (queries -> dynamodb/aurora_mysql) plus the
// #330 cache overlay layer (dynamodb/aurora_mysql -> elasticache), the shape
// addCacheOverlayNode (src/ui/src/utils/cacheLayer.js) produces.
const dataWithCacheLayer = {
  nodes: [{ id: 'queries' }, { id: 'dynamodb' }, { id: 'aurora_mysql' }, { id: 'elasticache' }],
  links: [
    { source: 'queries', target: 'dynamodb', value: 11 },
    { source: 'queries', target: 'aurora_mysql', value: 2 },
    { source: 'dynamodb', target: 'elasticache', value: 11 },
    { source: 'aurora_mysql', target: 'elasticache', value: 2 },
  ],
};

test('the cache node is present and labeled with its total cached-reads value', () => {
  render(dataWithCacheLayer);
  expect(textLabels()).toContain('ElastiCache (13)');
});

test('owner node totals are unaffected by the cache overlay layer', () => {
  render(dataWithCacheLayer);
  // dynamodb's value is max(incoming 11, outgoing 11) = 11, not 11+11
  expect(textLabels()).toContain('DynamoDB (11)');
  expect(textLabels()).toContain('Aurora MySQL (2)');
});

test('the cache node renders with a dashed outline (overlay styling), owner nodes do not', () => {
  render(dataWithCacheLayer);
  const cacheRect = nodeGroupForLabel('ElastiCache (13)')?.querySelector('rect');
  const ownerRect = nodeGroupForLabel('DynamoDB (11)')?.querySelector('rect');
  expect(cacheRect?.getAttribute('stroke-dasharray')).toBeTruthy();
  expect(ownerRect?.getAttribute('stroke-dasharray')).toBeFalsy();
});

test('links into the cache node are dashed; links into owner nodes are not', () => {
  render(dataWithCacheLayer);
  const dashedPaths = Array.from(container.querySelectorAll('path')).filter((p) => p.getAttribute('stroke-dasharray'));
  const plainPaths = Array.from(container.querySelectorAll('path')).filter((p) => !p.getAttribute('stroke-dasharray'));
  // 2 owner->cache links are dashed, 2 queries->owner links are not
  expect(dashedPaths).toHaveLength(2);
  expect(plainPaths).toHaveLength(2);
});

test('the cache node is never clickable, even when onNodeClick is supplied', () => {
  const onNodeClick = jest.fn();
  render(dataWithCacheLayer, onNodeClick);

  act(() => {
    nodeGroupForLabel('ElastiCache (13)')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });

  expect(onNodeClick).not.toHaveBeenCalled();
});

test('owner nodes stay clickable with the cache overlay layer present', () => {
  const onNodeClick = jest.fn();
  render(dataWithCacheLayer, onNodeClick);

  act(() => {
    nodeGroupForLabel('DynamoDB (11)')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });

  expect(onNodeClick).toHaveBeenCalledWith('dynamodb');
});

test('a sankey with no cache overlay layer renders exactly as before (no dashed nodes/links)', () => {
  const data = {
    nodes: [{ id: 'queries' }, { id: 'dynamodb' }],
    links: [{ source: 'queries', target: 'dynamodb', value: 5 }],
  };
  render(data);
  expect(container.querySelectorAll('[stroke-dasharray]')).toHaveLength(0);
});
