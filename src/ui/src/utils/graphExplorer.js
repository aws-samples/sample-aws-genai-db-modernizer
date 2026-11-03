// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The experimental graph explorer's view state (#308, behind a server flag).
 *
 * Nodes are source tables and destination engines. Each query is an edge from
 * every table it touches to its assigned engine; the queries of one table and
 * engine pair form a bundle, drawn as one edge until the user fans it out.
 *
 * The API bounds every expansion; the view bounds what it draws
 * (MAX_VIEW_ELEMENTS nodes plus edges), so a few clicks never draw the whole
 * graph. A bundle's query count and calls per second are the API's; these
 * helpers only track and place what is shown: no figure here is a report fact.
 */

import { nodeIcon } from './nodeIcons';

export { EXPLORER_PROBE_PATH, explorerEnabledFrom, fetchExplorerEnabled } from './graphExplorerFlag';

// Nodes plus drawn edges (a collapsed bundle is one edge; a fanned bundle
// one per query).
export const MAX_VIEW_ELEMENTS = 150;
export const LINK_LIMIT = 25;
// The start pickers ask for the API's documented maximum (the probe's max_options).
export const OPTION_LIMIT = 1000;
export const START_KINDS = ['wave', 'table', 'engine'];
export const SEARCH_MIN_CHARS = 2;

export const emptyView = () => ({ nodes: {}, bundles: {}, fanned: {}, pinned: {}, expanded: {}, capped: false });

const base = (jobId) => `assessments/${encodeURIComponent(jobId)}/graph/explore`;

export function linksPath(jobId, kind, id, limit = LINK_LIMIT) {
  const query = new URLSearchParams({ kind, id: String(id), limit: String(limit) });
  return `${base(jobId)}/links?${query.toString()}`;
}

export function searchPath(jobId, text) {
  return `${base(jobId)}/search?${new URLSearchParams({ text }).toString()}`;
}

export function queryPath(jobId, id) {
  return `${base(jobId)}/query?${new URLSearchParams({ id }).toString()}`;
}

export function optionsPath(jobId, limit = OPTION_LIMIT) {
  return `${base(jobId)}/options?${new URLSearchParams({ limit: String(limit) }).toString()}`;
}

/**
 * The picker's limit when the API cut the table list short (it lists the first
 * tables by name), else null: the toolbar then says only the first N are listed.
 */
export function truncatedPickerLimit(kind, options) {
  if (kind !== 'table' || !options || !options.tables_truncated) return null;
  return options.limit ?? null;
}

export function walkthroughsPath(jobId) {
  return `${base(jobId)}/walkthroughs`;
}

export function overlaysPath(jobId) {
  return `${base(jobId)}/overlays`;
}

export function belowTrafficPath(jobId, threshold, queryIds, bundles) {
  const query = new URLSearchParams({ min_calls_per_second: String(threshold) });
  queryIds.forEach((id) => query.append('query_id', id));
  bundles.forEach((b) => query.append('bundle', `${b.table}|${b.engine}`));
  return `${base(jobId)}/below-traffic?${query.toString()}`;
}

export const tableKey = (table) => `table:${table}`;
export const engineKey = (engine) => `engine:${engine}`;
export const queryEdgeId = (queryId, table) => `q:${queryId}|${table}`;

/**
 * The edges the view draws: each collapsed bundle as one edge; each query of a
 * fanned bundle, and each pinned query (from a search), as one edge from each
 * of its tables in view to its engine. A query's edges share its id.
 */
