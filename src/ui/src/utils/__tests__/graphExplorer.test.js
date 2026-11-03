// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import {
  availableViews,
  belowTrafficPath,
  drawnEdges,
  elementCount,
  emptyView,
  explorerEnabledFrom,
  fetchExplorerEnabled,
  linksPath,
  logScale,
  optionsPath,
  mergeLinks,
  pinQueries,
  removeNode,
  ringPositions,
  searchPath,
  shortLabel,
  sliderThreshold,
  toElements,
  toggleBundle,
  trafficSubjects,
  truncatedPickerLimit,
} from '../graphExplorer';

const node = (kind, id, extra = {}) => ({ key: `${kind}:${id}`, kind, id, label: id, ...extra });
const query = (id, tables, engine, cps, extra = {}) => ({
  query_id: id, label: `SELECT ${id}`, tables, engine, calls_per_second: cps, wave: null, path: [], ...extra,
});

// The wave plan's path for a DynamoDB query after a homogeneous Aurora cutover.
const PATH = [
  { wave: 1, engine: 'aurora', engine_label: 'Aurora', kind: 'cutover' },
  { wave: 3, engine: 'dynamodb', engine_label: 'DynamoDB', kind: 'move' },
];

// wp_posts: one bundle to DynamoDB (busy, a writer and a quiet join), one to Aurora.
const links = {
  anchor_kind: 'table',
  anchor_id: 'wp_posts',
  nodes: [node('table', 'wp_posts'), node('engine', 'dynamodb', { wave: 3 }), node('engine', 'aurora', { wave: 1 }), node('table', 'wp_users')],
  bundles: [
    {
      key: 'bundle:wp_posts|dynamodb', table: 'wp_posts', engine: 'dynamodb', query_count: 2,
      calls_per_second: 100.5, wave: 3, cached_in_wave: 2, has_cached_reads: true, cutover_wave: 1, access: 'write',
      queries: [
        query('busy', ['wp_posts'], 'dynamodb', 100, { wave: 3, cached_in_wave: 2, path: PATH, access: 'write', unit: 'unit-1' }),
        query('join', ['wp_posts', 'wp_users'], 'dynamodb', 0.5, { wave: 3, path: PATH, access: 'read' }),
      ],
      queries_truncated: false,
    },
    {
      key: 'bundle:wp_posts|aurora', table: 'wp_posts', engine: 'aurora', query_count: 30,
      calls_per_second: 2, wave: 1, access: 'replica_read', queries: [], queries_truncated: true,
    },
  ],
};

describe('the flag probe', () => {
  test('only an explicit enabled: true turns the explorer on', () => {
    expect(explorerEnabledFrom({ success: true, enabled: true })).toBe(true);
    expect(explorerEnabledFrom({ success: false, status: 404 })).toBe(false);
    expect(explorerEnabledFrom({ success: true, enabled: 'true' })).toBe(false);
    expect(explorerEnabledFrom(undefined)).toBe(false);
  });

  test('a failing request reads as off', async () => {
    const api = { execute: jest.fn().mockRejectedValue(new Error('network')) };
    await expect(fetchExplorerEnabled(api)).resolves.toBe(false);
  });
});

test('paths encode customer ids and search text', () => {
  expect(linksPath('job-1', 'table', 'a&b c', 5)).toBe('assessments/job-1/graph/explore/links?kind=table&id=a%26b+c&limit=5');
  expect(searchPath('job-1', 'SELECT *')).toBe('assessments/job-1/graph/explore/search?text=SELECT+*');
  expect(belowTrafficPath('job-1', 0.5, ['-1'], [{ table: 'db.t', engine: 'dynamodb' }])).toBe(
    'assessments/job-1/graph/explore/below-traffic?min_calls_per_second=0.5&query_id=-1&bundle=db.t%7Cdynamodb',
  );
});

