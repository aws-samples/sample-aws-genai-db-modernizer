// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Explore (experimental): the context graph of one job (#308). Shown only
 * when the server's MODERNIZER_EXPERIMENTAL_GRAPH_UI flag is on; otherwise
 * this route behaves like an unknown one (back to the home page).
 *
 * Source tables and destination engines are nodes; each query is an edge
 * from every table it touches to its engine, bundled per table and engine
 * pair until the user fans the bundle out. Built on the bounded, read-only
 * explorer endpoints; every figure on the page comes from the API as it is.
 * Customer names and SQL are rendered as text.
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { Navigate, useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

import AppLayoutToolbar from '@cloudscape-design/components/app-layout';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import BreadcrumbGroup from '@cloudscape-design/components/breadcrumb-group';
import Button from '@cloudscape-design/components/button';
import HelpPanel from '@cloudscape-design/components/help-panel';
import Link from '@cloudscape-design/components/link';
import Popover from '@cloudscape-design/components/popover';
import SideNavigation from '@cloudscape-design/components/side-navigation';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';

import AppHeader from '../components/AppHeader';
import GraphExplorerCanvas from '../components/GraphExplorerCanvas';
import GraphExplorerLegendCard from '../components/GraphExplorerLegendCard';
import GraphExplorerQueryList from '../components/GraphExplorerQueryList';
import GraphExplorerQueryPanel from '../components/GraphExplorerQueryPanel';
import GraphExplorerSidePanel from '../components/GraphExplorerSidePanel';
import GraphExplorerToolbar from '../components/GraphExplorerToolbar';
import GraphExplorerViewBar from '../components/GraphExplorerViewBar';
import { StepBar, WalkthroughPanel } from '../components/GraphExplorerWalkthroughs';
import ApiManager from '../classes/ApiManager';
import useGraphExplorerFlag from '../hooks/useGraphExplorerFlag';
import { SideNavigationConfigurations } from '../config/GlobalConfigurations';
import { displayEngine } from '../utils/engineNames';
import { isTyping, loadLayout, resetLayout, saveLayout } from '../utils/explorerLayout';
import {
  LINK_LIMIT,
  MAX_VIEW_ELEMENTS,
  SEARCH_MIN_CHARS,
  START_KINDS,
  anyFanned,
  availableViews,
  belowTrafficPath,
  collapseAll,
  elementCount,
  emptyView,
  fanOutAll,
  filterListed,
  isQueryDrawn,
  linksPath,
  listedQueries,
  startFrom,
  mergeLinks,
  optionsPath,
  overlaysPath,
  pinQueries,
  queryPath,
  removeNode,
  searchPath,
  sliderThreshold,
  toElements,
  toggleBundle,
  trafficSubjects,
  truncatedPickerLimit,
  walkthroughsPath,
} from '../utils/graphExplorer';

function errorText(result) {
  return (result && result.error && result.error.message) || '';
}

async function get(id, path) {
  const results = await new ApiManager().execute([{ id, path, method: 'GET', params: {} }]);
  return results[id];
}

/** The start picker's choices for a kind (one list per kind, by name). */
function startChoices(kind, options, overlays, t) {
  if (kind === 'wave') {
    return ((overlays && overlays.waves) || []).map((w) => ({
      value: String(w.wave),
      label: t('graph-explorer.filters.wave-option', { wave: w.wave, engines: w.engine_labels.join(', ') }),
    }));
  }
  const list = options ? (kind === 'table' ? options.tables : options.engines) : [];
  return (list || []).map((o) => ({
    value: o.id,
    label: o.label,
    description: t('graph-explorer.picker.query-count', { count: o.query_count ?? 0 }),
  }));
}

export default function GraphExplorer() {
  const { jobId } = useParams();
  const { t } = useTranslation();
  const flag = useGraphExplorerFlag();
  const [navigationOpen, setNavigationOpen] = useState(false);

  const [options, setOptions] = useState(null);
  const [overlays, setOverlays] = useState(null);
  const [optionsError, setOptionsError] = useState('');
  const [startKind, setStartKind] = useState('table');
  const [startOption, setStartOption] = useState(null);

  const [view, setView] = useState(emptyView);
  const [selectedKey, setSelectedKey] = useState(null);
  const [lastLinks, setLastLinks] = useState(null);
  const [anchorKey, setAnchorKey] = useState(null);
  const [focus, setFocus] = useState({ signal: 0, keys: [] });
  const [loading, setLoading] = useState(false);
  const [problem, setProblem] = useState('');

  const [mode, setMode] = useState('traffic');
  const [wave, setWave] = useState(null);
  const [engineFilter, setEngineFilter] = useState(null);
  const [slider, setSlider] = useState(0);
  // Waves give context: what is not in the selected wave stays, greyed out (on by default).
  const [showOtherWaves, setShowOtherWaves] = useState(true);
  // Nodes the user dropped somewhere: pinned there for the session (not saved).
  const [pinned, setPinned] = useState([]);
  const onPinChange = useCallback((key, on) => {
    setPinned((current) => (on ? [...new Set([...current, key])] : current.filter((k) => k !== key)));
  }, []);
  const [below, setBelow] = useState(null);
  const [hovered, setHovered] = useState(null);

  const [searchText, setSearchText] = useState('');
  const [search, setSearch] = useState(null);
  const [searching, setSearching] = useState(false);
  const [detail, setDetail] = useState({ data: null, loading: false, error: false });
  // The query list: its filter box, the bundle it shows (when one is fanned
  // out from the graph), the card hovered, and how many bundles a "fan out
  // all" left bundled (view state).
  const [listFilter, setListFilter] = useState('');
  const [listBundle, setListBundle] = useState(null);
  const [hoverQid, setHoverQid] = useState(null);
  const [stayed, setStayed] = useState(0);
  // Walkthroughs (the API's steps), the one in progress, and the help drawer.
  const [walkthroughs, setWalkthroughs] = useState([]);
  const [walk, setWalk] = useState(null);
  const [walkLoading, setWalkLoading] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);

  // The workspace layout, remembered per browser: panel width, collapsed or
  // open, active tab, and whether the experimental notice was dismissed.
  const [layout, setLayoutState] = useState(() => loadLayout());
  const setLayout = useCallback((patch) => {
    setLayoutState((current) => {
      const next = { ...current, ...patch };
      saveLayout(next);
      return next;
    });
  }, []);
  const togglePanel = useCallback(() => setLayoutState((current) => {
    const next = { ...current, collapsed: !current.collapsed };
    saveLayout(next);
    return next;
  }), []);

  // "[" collapses or opens the side panel (not while typing in a field).
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === '[' && !e.ctrlKey && !e.metaKey && !e.altKey && !isTyping(e.target)) togglePanel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [togglePanel]);

  // The workspace fills the viewport under the page bar; the page itself never scrolls.
  const workspace = useRef(null);
  const [workspaceHeight, setWorkspaceHeight] = useState(null);
  useLayoutEffect(() => {
    const measure = () => {
      if (!workspace.current) return;
      const height = Math.max(420, window.innerHeight - workspace.current.getBoundingClientRect().top - 4);
      setWorkspaceHeight(height);
      // Whatever the app layout adds below the workspace comes off its height.
      window.requestAnimationFrame(() => {
        const over = document.documentElement.scrollHeight - window.innerHeight;
        if (over > 0) setWorkspaceHeight(Math.max(420, height - over));
      });
    };
    measure();
    window.addEventListener('resize', measure);
    const previous = [document.documentElement.style.overflow, document.body.style.overflow];
    document.documentElement.style.overflow = 'hidden';
    document.body.style.overflow = 'hidden';
    try {
      window.scrollTo(0, 0);
    } catch (e) {
      // Not every environment scrolls (a test document, say).
    }
    return () => {
      window.removeEventListener('resize', measure);
      [document.documentElement.style.overflow, document.body.style.overflow] = previous;
    };
  }, [flag.enabled, layout.noticeDismissed]);


  useEffect(() => {
    if (!flag.enabled || !jobId) return;
    let live = true;
    new ApiManager()
      .execute([
        { id: 'options', path: optionsPath(jobId), method: 'GET', params: {} },
        { id: 'overlays', path: overlaysPath(jobId), method: 'GET', params: {} },
        { id: 'walkthroughs', path: walkthroughsPath(jobId), method: 'GET', params: {} },
      ])
      .then((results) => {
        if (!live) return;
        if (results.walkthroughs && results.walkthroughs.success) setWalkthroughs(results.walkthroughs.walkthroughs || []);
        if (results.overlays && results.overlays.success) setOverlays(results.overlays);
        const result = results.options;
        if (result && result.success) setOptions(result);
        else setOptionsError(errorText(result) || 'default');
      });
    return () => {
      live = false;
    };
  }, [flag.enabled, jobId]);

  // The query search box: server-side, bounded, after a short pause in typing.
  useEffect(() => {
    const text = searchText.trim();
    if (!flag.enabled || text.length < SEARCH_MIN_CHARS) {
      setSearch(null);
      return undefined;
    }
    let live = true;
    setSearching(true);
    const timer = setTimeout(() => {
      get('search', searchPath(jobId, text)).then((result) => {
        if (!live) return;
        setSearching(false);
        setSearch(result && result.success ? result : { results: [], nodes: [], failed: true });
      });
    }, 250);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [searchText, jobId, flag.enabled]);

  const openQuery = useCallback(
    async (queryId) => {
      setDetail({ data: null, loading: true, error: false });
      const result = await get('query', queryPath(jobId, queryId));
      setDetail(result && result.success ? { data: result, loading: false, error: false } : { data: null, loading: false, error: true });
    },
    [jobId],
  );

  // `start`: a new starting point (the toolbar's Explore), which collapses
  // every bundle; expanding a node in the graph keeps what is fanned out.
  const explore = useCallback(
    async (kind, id, { start = false } = {}) => {
      setLoading(true);
      setProblem('');
      const result = await get('links', linksPath(jobId, kind, id, LINK_LIMIT));
      setLoading(false);
      if (!result || !result.success) {
        setProblem(errorText(result) || 'expand');
        return;
      }
      setView((current) => (start ? startFrom(current, result, MAX_VIEW_ELEMENTS) : mergeLinks(current, result, MAX_VIEW_ELEMENTS)));
      if (start) setStayed(0);
      setLastLinks(result);
      setListBundle(null);
      const key = kind === 'wave' ? null : `${kind}:${id}`;
      setAnchorKey(key);
      setSelectedKey(key);
      if (kind === 'wave') setWave(Number(id));
    },
    [jobId],
  );

  // A walkthrough step sets the graph afresh: its start point, the view, the
  // wave filter, fanned bundles and the query to select. The user explores
  // freely from there.
  const applyStep = async (walkthroughId, index) => {
    const w = walkthroughs.find((x) => x.id === walkthroughId);
    const step = w && w.steps[index];
    if (!step) return;
    setWalk({ id: walkthroughId, index });
    setWalkLoading(true);
    setProblem('');
    const result = await get('links', linksPath(jobId, step.start_kind, step.start_id, LINK_LIMIT));
    setWalkLoading(false);
    if (!result || !result.success) {
      setProblem('expand');
      return;
    }
    // Every step starts collapsed; a step about one query draws that query's
    // edges only (never its whole bundles).
    let next = startFrom(emptyView(), result, MAX_VIEW_ELEMENTS);
    if (step.kind === 'query' && step.query) {
      const out = pinQueries(next, { nodes: result.nodes }, [step.query], MAX_VIEW_ELEMENTS);
      if (out.ok) next = out.view;
    }
    setView(next);
    setStayed(0);
    setLastLinks(result);
    setListBundle(null);
    setMode(availableViews(overlays).includes(step.view) ? step.view : 'traffic');
    setWave(step.wave_filter ?? null);
    const anchor = ['table', 'engine'].includes(step.start_kind) ? `${step.start_kind}:${step.start_id}` : null;
    setAnchorKey(anchor);
    if (step.select_query) {
      setSelectedKey(`qid:${step.select_query}`);
      openQuery(step.select_query);
    } else {
      setSelectedKey(anchor);
      setDetail({ data: null, loading: false, error: false });
    }
  };

  const onNodeTap = useCallback(
    (key) => {
      const node = view.nodes[key];
      if (node) {
        explore(node.kind, node.id);
        setLayout({ tab: 'details' });
      }
    },
    [view.nodes, explore, setLayout],
  );

  const onEdgeTap = useCallback(
    (data) => {
      if (data.kind === 'bundle') {
        const out = toggleBundle(view, data.id, MAX_VIEW_ELEMENTS);
        if (out.ok) {
          setView(out.view);
          setSelectedKey(data.id);
          setLayout({ tab: 'details' });
          // The list shows the bundle's queries while it is fanned out.
          setListBundle(out.view.fanned[data.id] ? data.id : null);
        } else setProblem('room');
        return;
      }
      setSelectedKey(`qid:${data.qid}`);
      openQuery(data.qid);
      setLayout({ tab: 'details' });
    },
    [view, openQuery, setLayout],
  );

  const showQuery = (queryId) => {
    const key = `qid:${queryId}`;
    setSelectedKey(key);
    setFocus((f) => ({ signal: f.signal + 1, keys: [key] }));
    openQuery(queryId);
  };

  const pickSearchResult = (q) => {
    const out = pinQueries(view, search, [q], MAX_VIEW_ELEMENTS);
    if (!out.ok) {
      setProblem('room');
      return;
    }
    setView(out.view);
    showQuery(q.query_id);
  };

  // A card clicked in the list: draw its edges if they are not (fan its
  // bundle out), then select it, centre the graph on it and open its details.
  const onSelectCard = (q, bundleKey) => {
    // Draw only this query's edges (not its whole bundle) when they are not drawn yet.
    if (!isQueryDrawn(view, q.query_id)) {
      const out = pinQueries(view, { nodes: [] }, [q], MAX_VIEW_ELEMENTS);
      if (!out.ok) {
        setProblem('room');
        return;
      }
      setView(out.view);
    }
    showQuery(q.query_id);
    setLayout({ tab: 'details' });
  };

  const onFanAll = () => {
    if (anyFanned(view)) {
      setView(collapseAll(view));
      setListBundle(null);
      return;
    }
    const out = fanOutAll(view, MAX_VIEW_ELEMENTS);
    setView(out.view);
    setStayed(out.stayed);
  };

  const onRemove = () => {
    if (!selectedKey || !view.nodes[selectedKey]) return;
    setView((current) => removeNode(current, selectedKey));
    setSelectedKey(null);
  };

  const onReset = () => {
    setView(emptyView());
    setSelectedKey(null);
    setLastLinks(null);
    setAnchorKey(null);
    setListBundle(null);
    setStayed(0);
    setPinned([]);
    setDetail({ data: null, loading: false, error: false });
  };

  const traffic = overlays ? overlays.traffic : null;
  const threshold = sliderThreshold(slider, traffic);
  const subjects = useMemo(() => trafficSubjects(view), [view]);
  const subjectsKey = JSON.stringify(subjects);

  // The minimum-traffic filter: the API says which drawn edges are under the
  // threshold, and how many (the UI compares and counts nothing).
  useEffect(() => {
    if (!(threshold > 0) || (subjects.queryIds.length === 0 && subjects.bundles.length === 0)) {
      setBelow(null);
      return undefined;
    }
    let live = true;
    const timer = setTimeout(() => {
      get('below', belowTrafficPath(jobId, threshold, subjects.queryIds, subjects.bundles)).then((result) => {
        if (live && result && result.success) setBelow(result);
      });
    }, 250);
    return () => {
      live = false;
      clearTimeout(timer);
    };
    // subjectsKey stands for subjects.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threshold, subjectsKey, jobId]);

  const hiddenQueries = useMemo(() => new Set((below && below.hidden_query_ids) || []), [below]);
  const hiddenBundles = useMemo(
    () => new Set(((below && below.hidden_bundles) || []).map((b) => `bundle:${b.table}|${b.engine}`)),
    [below],
  );

  const elements = useMemo(
    () =>
      toElements(view, {
        mode,
        traffic,
        bundleTraffic: overlays ? overlays.bundle_traffic : null,
        cost: overlays ? overlays.cost : null,
        wave,
        hiddenQueries,
        hiddenBundles,
        engineFilter,
        placeholderLabel: t('graph-explorer.node.no-table'),
        showOtherWaves,
        waveBadge: (w) => t('graph-explorer.wave.badge', { wave: w }),
      }),
    // t is stable in the app; leaving it out keeps a test double from re-rendering forever.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [view, mode, traffic, overlays, wave, hiddenQueries, hiddenBundles, engineFilter, showOtherWaves],
  );

  if (!flag.checked) {
    return (
      <Box textAlign="center" padding={{ top: 'xxxl' }}>
        <Spinner size="large" />
      </Box>
    );
  }
  // Flag off: the same answer as a route that does not exist.
  if (!flag.enabled) return <Navigate to="/" replace />;

  const hasWaves = Boolean(overlays && overlays.waves && overlays.waves.length);
  const kinds = START_KINDS.filter((k) => k !== 'wave' || hasWaves);
  const choices = startChoices(startKind, options, overlays, t);
  const shown = elementCount(view);
  const selectedNode = selectedKey ? view.nodes[selectedKey] : null;
  const selectedBundle = selectedKey ? view.bundles[selectedKey] : null;
  const listed = filterListed(listedQueries(view, listBundle), listFilter);
  const selectedQid = selectedKey && selectedKey.startsWith('qid:') ? selectedKey.slice(4) : null;
  const placeholderTables = (overlays && overlays.placeholder_tables) || [];
  const listBundleObj = listBundle ? view.bundles[listBundle] : null;
  const activeFilters = [engineFilter != null, threshold > 0, wave != null && startKind !== 'wave'].filter(Boolean).length;

  const breadcrumbs = [
    { href: '/dashboard', text: t('dashboard.breadcrumb.dashboard') },
    { href: `/analysis/results/${jobId}`, text: t('analysis-results.breadcrumb.results') },
    { href: `/analysis/explore/${jobId}`, text: t('graph-explorer.breadcrumb') },
  ];

  // The tooltip: what the hovered element is, its figure (the API's), and what a click does.
  let tip = null;
  if (hovered) {
    const node = hovered.group === 'node' ? view.nodes[hovered.id] : null;
    let lines;
    if (hovered.group === 'edge' && hovered.kind === 'bundle') {
      lines = [
        t('graph-explorer.hover.bundle', { count: hovered.count, value: (hovered.cps ?? 0).toLocaleString() }),
        t('graph-explorer.tip.fan', { count: hovered.count }),
      ];
    } else if (hovered.group === 'edge') {
      lines = [
        hovered.label,
        hovered.cps != null ? t('graph-explorer.hover.cps', { value: hovered.cps.toLocaleString() }) : null,
        t('graph-explorer.tip.details'),
      ];
    } else if (node && node.placeholder) {
      lines = [t('graph-explorer.node.no-table'), t('graph-explorer.tip.no-table'), t('graph-explorer.tip.expand')];
    } else {
      lines = [
        hovered.label,
        hovered.cost != null ? t('graph-explorer.hover.cost', { value: hovered.cost.toLocaleString() }) : null,
        t('graph-explorer.tip.expand'),
      ];
    }
    // With a wave picked, every element says which wave it moves in (the API's wave).
    if (wave != null && hovered.moveWave != null && hovered.moveWave !== wave) {
      lines.splice(lines.length - 1, 0, t('graph-explorer.wave.moves-in', { wave: hovered.moveWave }));
    }
    if (wave != null && hovered.cachedIn != null) {
      lines.splice(lines.length - 1, 0, t('graph-explorer.wave.cached-in', { wave: hovered.cachedIn }));
    }
    tip = { x: hovered.x, y: hovered.y, lines: lines.filter(Boolean) };
  }

  const linksNote = lastLinks
    ? t(lastLinks.truncated ? 'graph-explorer.results.truncated' : 'graph-explorer.results.complete', {
        count: lastLinks.total_bundles,
        limit: lastLinks.limit,
      })
    : null;

  const filters = (
    <GraphExplorerViewBar
      t={t}
      views={availableViews(overlays)}
      mode={mode}
      onMode={setMode}
      engines={options ? options.engines : []}
      engineFilter={engineFilter}
      onEngine={setEngineFilter}
      waves={overlays ? overlays.waves : []}
      wave={wave}
      onWave={setWave}
      // Starting from a wave already picks the wave: one control, not two.
      hideWave={startKind === 'wave'}
      hideViews
      hideLegend
      showOtherWaves={showOtherWaves}
      onShowOtherWaves={setShowOtherWaves}
      traffic={traffic}
      cost={overlays ? overlays.cost : null}
      slider={slider}
      onSlider={setSlider}
      threshold={threshold}
      below={below}
    />
  );

  const actions = [
    { type: 'icon-button', id: 'fit', iconName: 'zoom-to-fit', text: t('graph-explorer.actions.fit'), disabled: shown === 0 },
    {
      type: 'icon-button',
      id: 'fan',
      iconName: anyFanned(view) ? 'treeview-collapse' : 'treeview-expand',
      text: anyFanned(view) ? t('graph-explorer.actions.collapse-all') : t('graph-explorer.actions.fan-all'),
      disabled: Object.keys(view.bundles).length === 0,
    },
    { type: 'icon-button', id: 'remove', iconName: 'remove', text: t('graph-explorer.actions.remove'), disabled: !selectedNode },
    { type: 'icon-button', id: 'clear', iconName: 'delete-marker', text: t('graph-explorer.actions.reset'), disabled: shown === 0 },
  ];
  const onAction = (id) => {
    if (id === 'fit') setFocus((f) => ({ signal: f.signal + 1, keys: [] }));
    if (id === 'fan') onFanAll();
    if (id === 'remove') onRemove();
    if (id === 'clear') onReset();
  };

  // The fanned-out bundle the selection belongs to (the bundle itself, or one of its query edges).
  const fannedBundle = selectedBundle && view.fanned[selectedBundle.key]
    ? selectedBundle
    : selectedQid
      ? Object.values(view.bundles).find((b) => view.fanned[b.key] && b.queries.some((q) => q.query_id === selectedQid)) || null
      : null;
  const collapseButton = fannedBundle ? (
    <Button
      iconName="treeview-collapse"
      ariaLabel={t('graph-explorer.details.collapse-label', { table: fannedBundle.table, engine: displayEngine(fannedBundle.engine) })}
      onClick={() => {
        const out = toggleBundle(view, fannedBundle.key, MAX_VIEW_ELEMENTS);
        if (out.ok) {
          setView(out.view);
          setSelectedKey(fannedBundle.key);
          if (listBundle === fannedBundle.key) setListBundle(null);
        }
      }}
    >
      {t('graph-explorer.details.collapse')}
    </Button>
  ) : null;

  let details = <Box color="text-body-secondary">{t('graph-explorer.details.empty')}</Box>;
  if (selectedQid) {
    details = (
      <SpaceBetween size="s">
        {collapseButton}
        <GraphExplorerQueryPanel t={t} detail={detail.data} loading={detail.loading} error={detail.error} embedded />
      </SpaceBetween>
    );
  } else if (selectedBundle) {
    details = (
      <SpaceBetween size="xs">
        {collapseButton}
        <Box variant="h3">{t('graph-explorer.details.bundle', { table: selectedBundle.table, engine: displayEngine(selectedBundle.engine) })}</Box>
        <Box>{t('graph-explorer.hover.bundle', { count: selectedBundle.query_count, value: selectedBundle.calls_per_second.toLocaleString() })}</Box>
        {selectedBundle.access && <Box>{t(`graph-explorer.access.${selectedBundle.access}`)}</Box>}
        {selectedBundle.wave != null && <Box>{t('graph-explorer.tags.wave', { wave: selectedBundle.wave })}</Box>}
        {selectedBundle.cached_in_wave != null && <Box>{t('graph-explorer.tags.cached', { wave: selectedBundle.cached_in_wave })}</Box>}
      </SpaceBetween>
    );
  } else if (selectedNode) {
    details = (
      <SpaceBetween size="xs">
        <Box variant="h3">{selectedNode.placeholder ? t('graph-explorer.node.no-table') : selectedNode.label}</Box>
        {pinned.includes(selectedNode.key) && (
          <SpaceBetween direction="horizontal" size="xs">
            <Box variant="small">{t('graph-explorer.details.pinned')}</Box>
            <Button iconName="unlocked" onClick={() => onPinChange(selectedNode.key, false)}>
              {t('graph-explorer.details.unpin')}
            </Button>
          </SpaceBetween>
        )}
        <Box>{t(`graph-explorer.details.kind-${selectedNode.kind}`)}</Box>
        {selectedNode.placeholder && <Box>{t('graph-explorer.tip.no-table')}</Box>}
        {selectedNode.source_of_truth && (
          <Box>{t('graph-explorer.details.writer', { engine: displayEngine(selectedNode.source_of_truth) })}</Box>
        )}
        {selectedNode.replica_engines && selectedNode.replica_engines.length > 0 && (
          <Box>{t('graph-explorer.details.replicas', { engines: selectedNode.replica_engines.map(displayEngine).join(', ') })}</Box>
        )}
        {selectedNode.wave != null && <Box>{t('graph-explorer.tags.wave', { wave: selectedNode.wave })}</Box>}
        {selectedNode.monthly_cost_usd != null && <Box>{t('graph-explorer.hover.cost', { value: selectedNode.monthly_cost_usd.toLocaleString() })}</Box>}
        {linksNote && <Box variant="small" color="text-body-secondary">{linksNote}</Box>}
      </SpaceBetween>
    );
  }

  const panels = {
    queries: (
      <>
        <Box variant="small" color="text-body-secondary">
          {t('graph-explorer.list.count', { count: listed.length })}
        </Box>
        <div style={{ flex: 1, minHeight: 0, marginTop: 4 }}>
          <GraphExplorerQueryList
            t={t}
            items={listed}
            filter={listFilter}
            onFilter={setListFilter}
            selectedQid={selectedQid}
            onHover={setHoverQid}
            onSelect={onSelectCard}
            bundleLabel={listBundleObj ? `${listBundleObj.table} → ${displayEngine(listBundleObj.engine)}` : null}
            onClearBundle={() => setListBundle(null)}
            placeholderTables={placeholderTables}
            active={layout.tab === 'queries' && !layout.collapsed}
          />
        </div>
      </>
    ),
    walkthrough: (
      <WalkthroughPanel
        t={t}
        walkthroughs={walkthroughs}
        active={walk}
        onStart={(id) => applyStep(id, 0)}
        onStep={(index) => walk && applyStep(walk.id, index)}
        onClose={() => setWalk(null)}
      />
    ),
    details: <div data-testid="graph-explorer-details">{details}</div>,
  };

  return (
    <>
      {/* The layout sizes itself below this header, so the workspace fits the viewport. */}
      <div id="graph-explorer-top-nav">
        <AppHeader />
      </div>
      <AppLayoutToolbar
        // This page only: the explorer is a full-width workspace.
        headerSelector="#graph-explorer-top-nav"
        maxContentWidth={Number.MAX_VALUE}
        disableContentPaddings
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail: d }) => setNavigationOpen(d.open)}
        navigation={
          <SideNavigation
            activeHref={`/analysis/results/${jobId}`}
            header={SideNavigationConfigurations.header}
            items={SideNavigationConfigurations.items}
          />
        }
        toolsOpen={toolsOpen}
        onToolsChange={({ detail: d }) => setToolsOpen(d.open)}
        tools={
          <HelpPanel header={<h2>{t('graph-explorer.help.title')}</h2>}>
            <div data-testid="graph-explorer-help">
              <h3>{t('graph-explorer.help.graph-title')}</h3>
              <p>{t('graph-explorer.help.graph')}</p>
              <p>{t('graph-explorer.help.click')}</p>
              <h3>{t('graph-explorer.help.workspace-title')}</h3>
              <p>{t('graph-explorer.help.workspace')}</p>
              <h3>{t('graph-explorer.help.views-title')}</h3>
              <p>{t('graph-explorer.help.views')}</p>
              <p>{t('graph-explorer.help.filters')}</p>
              <h3>{t('graph-explorer.help.waves-title')}</h3>
              <p>{t('graph-explorer.help.waves')}</p>
              <h3>{t('graph-explorer.help.walkthroughs-title')}</h3>
              <p>{t('graph-explorer.help.walkthroughs')}</p>
              <h3>{t('graph-explorer.help.limits-title')}</h3>
              <p>{t('graph-explorer.help.limits')}</p>
            </div>
          </HelpPanel>
        }
        content={
          <div data-testid="graph-explorer-page">
            {/* The thin page bar: where you are, what this is, help and the way back. */}
            <div
              data-testid="graph-explorer-page-bar"
              // Room either side for the app layout's navigation and help triggers.
              style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', padding: '6px 60px', borderBottom: '1px solid #414d5c' }}
            >
              <BreadcrumbGroup items={breadcrumbs} ariaLabel={t('graph-explorer.breadcrumb')} />
              <Box variant="h1" fontSize="heading-m" margin={{ left: 'xs' }}>
                {t('graph-explorer.title')}
              </Box>
              {layout.noticeDismissed ? (
                <Popover triggerType="custom" header={t('graph-explorer.notice.header')} content={t('graph-explorer.notice.body')} dismissAriaLabel={t('common.actions.close')}>
                  <Button variant="inline-link" ariaLabel={t('graph-explorer.notice.chip-label')}>
                    <span data-testid="graph-explorer-notice-chip">{t('graph-explorer.notice.chip')}</span>
                  </Button>
                </Popover>
              ) : (
                <div data-testid="graph-explorer-notice" style={{ flex: '1 1 420px', minWidth: 280 }}>
                  <Alert type="warning" dismissible onDismiss={() => setLayout({ noticeDismissed: true })} dismissAriaLabel={t('common.actions.close')}>
                    <Box variant="small">
                      <strong>{t('graph-explorer.notice.header')}.</strong> {t('graph-explorer.notice.short')}
                    </Box>
                  </Alert>
                </div>
              )}
              <div style={{ marginLeft: 'auto', display: 'flex', gap: 8, alignItems: 'center' }}>
                <Link variant="info" onFollow={() => setToolsOpen(true)} ariaLabel={t('graph-explorer.help.open')}>
                  {t('graph-explorer.help.info')}
                </Link>
                <Button
                  variant="inline-link"
                  onClick={() => {
                    setLayoutState(resetLayout());
                    setPinned([]);
                  }}
                >
                  {t('graph-explorer.layout.reset')}
                </Button>
                <Button href={`/analysis/results/${jobId}`}>{t('graph-explorer.actions.back')}</Button>
              </div>
            </div>

            <div
              ref={workspace}
              data-testid="graph-explorer-workspace"
              style={{ display: 'flex', height: workspaceHeight || 'calc(100vh - 160px)', minHeight: 420, overflow: 'hidden' }}
            >
              <GraphExplorerSidePanel
                t={t}
                width={layout.panelWidth}
                collapsed={layout.collapsed}
                onToggle={togglePanel}
                onResize={(panelWidth) => setLayout({ panelWidth })}
                tab={layout.tab}
                onTab={(tab) => setLayout({ tab })}
                panels={panels}
              />
              <div data-testid="graph-explorer-canvas-area" style={{ position: 'relative', flex: 1, minWidth: 0 }}>
                <GraphExplorerCanvas
                  elements={elements}
                  onNodeTap={onNodeTap}
                  onEdgeTap={onEdgeTap}
                  onHover={setHovered}
                  selectedKey={selectedKey}
                  hoverKey={hoverQid ? `qid:${hoverQid}` : null}
                  anchorKey={anchorKey}
                  focus={focus}
                  unitHighlight={mode === 'moves-together'}
                  pinnedKeys={pinned}
                  onPinChange={onPinChange}
                  borderless
                />
                <GraphExplorerToolbar
                  t={t}
                  kinds={kinds}
                  startKind={startKind}
                  onStartKind={(kind) => {
                    setStartKind(kind);
                    setStartOption(null);
                  }}
                  choices={choices}
                  startOption={startOption}
                  onStartOption={setStartOption}
                  optionsReady={Boolean(options || optionsError)}
                  choicesLimit={truncatedPickerLimit(startKind, options)}
                  onExplore={() => startOption && explore(startKind, startOption.value, { start: true })}
                  exploring={loading}
                  views={availableViews(overlays)}
                  mode={mode}
                  onMode={setMode}
                  searchText={searchText}
                  onSearchText={setSearchText}
                  search={search}
                  searching={searching}
                  onPickSearch={(q) => {
                    pickSearchResult(q);
                    setSearchText('');
                  }}
                  filters={filters}
                  activeFilters={activeFilters}
                  actions={actions}
                  onAction={onAction}
                />
                {/* Messages sit below the toolbar (two rows when the canvas is narrow). */}
                <div style={{ position: 'absolute', top: 120, left: 10, right: 10, zIndex: 3, display: 'flex', flexDirection: 'column', gap: 6, pointerEvents: 'none' }}>
                  {(problem || stayed > 0 || view.capped || optionsError) && (
                    <div style={{ maxWidth: 560, pointerEvents: 'auto' }}>
                      {problem && (
                        <Alert type={problem === 'room' ? 'info' : 'error'} dismissible onDismiss={() => setProblem('')}>
                          {problem === 'room'
                            ? t('graph-explorer.graph.no-room', { max: MAX_VIEW_ELEMENTS })
                            : problem === 'expand'
                              ? t('graph-explorer.errors.expand')
                              : problem}
                        </Alert>
                      )}
                      {stayed > 0 && (
                        <Alert type="info" dismissible onDismiss={() => setStayed(0)}>
                          {t('graph-explorer.graph.stayed', { count: stayed, max: MAX_VIEW_ELEMENTS })}
                        </Alert>
                      )}
                      {view.capped && <Alert type="info">{t('graph-explorer.graph.capped', { max: MAX_VIEW_ELEMENTS })}</Alert>}
                      {optionsError && <Alert type="error">{t('graph-explorer.errors.options')}</Alert>}
                    </div>
                  )}
                </div>
                {shown === 0 && (
                  <div style={{ position: 'absolute', top: '45%', left: 0, right: 0, textAlign: 'center', pointerEvents: 'none' }}>
                    <Box color="text-body-secondary">{t('graph-explorer.graph.empty')}</Box>
                  </div>
                )}
                <GraphExplorerLegendCard
                  t={t}
                  mode={mode}
                  traffic={traffic}
                  cost={overlays ? overlays.cost : null}
                  status={t('graph-explorer.graph.counter', { count: shown, max: MAX_VIEW_ELEMENTS })}
                  engines={Object.values(view.nodes).filter((n) => n.kind === 'engine').map((n) => n.id).sort()}
                  placeholder={Object.values(view.nodes).some((n) => n.placeholder)}
                  wave={wave}
                />
                <StepBar
                  t={t}
                  walkthroughs={walkthroughs}
                  active={walk}
                  loading={walkLoading}
                  onStep={(index) => walk && applyStep(walk.id, index)}
                  onClose={() => setWalk(null)}
                />
                {tip && (
                  <div
                    role="tooltip"
                    data-testid="graph-explorer-tooltip"
                    style={{
                      position: 'absolute',
                      left: Math.max(4, tip.x + 14),
                      top: Math.max(4, tip.y + 14),
                      maxWidth: 340,
                      pointerEvents: 'none',
                      background: '#0f141a',
                      color: '#e9ebed',
                      border: '1px solid #5f6b7a',
                      borderRadius: 6,
                      padding: '6px 8px',
                      fontSize: 12,
                      zIndex: 5,
                    }}
                  >
                    {tip.lines.map((line, i) => (
                      <div key={line} style={i === tip.lines.length - 1 ? { color: '#ff9900', marginTop: 2 } : undefined}>
                        {line}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          </div>
        }
      />
    </>
  );
}