export function drawnEdges(view) {
  const edges = new Map();
  // `bundleTable`: the table of the bundle the query came in; the API's
  // `access` says how the query uses that table only.
  const addQuery = (q, bundleTable = null) => {
    const target = engineKey(q.engine);
    if (!q.engine || !view.nodes[target]) return;
    (q.tables || []).forEach((table) => {
      const source = tableKey(table);
      const id = queryEdgeId(q.query_id, table);
      if (view.nodes[source] && !edges.has(id)) {
        const access = table === bundleTable ? q.access : null;
        edges.set(id, { id, kind: 'query', source, target, query: q, table, access });
      }
    });
  };
  Object.values(view.bundles).forEach((b) => {
    const source = tableKey(b.table);
    const target = engineKey(b.engine);
    if (!view.nodes[source] || !view.nodes[target]) return;
    if (view.fanned[b.key]) b.queries.forEach((q) => addQuery(q, b.table));
    else edges.set(b.key, { id: b.key, kind: 'bundle', source, target, bundle: b });
  });
  Object.values(view.pinned).forEach((q) => addQuery(q));
  return [...edges.values()];
}

/** How many elements the view draws: its nodes plus its drawn edges. */
export function elementCount(view) {
  return Object.keys(view.nodes).length + drawnEdges(view).length;
}

/**
 * Merge one start point's links (the API's `{nodes, bundles}`) into the view,
 * in the API's order (busiest bundle first), while the view stays within
 * `max` elements. What does not fit is left out and `capped` is set.
 */
export function mergeLinks(view, links, max = MAX_VIEW_ELEMENTS) {
  let next = { ...view, nodes: { ...view.nodes }, bundles: { ...view.bundles }, capped: false };
  const nodeByKey = Object.fromEntries((links.nodes || []).map((n) => [n.key, n]));
  const anchor = `${links.anchor_kind}:${links.anchor_id}`;
  if (nodeByKey[anchor] && !next.nodes[anchor] && elementCount(next) < max) {
    next.nodes[anchor] = nodeByKey[anchor];
  }
  (links.bundles || []).forEach((b) => {
    const ends = [tableKey(b.table), engineKey(b.engine)].filter((k) => !next.nodes[k]);
    const trial = {
      ...next,
      nodes: { ...next.nodes, ...Object.fromEntries(ends.map((k) => [k, nodeByKey[k]])) },
      bundles: { ...next.bundles, [b.key]: b },
    };
    if (ends.some((k) => !nodeByKey[k]) || elementCount(trial) > max) {
      next.capped = true;
      return;
    }
    next = trial;
  });
  if (links.anchor_kind !== 'wave') next.expanded = { ...view.expanded, [anchor]: true };
  return next;
}

/**
 * Fan a bundle out into its queries, or fold it back. Refused (`ok: false`)
 * when fanning would take the view past `max` elements.
 */
export function toggleBundle(view, key, max = MAX_VIEW_ELEMENTS) {
  if (!view.bundles[key]) return { view, ok: false };
  const fanned = { ...view.fanned };
  if (fanned[key]) delete fanned[key];
  else fanned[key] = true;
  const next = { ...view, fanned };
  if (fanned[key] && elementCount(next) > max) return { view, ok: false };
  return { view: next, ok: true };
}

/**
 * Pin searched queries: their tables and engine join the view and their edges
 * are drawn. Refused when that would take the view past `max` elements.
 */
export function pinQueries(view, search, queries, max = MAX_VIEW_ELEMENTS) {
  const nodeByKey = Object.fromEntries((search.nodes || []).map((n) => [n.key, n]));
  const nodes = { ...view.nodes };
  const pinned = { ...view.pinned };
  queries.forEach((q) => {
    pinned[q.query_id] = q;
    [...(q.tables || []).map(tableKey), q.engine ? engineKey(q.engine) : null]
      .filter((k) => k && !nodes[k] && nodeByKey[k])
      .forEach((k) => {
        nodes[k] = nodeByKey[k];
      });
  });
  const next = { ...view, nodes, pinned };
  if (elementCount(next) > max) return { view, ok: false };
  return { view: next, ok: true };
}

/** Drop a node with its bundles and the pinned queries that end on it. */
export function removeNode(view, key) {
  if (!view.nodes[key]) return view;
  const nodes = { ...view.nodes };
  delete nodes[key];
  const bundles = {};
  const fanned = {};
  Object.values(view.bundles).forEach((b) => {
    if (tableKey(b.table) !== key && engineKey(b.engine) !== key) {
      bundles[b.key] = b;
      if (view.fanned[b.key]) fanned[b.key] = true;
    }
  });
  const pinned = {};
  Object.values(view.pinned).forEach((q) => {
    if (engineKey(q.engine) !== key) pinned[q.query_id] = q;
  });
  const expanded = { ...view.expanded };
  delete expanded[key];
  return { nodes, bundles, fanned, pinned, expanded, capped: false };
}

