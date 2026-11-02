// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #308: the experimental graph explorer is invisible unless the server flag is on.
 *
 * - The results page's entry button renders nothing when the flag is off, and the
 *   results page only mounts it under `graphExplorer.enabled &&` (so its header
 *   actions are unchanged with the flag off).
 * - The explorer route sends the user home when the probe says off (404), like
 *   an unknown path, and renders the page with its notice when on.
 *
 * Cloudscape ships ESM this repo's Jest config cannot transform, so its
 * components are mocked as plain DOM elements (as in PatternAnalysisFacts.test.js).
 */
import fs from 'fs';
import path from 'path';
import { act } from 'react';
import { createRoot } from 'react-dom/client';

jest.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key) => key }),
}));

let mockNavigatedTo = null;
jest.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  Navigate: ({ to }) => {
    mockNavigatedTo = to;
    return null;
  },
}));

const mockExecute = jest.fn();
jest.mock('../../classes/ApiManager', () => ({
  __esModule: true,
  default: function ApiManager() {
    this.execute = mockExecute;
  },
}));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
let mockCanvasProps = null;
jest.mock('../../components/GraphExplorerCanvas', () => ({
  __esModule: true,
  default: (props) => {
    mockCanvasProps = props;
    return <div data-testid="canvas" />;
  },
}));