describe('the edge model', () => {
  test('tables and engines are nodes; each bundle is one edge until fanned', () => {
    const view = mergeLinks(emptyView(), links);
    // wp_users has no bundle yet, so it is not drawn.
    expect(Object.keys(view.nodes).sort()).toEqual(['engine:aurora', 'engine:dynamodb', 'table:wp_posts']);
    expect(drawnEdges(view).map((e) => e.id)).toEqual(['bundle:wp_posts|dynamodb', 'bundle:wp_posts|aurora']);
    expect(elementCount(view)).toBe(5);
    expect(view.expanded['table:wp_posts']).toBe(true);
  });

  test('fanning a bundle draws one edge per query, sharing the query id across tables', () => {
    let view = mergeLinks(emptyView(), links);
    view = { ...view, nodes: { ...view.nodes, 'table:wp_users': node('table', 'wp_users') } };
    const fanned = toggleBundle(view, 'bundle:wp_posts|dynamodb');
    expect(fanned.ok).toBe(true);
    const edges = drawnEdges(fanned.view);
    expect(edges.map((e) => e.id)).toEqual([
      'q:busy|wp_posts', 'q:join|wp_posts', 'q:join|wp_users', 'bundle:wp_posts|aurora',
    ]);
    const els = toElements(fanned.view, {});
    expect(els.filter((e) => e.data.qid === 'join').map((e) => e.data.source)).toEqual(['table:wp_posts', 'table:wp_users']);
    // Folding it back draws the bundle again.
    expect(drawnEdges(toggleBundle(fanned.view, 'bundle:wp_posts|dynamodb').view)[0].id).toBe('bundle:wp_posts|dynamodb');
  });

  test('the view stays within its element bound', () => {
    const capped = mergeLinks(emptyView(), links, 4);
    expect(elementCount(capped)).toBeLessThanOrEqual(4);
    expect(capped.capped).toBe(true);
    const full = mergeLinks(emptyView(), links, 5);
    expect(toggleBundle(full, 'bundle:wp_posts|dynamodb', 5).ok).toBe(false);
  });

  test('a searched query is pinned with its tables and engine', () => {
    const q = query('found', ['wp_posts'], 'dynamodb', 3);
    const out = pinQueries(emptyView(), { nodes: [node('table', 'wp_posts'), node('engine', 'dynamodb')] }, [q]);
    expect(out.ok).toBe(true);
    expect(drawnEdges(out.view).map((e) => e.id)).toEqual(['q:found|wp_posts']);
    expect(pinQueries(emptyView(), { nodes: [node('table', 'wp_posts'), node('engine', 'dynamodb')] }, [q], 2).ok).toBe(false);
  });

  test('removing a node drops its bundles and the pinned queries that end on it', () => {
    let view = mergeLinks(emptyView(), links);
    view = pinQueries(view, { nodes: [] }, [query('p', ['wp_posts'], 'aurora', 1)]).view;
    const out = removeNode(view, 'engine:aurora');
    expect(Object.keys(out.bundles)).toEqual(['bundle:wp_posts|dynamodb']);
    expect(out.pinned).toEqual({});
    expect(removeNode(out, 'table:none')).toBe(out);
  });

  test('the below-traffic request names the drawn queries and bundles', () => {
    const view = toggleBundle(mergeLinks(emptyView(), links), 'bundle:wp_posts|dynamodb').view;
    expect(trafficSubjects(view)).toEqual({
      queryIds: ['busy', 'join'],
      bundles: [{ table: 'wp_posts', engine: 'aurora' }],
    });
  });

  test('labels stay data, never markup', () => {
    const hostile = { ...links, nodes: [node('table', '<b>x</b>')], bundles: [], anchor_id: '<b>x</b>' };
    const [el] = toElements(mergeLinks(emptyView(), hostile));
    expect(el.data.label).toBe('<b>x</b>');
    expect(shortLabel('a'.repeat(40), 10)).toBe(`${'a'.repeat(9)}…`);
  });
});

