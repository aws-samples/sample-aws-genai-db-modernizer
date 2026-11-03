// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useRef } from 'react';
import cytoscape from 'cytoscape';
import cola from 'cytoscape-cola';
import { ringPositions } from '../utils/graphExplorer';
import { DROP_PASS_MS, EXPAND_PASS_MS, TIDY_PASS_MS, colaOptions, dragGroup, isPinned, lockAllBut, pin, releaseLocks, settle, unpin } from './graphExplorerPhysics';

// Register the force-directed layout once (cytoscape.use warns on a second call).
let colaRegistered = false;
function ensureCola() {
  if (!colaRegistered) {
    cytoscape.use(cola);
    colaRegistered = true;
  }
}

// One live pass runs only while a node is dragged (its neighbours follow it);
// every other pass is short and bounded (graphExplorerPhysics).
const LIVE = colaOptions({ infinite: true });
const FOCUS_MIN_ZOOM = 0.7;
const FOCUS_MAX_ZOOM = 1.2;

function stylesheet(dark) {
  const text = dark ? '#e9ebed' : '#000716';
  const background = dark ? '#161d26' : '#ffffff';
  const edge = dark ? '#8d99a8' : '#7d8998';
  return [
    {
      selector: 'node',
      style: {
        // Identity is the icon (utils/nodeIcons) and the label, never the
        // fill: one neutral node shape for every kind, so colour is free for
        // analysis. States are outlines, halos and opacity.
        'background-color': '#2a3644',
        'background-image': 'data(icon)',
        'background-fit': 'contain',
        'background-width': '74%',
        'background-height': '74%',
        'background-clip': 'none',
        'border-width': 1,
        'border-color': '#5f6b7a',
        // Labels are drawn on the canvas as plain text, never parsed as markup.
        label: 'data(label)',
        color: text,
        'font-size': 11,
        'text-valign': 'bottom',
        'text-margin-y': 4,
        'text-background-color': background,
        'text-background-opacity': 0.75,
        'text-background-padding': 2,
        // A greyed engine's label carries its "wave N" badge on a second line.
        'text-wrap': 'wrap',
        shape: 'round-rectangle',
        // At least 24 px, and a few more pixels of hit area around it.
        width: 32,
        height: 32,
        'overlay-padding': 6,
        'overlay-opacity': 0,
      },
    },
    { selector: 'node[placeholder = "yes"]', style: { 'border-width': 2, 'border-style': 'dashed', 'border-color': '#7d8998', opacity: 0.85 } },
    // An engine is a rounded square filled by its AWS service icon, as
    // published (not recoloured); tables are the smaller table-icon rectangles.
    {
      selector: 'node[kind = "engine"]',
      style: { width: 42, height: 42, 'background-width': '100%', 'background-height': '100%', 'font-size': 12, 'font-weight': 'bold' },
    },
    // In the Cost view, size carries the engine's monthly cost; the icon stays its identity.
    { selector: 'node[size]', style: { width: 'data(size)', height: 'data(size)' } },
    { selector: 'node[expanded = "yes"]', style: { 'border-width': 3, 'border-color': text } },
    // Pinned (dropped there by the user): a dotted outline around the node.
    { selector: 'node.pinned', style: { 'outline-width': 2, 'outline-style': 'dotted', 'outline-color': text, 'outline-offset': 4 } },
    // Selected: an orange outline and a soft halo around the icon.
    {
      selector: 'node:selected',
      style: { 'border-width': 4, 'border-color': '#ff9900', 'underlay-color': '#ff9900', 'underlay-opacity': 0.25, 'underlay-padding': 6 },
    },
    {
      selector: 'edge',
      style: {
        // Width carries traffic (calls per second, log scale); colour stays neutral.
        width: 'data(width)',
        opacity: 0.45,
        'line-color': edge,
        'curve-style': 'bezier',
        'control-point-step-size': 10,
        // A transparent hit area 8 px either side of the line, so a thin edge is easy to click.
        'overlay-padding': 8,
        'overlay-opacity': 0,
      },
    },
    // A collapsed bundle: straight, so it reads as one link.
    { selector: 'edge.bundle', style: { 'curve-style': 'straight', opacity: 0.6, 'line-cap': 'round' } },
    { selector: 'edge.in-unit', style: { 'line-style': 'dotted', opacity: 0.8 } },
    // Who writes: a table's writer solid and bright, reads served from a replica dashed.
    { selector: 'edge.access-write', style: { opacity: 0.95, 'line-color': text } },
    { selector: 'edge.access-replica_read', style: { opacity: 0.85, 'line-style': 'dashed', 'line-dash-pattern': [6, 4] } },
    { selector: 'edge.access-read', style: { opacity: 0.3 } },
    // A read the report cannot yet place (built from another assignment version).
    { selector: 'edge.access-read_or_replica', style: { opacity: 0.4, 'line-style': 'dotted' } },
    // Wave filter, one opacity scale: in the wave 1 (base), its cache layer 0.75,
    // what only cuts over in it 0.45, everything else greyed out at 0.25.
    { selector: 'edge.cache-layer', style: { opacity: 0.75 } },
    { selector: 'edge.cutover', style: { opacity: 0.45 } },
    { selector: 'edge.other-wave', style: { opacity: 0.25, 'line-color': '#7d8998' } },
    {
      selector: 'node.other-wave',
      style: { opacity: 0.55, 'background-image-opacity': 0.3, 'border-color': '#7d8998', color: '#8d99a8' },
    },
    // Cross-wave links: thin, short-dashed and grey, unlike Who writes'
    // replica reads (longer dashes, at the link's own width and colour).
    {
      selector: 'edge.cross-wave',
      style: { width: 1, 'line-style': 'dashed', 'line-dash-pattern': [2, 5], 'line-color': '#8d99a8', 'curve-style': 'straight' },
    },
    { selector: '.wave-hidden', style: { display: 'none' } },
    { selector: '.hidden', style: { display: 'none' } },
    { selector: 'edge.highlight', style: { opacity: 1, 'line-color': '#ff9900', 'z-index': 10 } },
  ];
}