function mockPlain() {
  return {
    __esModule: true,
    default: ({ children, header, content, actions, description, href, info }) => (
      <div data-href={href}>
        {header}
        {info}
        {description}
        {actions}
        {content}
        {children}
      </div>
    ),
  };
}
let mockLayoutProps = null;
jest.mock('@cloudscape-design/components/app-layout', () => ({
  __esModule: true,
  default: (props) => {
    mockLayoutProps = props;
    return <div>{props.content}</div>;
  },
}));
jest.mock('@cloudscape-design/components/alert', () => mockPlain());
jest.mock('@cloudscape-design/components/box', () => mockPlain());
jest.mock('@cloudscape-design/components/breadcrumb-group', () => mockPlain());
jest.mock('@cloudscape-design/components/button', () => ({
  __esModule: true,
  default: ({ children, onClick, href, ariaLabel, ariaExpanded, disabled }) => (
    <button type="button" data-href={href} onClick={onClick} aria-label={ariaLabel} aria-expanded={ariaExpanded} disabled={disabled}>
      {children}
    </button>
  ),
}));
jest.mock('@cloudscape-design/components/tabs', () => ({
  __esModule: true,
  default: ({ tabs, activeTabId, onChange }) => (
    <div role="tablist">
      {tabs.map((tab) => (
        <button key={tab.id} type="button" role="tab" aria-selected={tab.id === activeTabId} onClick={() => onChange({ detail: { activeTabId: tab.id } })}>
          {tab.label}
        </button>
      ))}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/button-group', () => ({
  __esModule: true,
  default: ({ items, onItemClick }) => (
    <div data-testid="button-group">
      {items.map((item) => (
        <button key={item.id} type="button" aria-label={item.text} disabled={item.disabled} onClick={() => onItemClick({ detail: { id: item.id } })} />
      ))}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/popover', () => ({
  __esModule: true,
  default: ({ children, content }) => (
    <div>
      {children}
      <div data-testid="popover-content">{content}</div>
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/input', () => ({
  __esModule: true,
  default: ({ value, onChange, ariaLabel }) => (
    <input aria-label={ariaLabel} value={value} onChange={(e) => onChange({ detail: { value: e.target.value } })} />
  ),
}));
jest.mock('@cloudscape-design/components/column-layout', () => mockPlain());
jest.mock('@cloudscape-design/components/badge', () => mockPlain());
jest.mock('@cloudscape-design/components/container', () => mockPlain());
jest.mock('@cloudscape-design/components/form-field', () => mockPlain());
jest.mock('@cloudscape-design/components/header', () => mockPlain());
jest.mock('@cloudscape-design/components/segmented-control', () => ({
  __esModule: true,
  default: ({ options, onChange }) => (
    <div data-testid="segments">
      {options.map((o) => (
        <span key={o.id} data-segment={o.id} role="button" tabIndex={0} onClick={() => onChange({ detail: { selectedId: o.id } })}>
          {o.text}
        </span>
      ))}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/help-panel', () => mockPlain());
jest.mock('@cloudscape-design/components/link', () => ({
  __esModule: true,
  default: ({ children, onFollow }) => (
    <button type="button" data-testid="info-link" onClick={() => onFollow({ preventDefault() {} })}>
      {children}
    </button>
  ),
}));
jest.mock('@cloudscape-design/components/select', () => ({
  __esModule: true,
  default: ({ options, onChange, finishedText }) => (
    <ul data-testid="select">
      {(options || []).map((o) => (
        // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions
        <li key={o.value} data-option={o.value} onClick={() => onChange && onChange({ detail: { selectedOption: o } })}>
          {o.label}
        </li>
      ))}
      {finishedText && <li data-testid="select-finished">{finishedText}</li>}
    </ul>
  ),
}));
jest.mock('@cloudscape-design/components/side-navigation', () => mockPlain());
jest.mock('@cloudscape-design/components/space-between', () => mockPlain());
jest.mock('@cloudscape-design/components/spinner', () => mockPlain());
jest.mock('@cloudscape-design/components/table', () => mockPlain());
jest.mock('@cloudscape-design/components/toggle', () => ({
  __esModule: true,
  default: ({ checked, onChange, children }) => (
    <button type="button" data-testid="toggle" data-checked={String(checked)} onClick={() => onChange({ detail: { checked: !checked } })}>
      {children}
    </button>
  ),
}));
jest.mock('@cloudscape-design/components/slider', () => mockPlain());

/* eslint-disable import/first */
import ExploreGraphButton from '../../components/ExploreGraphButton';
import useGraphExplorerFlag from '../../hooks/useGraphExplorerFlag';
import GraphExplorer from '../GraphExplorer';
/* eslint-enable import/first */

global.IS_REACT_ACT_ENVIRONMENT = true;

const FLAG_OFF = { 'explorer-flag': { success: false, status: 404 } };
const FLAG_ON = { 'explorer-flag': { success: true, status: 200, enabled: true } };

let container;
let root;

beforeEach(() => {
  mockNavigatedTo = null;
  mockExecute.mockReset();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function render(element) {
  await act(async () => {
    root.render(element);
  });
  await act(async () => {});
}

function EntryAsTheResultsPageMountsIt() {
  const flag = useGraphExplorerFlag();
  return flag.enabled ? <ExploreGraphButton jobId="job-1" enabled={flag.enabled} /> : null;
}

test('the entry is hidden when the server flag is off', async () => {
  mockExecute.mockResolvedValue(FLAG_OFF);
  await render(<EntryAsTheResultsPageMountsIt />);
  expect(container.innerHTML).toBe('');
  expect(mockExecute.mock.calls[0][0][0].path).toBe('experimental/graph-explorer');
});

test('the entry is hidden when the probe fails', async () => {
  mockExecute.mockRejectedValue(new Error('network'));
  await render(<EntryAsTheResultsPageMountsIt />);
  expect(container.innerHTML).toBe('');
});

test('the entry button renders nothing unless enabled', async () => {
  await render(<ExploreGraphButton jobId="job-1" enabled={false} />);
  expect(container.innerHTML).toBe('');
});

test('the entry links to the job explorer when the flag is on', async () => {
  mockExecute.mockResolvedValue(FLAG_ON);
  await render(<EntryAsTheResultsPageMountsIt />);
  expect(container.textContent).toBe('graph-explorer.entry');
  expect(container.querySelector('[data-href]').getAttribute('data-href')).toBe('/analysis/explore/job-1');
});

test('the results page mounts the entry only under the flag', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'AnalysisResults.js'), 'utf8');
  const uses = source.match(/<ExploreGraphButton\b[^>]*>/g) || [];
  expect(uses).toHaveLength(1);
  expect(source).toMatch(/\{graphExplorer\.enabled && <ExploreGraphButton\b/);
});

test('the explorer route sends the user home when the flag is off', async () => {
  mockExecute.mockResolvedValue(FLAG_OFF);
  await render(<GraphExplorer />);
  expect(mockNavigatedTo).toBe('/');
  expect(container.textContent).not.toMatch(/graph-explorer/);
});

const OPTIONS = {
  success: true, limit: 500, tables_truncated: false,
  tables: [{ id: 'wordpress.wp_posts', label: 'wordpress.wp_posts', query_count: 36 }],
  engines: [{ id: 'dynamodb', label: 'DynamoDB', query_count: 40 }],
};
const OVERLAYS = {
  success: true, views: ['traffic', 'who-writes'], traffic: { min: 0.1, max: 10, samples: [0.1, 1, 10] },
  bundle_traffic: null, cost: null,
  waves: [{ wave: 3, engines: ['dynamodb'], engine_labels: ['DynamoDB'], migrating: true }],
};
const LINKS = {
  success: true, anchor_kind: 'wave', anchor_id: '1', limit: 25, total_bundles: 1, truncated: false,
  nodes: [
    { key: 'table:wordpress.wp_posts', kind: 'table', id: 'wordpress.wp_posts', label: 'wordpress.wp_posts' },
    { key: 'engine:dynamodb', kind: 'engine', id: 'dynamodb', label: 'DynamoDB' },
  ],
  bundles: [{
    key: 'bundle:wordpress.wp_posts|dynamodb', table: 'wordpress.wp_posts', engine: 'dynamodb', query_count: 1,
    calls_per_second: 0.2, wave: 3, queries_truncated: false,
    queries: [
      { query_id: 'q1', short_id: 'q1', label: 'SELECT q1', engine: 'dynamodb', engine_label: 'DynamoDB', tables: ['wordpress.wp_posts'], calls_per_second: 0.1, path: [] },
      { query_id: 'q2', short_id: 'q2', label: 'SELECT q2', engine: 'dynamodb', engine_label: 'DynamoDB', tables: ['wordpress.wp_posts'], calls_per_second: 0.1, path: [] },
    ],
  }],
};
const WALKTHROUGHS = {
  success: true,
  walkthroughs: [
    {
      id: 'waves',
      steps: [
        { kind: 'wave', start_kind: 'wave', start_id: '1', view: 'traffic', wave_filter: 1, wave: 1, title: 'Move to Aurora MySQL', rationale: 'The whole <b>database</b> moves 1:1.', gate: 'Parity.', engine_labels: ['Aurora MySQL'], tables: [], busiest_tables: [] },
        { kind: 'group', start_kind: 'group', start_id: '3:0', view: 'traffic', wave_filter: 3, fan_out: true, wave: 3, group_index: 1, group_total: 1, group_kind: 'independent', table_count: 18, query_count: 40, engine_labels: ['DynamoDB'], tables: ['wordpress.wp_posts'], busiest_tables: [] },
      ],
    },
    {
      id: 'busiest',
      steps: [{
        kind: 'query', start_kind: 'query', start_id: 'q1', view: 'traffic', fan_out: true, select_query: 'q1', tables: [], engine_labels: [], busiest_tables: [],
        query: { query_id: 'q1', short_id: 'q1', label: 'SELECT q1', engine: 'dynamodb', engine_label: 'DynamoDB', tables: ['wordpress.wp_posts'], calls_per_second: 0.1, path: [] },
      }],
    },
  ],
};
const SEARCH = {
  success: true, text: 'insert', truncated: false,
  nodes: [
    { key: 'table:wordpress.wp_posts', kind: 'table', id: 'wordpress.wp_posts', label: 'wordpress.wp_posts' },
    { key: 'engine:dynamodb', kind: 'engine', id: 'dynamodb', label: 'DynamoDB' },
  ],
  results: [{
    query_id: '-5048447123456789012', short_id: '-50484471234…', label: 'INSERT -50484471234…',
    operation: 'INSERT', sql_preview: 'INSERT INTO <b>wp_posts</b>', calls_per_second: 0.2,
    engine: 'dynamodb', engine_label: 'DynamoDB', tables: ['wordpress.wp_posts'], wave: 3, cached_in_wave: 2,
    path: [
      { wave: 1, engine: 'aurora_mysql', engine_label: 'Aurora MySQL', kind: 'cutover' },
      { wave: 3, engine: 'dynamodb', engine_label: 'DynamoDB', kind: 'move' },
    ],
  }],
};

function answer(calls) {
  const [call] = calls;
  if (call.id === 'explorer-flag') return FLAG_ON;
  if (call.id === 'options') return { options: OPTIONS, overlays: OVERLAYS, walkthroughs: WALKTHROUGHS };
  if (call.id === 'links') return { links: LINKS };
  if (call.id === 'search') return { search: SEARCH };
  if (call.id === 'query') return { query: { success: true, ...SEARCH.results[0], access_pattern_class: 'append', accesses: [] } };
  return {};
}

test('the explorer page renders with its notice when the flag is on', async () => {
  mockExecute.mockImplementation(async (calls) => answer(calls));
  await render(<GraphExplorer />);
  expect(mockNavigatedTo).toBe(null);
  expect(container.textContent).toMatch('graph-explorer.title');
  expect(container.textContent).toMatch('graph-explorer.notice.header');
  expect(container.querySelector('[data-testid="canvas"]')).not.toBe(null);
});

test('start from: wave, source table or destination engine; no query picker', async () => {
  mockExecute.mockImplementation(async (calls) => answer(calls));
  await render(<GraphExplorer />);
  const segments = [...container.querySelectorAll('[data-testid=segments]')[0].querySelectorAll('[data-segment]')];
  expect(segments.map((s) => s.getAttribute('data-segment'))).toEqual(['wave', 'table', 'engine']);
  // The picker lists the API's tables (the round-2 query picker read a
  // "querys" key that does not exist, so it stayed empty).
  expect(container.querySelector('[data-testid=select]').textContent).toBe('wordpress.wp_posts');
});

test('without waves in the data, Wave is not a start point', async () => {
  mockExecute.mockImplementation(async (calls) => {
    const out = answer(calls);
    if (calls[0].id === 'options') return { options: OPTIONS, overlays: { ...OVERLAYS, waves: [] } };
    return out;
  });
  await render(<GraphExplorer />);
  const segments = [...container.querySelectorAll('[data-testid=segments]')[0].querySelectorAll('[data-segment]')];
  expect(segments.map((s) => s.getAttribute('data-segment'))).toEqual(['table', 'engine']);
});

test('the search box finds a query on the server, shows it as text, and opens it', async () => {
  mockExecute.mockImplementation(async (calls) => answer(calls));
  await render(<GraphExplorer />);
  const input = container.querySelector('input[aria-label="graph-explorer.search.label"]');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, 'insert');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 300));
  });
  const searchCall = mockExecute.mock.calls.find(([calls]) => calls[0].id === 'search');
  expect(searchCall[0][0].path).toBe('assessments/job-1/graph/explore/search?text=insert');
  const results = container.querySelector('[data-testid=graph-explorer-search-results]');
  expect(results.textContent).toContain('INSERT -50484471234…');
  // Customer SQL is text, never markup.
  expect(results.textContent).toContain('INSERT INTO <b>wp_posts</b>');
  expect(results.querySelector('b')).toBe(null);
  await act(async () => {
    results.querySelector('button').click();
  });
  await act(async () => {});
  const queryCall = mockExecute.mock.calls.find(([calls]) => calls[0].id === 'query');
  expect(queryCall[0][0].path).toBe('assessments/job-1/graph/explore/query?id=-5048447123456789012');
  expect(container.textContent).toContain('append');
});

test('one character does not search', async () => {
  mockExecute.mockImplementation(async (calls) => answer(calls));
  await render(<GraphExplorer />);
  const input = container.querySelector('input[aria-label="graph-explorer.search.label"]');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, 'i');
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 300));
  });
  expect(mockExecute.mock.calls.some(([calls]) => calls[0].id === 'search')).toBe(false);
});