describe('traffic scale and views', () => {
  const domain = { min: 0.01, max: 100 };

  test('logScale maps the API range onto pixels on a log scale, clamped', () => {
    expect(logScale(0.01, domain, [1, 8])).toBe(1);
    expect(logScale(100, domain, [1, 8])).toBe(8);
    expect(logScale(1, domain, [1, 8])).toBeCloseTo(4.5);
    expect(logScale(1000, domain, [1, 8])).toBe(8);
    expect(logScale(0.0001, domain, [1, 8])).toBe(1);
    expect(logScale(null, domain, [1, 8])).toBe(1);
    expect(logScale(0, domain, [1, 8])).toBe(1);
    expect(logScale(5, null, [1, 8])).toBe(1);
    expect(logScale(5, { min: 5, max: 5 }, [1, 8])).toBe(8);
  });

  test('the slider covers the traffic range on a log scale, from zero', () => {
    expect(sliderThreshold(0, domain)).toBe(0);
    expect(sliderThreshold(100, domain)).toBeCloseTo(100);
    expect(sliderThreshold(50, domain)).toBeCloseTo(1);
    expect(sliderThreshold(25, null)).toBe(0);
  });

  test('the view menu follows the API, in a fixed order, always with traffic', () => {
    expect(availableViews({ views: ['cost', 'traffic', 'who-writes'] })).toEqual(['traffic', 'who-writes', 'cost']);
    expect(availableViews(null)).toEqual(['traffic']);
  });

  const fanned = toggleBundle(mergeLinks(emptyView(), links), 'bundle:wp_posts|dynamodb').view;
  const byId = (els) => Object.fromEntries(els.map((e) => [e.data.id, e]));

  test('traffic: a busy query edge is thick and heavy; a bundle uses the bundle scale', () => {
    const els = byId(toElements(fanned, { traffic: domain, bundleTraffic: { min: 2, max: 200 } }));
    expect(els['q:busy|wp_posts'].data.width).toBe(8);
    expect(els['q:busy|wp_posts'].data.weight).toBe(1);
    expect(els['q:join|wp_posts'].data.width).toBeLessThan(5);
    expect(els['bundle:wp_posts|aurora'].data.width).toBe(1);
    expect(els['bundle:wp_posts|aurora'].data.count).toBe(30);
    expect(els['bundle:wp_posts|aurora'].data.cps).toBe(2);
  });

  test('the wave filter fades what does not move in the wave', () => {
    const els = byId(toElements(fanned, { wave: 3 }));
    expect(els['q:busy|wp_posts'].classes).not.toMatch('other-wave');
    expect(els['bundle:wp_posts|aurora'].classes).toMatch('other-wave');
    expect(els['engine:aurora'].classes).toMatch('other-wave');
    expect(els['engine:dynamodb'].classes).not.toMatch('other-wave');
  });

  test('who writes: the writer edge and replica reads carry their class', () => {
    const els = byId(toElements(fanned, { mode: 'who-writes' }));
    expect(els['q:busy|wp_posts'].classes).toMatch('access-write');
    expect(els['bundle:wp_posts|aurora'].classes).toMatch('access-replica_read');
    expect(byId(toElements(fanned, {}))['q:busy|wp_posts'].classes).not.toMatch('access-');
  });

  test('who writes: a read the report cannot yet place carries its own class', () => {
    const pending = JSON.parse(JSON.stringify(fanned));
    Object.values(pending.bundles).forEach((b) => {
      b.access = 'read_or_replica';
    });
    const els = byId(toElements(pending, { mode: 'who-writes' }));
    expect(els['bundle:wp_posts|aurora'].classes).toMatch('access-read_or_replica');
  });

  test('moves together marks the edges of a placement unit', () => {
    const els = byId(toElements(fanned, { mode: 'moves-together' }));
    expect(els['q:busy|wp_posts'].classes).toMatch('in-unit');
    expect(els['q:busy|wp_posts'].data.unit).toBe('unit-1');
    expect(els['q:join|wp_posts'].classes).not.toMatch('in-unit');
  });

  test('cost sizes engines by the report cost', () => {
    const view = mergeLinks(emptyView(), {
      ...links, nodes: [node('table', 'wp_posts'), node('engine', 'dynamodb', { monthly_cost_usd: 50 }), node('engine', 'aurora', { monthly_cost_usd: 10 })],
    });
    const els = byId(toElements(view, { mode: 'cost', cost: { min: 10, max: 50 } }));
    expect(els['engine:dynamodb'].data.size).toBe(70);
    expect(els['engine:aurora'].data.size).toBe(26);
    expect(els['table:wp_posts'].data.size).toBeUndefined();
  });

  test('hidden edges come from the API answer and the engine filter', () => {
    const els = byId(toElements(fanned, { hiddenQueries: new Set(['join']), hiddenBundles: new Set(['bundle:wp_posts|aurora']) }));
    expect(els['q:join|wp_posts'].classes).toMatch('hidden');
    expect(els['bundle:wp_posts|aurora'].classes).toMatch('hidden');
    expect(els['q:busy|wp_posts'].classes).not.toMatch('hidden');
    const filtered = byId(toElements(fanned, { engineFilter: 'aurora' }));
    expect(filtered['q:busy|wp_posts'].classes).toMatch('hidden');
    expect(filtered['engine:dynamodb'].classes).toMatch('hidden');
  });
});