/**
 * Light up the edges of `keys`: a node's edges, a query's edges (all tables
 * it touches share its id), a bundle, and in the Moves together view the
 * whole placement unit of a query.
 */
function highlight(instance, keys, withUnit) {
  instance.batch(() => {
    instance.edges().removeClass('highlight');
    keys.forEach((key) => {
      if (!key) return;
      if (key.startsWith('qid:')) {
        const qid = key.slice(4);
        const edges = instance.edges().filter((e) => e.data('qid') === qid);
        edges.addClass('highlight');
        const unit = withUnit && edges.nonempty() ? edges[0].data('unit') : null;
        if (unit) instance.edges().filter((e) => e.data('unit') === unit).addClass('highlight');
        return;
      }
      const el = instance.getElementById(key);
      if (el.empty()) return;
      if (el.isNode()) el.connectedEdges().addClass('highlight');
      else el.addClass('highlight');
    });
  });
}

/** Fit the visible graph, without zooming in past FOCUS_MAX_ZOOM on a small one. */
function fitView(instance) {
  const eles = instance.elements(':visible');
  if (eles.empty()) return;
  // Room for the floating toolbar over the top of the graph.
  instance.fit(eles, 90);
  if (instance.zoom() > FOCUS_MAX_ZOOM) {
    instance.zoom(FOCUS_MAX_ZOOM);
    instance.center(eles);
  }
}

const keyOf = (ele) => (ele.isEdge() && ele.data('qid') ? `qid:${ele.data('qid')}` : ele.id());

/**
 * The explorer's node-link view (Cytoscape.js with the cola force layout).
 *
 * - `elements` is the whole view (utils/graphExplorer.toElements). New nodes
 *   start in a ring around `anchorKey` (the node just expanded), and only they
 *   move while the layout settles.
 * - Dragging a node moves it and its neighbours; on drop it is pinned there
 *   (`onPinChange(key, true)`), and a double-click releases it. `pinnedKeys`
 *   are the pinned nodes (the page's, for Unpin and Reset layout).
 * - Tapping a node calls `onNodeTap(key)`; tapping an edge `onEdgeTap(data)`.
 * - `selectedKey` is a node id, a bundle id or `qid:<query id>`.
 * - A new `focus.signal` zooms to `focus.keys` (or to everything).
 */