async function typeSearch(text) {
  const input = container.querySelector('input[aria-label="graph-explorer.search.label"]');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(input, text);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 300));
  });
}

test('the explorer, and only the explorer, uses the full width', async () => {
  mockExecute.mockImplementation(async (calls) => answer(calls));
  await render(<GraphExplorer />);
  expect(mockLayoutProps.maxContentWidth).toBe(Number.MAX_VALUE);
  ['AnalysisResults.js', 'AssignmentGate.js', 'Dashboard.js', 'JobMonitoringSummary.js', 'PatternAnalysis.js', 'CreateAnalysis.js'].forEach((page) => {
    // `page` comes from the fixed list above, in this test file itself -- not user
    // input, so this is not a path-traversal vector despite the generic pattern.
    // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
    const source = fs.readFileSync(path.join(__dirname, '..', page), 'utf8');
    expect([page, /maxContentWidth/.test(source)]).toEqual([page, false]);
  });
  // The workspace: the side panel beside the graph, filling the viewport.
  const workspace = container.querySelector('[data-testid=graph-explorer-workspace]');
  expect(workspace.querySelector('[data-testid=graph-explorer-side-panel]')).not.toBe(null);
  expect(workspace.querySelector('[data-testid=canvas]')).not.toBe(null);
  expect(mockLayoutProps.disableContentPaddings).toBe(true);
  // The page itself does not scroll while exploring.
  expect(document.documentElement.style.overflow).toBe('hidden');
});

