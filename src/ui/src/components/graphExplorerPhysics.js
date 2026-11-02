// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * How the explorer's graph moves (#308): the cola force layout, run only in
 * short bounded passes over the nodes that should move, and never left
 * running. Every other node is locked for the pass, so it stays where it is.
 *
 * - After an expansion, the new nodes move first (around their parent), then
 *   one short tidy pass lets every unpinned node make room so nothing overlaps.
 * - While a node is dragged, only it and its direct neighbours move (they
 *   follow it along their edges); on drop it is pinned where it was let go,
 *   its neighbours settle in one short pass, and the layout stops.
 * - A pinned node never moves in a layout pass (only the user moves it).
 *
 * Why locking rather than running cola on a sub-collection: cola then still
 * sees every node, so the moving ones keep clear of the still ones (overlap
 * avoidance needs the whole picture), and a still node cannot drift.
 */

// Edge length for the heaviest and the lightest traffic. The minimum keeps a
// busy table from being pulled onto its engine; traffic still shortens edges
// within that range ("gravitational weight").
export const EDGE_LENGTH = [130, 280];
// Space kept around each node's box (cola's nodeSpacing), with its label in
// the box. Engines keep more, so two engines never sit on top of each other.
export const NODE_SPACING = 16;
export const ENGINE_SPACING = 80;
export const EXPAND_PASS_MS = 1200;
export const DROP_PASS_MS = 600;
// After an expansion: one short pass for every unpinned node, to clear overlaps.
export const TIDY_PASS_MS = 900;

const clamp01 = (x) => Math.min(1, Math.max(0, Number(x) || 0));

/** An edge's ideal length from its place on the traffic scale (0 to 1), never under the minimum. */
export function edgeLengthFor(weight) {
  return EDGE_LENGTH[1] - (EDGE_LENGTH[1] - EDGE_LENGTH[0]) * clamp01(weight);
}

export function nodeSpacingFor(node) {
  return node.data('kind') === 'engine' ? ENGINE_SPACING : NODE_SPACING;
}

/** The cola options of one bounded pass. */
export function colaOptions(extra = {}) {
  return {
    name: 'cola',
    animate: true,
    randomize: false,
    fit: false,
    // Start from the current positions; never re-centre the graph under the user.
    centerGraph: false,
    edgeLength: (edge) => edgeLengthFor(edge.data('weight')),
    nodeSpacing: nodeSpacingFor,
    nodeDimensionsIncludeLabels: true,
    avoidOverlap: true,
    handleDisconnected: false,
    maxSimulationTime: EXPAND_PASS_MS,
    ...extra,
  };
}

export const isPinned = (node) => node.hasClass('pinned');

/** Lock everything except `movable` (ids), and pinned nodes always. */
export function lockAllBut(cy, movable) {
  cy.nodes().forEach((n) => {
    if (movable.has(n.id()) && !isPinned(n)) n.unlock();
    else n.lock();
  });
}

/** Outside a layout pass nothing is locked: the user can drag any node, pinned ones included. */
export function releaseLocks(cy) {
  cy.nodes().unlock();
}

/**
 * One bounded cola pass moving only `movable` (a Set of node ids). Resolves
 * once the pass has stopped (or its time is up), with every lock released.
 * Returns `{ promise, stop }`.
 */
export function settle(cy, movable, extra = {}) {
  const options = colaOptions(extra);
  lockAllBut(cy, movable);
  const layout = cy.layout(options);
  let done = false;
  let finish;
  const promise = new Promise((resolve) => {
    finish = () => {
      if (done) return;
      done = true;
      layout.stop();
      releaseLocks(cy);
      resolve();
    };
  });
  layout.one('layoutstop', finish);
  setTimeout(finish, options.maxSimulationTime + 300);
  layout.run();
  return { promise, stop: finish };
}

/** The node and its direct neighbours that are free to move while it is dragged. */
export function dragGroup(node) {
  const ids = new Set([node.id()]);
  node.neighborhood('node').forEach((n) => {
    if (!isPinned(n)) ids.add(n.id());
  });
  return ids;
}

export function pin(node) {
  node.addClass('pinned');
}

export function unpin(node) {
  node.removeClass('pinned');
}