describe('ring placement', () => {
  const dist = (p, c) => Math.hypot(p.x - c.x, p.y - c.y);

  test('new nodes fan out around the expanded node, never stacked', () => {
    const ring = ringPositions({ x: 0, y: 0 }, 52);
    expect(ring).toHaveLength(52);
    expect(new Set(ring.map((p) => `${Math.round(p.x)},${Math.round(p.y)}`)).size).toBe(52);
    expect(dist(ring[0], { x: 0, y: 0 })).toBeCloseTo(110);
    expect(ringPositions({ x: 0, y: 0 }, 0)).toEqual([]);
  });
});

describe('round 4: fan out all and the query list', () => {
  const { fanOutAll, collapseAll, anyFanned, listedQueries, filterListed, isQueryDrawn } = jest.requireActual('../graphExplorer');
  const big = (key, table, n, cps) => ({
    key, table, engine: 'dynamodb', query_count: n, calls_per_second: cps, wave: null, access: null,
    queries: Array.from({ length: n }, (_, i) => query(`${table}-${i}`, [table], 'dynamodb', cps / n)),
    queries_truncated: false,
  });
  const many = {
    anchor_kind: 'engine', anchor_id: 'dynamodb',
    nodes: [node('engine', 'dynamodb'), node('table', 'a'), node('table', 'b'), node('table', 'c')],
    bundles: [big('bundle:a|dynamodb', 'a', 3, 1), big('bundle:b|dynamodb', 'b', 5, 50), big('bundle:c|dynamodb', 'c', 4, 10)],
  };

  test('fan out all fans every bundle when they fit', () => {
    const view = mergeLinks(emptyView(), many);
    const out = fanOutAll(view);
    expect(out.stayed).toBe(0);
    expect(drawnEdges(out.view).every((e) => e.kind === 'query')).toBe(true);
    expect(anyFanned(out.view)).toBe(true);
    expect(drawnEdges(collapseAll(out.view)).every((e) => e.kind === 'bundle')).toBe(true);
  });

  test('past the bound, the busiest bundles fan out first and the rest stay bundled', () => {
    const view = mergeLinks(emptyView(), many);
    // 4 nodes + 3 bundles = 7; fanning b (5) gives 11, then c (4) 14, then a (3) 16.
    const out = fanOutAll(view, 14);
    expect(out.stayed).toBe(1);
    expect(out.view.fanned).toEqual({ 'bundle:b|dynamodb': true, 'bundle:c|dynamodb': true });
    expect(elementCount(out.view)).toBeLessThanOrEqual(14);
  });

  test('the list shows the drawn queries busiest first, or one bundle', () => {
    const view = mergeLinks(emptyView(), many);
    const all = listedQueries(view).map((i) => i.query.query_id);
    expect(all.slice(0, 5)).toEqual(['b-0', 'b-1', 'b-2', 'b-3', 'b-4']);
    expect(all).toHaveLength(12);
    expect(listedQueries(view, 'bundle:a|dynamodb').map((i) => i.query.query_id)).toEqual(['a-0', 'a-1', 'a-2']);
    expect(listedQueries(view)[0].bundleKey).toBe('bundle:b|dynamodb');
  });

  test('the list filter matches id, SQL and table, as text', () => {
    const items = listedQueries(mergeLinks(emptyView(), many));
    expect(filterListed(items, 'C-1').map((i) => i.query.query_id)).toEqual(['c-1']);
    expect(filterListed(items, '')).toBe(items);
  });

  test('a query is drawn only once its bundle is fanned', () => {
    const view = mergeLinks(emptyView(), many);
    expect(isQueryDrawn(view, 'b-0')).toBe(false);
    expect(isQueryDrawn(toggleBundle(view, 'bundle:b|dynamodb').view, 'b-0')).toBe(true);
  });
});

test('the collector placeholder table is labelled as no table', () => {
  const view = mergeLinks(emptyView(), {
    ...links, anchor_id: 'unknown', nodes: [node('table', 'unknown', { placeholder: true })], bundles: [],
  });
  const [el] = toElements(view, { placeholderLabel: 'No table' });
  expect(el.data.label).toBe('No table');
  expect(el.data.placeholder).toBe('yes');
});