describe('the query list and the graph stay in step', () => {
  const QID = SEARCH.results[0].query_id;

  async function withPinnedQuery() {
    Element.prototype.scrollIntoView = jest.fn();
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    await typeSearch('insert');
    await act(async () => {
      container.querySelector('[data-testid=graph-explorer-search-results] button').click();
    });
    await act(async () => {});
    return container.querySelector(`[data-qid="${QID}"]`);
  }

  test('a picked query gets a card, as text, with its tags', async () => {
    const card = await withPinnedQuery();
    expect(card).not.toBe(null);
    expect(card.textContent).toContain('-50484471234…');
    expect(card.textContent).toContain('INSERT INTO <b>wp_posts</b>');
    expect(card.querySelector('b')).toBe(null);
    expect(card.textContent).toContain('DynamoDB');
    expect(card.textContent).toContain('graph-explorer.tags.on-from');
    expect(card.textContent).toContain('graph-explorer.tags.moves-to');
    expect(card.textContent).toContain('graph-explorer.tags.cached');
  });

  test('hovering a card lights its edges in the graph', async () => {
    const card = await withPinnedQuery();
    await act(async () => {
      card.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
    });
    expect(mockCanvasProps.hoverKey).toBe(`qid:${QID}`);
    await act(async () => {
      card.dispatchEvent(new MouseEvent('mouseout', { bubbles: true }));
    });
    expect(mockCanvasProps.hoverKey).toBe(null);
  });

  test('selecting an edge in the graph selects and scrolls to its card', async () => {
    const card = await withPinnedQuery();
    await act(async () => {
      mockCanvasProps.onEdgeTap({ kind: 'query', qid: QID });
    });
    await act(async () => {});
    expect(container.querySelector(`[data-qid="${QID}"]`).getAttribute('aria-pressed')).toBe('true');
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
    expect(mockCanvasProps.selectedKey).toBe(`qid:${QID}`);
    expect(card).not.toBe(null);
  });

  test('clicking a card selects the query, centres the graph on it and opens its details', async () => {
    const card = await withPinnedQuery();
    const before = mockCanvasProps.focus.signal;
    mockExecute.mockClear();
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await act(async () => {
      card.click();
    });
    await act(async () => {});
    expect(mockCanvasProps.focus.signal).toBe(before + 1);
    expect(mockCanvasProps.focus.keys).toEqual([`qid:${QID}`]);
    expect(mockExecute.mock.calls.some(([calls]) => calls[0].id === 'query')).toBe(true);
    // The details open in the panel's Details tab, not at the bottom of the page.
    const details = container.querySelector('[data-testid=graph-explorer-panel-details]');
    expect(details.getAttribute('aria-hidden')).toBe('false');
    expect(details.textContent).toContain('append');
  });
});