// Traffic is shown by size, never colour (colour stays node kind or engine).
export const EDGE_WIDTH = [1, 8];
export const COST_NODE_SIZE = [26, 70];
export const VIEW_ORDER = ['traffic', 'moves-together', 'who-writes', 'cost'];
export const SLIDER_STEPS = 100;

/**
 * Map a figure onto [lo, hi] on a log scale over the API's `domain`
 * ({min, max}), clamped. No figure, or no domain, maps to `lo`. This only
 * places a figure the API supplies on screen; it computes no figure.
 */
export function logScale(value, domain, [lo, hi]) {
  if (value == null || !(value > 0) || !domain || !(domain.min > 0) || !(domain.max > 0)) return lo;
  if (domain.max <= domain.min) return hi;
  const t = (Math.log(value) - Math.log(domain.min)) / (Math.log(domain.max) - Math.log(domain.min));
  const clamped = Math.min(1, Math.max(0, t));
  return lo + clamped * (hi - lo);
}

/**
 * The threshold a slider position stands for: 0 at the far left, then the
 * traffic range on a log scale, so each step covers the same ratio.
 */
export function sliderThreshold(position, domain, steps = SLIDER_STEPS) {
  if (!position || !domain || !(domain.min > 0) || !(domain.max > 0)) return 0;
  const t = Math.min(1, Math.max(0, position / steps));
  return Math.exp(Math.log(domain.min) + t * (Math.log(domain.max) - Math.log(domain.min)));
}

/** The menu's views: the API's list, in a fixed order; traffic when unknown. */
export function availableViews(overlays) {
  const offered = new Set((overlays && overlays.views) || ['traffic']);
  offered.add('traffic');
  return VIEW_ORDER.filter((v) => offered.has(v));
}

/**
 * Cytoscape elements for the view: nodes first, then edges.
 *
 * `options` restyles the same elements for the chosen view (they keep their
 * ids, so Cytoscape keeps their positions):
 * - `mode`: 'traffic' (default), 'moves-together', 'who-writes' or 'cost';
 * - `traffic` / `bundleTraffic` / `cost`: the API's scales; widths and sizes
 *   are only these figures placed on pixels;
 * - `wave`: when set, what moves in that wave is drawn in full, the reads a
 *   cache in that wave fronts show as the cache layer, the queries that only
 *   cut over in it (a homogeneous first wave, before a later move) dimmer, and
 *   everything else greyed out ('other-wave'), still visible for context; a
 *   link from a table in the wave to something of another wave is a
 *   cross-wave link. All of it is the API's (wave, cached_in_wave, path);
 * - `showOtherWaves` (default true): false hides what is not in the wave;
 * - `waveBadge(wave)`: the text of a greyed engine's "moves in wave N" badge;
 * - `hiddenQueries` / `hiddenBundles`: the API's below-traffic answer;
 * - `engineFilter`: show only that engine's edges;
 * - `placeholderLabel`: the label of the collector's placeholder table (a
 *   statement with no table), which is not a table of the source database.
 */
