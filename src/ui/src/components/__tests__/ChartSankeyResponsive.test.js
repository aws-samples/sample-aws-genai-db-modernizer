// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2317: the query-flow drawing fits its container. It is laid out at the container's
 * width (a viewBox with width 100%, no fixed 900 px that cut the chart off on a phone),
 * scales down below a minimum layout width instead of scrolling the page, is one named
 * image for a screen reader, and draws the cache layer as thin outlines that do not
 * outweigh the owner flows.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import ChartSankey from '../ChartSankey';

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;
let containerWidth = 0;
let notify = () => {};

beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get() { return containerWidth; },
  });
  global.ResizeObserver = class {
    constructor(cb) { notify = cb; }
    observe() {}
    disconnect() {}
  };
});
beforeEach(() => {
  containerWidth = 0;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const DATA = {
  nodes: [{ id: 'queries' }, { id: 'dynamodb' }, { id: 'aurora_mysql' }],
  links: [
    { source: 'queries', target: 'dynamodb', value: 60 },
    { source: 'queries', target: 'aurora_mysql', value: 40 },
  ],
};
const WITH_CACHE = {
  nodes: [...DATA.nodes, { id: 'elasticache' }],
  links: [...DATA.links, { source: 'dynamodb', target: 'elasticache', value: 20 }],
};

const draw = (props) => act(() => root.render(<ChartSankey height={280} data={DATA} {...props} />));
const svg = () => container.querySelector('svg');
const viewBox = () => svg().getAttribute('viewBox').split(' ').map(Number);

test('fills its container: a viewBox of the container width and an svg 100% wide', () => {
  containerWidth = 1100;
  draw({});
  expect(svg().getAttribute('width')).toBe('100%');
  expect(viewBox()).toEqual([0, 0, 1100, 280]);
});

test('follows the container when it is resized', () => {
  containerWidth = 1100;
  draw({});
  containerWidth = 800;
  act(() => notify());
  expect(viewBox()[2]).toBe(800);
});

test('moves its nodes, labels and links when the container is resized, not only the viewBox', () => {
  const positions = () => ({
    cacheNode: Number(svg().querySelector('[data-sankey-node="elasticache"] rect').getAttribute('x')),
    cacheLabel: Number(Array.from(svg().querySelectorAll('text')).find((n) => n.textContent.startsWith('ElastiCache')).getAttribute('x')),
    link: svg().querySelector('path').getAttribute('d'),
  });
  containerWidth = 1200;
  draw({ data: WITH_CACHE });
  const wide = positions();
  containerWidth = 700;
  act(() => notify());
  const narrow = positions();
  expect(narrow.cacheNode).toBeLessThan(wide.cacheNode - 300);
  expect(narrow.cacheLabel).toBeLessThan(wide.cacheLabel - 300);
  expect(narrow.link).not.toBe(wide.link);
  // Everything stays inside the drawing it is scaled to.
  expect(narrow.cacheLabel).toBeLessThan(700);
});

test('scales down on a phone instead of laying out narrower than the labels need', () => {
  containerWidth = 300;
  draw({});
  expect(viewBox()[2]).toBeGreaterThanOrEqual(560);
  expect(svg().style.maxWidth).toBe('100%');
});

test('uses the width prop until the container has a width', () => {
  draw({ width: 900 });
  expect(viewBox()[2]).toBe(900);
});

test('is one image with an accessible name when it is given one', () => {
  draw({ ariaLabel: 'Query flow of 100 queries, split as DynamoDB 60, Aurora MySQL 40.' });
  expect(svg().getAttribute('role')).toBe('img');
  expect(svg().getAttribute('aria-label')).toBe('Query flow of 100 queries, split as DynamoDB 60, Aurora MySQL 40.');
});

test('has no image role without a name, and no focusable node inside the image', () => {
  draw({ onNodeClick: () => {} });
  expect(svg().getAttribute('role')).toBeNull();
  expect(svg().querySelectorAll('[tabindex], [role="link"]')).toHaveLength(0);
});

test('puts the source label above its node on a narrow drawing, beside it on a wide one', () => {
  const rootLabel = () => Array.from(svg().querySelectorAll('text')).find((n) => n.textContent === 'queries (100)');
  const rootRect = () => svg().querySelector('[data-sankey-node="queries"] rect');
  containerWidth = 400;
  draw({});
  expect(Number(rootLabel().getAttribute('y'))).toBeLessThan(Number(rootRect().getAttribute('y')));
  containerWidth = 1200;
  act(() => notify());
  expect(Number(rootLabel().getAttribute('y'))).toBeGreaterThan(Number(rootRect().getAttribute('y')));
});

test('draws a cache link as two thin dashed edges, never a band as wide as its flow', () => {
  containerWidth = 1000;
  draw({ data: WITH_CACHE });
  const paths = Array.from(svg().querySelectorAll('path'));
  const dashed = paths.filter((p) => p.getAttribute('stroke-dasharray'));
  expect(dashed).toHaveLength(2);
  dashed.forEach((p) => expect(Number(p.getAttribute('stroke-width'))).toBeLessThanOrEqual(2));
  const solid = paths.filter((p) => !p.getAttribute('stroke-dasharray'));
  expect(solid).toHaveLength(2);
  solid.forEach((p) => expect(Number(p.getAttribute('stroke-width'))).toBeGreaterThan(2));
});