describe('walkthroughs', () => {
  const linkCalls = () => mockExecute.mock.calls.filter(([calls]) => calls[0].id === 'links').map(([calls]) => calls[0].path);
  const counter = () => container.querySelector('[data-testid=graph-explorer-walkthrough-counter]');
  const button = (label) => [...container.querySelectorAll('button')].find((b) => b.textContent === label || b.getAttribute('aria-label') === label);

  test('a walkthrough steps through its steps with Previous and Next', async () => {
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    await act(async () => {
      button('graph-explorer.walkthroughs.waves.title').click();
    });
    await act(async () => {});
    expect(linkCalls()).toEqual(['assessments/job-1/graph/explore/links?kind=wave&id=1&limit=25']);
    expect(counter().textContent).toBe('graph-explorer.walkthroughs.bar');
    // The plan's own rationale, as text.
    expect(container.textContent).toContain('The whole <b>database</b> moves 1:1.');
    expect(container.querySelector('[data-testid=graph-explorer-walkthroughs] b')).toBe(null);
    await act(async () => {
      button('graph-explorer.walkthroughs.next').click();
    });
    await act(async () => {});
    expect(linkCalls()[1]).toBe('assessments/job-1/graph/explore/links?kind=group&id=3%3A0&limit=25');
    expect(container.textContent).toContain('graph-explorer.walkthroughs.group-independent');
    await act(async () => {
      button('graph-explorer.walkthroughs.previous').click();
    });
    await act(async () => {});
    expect(linkCalls()[2]).toBe('assessments/job-1/graph/explore/links?kind=wave&id=1&limit=25');
    await act(async () => {
      button('graph-explorer.walkthroughs.close').click();
    });
    expect(button('graph-explorer.walkthroughs.waves.title')).not.toBe(undefined);
    // The step bar shows only while a walkthrough runs.
    expect(container.querySelector('[data-testid=graph-explorer-step-bar]')).toBe(null);
  });

  test('the step bar floats over the graph while a walkthrough runs', async () => {
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    expect(container.querySelector('[data-testid=graph-explorer-step-bar]')).toBe(null);
    await act(async () => {
      button('graph-explorer.walkthroughs.waves.title').click();
    });
    await act(async () => {});
    const bar = container.querySelector('[data-testid=graph-explorer-canvas-area] [data-testid=graph-explorer-step-bar]');
    expect(bar).not.toBe(null);
    expect(bar.querySelector('[aria-label="graph-explorer.walkthroughs.previous"]').disabled).toBe(true);
    // The step sets the wave filter: the Filters button says one filter is on.
    expect(container.querySelector('[data-testid=graph-explorer-filters-active]').textContent).toContain('graph-explorer.filters.active');
  });

  test('only the walkthroughs the API returns are offered', async () => {
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    const offered = [...container.querySelectorAll('[data-testid=graph-explorer-walkthroughs] button')].map((b) => b.textContent);
    expect(offered).toEqual(['graph-explorer.walkthroughs.waves.title', 'graph-explorer.walkthroughs.busiest.title']);
    // No filter is on yet.
    expect(container.querySelector('[data-testid=graph-explorer-filters-active]')).toBe(null);
  });

  test('with no walkthrough data, the panel is not shown', async () => {
    mockExecute.mockImplementation(async (calls) => {
      if (calls[0].id === 'options') return { options: OPTIONS, overlays: OVERLAYS, walkthroughs: { success: true, walkthroughs: [] } };
      return answer(calls);
    });
    await render(<GraphExplorer />);
    expect(container.textContent).toContain('graph-explorer.walkthroughs.none');
    expect(container.querySelector('[data-testid=graph-explorer-walkthroughs]')).toBe(null);
  });
});

test('the (i) link opens the help drawer', async () => {
  mockExecute.mockImplementation(async (calls) => answer(calls));
  await render(<GraphExplorer />);
  expect(mockLayoutProps.toolsOpen).toBe(false);
  await act(async () => {
    container.querySelector('[data-testid=info-link]').click();
  });
  expect(mockLayoutProps.toolsOpen).toBe(true);
  const host = document.createElement('div');
  await act(async () => {
    createRoot(host).render(mockLayoutProps.tools);
  });
  expect(host.textContent).toContain('graph-explorer.help.graph');
  expect(host.textContent).toContain('graph-explorer.help.walkthroughs');
  expect(host.textContent).toContain('graph-explorer.help.limits');
});