describe('round 5: one move wave, the cache layer and the cutover', () => {
  const { waveState, waveTags } = jest.requireActual('../graphExplorer');
  const fanned = toggleBundle(mergeLinks(emptyView(), links), 'bundle:wp_posts|dynamodb').view;
  const byId = (els) => Object.fromEntries(els.map((e) => [e.data.id, e]));
  const t = (key, opts) => `${key} ${JSON.stringify(opts)}`;

  test('the wave filter follows the final move wave', () => {
    const els = byId(toElements(fanned, { wave: 3 }));
    expect(els['q:busy|wp_posts'].classes).not.toMatch(/other-wave|cutover|cache-layer/);
    expect(els['bundle:wp_posts|aurora'].classes).toMatch('other-wave');
  });

  test('in the cache wave, the reads its cache fronts show as the cache layer', () => {
    const els = byId(toElements(fanned, { wave: 2 }));
    expect(els['q:busy|wp_posts'].classes).toMatch('cache-layer');
    expect(els['q:join|wp_posts'].classes).toMatch('other-wave');
  });

  test('in the cutover wave, queries that move later show dimmer, its own in full', () => {
    const els = byId(toElements(fanned, { wave: 1 }));
    expect(els['q:busy|wp_posts'].classes).toMatch('cutover');
    expect(els['bundle:wp_posts|aurora'].classes).not.toMatch(/other-wave|cutover/);
    // A dimmed cutover edge does not light its engine.
    expect(els['engine:dynamodb'].classes).toMatch('other-wave');
    expect(els['engine:aurora'].classes).not.toMatch('other-wave');
    // A collapsed bundle carries the cutover wave itself.
    const collapsed = byId(toElements(mergeLinks(emptyView(), links), { wave: 1 }));
    expect(collapsed['bundle:wp_posts|dynamodb'].classes).toMatch('cutover');
  });

  test('no wave, no state', () => {
    expect(waveState({ wave: 3 }, null)).toBe(null);
  });

  test('a path-tagged DynamoDB query and a cached read get their tags from the API', () => {
    expect(waveTags({ wave: 3, cached_in_wave: 2, path: PATH }, t)).toEqual([
      'graph-explorer.tags.on-from {"engine":"Aurora","wave":1}',
      'graph-explorer.tags.moves-to {"engine":"DynamoDB","wave":3}',
      'graph-explorer.tags.cached {"wave":2}',
    ]);
    expect(waveTags({ wave: 1, path: [] }, t)).toEqual(['graph-explorer.tags.wave {"wave":1}']);
    expect(waveTags({ wave: null, path: [] }, t)).toEqual([]);
  });
});

test('nodes carry their icon: the engine service icon, the table icon, the muted placeholder', () => {
  const { ENGINE_ICONS, NODE_TYPE_ICONS } = jest.requireActual('../nodeIcons');
  const view = mergeLinks(emptyView(), {
    ...links, nodes: [...links.nodes, node('table', 'unknown', { placeholder: true })],
  });
  const withPlaceholder = { ...view, nodes: { ...view.nodes, 'table:unknown': node('table', 'unknown', { placeholder: true }) } };
  const byId = Object.fromEntries(toElements(withPlaceholder, {}).map((e) => [e.data.id, e]));
  expect(byId['engine:dynamodb'].data.icon).toBe(ENGINE_ICONS.dynamodb);
  expect(byId['table:wp_posts'].data.icon).toBe(NODE_TYPE_ICONS.table.icon);
  expect(byId['table:unknown'].data.icon).toBe(NODE_TYPE_ICONS.placeholder_table.icon);
});

describe('every starting point starts collapsed', () => {
  const { startFrom, fanOutAll } = jest.requireActual('../graphExplorer');

  test('a new starting point folds what an earlier one fanned out or pinned', () => {
    let view = fanOutAll(mergeLinks(emptyView(), links)).view;
    view = pinQueries(view, { nodes: [] }, [query('p', ['wp_posts'], 'dynamodb', 1)]).view;
    expect(drawnEdges(view).some((e) => e.kind === 'query')).toBe(true);
    const next = startFrom(view, links);
    expect(next.fanned).toEqual({});
    expect(next.pinned).toEqual({});
    expect(drawnEdges(next).every((e) => e.kind === 'bundle')).toBe(true);
  });

  test('expanding a node keeps what the user fanned out', () => {
    const view = toggleBundle(mergeLinks(emptyView(), links), 'bundle:wp_posts|dynamodb').view;
    const more = mergeLinks(view, { ...links, anchor_kind: 'engine', anchor_id: 'aurora' });
    expect(more.fanned).toEqual({ 'bundle:wp_posts|dynamodb': true });
  });
});