export function toElements(view, options = {}) {
  const {
    mode = 'traffic',
    traffic = null,
    bundleTraffic = null,
    cost = null,
    wave = null,
    hiddenQueries = new Set(),
    hiddenBundles = new Set(),
    engineFilter = null,
    placeholderLabel = null,
    showOtherWaves = true,
    waveBadge = null,
    resultKeys = new Set(),
    resultQueries = new Set(),
  } = options;
  const states = {};
  const edges = drawnEdges(view).map((e) => {
    const isBundle = e.kind === 'bundle';
    const item = isBundle ? e.bundle : e.query;
    const cps = item.calls_per_second;
    const scale = isBundle ? bundleTraffic : traffic;
    const classes = [isBundle ? 'bundle' : 'query'];
    const hidden = isBundle ? hiddenBundles.has(e.id) : hiddenQueries.has(item.query_id);
    if (hidden || (engineFilter && item.engine !== engineFilter)) classes.push('hidden');
    const state = waveState(item, wave);
    states[e.id] = state;
    if (state === 'other-wave' && !showOtherWaves) classes.push('wave-hidden');
    else if (state) classes.push(state);
    const access = isBundle ? item.access : e.access;
    if (mode === 'who-writes' && access) classes.push(`access-${access}`);
    if (mode === 'moves-together' && !isBundle && item.unit) classes.push('in-unit');
    // A question's result: the link carries one of its queries.
    if (resultQueries.size && (isBundle ? (item.queries || []).some((q) => resultQueries.has(q.query_id)) : resultQueries.has(item.query_id))) classes.push('result');
    const data = {
      id: e.id,
      kind: e.kind,
      source: e.source,
      target: e.target,
      width: mode === 'cost' ? EDGE_WIDTH[0] : logScale(cps, scale, EDGE_WIDTH),
      weight: logScale(cps, scale, [0, 1]),
      label: isBundle ? `${item.query_count}` : item.label,
    };
    // The API's own figures, shown as text on hover.
    if (cps != null) data.cps = cps;
    // Its own move wave and cache wave, for the greyed-out badge on hover.
    if (item.wave != null) data.moveWave = item.wave;
    if (item.cached_in_wave != null) data.cachedIn = item.cached_in_wave;
    if (isBundle) data.count = item.query_count;
    else {
      data.qid = item.query_id;
      if (item.unit) data.unit = item.unit;
    }
    return { group: 'edges', data, classes: classes.join(' ') };
  });
  // What is in the wave: the ends of the links that move in it (or that its cache fronts).
  const lit = new Set();
  edges.forEach((e) => {
    const state = states[e.data.id];
    if ((state == null || state === 'cache-layer') && !/\bhidden\b/.test(e.classes)) {
      lit.add(e.data.source);
      lit.add(e.data.target);
    }
  });
  // Cross-wave links: a table in the wave linked to something of another wave.
  if (wave != null) {
    edges.forEach((e) => {
      const state = states[e.data.id];
      if ((state === 'other-wave' || state === 'cutover') && lit.has(e.data.source) && !/\bwave-hidden\b/.test(e.classes)) {
        e.classes = `${e.classes} cross-wave`;
      }
    });
  }
  const nodes = Object.values(view.nodes).map((node) => {
    const data = {
      id: node.key,
      label: shortLabel(node.placeholder && placeholderLabel ? placeholderLabel : node.label),
      kind: node.kind,
      expanded: view.expanded[node.key] ? 'yes' : 'no',
      placeholder: node.placeholder ? 'yes' : 'no',
      // The node's icon (utils/nodeIcons): its engine's AWS service icon, or its type's.
      icon: nodeIcon(node),
    };
    if (mode === 'cost' && node.kind === 'engine' && node.monthly_cost_usd != null) {
      data.size = logScale(node.monthly_cost_usd, cost, COST_NODE_SIZE);
    }
    if (node.monthly_cost_usd != null) data.cost = node.monthly_cost_usd;
    if (node.wave != null) data.moveWave = node.wave;
    const classes = [];
    if (wave != null && !lit.has(node.key)) {
      classes.push(showOtherWaves ? 'other-wave' : 'wave-hidden');
      // A greyed engine says which wave it moves in (the API's wave), under its name.
      if (showOtherWaves && node.kind === 'engine' && node.wave != null && waveBadge) {
        data.label = `${data.label}\n${waveBadge(node.wave)}`;
      }
    }
    if (engineFilter && node.kind === 'engine' && node.id !== engineFilter) classes.push('hidden');
    // A question's result (the Questions tab): its own tables and engines stand out.
    if (resultKeys.has(node.key)) classes.push('result');
    return { group: 'nodes', data, classes: classes.join(' ') };
  });
  return [...nodes, ...edges];
}