test('starting from a wave leaves one wave control', async () => {
  mockExecute.mockImplementation(async (calls) => answer(calls));
  await render(<GraphExplorer />);
  const selects = () => container.querySelectorAll('[data-testid=select]').length;
  const before = selects();
  await act(async () => {
    container.querySelector('[data-segment=wave]').click();
  });
  // The start picker lists the waves; the wave filter is hidden.
  expect(selects()).toBe(before - 1);
  expect(container.querySelector('[data-testid=select]').textContent).toContain('graph-explorer.filters.wave-option');
});

describe('the workspace panel', () => {
  const panel = () => container.querySelector('[data-testid=graph-explorer-side-panel]');
  const saved = () => JSON.parse(window.localStorage.getItem('dbm-graph-explorer-layout') || '{}');
  beforeEach(() => window.localStorage.clear());

  test('the panel collapses to a rail and back, by button and by "["', async () => {
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    const collapse = panel().querySelector('[aria-label="graph-explorer.panel.collapse"]');
    expect(collapse.getAttribute('aria-expanded')).toBe('true');
    await act(async () => {
      collapse.click();
    });
    expect(panel().querySelector('[role=tablist]')).toBe(null);
    expect(panel().querySelector('[aria-label="graph-explorer.panel.expand"]').getAttribute('aria-expanded')).toBe('false');
    expect(saved().collapsed).toBe(true);
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent('keydown', { key: '[' }));
    });
    expect(panel().querySelector('[role=tablist]')).not.toBe(null);
    expect(saved().collapsed).toBe(false);
  });

  test('"[" typed into a field is not a shortcut', async () => {
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    const input = container.querySelector('input[aria-label="graph-explorer.search.label"]');
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: '[', bubbles: true }));
    });
    expect(panel().querySelector('[role=tablist]')).not.toBe(null);
  });

  test('the panel edge resizes it, and the width is remembered', async () => {
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    const edge = container.querySelector('[data-testid=graph-explorer-panel-resize]');
    const before = Number(edge.getAttribute('aria-valuenow'));
    await act(async () => {
      edge.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    });
    expect(Number(container.querySelector('[data-testid=graph-explorer-panel-resize]').getAttribute('aria-valuenow'))).toBe(before + 24);
    expect(saved().panelWidth).toBe(before + 24);
  });

  test('a saved layout is restored, and Reset layout restores the default', async () => {
    window.localStorage.setItem('dbm-graph-explorer-layout', JSON.stringify({ collapsed: true, tab: 'details', noticeDismissed: true, panelWidth: 500 }));
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    expect(panel().querySelector('[role=tablist]')).toBe(null);
    // The dismissed notice stays a small chip.
    expect(container.querySelector('[data-testid=graph-explorer-notice]')).toBe(null);
    expect(container.querySelector('[data-testid=graph-explorer-notice-chip]')).not.toBe(null);
    const reset = [...container.querySelectorAll('button')].find((b) => b.textContent === 'graph-explorer.layout.reset');
    await act(async () => {
      reset.click();
    });
    expect(panel().querySelector('[role=tablist]')).not.toBe(null);
    expect(container.querySelector('[data-testid=graph-explorer-notice]')).not.toBe(null);
    expect(window.localStorage.getItem('dbm-graph-explorer-layout')).toBe(null);
  });

  test('selecting an edge switches to Details, keeping the list mounted', async () => {
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    const tabpanel = (id) => container.querySelector(`[data-testid=graph-explorer-panel-${id}]`);
    expect(tabpanel('queries').getAttribute('aria-hidden')).toBe('false');
    await act(async () => {
      mockCanvasProps.onEdgeTap({ kind: 'query', qid: 'q1' });
    });
    await act(async () => {});
    expect(tabpanel('details').getAttribute('aria-hidden')).toBe('false');
    expect(tabpanel('queries').getAttribute('aria-hidden')).toBe('true');
    // Hidden, not unmounted: the list keeps its place.
    expect(tabpanel('queries').querySelector('[data-testid=graph-explorer-query-list]')).not.toBe(null);
    expect(saved().tab).toBe('details');
  });
});

test('the query list keeps its scroll position across tab switches', async () => {
  window.localStorage.clear();
  mockExecute.mockImplementation(async (calls) => answer(calls));
  await render(<GraphExplorer />);
  const list = () => container.querySelector('[data-testid=graph-explorer-query-list]');
  await act(async () => {
    list().scrollTop = 120;
    list().dispatchEvent(new Event('scroll'));
  });
  const tab = (label) => [...container.querySelectorAll('[role=tab]')].find((b) => b.textContent === label);
  await act(async () => {
    tab('graph-explorer.panel.tab.details').click();
  });
  list().scrollTop = 0;
  await act(async () => {
    tab('graph-explorer.panel.tab.queries').click();
  });
  expect(list().scrollTop).toBe(120);
});

