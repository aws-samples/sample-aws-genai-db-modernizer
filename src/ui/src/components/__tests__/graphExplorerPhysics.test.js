// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #308 round 10: the graph moves in short bounded passes over the nodes that
 * should move; a dropped (pinned) node keeps its position; the layout stops;
 * engines keep their distance. Run on a headless Cytoscape with the real cola
 * layout (no canvas, so labels are left out of the node boxes here).
 */
import cytoscape from 'cytoscape';
import cola from 'cytoscape-cola';
import {
  EDGE_LENGTH,
  ENGINE_SPACING,
  colaOptions,
  dragGroup,
  edgeLengthFor,
  isPinned,
  nodeSpacingFor,
  pin,
  settle,
  unpin,
} from '../graphExplorerPhysics';

cytoscape.use(cola);

const TEST = { animate: false, nodeDimensionsIncludeLabels: false, maxSimulationTime: 400 };

function graph() {
  const cy = cytoscape({
    headless: true,
    styleEnabled: true,
    style: [{ selector: 'node', style: { width: 32, height: 32 } }, { selector: 'node[kind = "engine"]', style: { width: 42, height: 42 } }],
  });
  cy.add([
    { group: 'nodes', data: { id: 'engine:a', kind: 'engine' }, position: { x: 0, y: 0 } },
    // A second engine starting right on top of the first.
    { group: 'nodes', data: { id: 'engine:b', kind: 'engine' }, position: { x: 4, y: 3 } },
    { group: 'nodes', data: { id: 'table:t1', kind: 'table' }, position: { x: 10, y: 10 } },
    { group: 'nodes', data: { id: 'table:t2', kind: 'table' }, position: { x: -10, y: 8 } },
    { group: 'nodes', data: { id: 'table:far', kind: 'table' }, position: { x: 900, y: 900 } },
    { group: 'edges', data: { id: 'e1', source: 'table:t1', target: 'engine:a', weight: 1 } },
    { group: 'edges', data: { id: 'e2', source: 'table:t2', target: 'engine:a', weight: 0 } },
    { group: 'edges', data: { id: 'e3', source: 'table:t1', target: 'engine:b', weight: 0.5 } },
  ]);
  return cy;
}

const at = (cy, id) => ({ ...cy.getElementById(id).position() });
const dist = (p, q) => Math.hypot(p.x - q.x, p.y - q.y);

test('edge lengths follow traffic within a minimum', () => {
  expect(edgeLengthFor(1)).toBe(EDGE_LENGTH[0]);
  expect(edgeLengthFor(0)).toBe(EDGE_LENGTH[1]);
  expect(edgeLengthFor(5)).toBe(EDGE_LENGTH[0]);
  expect(edgeLengthFor(undefined)).toBe(EDGE_LENGTH[1]);
});

test('engines keep more space than tables; labels count in the node boxes; nothing re-centres', () => {
  const cy = graph();
  expect(nodeSpacingFor(cy.getElementById('engine:a'))).toBe(ENGINE_SPACING);
  expect(nodeSpacingFor(cy.getElementById('table:t1'))).toBeLessThan(ENGINE_SPACING);
  const o = colaOptions();
  expect(o.nodeDimensionsIncludeLabels).toBe(true);
  expect(o.avoidOverlap).toBe(true);
  expect(o.centerGraph).toBe(false);
  expect(o.infinite).toBeUndefined();
});

test('a pinned node keeps its position, and only the movable nodes move', async () => {
  const cy = graph();
  pin(cy.getElementById('table:t1'));
  const before = { t1: at(cy, 'table:t1'), far: at(cy, 'table:far'), b: at(cy, 'engine:b') };
  await settle(cy, new Set(['table:t1', 'table:t2', 'engine:a']), TEST).promise;
  expect(at(cy, 'table:t1')).toEqual(before.t1);
  // Not in the pass: untouched.
  expect(at(cy, 'table:far')).toEqual(before.far);
  expect(at(cy, 'engine:b')).toEqual(before.b);
  // After the pass nothing stays locked: the user can drag any node.
  expect(cy.nodes().some((n) => n.locked())).toBe(false);
});

test('the layout stops: no position changes after the pass', async () => {
  const cy = graph();
  await settle(cy, new Set(cy.nodes().map((n) => n.id())), TEST).promise;
  const settled = cy.nodes().map((n) => [n.id(), at(cy, n.id())]);
  await new Promise((resolve) => setTimeout(resolve, 600));
  expect(cy.nodes().map((n) => [n.id(), at(cy, n.id())])).toEqual(settled);
});

test('two engines end well apart', async () => {
  const cy = graph();
  await settle(cy, new Set(cy.nodes().map((n) => n.id())), TEST).promise;
  expect(dist(at(cy, 'engine:a'), at(cy, 'engine:b'))).toBeGreaterThan(42 + ENGINE_SPACING / 2);
});

test('dragging moves the node and its unpinned neighbours only; unpin releases', () => {
  const cy = graph();
  pin(cy.getElementById('table:t2'));
  expect([...dragGroup(cy.getElementById('engine:a'))].sort()).toEqual(['engine:a', 'table:t1']);
  unpin(cy.getElementById('table:t2'));
  expect(isPinned(cy.getElementById('table:t2'))).toBe(false);
  expect([...dragGroup(cy.getElementById('engine:a'))].sort()).toEqual(['engine:a', 'table:t1', 'table:t2']);
});