/**
 * How an edge (a query or a bundle) shows under the wave filter: null (it
 * moves in the wave), 'cache-layer' (a cache in the wave fronts it),
 * 'cutover' (it only cuts over in the wave, then moves later) or
 * 'other-wave' (it moves in another wave: greyed out, kept for context).
 */
export function waveState(item, wave) {
  if (wave == null) return null;
  if (item.wave === wave) return null;
  if (item.cached_in_wave === wave) return 'cache-layer';
  const cutover = item.cutover_wave ?? (item.path || []).find((s) => s.kind === 'cutover')?.wave;
  if (cutover === wave) return 'cutover';
  return 'other-wave';
}

/**
 * A query's wave path as text tags: "On Aurora MySQL from wave 1", "Moves to
 * DynamoDB in wave 3", and "Cached from wave 2" when a cache fronts it. All
 * from the API (the wave plan); a query without a path shows its wave.
 */
export function waveTags(q, t) {
  const tags = (q.path || []).map((step) =>
    step.kind === 'cutover'
      ? t('graph-explorer.tags.on-from', { engine: step.engine_label || step.engine, wave: step.wave })
      : t('graph-explorer.tags.moves-to', { engine: step.engine_label || step.engine, wave: step.wave }),
  );
  if (tags.length === 0 && q.wave != null) tags.push(t('graph-explorer.tags.wave', { wave: q.wave }));
  if (q.cached_in_wave != null) tags.push(t('graph-explorer.tags.cached', { wave: q.cached_in_wave }));
  return tags;
}