test('the walkthrough step list keeps each number beside its title', async () => {
  window.localStorage.clear();
  mockExecute.mockImplementation(async (calls) => answer(calls));
  await render(<GraphExplorer />);
  const start = [...container.querySelectorAll('button')].find((b) => b.textContent === 'graph-explorer.walkthroughs.waves.title');
  await act(async () => {
    start.click();
  });
  await act(async () => {});
  const items = [...container.querySelectorAll('[data-testid=graph-explorer-step-list] li')];
  expect(items).toHaveLength(2);
  items.forEach((li) => {
    expect(li.style.display).toBe('flex');
    expect(li.style.alignItems).toBe('flex-start');
    expect(li.firstChild.textContent).toBe('graph-explorer.walkthroughs.step-number');
  });
});

test('query cards show the engine with its service icon and name', async () => {
  window.localStorage.clear();
  mockExecute.mockImplementation(async (calls) => answer(calls));
  await render(<GraphExplorer />);
  await typeSearch('insert');
  await act(async () => {
    container.querySelector('[data-testid=graph-explorer-search-results] button').click();
  });
  await act(async () => {});
  const tag = container.querySelector('[data-qid] [data-testid=graph-explorer-engine-tag]');
  expect(tag.textContent).toBe('DynamoDB');
  expect(tag.querySelector('img').getAttribute('src')).toBe('amazon-dynamodb.svg');
});

describe('starting points begin collapsed (round 8 regression)', () => {
  const queryEdges = () => mockCanvasProps.elements.filter((e) => e.data.kind === 'query').map((e) => e.data.qid);
  const bundles = () => mockCanvasProps.elements.filter((e) => e.data.kind === 'bundle').map((e) => e.data.id);
  const button = (label) => [...container.querySelectorAll('button')].find((b) => b.textContent === label || b.getAttribute('aria-label') === label);
  beforeEach(() => window.localStorage.clear());

  test('a wave-plan group step starts collapsed, even if the API asks to fan out', async () => {
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    await act(async () => {
      button('graph-explorer.walkthroughs.waves.title').click();
    });
    await act(async () => {});
    expect(queryEdges()).toEqual([]);
    await act(async () => {
      button('graph-explorer.walkthroughs.next').click();
    });
    await act(async () => {});
    // Step 2 is a group step whose API answer says fan_out: true.
    expect(bundles()).toEqual(['bundle:wordpress.wp_posts|dynamodb']);
    expect(queryEdges()).toEqual([]);
  });

  test('a busiest-queries step draws its one query, not the whole bundle', async () => {
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    await act(async () => {
      button('graph-explorer.walkthroughs.busiest.title').click();
    });
    await act(async () => {});
    expect(queryEdges()).toEqual(['q1']);
    expect(bundles()).toEqual(['bundle:wordpress.wp_posts|dynamodb']);
  });

  test('Explore starts afresh: a fanned bundle and "Fan out all" fold back', async () => {
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    const segment = container.querySelector('[data-segment=engine]');
    await act(async () => {
      segment.click();
    });
    await act(async () => {
      container.querySelector('[data-testid=select] [data-option=dynamodb]').click();
    });
    await act(async () => {
      button('graph-explorer.actions.explore').click();
    });
    await act(async () => {});
    expect(queryEdges()).toEqual([]);
    // An explicit click fans out that bundle only.
    await act(async () => {
      mockCanvasProps.onEdgeTap({ kind: 'bundle', id: 'bundle:wordpress.wp_posts|dynamodb' });
    });
    expect(queryEdges().sort()).toEqual(['q1', 'q2']);
    await act(async () => {
      button('graph-explorer.actions.explore').click();
    });
    await act(async () => {});
    expect(queryEdges()).toEqual([]);
    // "Fan out all" is never part of the saved layout.
    expect(window.localStorage.getItem('dbm-graph-explorer-layout') || '').not.toMatch(/fan/);
  });
});

describe('Details: Collapse', () => {
  const queryEdges = () => mockCanvasProps.elements.filter((e) => e.data.kind === 'query').map((e) => e.data.qid);
  const collapse = () => container.querySelector('[data-testid=graph-explorer-details] [aria-label^="graph-explorer.details.collapse-label"]');
  beforeEach(() => window.localStorage.clear());

  async function exploreDynamo() {
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    await act(async () => {
      container.querySelector('[data-segment=engine]').click();
    });
    await act(async () => {
      container.querySelector('[data-testid=select] [data-option=dynamodb]').click();
    });
    await act(async () => {
      [...container.querySelectorAll('button')].find((b) => b.textContent === 'graph-explorer.actions.explore').click();
    });
    await act(async () => {});
  }

  test('a fanned-out bundle can be folded back from Details, keeping it selected', async () => {
    await exploreDynamo();
    expect(collapse()).toBe(null);
    await act(async () => {
      mockCanvasProps.onEdgeTap({ kind: 'bundle', id: 'bundle:wordpress.wp_posts|dynamodb' });
    });
    expect(queryEdges().sort()).toEqual(['q1', 'q2']);
    await act(async () => {
      collapse().click();
    });
    expect(queryEdges()).toEqual([]);
    expect(mockCanvasProps.selectedKey).toBe('bundle:wordpress.wp_posts|dynamodb');
    // Folded: no Collapse button any more.
    expect(collapse()).toBe(null);
  });

  test('a query edge of a fanned-out bundle offers Collapse too', async () => {
    await exploreDynamo();
    await act(async () => {
      mockCanvasProps.onEdgeTap({ kind: 'bundle', id: 'bundle:wordpress.wp_posts|dynamodb' });
    });
    await act(async () => {
      mockCanvasProps.onEdgeTap({ kind: 'query', qid: 'q2' });
    });
    await act(async () => {});
    await act(async () => {
      collapse().click();
    });
    expect(queryEdges()).toEqual([]);
    expect(mockCanvasProps.selectedKey).toBe('bundle:wordpress.wp_posts|dynamodb');
  });
});