export default function GraphExplorerCanvas({
  elements,
  onNodeTap,
  onEdgeTap,
  onHover,
  selectedKey,
  anchorKey,
  focus,
  unitHighlight = false,
  hoverKey = null,
  height = '100%',
  borderless = false,
  pinnedKeys = [],
  onPinChange = null,
}) {
  const container = useRef(null);
  const cy = useRef(null);
  const layout = useRef(null);
  const selection = useRef([]);
  const listHover = useRef(null);
  const pinnedRef = useRef(new Set(pinnedKeys));
  const handlers = useRef({});
  handlers.current = { onNodeTap, onEdgeTap, onHover, unitHighlight, onPinChange };

  useEffect(() => {
    ensureCola();
    const dark = (localStorage.getItem('dbm-theme') || 'dark') === 'dark';
    const instance = cytoscape({
      container: container.current,
      elements: [],
      style: stylesheet(dark),
      wheelSensitivity: 0.3,
      minZoom: 0.2,
      maxZoom: 3,
      boxSelectionEnabled: false,
    });
    const stopLayout = () => {
      if (layout.current) {
        layout.current.stop();
        layout.current = null;
      }
      releaseLocks(instance);
    };
    instance.scratch('explorer', { stopLayout });
    const h = () => handlers.current;
    instance.on('tap', 'node', (evt) => h().onNodeTap && h().onNodeTap(evt.target.id()));
    instance.on('tap', 'edge', (evt) => h().onEdgeTap && h().onEdgeTap({ ...evt.target.data() }));
    const report = (evt) => {
      if (!h().onHover) return;
      const p = evt.renderedPosition || evt.target.renderedPosition();
      h().onHover({ group: evt.target.isNode() ? 'node' : 'edge', ...evt.target.data(), x: p.x, y: p.y });
    };
    instance.on('mouseover', 'node, edge', (evt) => {
      instance.container().style.cursor = 'pointer';
      highlight(instance, [...selection.current, listHover.current, keyOf(evt.target)], h().unitHighlight);
      report(evt);
    });
    instance.on('mousemove', 'node, edge', report);
    instance.on('mouseout', 'node, edge', () => {
      instance.container().style.cursor = '';
      highlight(instance, [...selection.current, listHover.current], h().unitHighlight);
      if (h().onHover) h().onHover(null);
    });
    // Dragging: only the node and its unpinned neighbours move (they follow it
    // along their edges); every other node, and every pinned one, is locked.
    let replaying = false;
    let dragGroupIds = null;
    instance.on('grab', 'node', (evt) => {
      if (replaying) return;
      stopLayout();
      const node = evt.target;
      dragGroupIds = dragGroup(node);
      lockAllBut(instance, dragGroupIds);
      node.unlock();
      const live = instance.layout(LIVE);
      layout.current = live;
      live.run();
      // The layout starts listening after this grab: replay it, so the layout
      // holds the dragged node under the pointer instead of pulling it back.
      replaying = true;
      node.emit('grab');
      replaying = false;
    });
    // On drop: the node is pinned where it was let go, its neighbours settle in
    // one short pass, and the layout stops (no re-centring, nothing else moves).
    instance.on('free', 'node', (evt) => {
      const dropped = evt.target;
      if (layout.current) layout.current.stop();
      layout.current = null;
      pin(dropped);
      pinnedRef.current.add(dropped.id());
      if (handlers.current.onPinChange) handlers.current.onPinChange(dropped.id(), true);
      const neighbours = new Set([...(dragGroupIds || [])].filter((id) => id !== dropped.id()));
      dragGroupIds = null;
      const pass = settle(instance, neighbours, { maxSimulationTime: DROP_PASS_MS });
      layout.current = { stop: pass.stop };
    });
    // Double-click a pinned node to release it.
    instance.on('dbltap', 'node', (evt) => {
      const node = evt.target;
      if (!isPinned(node)) return;
      unpin(node);
      pinnedRef.current.delete(node.id());
      if (handlers.current.onPinChange) handlers.current.onPinChange(node.id(), false);
    });
    // Keep the canvas sized to its pane (the help drawer and the list resize it).
    const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => instance.resize()) : null;
    if (observer) observer.observe(container.current);
    cy.current = instance;
    return () => {
      if (observer) observer.disconnect();
      stopLayout();
      instance.destroy();
      cy.current = null;
    };
  }, []);

  useEffect(() => {
    const instance = cy.current;
    if (!instance) return;
    const wanted = new Map(elements.map((el) => [el.data.id, el]));
    const fresh = new Set();
    let removed = false;
    instance.batch(() => {
      instance.elements().forEach((el) => {
        if (el.inside() && !wanted.has(el.id())) {
          el.remove();
          removed = true;
        }
      });
      const anchor = anchorKey ? instance.getElementById(anchorKey) : null;
      const centre = anchor && anchor.nonempty() ? { ...anchor.position() } : { x: 0, y: 0 };
      const newNodes = elements.filter((el) => el.group === 'nodes' && instance.getElementById(el.data.id).empty());
      const ring = ringPositions(centre, newNodes.filter((el) => el.data.id !== anchorKey).length);
      let next = 0;
      newNodes.forEach((el) => {
        const position = el.data.id === anchorKey ? centre : ring[next++];
        instance.add({ ...el, position });
        fresh.add(el.data.id);
      });
      elements.forEach((el) => {
        const existing = instance.getElementById(el.data.id);
        if (existing.empty()) {
          instance.add(el);
          return;
        }
        ['size', 'cps', 'cost', 'unit'].forEach((k) => {
          if (el.data[k] === undefined && existing.data(k) !== undefined) existing.removeData(k);
        });
        existing.data(el.data);
        existing.classes(el.classes || '');
        if (pinnedRef.current.has(el.data.id)) pin(existing);
      });
    });
    // An element removed under the pointer sends no mouseout: drop its tooltip.
    if (removed && handlers.current.onHover) handlers.current.onHover(null);
    highlight(instance, [...selection.current, listHover.current], handlers.current.unitHighlight);
    if (fresh.size === 0) return;
    instance.scratch('explorer').stopLayout();
    // First only the new nodes move, around their parent; then one short tidy
    // pass lets every unpinned node make room, so no boxes (labels included)
    // overlap. Pinned nodes, the ones the user placed, never move.
    const pass = settle(instance, fresh, { maxSimulationTime: EXPAND_PASS_MS });
    layout.current = { stop: pass.stop };
    pass.promise.then(() => {
      if (cy.current !== instance) return;
      const tidy = settle(instance, new Set(instance.nodes().map((n) => n.id())), { maxSimulationTime: TIDY_PASS_MS });
      layout.current = { stop: tidy.stop };
      // Zoom to fit once it has settled, so the fit sees the final positions.
      tidy.promise.then(() => {
        if (cy.current === instance) fitView(instance);
      });
    });
  }, [elements, anchorKey]);

  // Pins come from the page too (Unpin in Details, Reset layout).
  useEffect(() => {
    const instance = cy.current;
    pinnedRef.current = new Set(pinnedKeys);
    if (!instance) return;
    instance.nodes().forEach((n) => {
      if (pinnedRef.current.has(n.id())) pin(n);
      else unpin(n);
    });
  }, [pinnedKeys]);

  useEffect(() => {
    const instance = cy.current;
    if (!instance) return;
    selection.current = selectedKey ? [selectedKey] : [];
    instance.nodes().unselect();
    if (selectedKey && !selectedKey.startsWith('qid:')) {
      const el = instance.getElementById(selectedKey);
      if (el.nonempty() && el.isNode()) el.select();
    }
    highlight(instance, [...selection.current, listHover.current], unitHighlight);
  }, [selectedKey, elements, unitHighlight]);

  // A query card hovered in the list lights its edges here.
  useEffect(() => {
    const instance = cy.current;
    listHover.current = hoverKey;
    if (instance) highlight(instance, [...selection.current, hoverKey], unitHighlight);
  }, [hoverKey, unitHighlight]);

  useEffect(() => {
    const instance = cy.current;
    if (!instance || !focus || !focus.signal) return;
    if (focus.keys && focus.keys.length) {
      let picked = instance.collection();
      focus.keys.forEach((key) => {
        if (key.startsWith('qid:')) {
          const edges = instance.edges().filter((e) => e.data('qid') === key.slice(4));
          picked = picked.union(edges).union(edges.connectedNodes());
        } else picked = picked.union(instance.getElementById(key));
      });
      if (picked.nonempty()) {
        // Centre on a query without zooming in so far that its neighbours leave the view.
        const zoom = Math.min(FOCUS_MAX_ZOOM, Math.max(FOCUS_MIN_ZOOM, instance.zoom()));
        instance.animate({ center: { eles: picked }, zoom, duration: 300 });
        return;
      }
    }
    fitView(instance);
  }, [focus]);

  return (
    <div
      ref={container}
      data-testid="graph-explorer-canvas"
      style={borderless ? { width: '100%', height } : { width: '100%', height, minHeight: 420, border: '1px solid #5f6b7a', borderRadius: 8 }}
    />
  );
}