/** Long names are cut for the canvas only; the results table shows them whole. */
export function shortLabel(label, max = 32) {
  const text = String(label ?? '');
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** The edges the below-traffic request asks about: the drawn queries and bundles. */
export function trafficSubjects(view) {
  const queryIds = new Set();
  const bundles = [];
  drawnEdges(view).forEach((e) => {
    if (e.kind === 'bundle') bundles.push({ table: e.bundle.table, engine: e.bundle.engine });
    else queryIds.add(e.query.query_id);
  });
  return { queryIds: [...queryIds].sort(), bundles };
}

// Above this zoom level the canvas labels every node.
export const LABEL_ZOOM = 1.6;
const RING_RADIUS = 110;
const RING_STEP = 70;
const RING_SIZE = 12;

/**
 * Start positions for `count` new nodes around `centre`: rings of up to
 * RING_SIZE (more on each outer ring), so an expansion fans out around the
 * node that was expanded instead of landing in a grid. The force layout then
 * settles them.
 */
export function ringPositions(centre, count) {
  const positions = [];
  let ring = 0;
  while (positions.length < count) {
    const capacity = RING_SIZE + ring * 6;
    const onRing = Math.min(capacity, count - positions.length);
    const radius = RING_RADIUS + ring * RING_STEP;
    const offset = ring % 2 ? Math.PI / capacity : 0;
    for (let i = 0; i < onRing; i += 1) {
      const angle = offset + (2 * Math.PI * i) / onRing;
      positions.push({ x: centre.x + radius * Math.cos(angle), y: centre.y + radius * Math.sin(angle) });
    }
    ring += 1;
  }
  return positions;
}

/** Bundles drawn collapsed, busiest first (by the API's calls/s total). */
function collapsedBundles(view) {
  return drawnEdges(view)
    .filter((e) => e.kind === 'bundle')
    .map((e) => e.bundle)
    .sort((a, b) => b.calls_per_second - a.calls_per_second);
}

/**
 * Fan out every drawn bundle, busiest first, while the view stays within
 * `max` elements. Returns the new view and how many bundles stayed bundled
 * (view state: how much of the view could be drawn, not a report figure).
 */
export function fanOutAll(view, max = MAX_VIEW_ELEMENTS) {
  let next = view;
  let stayed = 0;
  collapsedBundles(view).forEach((b) => {
    const out = toggleBundle(next, b.key, max);
    if (out.ok) next = out.view;
    else stayed += 1;
  });
  return { view: next, stayed };
}

/** Fold every fanned bundle back. */
export function collapseAll(view) {
  return { ...view, fanned: {}, pinned: {} };
}

/**
 * A new starting point (Explore from the toolbar, a walkthrough step): its
 * links join the view with every table and engine pair as one collapsed
 * bundle. Nothing fanned out or pinned before carries over; only an explicit
 * bundle click, "Fan out all", a query search or selection, or a step about
 * one query draws query edges again.
 */
export function startFrom(view, links, max = MAX_VIEW_ELEMENTS) {
  return mergeLinks(collapseAll(view), links, max);
}

export const anyFanned = (view) => Object.keys(view.fanned).length > 0;

/**
 * The queries the list shows: those of `bundleKey` when one is selected,
 * otherwise every query the drawn bundles carry and every pinned query, once
 * each, busiest first (by the API's calls/s).
 */
export function listedQueries(view, bundleKey = null) {
  const seen = new Map();
  const add = (q, bundle) => {
    if (!seen.has(q.query_id)) seen.set(q.query_id, { query: q, bundleKey: bundle ? bundle.key : null, cps: q.calls_per_second || 0 });
  };
  const selected = bundleKey && view.bundles[bundleKey];
  if (selected) selected.queries.forEach((q) => add(q, selected));
  else {
    Object.values(view.bundles).forEach((b) => {
      if (view.nodes[tableKey(b.table)] && view.nodes[engineKey(b.engine)]) b.queries.forEach((q) => add(q, b));
    });
    Object.values(view.pinned).forEach((q) => add(q, null));
  }
  return [...seen.values()].sort((a, b) => b.cps - a.cps);
}

/** The list's filter box: a case-insensitive match on id, label, SQL or table. */
export function filterListed(items, text) {
  const needle = String(text || '').trim().toLowerCase();
  if (!needle) return items;
  return items.filter(({ query: q }) =>
    [q.query_id, q.label, q.sql_preview, ...(q.tables || [])].some((v) => String(v || '').toLowerCase().includes(needle)),
  );
}

/** Whether a query's edges are drawn (fanned out, or pinned). */
export function isQueryDrawn(view, queryId) {
  return drawnEdges(view).some((e) => e.kind === 'query' && e.query.query_id === queryId);
}

// ---------------------------------------------------------------------------
// The Questions tab (#308): the graph query catalog (#1789). The API lists the
// fixed questions with their parameters' choices and runs one; no Cypher is
// ever sent from here. A result's projection is drawn as a new start.
// ---------------------------------------------------------------------------

export function catalogPath(jobId) {
  return `assessments/${encodeURIComponent(jobId)}/graph/catalog`;
}

export function catalogRunPath(jobId, entryId) {
  return `${catalogPath(jobId)}/${encodeURIComponent(entryId)}`;
}

/**
 * The API's refusals that have their own message: body too large, not JSON,
 * invalid values, too many catalog runs, no read slot, and the time limit.
 */
export const CATALOG_ERROR_STATUSES = [413, 415, 422, 429, 503, 504];

export function catalogErrorKey(result) {
  const status = result && result.status;
  return CATALOG_ERROR_STATUSES.includes(status)
    ? `graph-explorer.questions.errors.${status}`
    : 'graph-explorer.questions.errors.default';
}

/**
 * The catalog's entries grouped by section, in the API's order, keeping only
 * those whose section, title or question contains `text` (any case).
 */
export function groupEntries(entries, text) {
  const needle = (text || '').trim().toLowerCase();
  const groups = [];
  const bySection = new Map();
  (entries || []).forEach((entry) => {
    const words = [entry.section, entry.title, entry.question].join(' ').toLowerCase();
    if (needle && !words.includes(needle)) return;
    if (!bySection.has(entry.section)) {
      const group = { section: entry.section, entries: [] };
      bySection.set(entry.section, group);
      groups.push(group);
    }
    bySection.get(entry.section).entries.push(entry);
  });
  return groups;
}

// How many start points "Show on graph" expands at most (each one is a bounded links read).
export const RESULT_START_LIMIT = 12;
const KIND_OF_LABEL = { Query: 'query', SourceTable: 'table', Engine: 'engine', Destination: 'engine' };

/**
 * How a result's projection is drawn. The start points cover the result with
 * as few reads as they can: its tables first (their bundles carry their
 * queries); its engines only when it has neither tables nor queries (an engine
 * start draws the engine's busiest bundles, not the result's); then, once the
 * table reads are back, the queries they did not reach (`queryStarts`). At
 * most RESULT_START_LIMIT starts in all. `keys` are the result's own table and
 * engine nodes and `queryIds` its queries, drawn highlighted; `undrawn` names
 * the node kinds this view has no place for.
 */
export function resultPlan(projection) {
  const keys = new Set();
  const undrawn = new Set();
  const found = { query: [], table: [], engine: [] };
  ((projection && projection.nodes) || []).forEach((n) => {
    const kind = KIND_OF_LABEL[n.label];
    if (!kind) {
      undrawn.add(n.label);
      return;
    }
    if (!found[kind].includes(n.id)) found[kind].push(n.id);
    if (kind !== 'query') keys.add(`${kind}:${n.id}`);
  });
  const lead = found.table.length
    ? found.table.map((id) => ({ kind: 'table', id }))
    : found.query.length
      ? []
      : found.engine.map((id) => ({ kind: 'engine', id }));
  return {
    first: lead.slice(0, RESULT_START_LIMIT),
    more: lead.length > RESULT_START_LIMIT,
    queries: found.query,
    keys,
    queryIds: new Set(found.query),
    undrawn: [...undrawn].sort(),
  };
}

export const hasStarts = (plan) => plan.first.length + plan.queries.length > 0;

// The most query ids one membership read names (the API's MAX_VIEW_QUERIES).
export const MEMBERSHIP_LIMIT = 150;

export function queryBundlesPath(jobId, queryIds) {
  const query = new URLSearchParams(queryIds.map((id) => ['query_id', String(id)]));
  return `assessments/${encodeURIComponent(jobId)}/graph/explore/query-bundles?${query.toString()}`;
}

/** The result's queries whose full bundle membership is asked for: only when tables were read first. */
export function membershipQueries(plan) {
  return plan.first.length ? plan.queries.slice(0, MEMBERSHIP_LIMIT) : [];
}

/**
 * The query starts left after the first reads: the result's queries no drawn
 * bundle holds. `membership` (the API's every bundle each query is in, or
 * null when it could not be read) decides for the queries it names: a query
 * is covered when every bundle it is in was drawn. Otherwise the drawn
 * bundles' listed queries (their busiest) are all that is known.
 */
export function queryStarts(plan, firstLinks, membership = null) {
  const drawn = new Set();
  const listed = new Set();
  (firstLinks || []).forEach((links) => {
    if (links && links.success) {
      (links.bundles || []).forEach((b) => {
        drawn.add(`${b.table}|${b.engine}`);
        (b.queries || []).forEach((q) => listed.add(q.query_id));
      });
    }
  });
  const inBundles = new Map();
  (membership || []).forEach((m) => {
    if (!inBundles.has(m.query_id)) inBundles.set(m.query_id, []);
    inBundles.get(m.query_id).push(`${m.table}|${m.engine}`);
  });
  const covered = (id) => (inBundles.has(id) ? inBundles.get(id).every((key) => drawn.has(key)) : listed.has(id));
  const left = plan.queries.filter((id) => !covered(id));
  const room = Math.max(0, RESULT_START_LIMIT - plan.first.length);
  return {
    starts: left.slice(0, room).map((id) => ({ kind: 'query', id })),
    more: plan.more || left.length > room,
  };
}

/** One result cell as plain text: a list shows its items, anything else its value. */
export function cellText(value) {
  if (value == null) return '';
  if (Array.isArray(value)) return value.map(cellText).join(', ');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