describe('round 9: waves give context instead of hiding', () => {
  const fanned = toggleBundle(mergeLinks(emptyView(), links), 'bundle:wp_posts|dynamodb').view;
  const byId = (els) => Object.fromEntries(els.map((e) => [e.data.id, e]));
  const badge = (w) => `wave ${w}`;

  test('what is not in the wave is greyed out, not hidden, by default', () => {
    const inCacheWave = byId(toElements(fanned, { wave: 2, waveBadge: badge }));
    expect(inCacheWave['q:join|wp_posts'].classes).toMatch('other-wave');
    expect(inCacheWave['q:join|wp_posts'].classes).not.toMatch(/hidden/);
    const els = byId(toElements(fanned, { wave: 1, waveBadge: badge }));
    expect(els['engine:dynamodb'].classes).toMatch('other-wave');
    // A greyed engine carries its own move wave under its name.
    expect(els['engine:dynamodb'].data.label).toBe('dynamodb\nwave 3');
    expect(els['engine:aurora'].data.label).toBe('aurora');
  });

  test('"Show other waves" off hides the rest, keeping the cutover and cache layer', () => {
    const els = byId(toElements(fanned, { wave: 2, showOtherWaves: false, waveBadge: badge }));
    expect(els['q:join|wp_posts'].classes).toMatch('wave-hidden');
    expect(els['bundle:wp_posts|aurora'].classes).toMatch('wave-hidden');
    expect(els['q:busy|wp_posts'].classes).toMatch('cache-layer');
    expect(els['engine:aurora'].classes).toMatch('wave-hidden');
    expect(els['engine:aurora'].data.label).toBe('aurora');
  });

  test('each edge carries its own move wave and cache wave from the API', () => {
    const els = byId(toElements(fanned, { wave: 1 }));
    // A cached query that cuts over in wave 1 and moves in wave 3.
    expect(els['q:busy|wp_posts'].data.moveWave).toBe(3);
    expect(els['q:busy|wp_posts'].data.cachedIn).toBe(2);
    expect(els['q:busy|wp_posts'].classes).toMatch('cutover');
    // A cutover query with no cache.
    expect(els['q:join|wp_posts'].data.moveWave).toBe(3);
    expect(els['q:join|wp_posts'].data.cachedIn).toBeUndefined();
    expect(els['bundle:wp_posts|aurora'].data.moveWave).toBe(1);
  });

  test('a table in the wave linked to another wave gets the cross-wave style', () => {
    const els = byId(toElements(fanned, { wave: 1 }));
    // wp_posts is in wave 1 (its Aurora bundle moves in it); its DynamoDB queries move in wave 3.
    expect(els['q:busy|wp_posts'].classes).toMatch('cross-wave');
    expect(els['q:join|wp_posts'].classes).toMatch('cross-wave');
    expect(els['bundle:wp_posts|aurora'].classes).not.toMatch('cross-wave');
    // In wave 3 the Aurora bundle is greyed, but wp_posts is in wave 3 too: cross-wave.
    expect(byId(toElements(fanned, { wave: 3 }))['bundle:wp_posts|aurora'].classes).toMatch('cross-wave');
    // No wave picked: no cross-wave links.
    expect(byId(toElements(fanned, {}))['q:busy|wp_posts'].classes).not.toMatch('cross-wave');
  });
});


describe('the start picker options', () => {
  test('the options path asks for the documented maximum', () => {
    expect(optionsPath('job 1')).toBe('assessments/job%201/graph/explore/options?limit=1000');
    expect(optionsPath('job-1', 5)).toBe('assessments/job-1/graph/explore/options?limit=5');
  });

  test('only a cut-short table list names its limit', () => {
    expect(truncatedPickerLimit('table', { limit: 1000, tables_truncated: true })).toBe(1000);
    expect(truncatedPickerLimit('table', { limit: 1000, tables_truncated: false })).toBe(null);
    expect(truncatedPickerLimit('engine', { limit: 1000, tables_truncated: true })).toBe(null);
    expect(truncatedPickerLimit('table', null)).toBe(null);
  });
});