test('"Show other waves" is in the Filters popover, on by default', async () => {
  window.localStorage.clear();
  mockExecute.mockImplementation(async (calls) => answer(calls));
  await render(<GraphExplorer />);
  const toggle = container.querySelector('[data-testid=popover-content] [data-testid=toggle]');
  expect(toggle).not.toBe(null);
  expect(toggle.getAttribute('data-checked')).toBe('true');
});

describe('pins', () => {
  beforeEach(() => window.localStorage.clear());
  const button = (label) => [...container.querySelectorAll('button')].find((b) => b.textContent === label);

  async function withDynamo() {
    mockExecute.mockImplementation(async (calls) => answer(calls));
    await render(<GraphExplorer />);
    await act(async () => {
      container.querySelector('[data-segment=engine]').click();
    });
    await act(async () => {
      container.querySelector('[data-testid=select] [data-option=dynamodb]').click();
    });
    await act(async () => {
      button('graph-explorer.actions.explore').click();
    });
    await act(async () => {});
  }

  test('a dropped node is pinned; Unpin in Details releases it', async () => {
    await withDynamo();
    expect(mockCanvasProps.pinnedKeys).toEqual([]);
    await act(async () => {
      mockCanvasProps.onPinChange('engine:dynamodb', true);
    });
    expect(mockCanvasProps.pinnedKeys).toEqual(['engine:dynamodb']);
    await act(async () => {
      mockCanvasProps.onNodeTap('engine:dynamodb');
    });
    await act(async () => {});
    const details = container.querySelector('[data-testid=graph-explorer-details]');
    expect(details.textContent).toContain('graph-explorer.details.pinned');
    await act(async () => {
      [...details.querySelectorAll('button')].find((b) => b.textContent === 'graph-explorer.details.unpin').click();
    });
    expect(mockCanvasProps.pinnedKeys).toEqual([]);
    // Pins are session state, never part of the saved layout.
    expect(window.localStorage.getItem('dbm-graph-explorer-layout') || '').not.toMatch(/engine:dynamodb/);
  });

  test('Reset layout unpins everything', async () => {
    await withDynamo();
    await act(async () => {
      mockCanvasProps.onPinChange('engine:dynamodb', true);
      mockCanvasProps.onPinChange('table:wordpress.wp_posts', true);
    });
    expect(mockCanvasProps.pinnedKeys.sort()).toEqual(['engine:dynamodb', 'table:wordpress.wp_posts']);
    await act(async () => {
      button('graph-explorer.layout.reset').click();
    });
    expect(mockCanvasProps.pinnedKeys).toEqual([]);
  });
});


describe('the start pickers ask for every option the API allows', () => {
  async function renderTableStart(options) {
    mockExecute.mockImplementation(async (calls) => {
      const out = answer(calls);
      return calls[0].id === 'options' ? { ...out, options } : out;
    });
    await render(<GraphExplorer />);
    await act(async () => {
      container.querySelector('[data-segment=table]').click();
    });
  }

  test('the options request asks for the documented maximum of 1000', async () => {
    await renderTableStart(OPTIONS);
    const optionsCall = mockExecute.mock.calls.find(([calls]) => calls[0].id === 'options');
    expect(optionsCall[0][0].path).toBe('assessments/job-1/graph/explore/options?limit=1000');
  });

  test('a cut-short table list says only the first N are listed', async () => {
    await renderTableStart({ ...OPTIONS, limit: 1000, tables_truncated: true });
    expect(container.querySelector('[data-testid=select-finished]').textContent).toBe('graph-explorer.picker.truncated');
  });

  test('a complete table list, or the engine list, carries no note', async () => {
    await renderTableStart(OPTIONS);
    expect(container.querySelector('[data-testid=select-finished]')).toBe(null);
    await act(async () => {
      container.querySelector('[data-segment=engine]').click();
    });
    expect(container.querySelector('[data-testid=select-finished]')).toBe(null);
  });
});