// #308 Questions tab: the graph query catalog (#1789) in the explorer.
describe('the questions tab helpers', () => {
  // eslint-disable-next-line global-require
  const q = require('../graphExplorer');

  test('the catalog paths encode the job and the entry', () => {
    expect(q.catalogPath('job 1')).toBe('assessments/job%201/graph/catalog');
    expect(q.catalogRunPath('job-1', 'table_engines')).toBe('assessments/job-1/graph/catalog/table_engines');
  });

  test('entries are grouped by section in the API order and filtered by their words', () => {
    const entries = [
      { id: 'a', section: 'summary', title: 'Engine distribution', question: 'How many queries go to each engine?' },
      { id: 'b', section: 'impact', title: 'Table blast radius', question: 'What touches this table?' },
      { id: 'c', section: 'summary', title: 'Risk register', question: 'Which risks are open?' },
    ];
    expect(q.groupEntries(entries, '')).toEqual([
      { section: 'summary', entries: [entries[0], entries[2]] },
      { section: 'impact', entries: [entries[1]] },
    ]);
    expect(q.groupEntries(entries, 'TABLE')).toEqual([{ section: 'impact', entries: [entries[1]] }]);
    expect(q.groupEntries(entries, 'risks')).toEqual([{ section: 'summary', entries: [entries[2]] }]);
    expect(q.groupEntries(entries, 'nothing like it')).toEqual([]);
  });

  test('each API refusal has its own friendly message', () => {
    [413, 415, 422, 429].forEach((status) => {
      expect(q.catalogErrorKey({ success: false, status })).toBe(`graph-explorer.questions.errors.${status}`);
    });
    expect(q.catalogErrorKey({ success: false, status: 500 })).toBe('graph-explorer.questions.errors.default');
    expect(q.catalogErrorKey({ success: false })).toBe('graph-explorer.questions.errors.default');
  });

  const proj = (...nodes) => ({ nodes: nodes.map(([label, id]) => ({ id, label })), edges: [] });

  test('a result starts from its tables; its queries only where the tables do not reach them', () => {
    const projection = proj(
      ['SourceTable', 'db.t1'], ['Query', 'q1'], ['Query', 'q9'], ['Destination', 'dynamodb'],
      ['Risk', 'R-1'], ['AccessPattern', 'AP-1'], ['Risk', 'R-2'],
    );
    const plan = q.resultPlan(projection);
    // Tables first: an engine start would draw the engine's busiest bundles, not the result's.
    expect(plan.first).toEqual([{ kind: 'table', id: 'db.t1' }]);
    expect(plan.queries).toEqual(['q1', 'q9']);
    expect([...plan.keys].sort()).toEqual(['engine:dynamodb', 'table:db.t1']);
    expect([...plan.queryIds].sort()).toEqual(['q1', 'q9']);
    // Node kinds the explorer does not draw are named, not counted.
    expect(plan.undrawn).toEqual(['AccessPattern', 'Risk']);
    expect(q.hasStarts(plan)).toBe(true);
    // The table's bundles already carry q1: only q9 needs a start of its own.
    const tableLinks = { success: true, bundles: [{ queries: [{ query_id: 'q1' }] }] };
    expect(q.queryStarts(plan, [tableLinks])).toEqual({ starts: [{ kind: 'query', id: 'q9' }], more: false });
  });

  test("the reviewer's blast radius: one table and 36 queries start from the table alone", () => {
    const ids = Array.from({ length: 36 }, (_, i) => `q${i}`);
    const plan = q.resultPlan(proj(['SourceTable', 'wordpress.wp_posts'], ...ids.map((id) => ['Query', id]), ['Destination', 'aurora_mysql']));
    expect(plan.first).toEqual([{ kind: 'table', id: 'wordpress.wp_posts' }]);
    const links = { success: true, bundles: [{ queries: ids.slice(0, 30).map((query_id) => ({ query_id })) }, { queries: ids.slice(30).map((query_id) => ({ query_id })) }] };
    expect(q.queryStarts(plan, [links])).toEqual({ starts: [], more: false });
  });

  test("a busy table's queries past its bundle's 40 busiest are covered by the API's full membership", () => {
    const ids = Array.from({ length: 60 }, (_, i) => `q${i}`);
    const plan = q.resultPlan(proj(['SourceTable', 't'], ...ids.map((id) => ['Query', id])));
    // The links read lists the bundle's 40 busiest; the other 20 are in it too.
    const links = { success: true, bundles: [{ key: 'bundle:t|dynamodb', table: 't', engine: 'dynamodb', queries: ids.slice(0, 40).map((query_id) => ({ query_id })) }] };
    // Without it, 20 look uncovered: 11 starts (the bound, after the table) and the note.
    expect(q.queryStarts(plan, [links])).toMatchObject({ more: true });
    expect(q.queryStarts(plan, [links]).starts).toHaveLength(q.RESULT_START_LIMIT - 1);
    const membership = ids.map((query_id) => ({ query_id, table: 't', engine: 'dynamodb' }));
    expect(q.queryStarts(plan, [links], membership)).toEqual({ starts: [], more: false });
    // A query also in a bundle no start drew still needs its own start.
    const plus = [...membership, { query_id: 'q5', table: 'other', engine: 'dynamodb' }];
    expect(q.queryStarts(plan, [links], plus)).toEqual({ starts: [{ kind: 'query', id: 'q5' }], more: false });
    expect(q.membershipQueries(plan)).toHaveLength(60);
  });

  test('the membership read asks for at most the bound of query ids', () => {
    const ids = Array.from({ length: q.MEMBERSHIP_LIMIT + 5 }, (_, i) => `q${i}`);
    const plan = q.resultPlan(proj(['SourceTable', 't'], ...ids.map((id) => ['Query', id])));
    expect(q.membershipQueries(plan)).toHaveLength(q.MEMBERSHIP_LIMIT);
    expect(q.queryBundlesPath('job 1', ['a&b', 'c'])).toBe('assessments/job%201/graph/explore/query-bundles?query_id=a%26b&query_id=c');
    expect(q.membershipQueries(q.resultPlan(proj(['Query', 'q1'])))).toEqual([]);
  });

  test('engines start only a result with neither tables nor queries', () => {
    expect(q.resultPlan(proj(['Engine', 'dynamodb'], ['Engine', 'aurora_mysql'])).first).toEqual([
      { kind: 'engine', id: 'dynamodb' }, { kind: 'engine', id: 'aurora_mysql' },
    ]);
    expect(q.resultPlan(proj(['Engine', 'dynamodb'], ['Query', 'q1'])).first).toEqual([]);
  });

  test('the start points are bounded and say so', () => {
    const ids = Array.from({ length: q.RESULT_START_LIMIT + 3 }, (_, i) => `q${i}`);
    const plan = q.resultPlan(proj(...ids.map((id) => ['Query', id])));
    const out = q.queryStarts(plan, []);
    expect(out.starts).toHaveLength(q.RESULT_START_LIMIT);
    expect(out.more).toBe(true);
    const tables = q.resultPlan(proj(...Array.from({ length: q.RESULT_START_LIMIT + 2 }, (_, i) => ['SourceTable', `t${i}`]), ['Query', 'qx']));
    expect(tables.first).toHaveLength(q.RESULT_START_LIMIT);
    expect(tables.more).toBe(true);
    // The tables used the whole bound: no room for the uncovered query.
    expect(q.queryStarts(tables, [])).toEqual({ starts: [], more: true });
    const none = q.resultPlan(proj());
    expect(q.hasStarts(none)).toBe(false);
    expect(q.hasStarts(q.resultPlan(null))).toBe(false);
  });

  test("only the result's own nodes and query links are drawn highlighted", () => {
    let view = q.emptyView();
    view = q.mergeLinks(view, links);
    const plain = q.toElements(view);
    expect(plain.filter((e) => /\bresult\b/.test(e.classes))).toEqual([]);
    const lit = q.toElements(view, { resultKeys: new Set(['table:wp_posts']), resultQueries: new Set(['busy']) });
    // wp_users comes along (a join of the result's query) but is not in the result.
    expect(lit.filter((e) => e.group === 'nodes' && /\bresult\b/.test(e.classes)).map((e) => e.data.id)).toEqual(['table:wp_posts']);
    // The bundle that carries the result's query is lit, the other is not.
    expect(lit.filter((e) => e.group !== 'nodes' && /\bresult\b/.test(e.classes)).map((e) => e.data.id)).toEqual(['bundle:wp_posts|dynamodb']);
  });

  test('a cell shows a value as text, a list as its items', () => {
    expect(q.cellText('wp_posts')).toBe('wp_posts');
    expect(q.cellText(3)).toBe('3');
    expect(q.cellText(['a', 'b'])).toBe('a, b');
    expect(q.cellText(null)).toBe('');
    expect(q.cellText(true)).toBe('true');
    expect(q.cellText({ k: 1 })).toBe('{"k":1}');
  });
});
