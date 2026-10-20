//##-- React
import { useState, useEffect, memo, useCallback, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

//##-- AWS UI Objects
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Flashbar from "@cloudscape-design/components/flashbar";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Spinner from "@cloudscape-design/components/spinner";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import Pagination from "@cloudscape-design/components/pagination";
import PropertyFilter from "@cloudscape-design/components/property-filter";
import PieChart from "@cloudscape-design/components/pie-chart";
import Table from "@cloudscape-design/components/table";
import Modal from "@cloudscape-design/components/modal";
import TextFilter from "@cloudscape-design/components/text-filter";
import Link from "@cloudscape-design/components/link";
import Multiselect from "@cloudscape-design/components/multiselect";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Tabs from "@cloudscape-design/components/tabs";
import Icon from "@cloudscape-design/components/icon";
import CodeEditor from "@cloudscape-design/components/code-editor";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import CopyToClipboard from "@cloudscape-design/components/copy-to-clipboard";

//##-- Custom
import { SideNavigationConfigurations, ApiConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";
import ChartSankey from "../components/ChartSankey";
import SummaryFallbackNotice from "../components/SummaryFallbackNotice";
import QueryDecisionTrace from "../components/QueryDecisionTrace";
import QueryRoutingTable from "../components/QueryRoutingTable";
import CloseCallCallout from "../components/CloseCallCallout";
import UnservableQueriesCallout from "../components/UnservableQueriesCallout";
import NoOpenRisksNote from "../components/NoOpenRisksNote";
import NoOpenRisksSection from "../components/NoOpenRisksSection";
import UnreviewedMovesNote from "../components/UnreviewedMovesNote";
import EngineChoicesNote from "../components/EngineChoicesNote";
import MigrationWavePlan from "../components/MigrationWavePlan";
import ExecutiveSummaryStats from "../components/ExecutiveSummaryStats";
import SourcesOfTruthCallout from "../components/SourcesOfTruthCallout";
import DeliverablesPanel from "../components/DeliverablesPanel";
import { buildQueryRoutingRows } from "../utils/queryRouting";
import { getCacheOverlay, ownerDistribution, formatCacheLayerLine, cacheOverlayNotes, targetEngineEntries, resolveCostBreakdown, ownerSchemaDesigns, cacheAccessPatternCount, addCacheOverlayNode, cacheLayerAccessPatterns } from "../utils/cacheLayer";
import { summaryFallbackInfo } from "../utils/summaryFallback";
import { fallbackWithheldReason } from "../utils/fallbackWithheldReason";
import { buildAuroraDesigns, isAuroraEngine, reasonTextFor } from "../utils/auroraDesign";
import { codeEditorI18nStrings } from "../utils/codeEditorI18n";
import { RISK_SEVERITIES, UNRATED, filterRisksWithContent, groupRisksBySeverity, mitigationRepeatsDescription, riskSeverityStatus, splitRiskDescription } from "../utils/riskAssessment";
import { costBaselineStats } from "../utils/tcoAnalysis";
// #358: engine display names come from the shared mapping (kept in sync with
// src/shared/engine_names.py by tests/unit/report/test_engine_names_js_sync.py)
// rather than this page's own hand-kept copy.
import { ENGINE_LABELS } from "../utils/engineNames";
import { WORKLOAD_FIT_DEFINITION } from "../utils/workloadFitLabels";
import { optimizationLabel } from "../utils/optimizationLabels";
import { accessPatternTotals, designTableCountOf, openRiskCountAt, queryPatternCountsOf, resolvedRiskCountOf, resolvedRisksBySeverityText, routingHistoryRecorded, routingKindsById, tradeOffsByTab, tradeOffTabLabel } from "../utils/reportFacts";


// ============================================
// Constants
// ============================================

// GET /assessments/{job_id}/query-journeys returns at most 200 a page (#822).
const JOURNEY_PAGE_SIZE = 200;
const JOURNEY_MAX_PAGES = 50;

const ENGINE_BADGE_COLORS = {
  dynamodb: 'blue',
  documentdb: 'green',
  elasticache: 'red',
  opensearch: 'grey',
  neptune: 'red',
  keyspaces: 'blue',
  aurora: 'green',
};

// #373: the severity colours the retired HTML decision report used -- red for the
// error-level severities (CRITICAL and HIGH, kept equally severe, #270),
// amber for MEDIUM, green for LOW. `stopped` is an unrated severity (#270
// round-1 review) -- grey, matching the deck's muted UNRATED accent.
const RISK_BORDER_COLOR = {
  error: '#d13212',
  warning: '#ffc107',
  success: '#1d8102',
  stopped: '#9ba7b6',
};

const ENGINE_HEX = {
  dynamodb: '#3184e8',
  documentdb: '#1d8102',
  opensearch: '#2ea597',
  elasticache: '#d13212',
  neptune: '#7d2105',
  keyspaces: '#8b6ccb',
  aurora: '#ec7211',
};

const OP_CATEGORY = {
  GetItem: 'read', Query: 'read', Scan: 'read', BatchGetItem: 'read',
  get_by_id: 'read', search: 'search', aggregate: 'search',
  find: 'read', findOne: 'read', lookup: 'read',
  GET: 'read', SEARCH: 'search', MGET: 'read', HGETALL: 'read',
  PutItem: 'write', BatchWriteItem: 'write',
  insertOne: 'write', insertMany: 'write', bulkWrite: 'write',
  SET: 'write', HSET: 'write', SADD: 'write', ZADD: 'write',
  UpdateItem: 'update', updateOne: 'update', updateMany: 'update',
  DeleteItem: 'delete', deleteOne: 'delete', deleteMany: 'delete',
  DEL: 'delete', HDEL: 'delete',
};

const OP_COLORS = {
  read: '#2ea597',
  write: '#ec7211',
  search: '#9b59b6',
  update: '#3184e8',
  delete: '#d13212',
};

const PAGE_SIZE = 10;


// ============================================
// Helpers
// ============================================

const getOpCategory = (op) => OP_CATEGORY[op] || (
  /get|read|find|scan|query|select|fetch|lookup/i.test(op || '') ? 'read' :
  /put|insert|create|write|set|add/i.test(op || '') ? 'write' :
  /search|agg|match|wildcard/i.test(op || '') ? 'search' :
  /update|modify|patch/i.test(op || '') ? 'update' :
  /delete|remove/i.test(op || '') ? 'delete' : 'read'
);

const shortTable = (t) => t?.split('.').pop() || t;

// Aurora's '__seq_scan__' sentinel (#157 review, S1) is a script-internal
// value, never a reader-facing one. Three facts collapse onto it, and only
// one is a claim worth showing:
// - the predicate itself is unknown (needs_judgment) -- nothing can be
//   shown either way, hidden;
// - the query's table could not be identified at all (load_testable=false
//   with that reason, #157 review round 2) -- there was nothing to resolve
//   an index against in the first place, so "sequential scan" would claim
//   more than is known; hidden, same as the unknown-predicate case;
// - the predicate is confirmed empty -- a sequential scan really is the
//   resolved answer, shown as "None (sequential scan)".
const auroraIndexUsedDisplay = (ap) => {
  if (ap?.index_used !== '__seq_scan__') return ap?.index_used || null;
  if (ap?.needs_judgment) return null;
  if (ap?.load_testable === false && (ap?.load_testable_reason || '').includes('could be identified')) {
    return null;
  }
  return 'None (sequential scan)';
};

// #429 part 2: shared by the owner rows and the cache-layer rows -- each is
// grouped by source table separately so the cache layer renders as its own
// section "By source table" too, instead of mixing its key designs into the
// owner groups' convergence counts.
const groupPatternsBySourceTable = (patterns) => {
  const groups = {};

  patterns.forEach(ap => {
    ap.sourceTables.forEach(table => {
      if (!groups[table]) {
        groups[table] = {
          table,
          engines: new Set(),
          destTables: new Set(),
          patterns: [],
          convergesFrom: new Set(),
        };
      }
      groups[table].engines.add(ap.engine);
      if (ap.destTable !== '—') groups[table].destTables.add(ap.destTable);
      groups[table].patterns.push(ap);
    });
  });

  // Detect convergence: if a dest table has multiple source tables
  const destToSources = {};
  patterns.forEach(ap => {
    if (ap.destTable !== '—') {
      if (!destToSources[ap.destTable]) destToSources[ap.destTable] = new Set();
      ap.sourceTables.forEach(t => destToSources[ap.destTable].add(t));
    }
  });

  Object.values(groups).forEach(g => {
    g.destTables.forEach(dt => {
      const sources = destToSources[dt];
      if (sources && sources.size > 1) {
        sources.forEach(s => {
          if (s !== g.table) g.convergesFrom.add(s);
        });
      }
    });
  });

  return Object.values(groups).sort((a, b) => b.patterns.length - a.patterns.length);
};


// ============================================
// Component
// ============================================

const AnalysisResultsPage = memo(() => {
  const { t } = useTranslation();
  const { jobId } = useParams();
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [resultsData, setResultsData] = useState(null);
  const [schemaDesigns, setSchemaDesigns] = useState([]);
  const [collectorData, setCollectorData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [flashbarItems, setFlashbarItems] = useState([]);

  // Explorer state
  const [browseMode, setBrowseMode] = useState('pattern'); // 'pattern' | 'source'
  const [engineFilter, setEngineFilter] = useState([]); // Array for multi-select
  const [opFilter, setOpFilter] = useState([]); // Array for multi-select
  const [selectedPattern, setSelectedPattern] = useState(null); // For modal
  const [selectedSourceTable, setSelectedSourceTable] = useState(null); // For source table modal
  const [showPatternModal, setShowPatternModal] = useState(false);
  const [selectedItems, setSelectedItems] = useState([]); // For table selection
  const [currentPage, setCurrentPage] = useState(1);
  const [filterQuery, setFilterQuery] = useState({ tokens: [], operation: 'and' });

  // Query journey modal state
  const [showQueryJourneyModal, setShowQueryJourneyModal] = useState(false);
  const [queryJourneyData, setQueryJourneyData] = useState(null);
  const [queryJourneyLoading, setQueryJourneyLoading] = useState(false);
  // #339: the journey's "Why this engine" section (graph/queries/{id}/trace)
  const [queryTrace, setQueryTrace] = useState({ data: null, error: null });
  const [selectedQueryId, setSelectedQueryId] = useState(null);
  // #822: every query's journey (the query-journeys list), for the query
  // routing table that opens a trace without schema design.
  const [queryJourneyList, setQueryJourneyList] = useState({ items: [], loading: true, error: null, truncated: false });
  // #957: the job's deliverables/ folder, for the download panel.
  const [deliverables, setDeliverables] = useState({ files: [], loading: true, error: null });

  // Ace editor state
  const [ace, setAce] = useState(null);
  const [aceLoading, setAceLoading] = useState(true);

  const addFlashbarMessage = useCallback((message) => {
    setFlashbarItems(prevItems => [...prevItems, message]);
  }, []);

  const handleFlashbarDismiss = useCallback((itemId) => {
    setFlashbarItems(prevItems => prevItems.filter(item => item.id !== itemId));
  }, []);

  const fetchData = useCallback(async () => {
    if (!jobId) {
      addFlashbarMessage({ type: 'error', header: t('analysis-results.error.invalid-job-id'), content: t('analysis-results.error.no-job-id'), dismissible: true, id: `error-${Date.now()}` });
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const apiManager = new ApiManager();
      const results = await apiManager.execute([
        { id: 'results', path: `assessments/${jobId}/results`, method: 'GET', params: {} },
        { id: 'schemas', path: `assessments/${jobId}/schema-designs`, method: 'GET', params: {} },
        { id: 'collector', path: `assessments/${jobId}/collector`, method: 'GET', params: {} },
      ]);

      if (results['results']?.success) {
        setResultsData(results['results']);
        setFlashbarItems([]);
      } else if (results['results']?.error) {
        addFlashbarMessage({ type: 'error', header: t('analysis-results.error.failed-to-load'), content: results['results'].error?.message || 'Unknown error', dismissible: true, id: `error-${Date.now()}` });
      }

      if (results['schemas']?.success) {
        setSchemaDesigns(results['schemas'].schema_designs || []);
      }

      if (results['collector']?.success) {
        setCollectorData(results['collector']);
      }
    } catch (error) {
      addFlashbarMessage({ type: 'error', header: t('common.labels.error'), content: error.message, dismissible: true, id: `error-${Date.now()}` });
    } finally {
      setLoading(false);
    }
  }, [jobId, addFlashbarMessage, t]);

  useEffect(() => { fetchData(); }, [jobId]); // eslint-disable-line react-hooks/exhaustive-deps

  // #822: the query routing table's rows. The list endpoint returns at most
  // JOURNEY_PAGE_SIZE journeys a page, so the remaining pages are read too.
  useEffect(() => {
    if (!jobId) return undefined;
    let cancelled = false;
    const listPath = page => `assessments/${jobId}/query-journeys?page=${page}&page_size=${JOURNEY_PAGE_SIZE}`;
    const load = async () => {
      setQueryJourneyList({ items: [], loading: true, error: null, truncated: false });
      try {
        const apiManager = new ApiManager();
        const first = (await apiManager.execute([{ id: 'p1', path: listPath(1), method: 'GET', params: {} }]))['p1'];
        if (!first?.success) throw new Error(first?.error?.message || t('analysis-results.query-routing.empty'));
        let items = first.items || [];
        const totalPages = Number(first.total_pages) || 1;
        const pages = Math.min(totalPages, JOURNEY_MAX_PAGES);
        if (pages > 1) {
          const calls = [];
          for (let page = 2; page <= pages; page += 1) {
            calls.push({ id: `p${page}`, path: listPath(page), method: 'GET', params: {} });
          }
          const rest = await apiManager.execute(calls);
          calls.forEach(({ id }) => {
            if (!rest[id]?.success) throw new Error(rest[id]?.error?.message || t('analysis-results.query-routing.empty'));
            items = items.concat(rest[id].items || []);
          });
        }
        if (!cancelled) setQueryJourneyList({ items, loading: false, error: null, truncated: totalPages > pages });
      } catch (error) {
        if (!cancelled) setQueryJourneyList({ items: [], loading: false, error: error.message, truncated: false });
      }
    };
    load();
    return () => { cancelled = true; };
  }, [jobId, t]);

  // #957: the files of the job's deliverables/ folder (GET .../deliverables).
  useEffect(() => {
    if (!jobId) return undefined;
    let cancelled = false;
    const load = async () => {
      setDeliverables({ files: [], loading: true, error: null });
      try {
        const apiManager = new ApiManager();
        const res = (await apiManager.execute([
          { id: 'deliverables', path: `assessments/${jobId}/deliverables`, method: 'GET', params: {} },
        ]))['deliverables'];
        if (!res?.success) throw new Error(res?.error?.message || 'Unknown error');
        if (!cancelled) setDeliverables({ files: res.files || [], loading: false, error: null });
      } catch (error) {
        if (!cancelled) setDeliverables({ files: [], loading: false, error: error.message });
      }
    };
    load();
    return () => { cancelled = true; };
  }, [jobId]);

  // Load Ace editor
  useEffect(() => {
    const loadAce = async () => {
      try {
        const ace = await import('ace-builds');
        await import('ace-builds/webpack-resolver');
        setAce(ace);
      } catch (error) {
        console.error('Error loading Ace editor:', error);
      } finally {
        setAceLoading(false);
      }
    };
    loadAce();
  }, []);

  // ---- Derived data ----

  const synthesis = resultsData?.synthesis || {};
  const queryRoutingRows = useMemo(
    () => buildQueryRoutingRows(queryJourneyList.items, routingKindsById(resultsData?.synthesis), ENGINE_LABELS),
    [queryJourneyList.items, resultsData],
  );
  const realityCheck = synthesis?.reality_check || {};
  // #296 cache overlay: a top-level synthesis.cache_overlay signals that
  // ElastiCache is a cache layer here, not an owner -- defensively strip it
  // from the owner distribution so it can't show up as its own Sankey/target
  // engine node with a workload share. Legacy reports with no cache_overlay
  // (ElastiCache still a real owner) are unaffected. Plain consts (not
  // useMemo) to match realityCheck/synthesis above, which are the same.
  const cacheOverlay = getCacheOverlay(synthesis);
  const afterDist = ownerDistribution(realityCheck?.after_distribution, !!cacheOverlay);
  const cacheLayerLine = formatCacheLayerLine(cacheOverlay, { t });

  // #269: whether this synthesis summary was replaced by its deterministic
  // fallback (a grounding check rejected the generated text against the final
  // records, #205) -- the engineering report already says so when it is
  // (src/report/engineering_report.py); this keeps the Results page in agreement with it.
  const summaryFallback = summaryFallbackInfo(
    synthesis?.summary_source,
    synthesis?.summary_validation_warnings,
  );

  // #358: "Target engines" must list the cache layer too (labeled distinctly,
  // e.g. "ElastiCache (cache layer)") -- it owns no queries, so afterDist
  // alone would leave it out entirely.
  const targetEngines = useMemo(
    () => targetEngineEntries(afterDist, cacheOverlay),
    [afterDist, cacheOverlay]
  );

  // Filter schema designs to only engines with actual content
  const activeDesigns = useMemo(() => {
    return schemaDesigns.filter(d => {
      const c = d.content || {};
      return (c.table_definitions?.length > 0) ||
             (c.index_designs?.length > 0) ||
             (c.collection_designs?.length > 0) ||
             (c.access_patterns?.length > 0);
    });
  }, [schemaDesigns]);

  // #358: cost breakdown keeps the cache layer's own cost card even though it
  // owns no workload share, and the projected total is the report's own
  // tco_analysis.projected_monthly_cost -- not a sum recomputed from a
  // filtered subset -- so it matches the executive report and chat.
  const { items: costBreakdown, total: projectedCost } = useMemo(
    () => resolveCostBreakdown(synthesis?.tco_analysis, afterDist, cacheOverlay, synthesis?.facts),
    [synthesis, afterDist, cacheOverlay]
  );

  // #373: synthesis.risk_assessment was never read on this page -- no risk
  // list, no severity tabs, no overall_risk_level, nothing, even for a
  // CRITICAL risk. openRisks/resolvedRisksList use the same content filter
  // (filterRisksWithContent) the decision/engineering reports use
  // (risks.filtered_risks/resolved_risks), so this view's risk count can
  // never disagree with theirs (#201). risksBySeverity always has one bucket
  // per severity -- including CRITICAL -- even when it is empty, so a
  // CRITICAL risk is never styled or tabbed the way #270 found HIGH/MEDIUM/LOW
  // treated it (a plain warning, no tab at all).
  const riskAssessment = synthesis?.risk_assessment || {};
  const overallRiskLevel = riskAssessment.overall_risk_level;
  const openRisks = useMemo(
    () => filterRisksWithContent(riskAssessment.risks),
    [riskAssessment]
  );
  const resolvedRisksList = useMemo(
    () => filterRisksWithContent(riskAssessment.resolved_risks),
    [riskAssessment]
  );
  const risksBySeverity = useMemo(() => groupRisksBySeverity(openRisks), [openRisks]);

  // #334: a report with no current-cost baseline (the collector never reported
  // RDS instance metadata) left current_monthly_cost/savings_percent at 0, and
  // this page showed neither at all -- so a reader comparing to the decision
  // report (which, since #380, says "source cost not provided"/"not available"
  // for the same report) saw nothing here, and would see a bare "$0.00"/"0%"
  // if this were added without the same unknown-baseline handling. known is
  // false both for an explicit current_cost_known: false and for an older
  // report with no current_cost_known field at all whose current_monthly_cost
  // is still 0 -- the same rule formatting._cost_baseline_text uses, so this
  // tile can never disagree with the decision/engineering reports (see
  // utils/tcoAnalysis.js).
  const costBaseline = useMemo(
    () => costBaselineStats(synthesis?.tco_analysis),
    [synthesis]
  );

  // Build Sankey from after_distribution
  const sankeyData = useMemo(() => {
    if (Object.keys(afterDist).length === 0) return null;
    const nodes = [{ id: 'queries' }];
    const links = [];
    Object.entries(afterDist).forEach(([engine, count]) => {
      nodes.push({ id: engine });
      links.push({ source: 'queries', target: engine, value: count });
    });
    // #330: show the cache layer in the diagram, fed by its owner engines'
    // flows, instead of only in the cacheLayerLine caption below it.
    return addCacheOverlayNode({ nodes, links }, cacheOverlay);
  }, [afterDist, cacheOverlay]);

  const breadcrumbItems = useMemo(() => [
    { href: "/", text: t('analysis-results.breadcrumb.home') },
    { href: "/dashboard", text: t('analysis-results.breadcrumb.dashboard') },
    { href: `/analysis/monitor/summary/${jobId}`, text: t('analysis-results.breadcrumb.assessment') },
    { href: `/analysis/results/${jobId}`, text: t('analysis-results.breadcrumb.results') }
  ], [jobId, t]);

  // Names for the layout's navigation landmark and its toggle buttons, so
  // assistive tech can tell them apart (axe: button-name, landmark-unique).
  // Accessible names for the explorer's pagers and single-select tables (axe:
  // button-name on the arrow buttons, label on the selection radios).
  const explorerPaginationLabels = useMemo(() => ({
    nextPageLabel: t('analysis-results.query-routing.next-page'),
    previousPageLabel: t('analysis-results.query-routing.previous-page'),
    pageLabel: n => t('analysis-results.query-routing.page', { page: n }),
    paginationLabel: t('analysis-results.explorer.pagination'),
  }), [t]);
  // Each table also gets its own name: a horizontally scrollable Cloudscape table
  // is a region landmark, and two unnamed ones collide (axe: landmark-unique).
  const explorerSelectionLabels = useCallback(tableLabel => ({
    tableLabel,
    selectionGroupLabel: t('analysis-results.explorer.selection-group'),
    itemSelectionLabel: (data, item) => t('analysis-results.explorer.select-item', {
      name: item.table || item.id || '',
    }),
  }), [t]);

  const layoutAriaLabels = useMemo(() => ({
    navigation: t('analysis-results.layout.navigation'),
    navigationToggle: t('analysis-results.layout.navigation-toggle'),
    navigationClose: t('analysis-results.layout.navigation-close'),
  }), [t]);


  // ============================================
  // Unified access patterns (all engines)
  // ============================================

  // #157 review round 2: the explorer's "Description" column names why a
  // query is on Aurora at all (the assignment's own reason), not just the
  // pattern's own structural text ("SELECT on t; filtered by id") -- same
  // fact synthesis_report.py's build_query_groups already attaches via
  // reasons/reason_index for the engineering report's "Reason" column and
  // auroraDesign.js's own "Queries on Aurora" tab. Keyed by pattern_id,
  // which both query_groups' access_patterns entries and the schema
  // design's own content.access_patterns below share (they are the same
  // derived dict).
  const auroraReasonByPatternId = useMemo(() => {
    const map = {};
    (synthesis?.query_groups || []).forEach(g => {
      (g.access_patterns || []).forEach(ap => {
        if (isAuroraEngine(ap?.engine) && ap?.pattern_id) {
          const reason = reasonTextFor(g, ap);
          if (reason) map[ap.pattern_id] = reason;
        }
      });
    });
    return map;
  }, [synthesis]);

  const allAccessPatterns = useMemo(() => {
    const patterns = [];

    // #361: the cache layer gets its own schema design (and its own
    // access_patterns) too, but those patterns were never queries it owns --
    // counting them here double-counted ElastiCache as if it were an owner
    // engine in the explorer's total and its engine pie. ownerSchemaDesigns
    // drops it (cacheAccessPatternCount below tracks it separately so the
    // page can still say how many there are, just not as owned patterns).
    // #405: both are no-ops without a cache_overlay -- a legacy report has
    // ElastiCache as a real owner, so its design stays in the owner total.
    ownerSchemaDesigns(activeDesigns, !!cacheOverlay).forEach(design => {
      const engine = design.target_type;
      const content = design.content || {};

      // Build table/index lookup for detail rendering
      const tableLookup = {};
      (content.table_definitions || []).forEach(t => { tableLookup[t.table_name] = t; });
      (content.index_designs || []).forEach(idx => { tableLookup[idx.index_name] = idx; });
      (content.collection_designs || []).forEach(col => { tableLookup[col.collection_name || col.name] = col; });

      (content.access_patterns || []).forEach(ap => {
        // Aurora (#157/#429 part 1) has no table_name/key_pattern/etc. of its
        // own -- its destination IS its source table(s), carried over 1:1 --
        // so the fallback chain ends on source_tables rather than '—'.
        const auroraDest = (ap.source_tables || []).map(shortTable).join(', ');
        const destName = ap.table_name || ap.key_pattern || ap.index_or_stream || ap.index || ap.collection || auroraDest || '—';
        const destDef = tableLookup[destName] || null;

        patterns.push({
          id: ap.pattern_id || ap.name || `${engine}-${patterns.length}`,
          engine,
          operation: ap.operation || ap.http_method || '—',
          opCategory: getOpCategory(ap.operation || ap.http_method),
          sourceTables: (ap.source_tables || []).map(shortTable),
          sourceTablesRaw: ap.source_tables || [],
          destTable: destName,
          destDef,
          gsiName: ap.gsi_name || null,
          keyCondition: ap.key_condition || null,
          // Aurora's one judgment field (#157): which index serves this
          // pattern's predicate, by leftmost-prefix. '__seq_scan__' never
          // reaches the reader as-is (S1) -- see auroraIndexUsedDisplay.
          indexUsed: auroraIndexUsedDisplay(ap),
          // #157 review round 2: for a designed Aurora row, the explorer's
          // Description column shows why the query is on Aurora at all
          // (the assignment's own reason) -- the structural text stays
          // available in patternDescription (rendered as a secondary line
          // in the detail panel) rather than being replaced outright.
          description: (isAuroraEngine(engine) && auroraReasonByPatternId[ap.pattern_id])
            || ap.description || ap.name || '',
          patternDescription: ap.description || ap.name || '',
          queryIds: ap.source_query_ids || ap.query_ids || [],
          patternGroup: ap.pattern_group || null,
          designRps: ap.design_rps || null,
          itemSize: ap.item_size_bytes || null,
          avgItems: ap.avg_items_returned || null,
          inScope: ap.in_scope !== false,
          outOfScopeReason: ap.out_of_scope_reason || null,
          // OpenSearch specific
          opensearchDsl: ap.opensearch_dsl || null,
          // Aurora's sql_text runs as-is on a carry_over target (#157), same
          // role as OpenSearch's own source_query below: the raw statement a
          // pattern-detail panel shows when no per-query table already has it.
          sourceQuery: ap.source_query || ap.sql_text || null,
        });
      });
    });

    return patterns;
  }, [activeDesigns, cacheOverlay, auroraReasonByPatternId]);

  // #429 part 2: the cache layer's own access patterns, tagged isCacheLayer so
  // the explorer can render them as their own "<Name> (cache layer)" group
  // after the owner rows above, instead of leaving them out of the explorer
  // entirely the way #361/#405 did (correct for the owner total/pie, wrong
  // for browsability). No-op without a cache_overlay -- see
  // cacheLayerAccessPatterns's hasOverlay guard.
  const cacheLayerPatterns = useMemo(() => {
    return cacheLayerAccessPatterns(activeDesigns, cacheOverlay).map(ap => ({
      ...ap,
      opCategory: getOpCategory(ap.operation),
    }));
  }, [activeDesigns, cacheOverlay]);

  // Owner rows followed by the cache-layer rows -- the explorer's combined
  // browsing set. Everything that must stay owner-only (the "Access
  // patterns" stat, the engine pie) keeps reading allAccessPatterns/
  // totalAccessPatterns directly instead of this.
  const explorerPatterns = useMemo(
    () => [...allAccessPatterns, ...cacheLayerPatterns],
    [allAccessPatterns, cacheLayerPatterns]
  );

  // Engine trade-offs lookup
  const engineTradeOffs = useMemo(() => {
    const map = {};
    activeDesigns.forEach(d => {
      map[d.target_type] = d.content?.trade_offs || [];
    });
    // #595: the reality check's own trade-offs that still apply, in their own tab.
    return tradeOffsByTab(map, resultsData?.synthesis);
  }, [activeDesigns, resultsData]);

  // #478: while Aurora's schema design has not run yet, its tables carry
  // over 1:1 with no access_patterns at all, so the explorer above has
  // nothing to show for it (#157 covers it with real ones once the design
  // has run -- see allAccessPatterns above). The relational branch in
  // build_query_groups (src/agents/referee/synthesis_report.py) puts every
  // query the assignment routed to an Aurora engine into
  // synthesis.query_groups regardless, so this section needs no extra fetch
  // -- just the schema designs already loaded and that same query_groups
  // list -- and keeps showing the full design (DDL, indexes, optimizations)
  // the generic explorer above does not.
  const auroraDesigns = useMemo(
    () => buildAuroraDesigns(activeDesigns, synthesis?.query_groups),
    [activeDesigns, synthesis]
  );

  // Query details lookup: query_id → { query_text, query_type, tables_accessed, calls_per_second }
  const queryLookup = useMemo(() => {
    const map = {};
    const patterns = collectorData?.queries?.query_patterns || [];
    patterns.forEach(q => { map[q.query_id] = q; });
    return map;
  }, [collectorData]);

  // Destination table definitions lookup: destName → { engine, def, allSourceTables }
  const destTableLookup = useMemo(() => {
    const lookup = {};
    activeDesigns.forEach(design => {
      const engine = design.target_type;
      const content = design.content || {};
      (content.table_definitions || []).forEach(t => {
        lookup[`${engine}::${t.table_name}`] = {
          engine,
          name: t.table_name,
          def: t,
          allSourceTables: (t.source_tables || []).map(shortTable),
        };
      });
      (content.index_designs || []).forEach(idx => {
        lookup[`${engine}::${idx.index_name}`] = {
          engine,
          name: idx.index_name,
          def: idx,
          allSourceTables: (idx.source_tables || []).map(shortTable),
        };
      });
      (content.collection_designs || []).forEach(col => {
        const name = col.collection_name || col.name;
        lookup[`${engine}::${name}`] = {
          engine,
          name,
          def: col,
          allSourceTables: (col.source_tables || col.source_collections || []).map(shortTable),
        };
      });
    });
    return lookup;
  }, [activeDesigns]);

  // Total access patterns (owner engines only -- see the #361 note on
  // allAccessPatterns above for why the cache layer is excluded).
  // #792: the total and the cache layer's count are report facts
  // (facts.access_pattern_totals); the rows the explorer lists below are its own.

  // #361: the cache layer's own pattern count, tracked separately so the page
  // can say "+N cache-layer patterns" instead of silently dropping them or
  // folding them into the owner total/pie. #405: 0 without a cache_overlay --
  // see the overlay guard note on allAccessPatterns above.
  const shownCachePatternCount = useMemo(
    () => cacheAccessPatternCount(activeDesigns, !!cacheOverlay),
    [activeDesigns, cacheOverlay]
  );
  const patternTotals = accessPatternTotals(synthesis, allAccessPatterns.length, shownCachePatternCount);
  const totalAccessPatterns = patternTotals.owned;
  const cachePatternCount = patternTotals.cacheLayer;

  // #361: the Query flow's query count (afterDist is already owner-only, see
  // ownerDistribution above) -- the number "Access patterns" is compared
  // against. Access patterns can legitimately differ from this: relational
  // engines model as tables, not per-pattern designs, and several queries can
  // converge into one access pattern.
  const ownerQueryCount = useMemo(
    () => Object.values(afterDist).reduce((a, b) => a + b, 0),
    [afterDist]
  );

  // #429 part 2: the explorer's own combined total (owner rows + cache-layer
  // rows), used for its header count ("64 (54 owned + 10 cache layer)") --
  // kept separate from totalAccessPatterns, which must stay owner-only (the
  // "Access patterns" stat above compares against it).
  const explorerTotal = patternTotals.listed;


  // ============================================
  // Filtering
  // ============================================

  // Property filter config
  const filterProperties = useMemo(() => [
    { key: 'engine', propertyLabel: 'Engine', groupValuesLabel: 'Engines', operators: ['=', '!='] },
    { key: 'source_table', propertyLabel: 'Source table', groupValuesLabel: 'Source tables', operators: ['=', '!=', ':'] },
    { key: 'dest_table', propertyLabel: 'Destination', groupValuesLabel: 'Destinations', operators: ['=', '!=', ':'] },
    { key: 'operation', propertyLabel: 'Operation', groupValuesLabel: 'Operations', operators: ['=', '!='] },
    { key: 'op_type', propertyLabel: 'Operation type', groupValuesLabel: 'Types', operators: ['='] },
    { key: 'pattern_group', propertyLabel: 'Pattern group', groupValuesLabel: 'Groups', operators: ['=', ':'] },
    { key: 'gsi', propertyLabel: 'Uses GSI', groupValuesLabel: 'GSI', operators: ['='] },
    { key: 'query_id', propertyLabel: 'Query ID', groupValuesLabel: 'Query IDs', operators: ['=', ':'] },
  ], []);

  const filterOptions = useMemo(() => {
    const engines = new Set();
    const sources = new Set();
    const dests = new Set();
    const ops = new Set();
    const opTypes = new Set();
    const groups = new Set();
    const queryIds = new Set();

    // #429 part 2: explorerPatterns (owner rows + cache-layer rows) so the
    // cache layer's engine/source tables/operations are filterable facets
    // too, not just the owner rows'.
    explorerPatterns.forEach(ap => {
      engines.add(ap.engine);
      ap.sourceTables.forEach(t => sources.add(t));
      if (ap.destTable !== '—') dests.add(ap.destTable);
      ops.add(ap.operation);
      opTypes.add(ap.opCategory);
      if (ap.patternGroup) groups.add(ap.patternGroup);
      (ap.queryIds || []).forEach(qid => queryIds.add(qid));
    });

    return [
      ...[...engines].sort().map(v => ({ propertyKey: 'engine', value: v })),
      ...[...sources].sort().map(v => ({ propertyKey: 'source_table', value: v })),
      ...[...dests].sort().map(v => ({ propertyKey: 'dest_table', value: v })),
      ...[...ops].sort().map(v => ({ propertyKey: 'operation', value: v })),
      ...[...opTypes].sort().map(v => ({ propertyKey: 'op_type', value: v })),
      ...[...groups].sort().map(v => ({ propertyKey: 'pattern_group', value: v })),
      { propertyKey: 'gsi', value: 'yes' },
      { propertyKey: 'gsi', value: 'no' },
      ...[...queryIds].sort().map(v => ({ propertyKey: 'query_id', value: v })),
    ];
  }, [explorerPatterns]);

  // Apply all filters (engine bar, op donut, property filter) over the
  // combined owner + cache-layer set (#429 part 2); filteredOwnerPatterns/
  // filteredCacheLayerPatterns below split it back out for rendering.
  const filteredPatterns = useMemo(() => {
    let items = [...explorerPatterns];

    // Engine filter (multi-select)
    if (engineFilter.length > 0) {
      const selectedEngines = engineFilter.map(e => e.value);
      items = items.filter(ap => selectedEngines.includes(ap.engine));
    }

    // Operation filter (multi-select)
    if (opFilter.length > 0) {
      const selectedOps = opFilter.map(o => o.value);
      items = items.filter(ap => selectedOps.includes(ap.opCategory));
    }

    // Property filter tokens
    const { tokens, operation } = filterQuery;
    if (tokens.length > 0) {
      const matchToken = (ap, token) => {
        if (!token.propertyKey) {
          const text = token.value?.toLowerCase() || '';
          return (
            ap.id?.toLowerCase().includes(text) ||
            ap.engine?.toLowerCase().includes(text) ||
            ap.description?.toLowerCase().includes(text) ||
            ap.sourceTables.some(t => t.toLowerCase().includes(text)) ||
            ap.destTable?.toLowerCase().includes(text)
          );
        }
        const val = token.value?.toLowerCase() || '';
        const op = token.operator || '=';

        if (token.propertyKey === 'engine') {
          const v = ap.engine?.toLowerCase() || '';
          return op === '=' ? v === val : op === '!=' ? v !== val : false;
        }
        if (token.propertyKey === 'source_table') {
          const tables = ap.sourceTables.map(t => t.toLowerCase());
          if (op === '=') {
            // Include consolidated tables: if the filtered table shares a destination, include siblings
            if (tables.includes(val)) return true;
            const destKey = `${ap.engine}::${ap.destTable}`;
            const destEntry = destTableLookup[destKey];
            if (destEntry) {
              const siblings = destEntry.allSourceTables.map(t => t.toLowerCase());
              if (siblings.includes(val)) return true;
            }
            return false;
          }
          if (op === '!=') return !tables.includes(val);
          if (op === ':') {
            if (tables.some(t => t.includes(val))) return true;
            const destKey = `${ap.engine}::${ap.destTable}`;
            const destEntry = destTableLookup[destKey];
            if (destEntry) {
              const siblings = destEntry.allSourceTables.map(t => t.toLowerCase());
              if (siblings.some(t => t.includes(val))) return true;
            }
            return false;
          }
          return false;
        }
        if (token.propertyKey === 'dest_table') {
          const d = ap.destTable?.toLowerCase() || '';
          if (op === '=') return d === val;
          if (op === '!=') return d !== val;
          if (op === ':') return d.includes(val);
          return false;
        }
        if (token.propertyKey === 'operation') {
          const o = ap.operation?.toLowerCase() || '';
          return op === '=' ? o === val : op === '!=' ? o !== val : false;
        }
        if (token.propertyKey === 'op_type') {
          return ap.opCategory === val;
        }
        if (token.propertyKey === 'pattern_group') {
          const g = ap.patternGroup?.toLowerCase() || '';
          if (op === '=') return g === val;
          if (op === ':') return g.includes(val);
          return false;
        }
        if (token.propertyKey === 'gsi') {
          const hasGsi = !!ap.gsiName;
          return val === 'yes' ? hasGsi : !hasGsi;
        }
        if (token.propertyKey === 'query_id') {
          const qids = (ap.queryIds || []).map(q => q.toLowerCase());
          if (op === '=') return qids.includes(val);
          if (op === ':') return qids.some(q => q.includes(val));
          return false;
        }
        return true;
      };

      items = items.filter(ap => {
        if (operation === 'and') return tokens.every(t => matchToken(ap, t));
        return tokens.some(t => matchToken(ap, t));
      });
    }

    return items;
  }, [explorerPatterns, engineFilter, opFilter, filterQuery]);

  // #429 part 2: split the combined filtered set back into owner rows and
  // the cache layer's rows, so each renders as its own group/section (the
  // cache layer always after the owners) instead of mixed together.
  const filteredOwnerPatterns = useMemo(
    () => filteredPatterns.filter(ap => !ap.isCacheLayer),
    [filteredPatterns]
  );
  const filteredCacheLayerPatterns = useMemo(
    () => filteredPatterns.filter(ap => ap.isCacheLayer),
    [filteredPatterns]
  );


  // ============================================
  // Source table view grouping
  // ============================================

  // #429 part 2: grouped separately so the cache layer is its own section in
  // the "By source table" view too, after the owner groups.
  const sourceTableGroups = useMemo(
    () => groupPatternsBySourceTable(filteredOwnerPatterns),
    [filteredOwnerPatterns]
  );
  const cacheSourceTableGroups = useMemo(
    () => groupPatternsBySourceTable(filteredCacheLayerPatterns),
    [filteredCacheLayerPatterns]
  );

  // ============================================
  // Chart Data (based on filtered patterns)
  // ============================================

  // Operation distribution for donut
  const opDistribution = useMemo(() => {
    const counts = {};
    filteredPatterns.forEach(ap => {
      const cat = ap.opCategory;
      counts[cat] = (counts[cat] || 0) + 1;
    });
    return Object.entries(counts)
      .map(([op, count]) => ({ op, count, color: OP_COLORS[op] || '#5f6b7a' }))
      .sort((a, b) => b.count - a.count);
  }, [filteredPatterns]);

  // Donut chart data
  const donutData = useMemo(() => {
    return opDistribution.map(d => ({
      id: d.op,
      title: d.op.charAt(0).toUpperCase() + d.op.slice(1),
      value: d.count,
      color: d.color,
    }));
  }, [opDistribution]);

  const operationOptions = useMemo(() => {
    return opDistribution.map(d => ({
      label: `${d.op.charAt(0).toUpperCase() + d.op.slice(1)} (${d.count})`,
      value: d.op
    }));
  }, [opDistribution]);

  // Engine distribution for pie chart (#429 part 2: owner rows only -- the
  // cache layer never owned a query, so it must stay out of the pie's share
  // the same way it stays out of the "Access patterns" stat; it gets its own
  // legend-style entry next to the pie instead, see renderEngineBar below).
  const filteredEngineDist = useMemo(() => {
    const counts = {};
    filteredOwnerPatterns.forEach(ap => {
      counts[ap.engine] = (counts[ap.engine] || 0) + 1;
    });
    return counts;
  }, [filteredOwnerPatterns]);


  // ============================================
  // Pagination
  // ============================================

  // #429 part 2: pagination still covers the owner rows only -- the cache
  // layer's own section (always small -- one group of key designs) renders
  // in full underneath, unpaginated.
  const displayItems = browseMode === 'pattern' ? filteredOwnerPatterns : sourceTableGroups;
  const paginatedItems = useMemo(() => {
    const start = (currentPage - 1) * PAGE_SIZE;
    return displayItems.slice(start, start + PAGE_SIZE);
  }, [displayItems, currentPage]);


  // ============================================
  // Handlers
  // ============================================

  const handlePatternClick = useCallback((pattern) => {
    setSelectedPattern(pattern);
    setSelectedSourceTable(null);
    setShowPatternModal(true);
  }, []);

  const handleSourceTableClick = useCallback((sourceTable) => {
    setSelectedSourceTable(sourceTable);
    setSelectedPattern(null);
    setShowPatternModal(true);
  }, []);

  const handleCloseModal = useCallback(() => {
    setShowPatternModal(false);
    setSelectedPattern(null);
    setSelectedSourceTable(null);
  }, []);

  const handleEngineBarClick = useCallback((engine) => {
    setEngineFilter(prev => prev === engine ? null : engine);
    setCurrentPage(1);
  }, []);

  const handleOpClick = useCallback((op) => {
    setOpFilter(prev => prev === op ? null : op);
    setCurrentPage(1);
  }, []);

  const handleOpChartClick = useCallback((event) => {
    console.log('Operation chart clicked - full event:', event);

    // Try different possible event structures
    let clickedOp = null;

    if (event.detail?.id) {
      clickedOp = event.detail.id;
    } else if (event.detail?.datum?.id) {
      clickedOp = event.detail.datum.id;
    } else if (event.detail?.data?.id) {
      clickedOp = event.detail.data.id;
    } else if (event.id) {
      clickedOp = event.id;
    } else if (event.detail?.title) {
      // Extract from title (e.g., "Read" -> "read")
      clickedOp = event.detail.title.toLowerCase();
    }

    console.log('Extracted operation:', clickedOp);

    if (clickedOp) {
      // Add to PropertyFilter
      const newToken = {
        propertyKey: 'op_type',
        operator: '=',
        value: clickedOp
      };

      // Check if this filter already exists
      const existingTokens = filterQuery.tokens || [];
      const alreadyExists = existingTokens.some(
        t => t.propertyKey === 'op_type' && t.value === clickedOp
      );

      if (!alreadyExists) {
        console.log('Adding operation filter token:', newToken);
        setFilterQuery({
          tokens: [...existingTokens, newToken],
          operation: filterQuery.operation || 'and'
        });
        setCurrentPage(1);
      } else {
        console.log('Operation filter already exists');
      }
    } else {
      console.error('Could not extract operation from event');
    }
  }, [filterQuery]);

  const filterByQueryId = useCallback((queryId) => {
    setFilterQuery({ tokens: [{ propertyKey: 'query_id', operator: '=', value: queryId }], operation: 'and' });
    setEngineFilter([]);
    setOpFilter([]);
    setCurrentPage(1);
    const el = document.getElementById('access-patterns-section');
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  const fetchQueryJourney = useCallback(async (queryId) => {
    setSelectedQueryId(queryId);
    setShowQueryJourneyModal(true);
    setQueryJourneyLoading(true);
    setQueryJourneyData(null);
    setQueryTrace({ data: null, error: null });

    try {
      const apiManager = new ApiManager();
      const result = await apiManager.execute([
        { id: 'query-journey', path: `assessments/${jobId}/query-journeys/${queryId}`, method: 'GET', params: {} },
        { id: 'query-trace', path: `assessments/${jobId}/graph/queries/${queryId}/trace`, method: 'GET', params: {} }
      ]);

      // The decision trace is its own request: a failure there leaves the
      // journey's other tabs as they are and shows the error in its tab only.
      const trace = result['query-trace'];
      if (trace?.success) {
        setQueryTrace({ data: trace, error: null });
      } else {
        setQueryTrace({ data: null, error: trace?.error?.message || t('analysis-results.query-trace.empty') });
      }

      if (result['query-journey']?.success) {
        setQueryJourneyData(result['query-journey']);
      } else if (result['query-journey']?.error) {
        setQueryJourneyData({ error: result['query-journey'].error.message || 'Failed to load query history' });
      } else {
        setQueryJourneyData({ error: 'No data available' });
      }
    } catch (error) {
      console.error('Error fetching query journey:', error);
      setQueryJourneyData({ error: error.message || 'Failed to load query history' });
    } finally {
      setQueryJourneyLoading(false);
    }
  }, [jobId, t]);

  const handleCloseQueryJourneyModal = useCallback(() => {
    setShowQueryJourneyModal(false);
    setQueryJourneyData(null);
    setQueryTrace({ data: null, error: null });
    setSelectedQueryId(null);
  }, []);

  const clearAllFilters = useCallback(() => {
    setEngineFilter([]);
    setOpFilter([]);
    setFilterQuery({ tokens: [], operation: 'and' });
    setCurrentPage(1);
  }, []);

  const hasActiveFilters = engineFilter.length > 0 || opFilter.length > 0 || filterQuery.tokens.length > 0;


  // ============================================
  // Table Column Definitions
  // ============================================

  const patternColumnDefinitions = useMemo(() => [
    {
      id: 'pattern_id',
      header: t('analysis-results.explorer.col-pattern'),
      cell: item => (
        <Link onFollow={() => handlePatternClick(item)}>
          {item.id?.slice(0, 8) || '—'}
        </Link>
      ),
      sortingField: 'id',
      width: 120
    },
    {
      id: 'operation',
      header: t('analysis-results.explorer.col-operation'),
      cell: item => item.operation,
      sortingField: 'operation',
      width: 150
    },
    {
      id: 'engine',
      header: t('analysis-results.explorer.col-engine'),
      cell: item => (
        <Badge color={ENGINE_BADGE_COLORS[item.engine] || 'grey'}>
          {ENGINE_LABELS[item.engine] || item.engine}
          {/* #429 part 2: every cache-layer row carries this suffix, even
              inside its own section, so the role is unambiguous wherever the
              row is seen (e.g. a copied/zoomed screenshot of just the row). */}
          {item.isCacheLayer ? t('cache-layer.engine-badge-suffix') : ''}
        </Badge>
      ),
      sortingField: 'engine',
      width: 140
    },
    {
      id: 'source_tables',
      header: t('analysis-results.explorer.col-source-tables'),
      cell: item => item.sourceTables.join(', '),
      width: 250
    },
    {
      id: 'destination',
      header: t('analysis-results.explorer.col-destination'),
      cell: item => {
        const base = item.gsiName ? `${item.destTable} (GSI: ${item.gsiName})` : item.destTable;
        // #429 part 2: the cache layer's "destination" is its key design
        // (key_pattern) -- show the TTL alongside it, when the pattern's key
        // design has one, instead of leaving the key design's eviction
        // policy entirely unbrowsable.
        return item.isCacheLayer && item.ttlSeconds != null
          ? `${base} · ${t('analysis-results.explorer.ttl-suffix', { seconds: item.ttlSeconds })}`
          : base;
      },
      sortingField: 'destTable',
      width: 220
    },
    {
      id: 'description',
      header: t('analysis-results.explorer.col-description'),
      cell: item => item.description || '—',
      width: 300
    }
  ], [t, handlePatternClick]);

  const sourceTableColumnDefinitions = useMemo(() => [
    {
      id: 'table_name',
      header: t('analysis-results.explorer.col-source-table'),
      cell: item => (
        <Link onFollow={() => handleSourceTableClick(item)}>
          {item.table}
        </Link>
      ),
      sortingField: 'table',
      width: 200
    },
    {
      id: 'engines',
      header: t('analysis-results.explorer.col-engines'),
      cell: item => (
        <SpaceBetween direction="horizontal" size="xxs">
          {[...item.engines].map(e => (
            <Badge key={e} color={ENGINE_BADGE_COLORS[e] || 'grey'}>
              {ENGINE_LABELS[e] || e}
              {/* #429 part 2: a source-table group's engines can include the
                  cache layer -- tag it the same way the pattern rows are. */}
              {e === cacheOverlay?.engine ? t('cache-layer.engine-badge-suffix') : ''}
            </Badge>
          ))}
        </SpaceBetween>
      ),
      width: 200
    },
    {
      id: 'dest_tables',
      header: t('analysis-results.explorer.col-destination-tables'),
      cell: item => (
        <Box fontSize="body-s">{[...item.destTables].join(', ')}</Box>
      ),
      width: 250
    },
    {
      id: 'patterns',
      header: t('analysis-results.explorer.col-patterns'),
      cell: item => (
        <Badge>{item.patterns.length}</Badge>
      ),
      sortingField: 'patterns.length',
      width: 100
    },
    {
      id: 'convergence',
      header: t('analysis-results.explorer.col-convergence'),
      cell: item => (
        item.convergesFrom.size > 0 ? (
          <Badge color="blue">{t('analysis-results.explorer.merged-badge', { count: item.convergesFrom.size })}</Badge>
        ) : '—'
      ),
      width: 150
    },
    {
      id: 'operations',
      header: t('analysis-results.explorer.col-operations'),
      cell: item => {
        const opSummary = {};
        item.patterns.forEach(p => { opSummary[p.operation] = (opSummary[p.operation] || 0) + 1; });
        return (
          <Box fontSize="body-s">
            {Object.entries(opSummary).map(([op, cnt]) => `${op}(${cnt})`).join(', ')}
          </Box>
        );
      },
      width: 250
    }
  ], [t, handleSourceTableClick, cacheOverlay]);


  // ============================================
  // Render: Detail panel for a pattern
  // ============================================

  const renderPatternDetail = (ap) => {
    // Resolve source queries from collector
    const sourceQueries = ap.queryIds
      .map(qid => queryLookup[qid])
      .filter(Boolean);

    // Build metrics items dynamically
    const metricsItems = [];
    if (ap.engine) metricsItems.push({ label: t('analysis-results.pattern-detail.engine'), value: <Badge color={ENGINE_BADGE_COLORS[ap.engine] || 'grey'}>{ENGINE_LABELS[ap.engine] || ap.engine}{ap.isCacheLayer ? t('cache-layer.engine-badge-suffix') : ''}</Badge> });
    if (ap.operation) metricsItems.push({ label: t('analysis-results.pattern-detail.operation'), value: ap.operation });
    if (ap.destTable) metricsItems.push({ label: t('analysis-results.pattern-detail.destination-table'), value: ap.destTable });
    if (ap.gsiName) metricsItems.push({ label: t('analysis-results.pattern-detail.gsi'), value: ap.gsiName });
    if (ap.indexUsed) metricsItems.push({ label: t('analysis-results.pattern-detail.index-used'), value: ap.indexUsed });
    if (ap.sourceTables?.length > 0) metricsItems.push({ label: t('analysis-results.pattern-detail.source-tables'), value: ap.sourceTables.join(', ') });
    if (ap.designRps != null) metricsItems.push({ label: t('analysis-results.pattern-detail.design-rps'), value: ap.designRps });
    if (ap.itemSize != null) metricsItems.push({ label: t('analysis-results.pattern-detail.item-size'), value: `${ap.itemSize} bytes` });
    if (ap.avgItems != null) metricsItems.push({ label: t('analysis-results.pattern-detail.avg-items-returned'), value: ap.avgItems });
    if (ap.patternGroup) metricsItems.push({ label: t('analysis-results.pattern-detail.pattern-group'), value: ap.patternGroup });
    // #429 part 2: the cache layer's key design (TTL, data type) -- the only
    // place that information exists is the cache design's key_designs[],
    // joined in by cacheLayerAccessPatterns.
    if (ap.ttlSeconds != null) metricsItems.push({ label: t('analysis-results.pattern-detail.ttl'), value: t('analysis-results.explorer.ttl-suffix', { seconds: ap.ttlSeconds }) });
    if (ap.keyDataType) metricsItems.push({ label: t('analysis-results.pattern-detail.key-data-type'), value: ap.keyDataType });

    return (
      <SpaceBetween size="l">
        {/* Description -- for a designed Aurora row this is the assignment's
            routing reason (#157 review round 2); patternDescription keeps the
            pattern's own structural text visible underneath when it differs
            (every other engine's row: the two are the same string, so the
            second line never renders). */}
        {ap.description && (
          <Container>
            <Box fontSize="body-m">{ap.description}</Box>
            {ap.patternDescription && ap.patternDescription !== ap.description && (
              <Box fontSize="body-s" color="text-body-secondary" padding={{ top: 's' }}>
                {ap.patternDescription}
              </Box>
            )}
          </Container>
        )}

        {/* Pattern Metrics */}
        <Container header={<Header variant="h3">{t('analysis-results.pattern-detail.title')}</Header>}>
          <KeyValuePairs
            columns={3}
            items={metricsItems}
          />
        </Container>

        {/* Source queries */}
        {sourceQueries.length > 0 && (
          <Container header={<Header variant="h3">{t('analysis-results.pattern-detail.source-queries', { count: sourceQueries.length })}</Header>}>
            <Table
              ariaLabels={{ tableLabel: t('analysis-results.pattern-detail.source-queries', { count: sourceQueries.length }) }}
              columnDefinitions={[
                {
                  id: 'query_id',
                  header: t('analysis-results.pattern-detail.query-id'),
                  cell: (item) => (
                    <Link
                      fontSize="body-s"
                      onFollow={(e) => {
                        e.preventDefault();
                        fetchQueryJourney(item.query_id);
                      }}
                    >
                      {item.query_id}
                    </Link>
                  ),
                  width: 280
                },
                {
                  id: 'type',
                  header: t('analysis-results.pattern-detail.type'),
                  cell: (item) => <Badge>{item.query_type || t('common.labels.sql')}</Badge>,
                  width: 80
                },
                {
                  id: 'calls_per_second',
                  header: t('analysis-results.pattern-detail.calls-per-sec'),
                  cell: (item) => item.calls_per_second?.toFixed(2) || '—',
                  width: 100
                },
                {
                  id: 'tables',
                  header: t('analysis-results.pattern-detail.tables'),
                  cell: (item) => item.tables_accessed?.map(shortTable).join(', ') || '—',
                  width: 200
                },
                {
                  id: 'query_text',
                  header: t('analysis-results.pattern-detail.query-text'),
                  cell: (item) => {
                    if (!item.query_text) return '—';
                    return (
                      <Box
                        fontSize="body-s"
                        fontFamily="monospace"
                        padding="s"
                        style={{
                          backgroundColor: '#232f3e',
                          color: '#d4d4d4',
                          borderRadius: '4px',
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-word',
                          overflowX: 'auto',
                          maxHeight: '150px',
                          overflow: 'auto'
                        }}
                      >
                        {item.query_text}
                      </Box>
                    );
                  },
                  minWidth: 400
                }
              ]}
              items={sourceQueries}
              loadingText={t('common.labels.loading')}
              empty={
                <Box textAlign="center" color="inherit">
                  <Box variant="p" color="inherit">
                    {t('common.labels.no-data')}
                  </Box>
                </Box>
              }
              variant="embedded"
              wrapLines={false}
            />
          </Container>
        )}

        {/* OpenSearch source query */}
        {sourceQueries.length === 0 && ap.sourceQuery && (
          <Container header={<Header variant="h3">{t('analysis-results.pattern-detail.source-sql')}</Header>}>
            <Box
              fontSize="body-s"
              fontFamily="monospace"
              padding="s"
              style={{
                backgroundColor: '#232f3e',
                color: '#d4d4d4',
                borderRadius: '4px',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                overflowX: 'auto'
              }}
            >
              {ap.sourceQuery}
            </Box>
          </Container>
        )}

        {/* Target design: key condition or DSL */}
        {(ap.keyCondition || ap.opensearchDsl) && (
          <Container header={<Header variant="h3">{t('analysis-results.pattern-detail.target-access-pattern')}</Header>}>
            <Box
              fontSize="body-s"
              fontFamily="monospace"
              padding="s"
              style={{
                backgroundColor: '#232f3e',
                color: '#d4d4d4',
                borderRadius: '4px',
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
                overflowX: 'auto'
              }}
            >
              {ap.keyCondition && ap.keyCondition}
              {ap.opensearchDsl && (typeof ap.opensearchDsl === 'string' ? ap.opensearchDsl : JSON.stringify(ap.opensearchDsl, null, 2))}
            </Box>
          </Container>
        )}

        {/* GSI detail if present */}
        {ap.gsiName && ap.destDef && (
          <Container header={<Header variant="h3">{t('analysis-results.source-table.gsi-prefix')}{ap.gsiName}</Header>}>
            {(ap.destDef.gsis || ap.destDef.gsi_indexes || [])
              .filter(g => (g.gsi_name || g.index_name) === ap.gsiName)
              .map((gsi, i) => {
                const pk = Array.isArray(gsi.partition_key) ? gsi.partition_key[0] : gsi.partition_key;
                const sk = Array.isArray(gsi.sort_key) ? gsi.sort_key[0] : gsi.sort_key;

                const gsiItems = [
                  { label: t('analysis-results.pattern-detail.partition-key'), value: pk ? `${pk.attribute_name} (${pk.attribute_type})` : '—' },
                  { label: t('analysis-results.pattern-detail.projection'), value: gsi.projection || t('common.labels.all') }
                ];

                if (sk) {
                  gsiItems.splice(1, 0, { label: t('analysis-results.pattern-detail.sort-key'), value: `${sk.attribute_name} (${sk.attribute_type})` });
                }

                return (
                  <KeyValuePairs
                    key={i}
                    columns={3}
                    items={gsiItems}
                  />
                );
              })}
          </Container>
        )}

        {/* Out of scope warning */}
        {!ap.inScope && ap.outOfScopeReason && (
          <Container>
            <Box color="text-status-error" fontSize="body-m">
              <strong>{t('analysis-results.pattern-detail.out-of-scope-reason')}:</strong> {ap.outOfScopeReason}
            </Box>
          </Container>
        )}
      </SpaceBetween>
    );
  };


  // ============================================
  // Render: Detail panel for a source table group
  // ============================================

  const renderSourceTableDetail = (group) => {
    // Group patterns by engine first, then by destination table
    const byEngine = {};
    group.patterns.forEach(ap => {
      if (!byEngine[ap.engine]) byEngine[ap.engine] = [];
      byEngine[ap.engine].push(ap);
    });

    // Create tabs for each engine
    const tabs = Object.entries(byEngine).map(([engine, patterns]) => {
      // Group patterns by destination table within this engine
      const byDest = {};
      patterns.forEach(ap => {
        const key = ap.destTable;
        if (!byDest[key]) byDest[key] = { engine: ap.engine, destTable: ap.destTable, patterns: [] };
        byDest[key].patterns.push(ap);
      });
      const destGroups = Object.values(byDest);

      return {
        id: engine,
        label: `${ENGINE_LABELS[engine] || engine}${engine === cacheOverlay?.engine ? t('cache-layer.engine-badge-suffix') : ''}`,
        content: (
          <SpaceBetween size="l">
            {destGroups.map((dg, idx) => {
              const destInfo = destTableLookup[`${dg.engine}::${dg.destTable}`];
              const def = destInfo?.def;
              const allSources = destInfo?.allSourceTables || [];
              const otherSources = allSources.filter(t => t !== group.table);
              const pk = def?.partition_key;
              const sk = def?.sort_key;
              const gsis = def?.gsis || def?.gsi_indexes || [];
              const fields = def?.field_mappings || def?.mappings?.properties;
              const fieldCount = Array.isArray(fields) ? fields.length : (fields ? Object.keys(fields).length : 0);
              const shards = def?.settings?.number_of_shards;

              return (
                <Container key={idx}>
                  <SpaceBetween size="m">
                    {/* Table design summary */}
                    <KeyValuePairs
                      columns={4}
                      items={[
                        {
                          label: t('analysis-results.source-table.table'),
                          value: dg.destTable
                        },
                        {
                          label: t('analysis-results.source-table.patterns'),
                          value: dg.patterns.length
                        },
                        ...(pk ? [{
                          label: t('analysis-results.pattern-detail.partition-key'),
                          value: <><code>{pk.attribute_name}</code> ({pk.attribute_type})</>
                        }] : []),
                        ...(sk ? [{
                          label: t('analysis-results.pattern-detail.sort-key'),
                          value: <><code>{sk.attribute_name}</code> ({sk.attribute_type})</>
                        }] : []),
                        ...(gsis.length > 0 ? [{
                          label: 'GSI Indexes',
                          value: `${gsis.length} GSI${gsis.length !== 1 ? 's' : ''}: ${gsis.map(g => g.gsi_name || g.index_name).join(', ')}`
                        }] : []),
                        ...(shards != null ? [{
                          label: 'Shards & Fields',
                          value: `${shards} shard${shards !== 1 ? 's' : ''} · ${fieldCount} fields`
                        }] : [])
                      ]}
                    />

                    {/* Consolidation: other source tables merging here */}
                    {otherSources.length > 0 && (
                      <KeyValuePairs
                        columns={4}
                        items={[
                          {
                            label: 'Consolidates with',
                            value: otherSources.join(', ')
                          }
                        ]}
                      />
                    )}

                    {/* Access patterns */}
                    <Table
                      ariaLabels={{ tableLabel: t('analysis-results.explorer.table-patterns') }}
                      columnDefinitions={[
                        {
                          id: 'pattern_id',
                          header: 'Pattern',
                          cell: item => (
                            <Link onFollow={() => handlePatternClick(item)}>{item.id}</Link>
                          ),
                          width: 120
                        },
                        {
                          id: 'operation',
                          header: 'Operation',
                          cell: item => item.operation,
                          width: 150
                        },
                        ...(dg.engine === 'dynamodb' ? [{
                          id: 'gsi',
                          header: 'GSI',
                          cell: item => item.gsiName || '—',
                          width: 150
                        }] : []),
                        {
                          id: 'description',
                          header: 'Description',
                          cell: item => item.description
                        }
                      ]}
                      items={dg.patterns}
                      variant="embedded"
                      empty={
                        <Box textAlign="center" color="inherit">
                          {t('analysis-results.source-table.no-patterns')}
                        </Box>
                      }
                    />
                  </SpaceBetween>
                </Container>
              );
            })}
          </SpaceBetween>
        )
      };
    });

    return <Tabs tabs={tabs} />;
  };


  // ============================================
  // Render: Engine filter bar
  // ============================================

  const engineOptions = useMemo(() => {
    const engines = Object.entries(filteredEngineDist);
    const options = engines.map(([engine, count]) => ({
      label: `${ENGINE_LABELS[engine] || engine} (${count})`,
      value: engine
    }));
    // #429 part 2: "ElastiCache (cache layer)" gets its own filter option --
    // without this, there's no way to filter the explorer down to just the
    // cache layer's rows (or exclude them) the way every owner engine can be.
    if (cacheOverlay?.engine) {
      const cacheEngine = cacheOverlay.engine;
      options.push({
        label: `${ENGINE_LABELS[cacheEngine] || cacheEngine}${t('cache-layer.engine-badge-suffix')} (${filteredCacheLayerPatterns.length})`,
        value: cacheEngine
      });
    }
    return options;
  }, [filteredEngineDist, cacheOverlay, filteredCacheLayerPatterns, t]);

  const enginePieData = useMemo(() => {
    const engines = Object.entries(filteredEngineDist);
    return engines.map(([engine, count]) => ({
      id: engine,
      title: ENGINE_LABELS[engine] || engine,
      value: count,
      color: ENGINE_HEX[engine] || '#5f6b7a',
      engine: engine
    }));
  }, [filteredEngineDist]);

  const handleEngineChartClick = useCallback((event) => {
    console.log('Engine chart clicked - full event:', event);

    // Try different possible event structures
    let clickedEngine = null;

    if (event.detail?.id) {
      clickedEngine = event.detail.id;
    } else if (event.detail?.datum?.id) {
      clickedEngine = event.detail.datum.id;
    } else if (event.detail?.data?.id) {
      clickedEngine = event.detail.data.id;
    } else if (event.id) {
      clickedEngine = event.id;
    }

    console.log('Extracted engine:', clickedEngine);

    if (clickedEngine) {
      // Add to PropertyFilter instead of Multiselect
      const newToken = {
        propertyKey: 'engine',
        operator: '=',
        value: clickedEngine
      };

      // Check if this filter already exists
      const existingTokens = filterQuery.tokens || [];
      const alreadyExists = existingTokens.some(
        t => t.propertyKey === 'engine' && t.value === clickedEngine
      );

      if (!alreadyExists) {
        console.log('Adding filter token:', newToken);
        setFilterQuery({
          tokens: [...existingTokens, newToken],
          operation: filterQuery.operation || 'and'
        });
        setCurrentPage(1);
      } else {
        console.log('Filter already exists');
      }
    } else {
      console.error('Could not extract engine from event');
    }
  }, [filterQuery]);

  const renderEngineBar = () => {
    if (enginePieData.length === 0) return null;

    return (
      <>
        <Multiselect
          selectedOptions={engineFilter}
          onChange={({ detail }) => {
            setEngineFilter(detail.selectedOptions);
            setCurrentPage(1);
          }}
          options={engineOptions}
          placeholder="Select engines"
          filteringType="auto"
          tokenLimit={3}
        />
        <Box margin={{ top: 's' }}>
          <div style={{ cursor: 'pointer' }} onClick={(e) => {
            // Get the clicked element
            const target = e.target;

            // Try to find if we clicked on a legend item or segment
            let legendText = null;

            // Check if clicked on legend text
            if (target.tagName === 'text' || target.closest('text')) {
              const textElement = target.tagName === 'text' ? target : target.closest('text');
              legendText = textElement.textContent;
            }

            // Check if clicked on a path (segment)
            if (target.tagName === 'path' || target.closest('path')) {
              // Try to find associated legend text from the chart data
              const pathElement = target.tagName === 'path' ? target : target.closest('path');
              const fill = pathElement.getAttribute('fill');

              // Find the engine by color
              const engine = enginePieData.find(e => e.color === fill);
              if (engine) {
                legendText = engine.title;
              }
            }

            if (legendText) {
              // Find the engine from the legend text
              const engine = enginePieData.find(e => e.title === legendText);
              if (engine) {
                console.log('Clicked engine:', engine.id);

                // Add to PropertyFilter
                const newToken = {
                  propertyKey: 'engine',
                  operator: '=',
                  value: engine.id
                };

                // Check if this filter already exists
                const existingTokens = filterQuery.tokens || [];
                const alreadyExists = existingTokens.some(
                  t => t.propertyKey === 'engine' && t.value === engine.id
                );

                if (!alreadyExists) {
                  console.log('Adding filter token:', newToken);
                  setFilterQuery({
                    tokens: [...existingTokens, newToken],
                    operation: filterQuery.operation || 'and'
                  });
                  setCurrentPage(1);
                }
              }
            }
          }}>
            <PieChart
              data={enginePieData}
              variant="pie"
              size="medium"
              hideFilter
              hideLegend={false}
              segmentDescription={(datum, sum) => `${datum.value} patterns, ${((datum.value / sum) * 100).toFixed(0)}%`}
              detailPopoverContent={(datum, sum) => [
                { key: 'Engine', value: datum.title },
                { key: 'Patterns', value: datum.value },
                { key: 'Percentage', value: `${((datum.value / sum) * 100).toFixed(1)}%` }
              ]}
              empty={<Box textAlign="center" color="inherit">{t('common.labels.no-data')}</Box>}
              noMatch={<Box textAlign="center" color="inherit">{t('common.labels.no-data')}</Box>}
              ariaLabel="Engine distribution"
            />
          </div>
        </Box>
        {/* #429 part 2: the cache layer stays out of the pie's shares (it
            never owned a query) but still needs to be visible here -- a
            legend-style entry with its own count, next to the pie rather
            than a slice inside it, so the owner shares still read as 100% of
            owned workload. */}
        {cacheOverlay?.engine && (
          <Box margin={{ top: 'xs' }} fontSize="body-s" color="text-body-secondary">
            <span
              aria-hidden="true"
              style={{
                display: 'inline-block',
                width: '10px',
                height: '10px',
                borderRadius: '2px',
                backgroundColor: ENGINE_HEX[cacheOverlay.engine] || '#5f6b7a',
                marginRight: '6px',
                verticalAlign: 'middle',
              }}
            />
            {`${ENGINE_LABELS[cacheOverlay.engine] || cacheOverlay.engine}${t('cache-layer.engine-badge-suffix')}: ${filteredCacheLayerPatterns.length}`}
          </Box>
        )}
      </>
    );
  };


  // ============================================
  // Loading state
  // ============================================

  if (loading && !resultsData) {
    return (
      <>
        <AppHeader />
        <AppLayoutToolbar
          disableContentPaddings={false}
          navigationOpen={navigationOpen}
          onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
          ariaLabels={layoutAriaLabels}
          breadcrumbs={<BreadcrumbGroup items={breadcrumbItems} />}
          navigation={
            <SideNavigation
              activeHref={`/analysis/results/${jobId}`}
              header={SideNavigationConfigurations.header}
              items={SideNavigationConfigurations.items}
            />
          }
          content={
            <Box textAlign="center" padding={{ top: 'xxxl' }}>
              <Spinner size="large" />
              <Box margin={{ top: 'm' }} color="text-body-secondary">{t('analysis-results.states.loading')}</Box>
            </Box>
          }
          toolsHide
        />
      </>
    );
  }


  // ============================================
  // Main render
  // ============================================

  return (
    <>
      <AppHeader />
      <AppLayoutToolbar
        disableContentPaddings={false}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        ariaLabels={layoutAriaLabels}
        breadcrumbs={<BreadcrumbGroup items={breadcrumbItems} />}
        navigation={
          <SideNavigation
            activeHref={`/analysis/results/${jobId}`}
            header={SideNavigationConfigurations.header}
            items={SideNavigationConfigurations.items}
          />
        }
        content={
          <SpaceBetween size="l">

            {flashbarItems.length > 0 && (
              <Flashbar items={flashbarItems.map(item => ({ ...item, onDismiss: () => handleFlashbarDismiss(item.id) }))} />
            )}

            <Header
              variant="h1"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button iconName="refresh" variant="normal" onClick={fetchData} loading={loading} ariaLabel={t('common.actions.refresh')} />
                  <Button href={`/analysis/monitor/summary/${jobId}`}>{t('analysis-results.actions.back-to-assessment')}</Button>
                </SpaceBetween>
              }
            >
              {t('analysis-results.header.title')}
            </Header>

            {/* Deliverables (#957, #1028): three download buttons, first. */}
            <DeliverablesPanel
              files={deliverables.files}
              loading={deliverables.loading}
              error={deliverables.error}
              baseUrl={ApiConfigurations.baseUrl}
              t={t}
            />

            {/* Executive Summary. #1028: the access-pattern count sits below
                the database name; a cost tile with no figure is left out. */}
            <Container id="executive-summary-section" header={<Header variant="h2">{t('analysis-results.executive-summary.title')}</Header>}>
              <SpaceBetween size="m">
                <Box variant="p" fontSize="body-m">
                  {synthesis?.summary || 'No summary available.'}
                </Box>
                {summaryFallback.isFallback && (
                  <SummaryFallbackNotice
                    text={t('analysis-results.executive-summary.fallback-notice', {
                      reason: fallbackWithheldReason(summaryFallback.warnings),
                    })}
                  />
                )}
                <ExecutiveSummaryStats
                  t={t}
                  databaseName={synthesis?.database_name}
                  accessPatternCount={totalAccessPatterns}
                  ownerQueryCount={ownerQueryCount}
                  cachePatternCount={cachePatternCount}
                  projectedCost={projectedCost}
                  costBaseline={costBaseline}
                  retiredEngines={synthesis?.facts?.retired_in_use_engines}
                  queryPatterns={queryPatternCountsOf(synthesis)}
                  targetEngines={(
                    <>
                      <Box fontSize="heading-m">
                        <SpaceBetween direction="horizontal" size="xxs">
                          {/* #358: the cache layer is listed here too (labeled
                              distinctly, e.g. "ElastiCache (cache layer)") -- it
                              owns no queries, but it is still a target engine
                              the deliverables name and price. */}
                          {targetEngines.map(({ engine, isCacheLayer }) => (
                            <Badge key={engine} color={ENGINE_BADGE_COLORS[engine] || 'grey'}>
                              {ENGINE_LABELS[engine] || engine}
                              {isCacheLayer ? t('cache-layer.engine-badge-suffix', { defaultValue: ' (cache layer)' }) : ''}
                            </Badge>
                          ))}
                        </SpaceBetween>
                      </Box>
                      {/* #296: additional cache-layer stats (reads fronted, %
                          of calls) shown as a note below the badges above. */}
                      {cacheLayerLine && (
                        <Box fontSize="body-s" color="text-body-secondary" padding={{ top: 'xxs' }}>
                          {cacheLayerLine}
                        </Box>
                      )}
                      {/* #424: the gate's cache_overlay (assigned at the
                          assignment gate) and this one (after schema design)
                          can disagree -- the post-schema-design safety net
                          drops cached reads the design doesn't cover. Each
                          note already states its own before/after numbers and
                          the reason, so it is shown verbatim, not summarized.
                          #459: cacheOverlayNotes reads safety_net_notes only --
                          never the full notes field, which also carries
                          customer-edit notes (full query hashes) and the
                          legacy-migration note. */}
                      {cacheOverlayNotes(cacheOverlay).map((note, idx) => (
                        <Box
                          key={idx}
                          fontSize="body-s"
                          color="text-status-warning"
                          padding={{ top: 'xxs' }}
                        >
                          {note}
                        </Box>
                      ))}
                    </>
                  )}
                />
              </SpaceBetween>
            </Container>

            {/* Query Flow (Sankey) */}
            {sankeyData && (
              <Container id="query-flow-section" header={
                <Header variant="h2" description={t('analysis-results.query-flow.description')}>
                  {t('analysis-results.query-flow.title')}
                </Header>
              }>
                <ChartSankey
                  width={900}
                  height={Math.max(250, Object.keys(afterDist).length * 120)}
                  data={sankeyData}
                />
              </Container>
            )}

            {/* Cost Breakdown */}
            {costBreakdown.length > 0 && (
              <Container id="cost-breakdown-section" header={<Header variant="h2" description={t('analysis-results.cost-breakdown.description')}>{t('analysis-results.cost-breakdown.title')}</Header>}>
                <SpaceBetween size="m">
                  <ColumnLayout columns={costBreakdown.length} variant="text-grid">
                    {costBreakdown.map((cb, idx) => {
                      // #358: the cache layer's card is labeled the same way
                      // as its "Target engines" badge, so the two sections
                      // agree on what ElastiCache is here.
                      const isCacheLayer = cb.database === cacheOverlay?.engine;
                      return (
                        <Box key={idx} textAlign="center">
                          <Badge color={ENGINE_BADGE_COLORS[cb.database] || 'grey'}>
                            {ENGINE_LABELS[cb.database] || cb.database}
                            {isCacheLayer ? t('cache-layer.engine-badge-suffix', { defaultValue: ' (cache layer)' }) : ''}
                          </Badge>
                          <Box fontSize="display-l" fontWeight="bold" margin={{ top: 'xs' }}>
                            ${cb.monthly_cost_usd?.toFixed(2)}
                          </Box>
                          <Box fontSize="body-s" color="text-body-secondary">/month · {cb.pricing_mode}</Box>
                        </Box>
                      );
                    })}
                  </ColumnLayout>
                  {synthesis?.tco_analysis?.assumptions?.length > 0 && (
                    <Box fontSize="body-s" color="text-body-secondary">
                      {synthesis.tco_analysis.assumptions.join(' · ')}
                    </Box>
                  )}
                </SpaceBetween>
              </Container>
            )}


            {/* Migration roadmap (#957): report.json's waves, the same ones the
                PDF and the engineering report show. */}
            <MigrationWavePlan synthesis={synthesis} t={t} />

            {/* Reality-check moves no model reviewed (#746): one line when any. */}
            <UnreviewedMovesNote synthesis={resultsData?.synthesis} t={t} />
            <EngineChoicesNote synthesis={resultsData?.synthesis} t={t} />

            {/* Queries on a fallback engine (#729): where each landed and why,
                its gates, a redesign hint and the assignment review that
                re-runs. Warning first, as in both reports. */}
            <UnservableQueriesCallout
              synthesis={resultsData?.synthesis}
              jobId={jobId}
              onOpen={fetchQueryJourney}
              t={t}
            />

            {/* Close calls (#730): queries a kept engine could serve almost
                as well, with the way into each one's decision trace and the
                assignment review that moves them. */}
            <CloseCallCallout
              synthesis={resultsData?.synthesis}
              jobId={jobId}
              onOpen={fetchQueryJourney}
              t={t}
            />

            {/* Query routing (#822): every query, and the way into its
                journey and decision trace without schema design. #1028:
                collapsed when the page opens. */}
            <QueryRoutingTable
              rows={queryRoutingRows}
              loading={queryJourneyList.loading}
              error={queryJourneyList.error}
              hasLedger={routingHistoryRecorded(resultsData?.synthesis)}
              truncated={queryJourneyList.truncated}
              onOpen={fetchQueryJourney}
              t={t}
            />

            {/* ============================================ */}
            {/* Access Pattern Explorer                      */}
            {/* ============================================ */}
            {(totalAccessPatterns > 0 || cachePatternCount > 0) && (
              <Container
                id="access-patterns-section"
                header={
                  <Header
                    variant="h2"
                    // #429 part 2: once there's a cache layer, the header
                    // breaks the combined total down into owned vs. cache
                    // layer ("64 (54 owned + 10 cache layer)") instead of
                    // just a bare count -- filtering still falls back to the
                    // simpler "(N of M)" form, same as before #429.
                    counter={
                      hasActiveFilters
                        ? `(${filteredOwnerPatterns.length + filteredCacheLayerPatterns.length} of ${explorerTotal})`
                        : cachePatternCount > 0
                          ? `(${t('analysis-results.explorer.header-count', {
                              count: explorerTotal,
                              total: explorerTotal,
                              owned: totalAccessPatterns,
                              cache: cachePatternCount,
                            })})`
                          : `(${totalAccessPatterns})`
                    }
                    description={t('analysis-results.explorer.description')}
                    actions={
                      <SpaceBetween direction="horizontal" size="xs">
                        <SegmentedControl
                          selectedId={browseMode}
                          onChange={({ detail }) => { setBrowseMode(detail.selectedId); setCurrentPage(1); setSelectedItems([]); }}
                          options={[
                            { id: 'pattern', text: t('analysis-results.explorer.by-access-pattern') },
                            { id: 'source', text: t('analysis-results.explorer.by-source-table') },
                          ]}
                        />
                      </SpaceBetween>
                    }
                  >
                    {t('analysis-results.explorer.title')}
                  </Header>
                }
              >
                <SpaceBetween size="m">

                  {/* Visual navigators: engine bar + operation donut */}
                  <ColumnLayout columns={2}>
                    <Box>
                      <Box variant="awsui-key-label" margin={{ bottom: 'xs' }}>{t('analysis-results.explorer.filter-by-engine')}</Box>
                      {renderEngineBar()}
                    </Box>
                    <Box>
                      <Box variant="awsui-key-label" margin={{ bottom: 'xs' }}>{t('analysis-results.explorer.filter-by-operation-type')}</Box>
                      <Multiselect
                        selectedOptions={opFilter}
                        onChange={({ detail }) => {
                          setOpFilter(detail.selectedOptions);
                          setCurrentPage(1);
                        }}
                        options={operationOptions}
                        placeholder="Select operations"
                        filteringType="auto"
                        tokenLimit={3}
                      />
                      <Box margin={{ top: 's' }}>
                        <div style={{ cursor: 'pointer' }} onClick={(e) => {
                          // Get the clicked element
                          const target = e.target;

                          // Try to find if we clicked on a legend item or segment
                          let legendText = null;

                          // Check if clicked on legend text
                          if (target.tagName === 'text' || target.closest('text')) {
                            const textElement = target.tagName === 'text' ? target : target.closest('text');
                            legendText = textElement.textContent;
                          }

                          // Check if clicked on a path (segment)
                          if (target.tagName === 'path' || target.closest('path')) {
                            // Try to find associated legend text from the chart data
                            const pathElement = target.tagName === 'path' ? target : target.closest('path');
                            const fill = pathElement.getAttribute('fill');

                            // Find the operation by color
                            const operation = donutData.find(o => o.color === fill);
                            if (operation) {
                              legendText = operation.title;
                            }
                          }

                          if (legendText) {
                            // Find the operation from the legend text
                            const operation = donutData.find(o => o.title === legendText);
                            if (operation) {
                              console.log('Clicked operation:', operation.id);

                              // Add to PropertyFilter
                              const newToken = {
                                propertyKey: 'op_type',
                                operator: '=',
                                value: operation.id
                              };

                              // Check if this filter already exists
                              const existingTokens = filterQuery.tokens || [];
                              const alreadyExists = existingTokens.some(
                                t => t.propertyKey === 'op_type' && t.value === operation.id
                              );

                              if (!alreadyExists) {
                                console.log('Adding operation filter token:', newToken);
                                setFilterQuery({
                                  tokens: [...existingTokens, newToken],
                                  operation: filterQuery.operation || 'and'
                                });
                                setCurrentPage(1);
                              }
                            }
                          }
                        }}>
                          <PieChart
                            data={donutData}
                            variant="donut"
                            size="medium"
                            innerMetricValue={filteredPatterns.length.toString()}
                            innerMetricDescription="patterns"
                            hideFilter
                            hideLegend={false}
                            segmentDescription={(datum, sum) => `${datum.value} patterns, ${((datum.value / sum) * 100).toFixed(0)}%`}
                            detailPopoverContent={(datum, sum) => [
                              { key: 'Operation', value: datum.title },
                              { key: 'Patterns', value: datum.value },
                              { key: 'Percentage', value: `${((datum.value / sum) * 100).toFixed(1)}%` }
                            ]}
                            empty={<Box textAlign="center" color="inherit">{t('common.labels.no-data')}</Box>}
                            noMatch={<Box textAlign="center" color="inherit">{t('common.labels.no-data')}</Box>}
                            ariaLabel="Operation type distribution"
                          />
                        </div>
                      </Box>
                    </Box>
                  </ColumnLayout>

                  {/* Property filter + active filter chips */}
                  <PropertyFilter
                    query={filterQuery}
                    onChange={({ detail }) => { setFilterQuery(detail); setCurrentPage(1); }}
                    filteringProperties={filterProperties}
                    filteringOptions={filterOptions}
                    filteringPlaceholder={t('analysis-results.explorer.filter-placeholder')}
                    countText={t('analysis-results.explorer.count-text', { count: filteredPatterns.length })}
                    expandToViewport
                    i18nStrings={{
                      filteringAriaLabel: t('analysis-results.explorer.filtering-aria-label'),
                      dismissAriaLabel: t('analysis-results.explorer.dismiss-aria-label'),
                      filteringPlaceholder: t('analysis-results.explorer.filter-placeholder'),
                      groupValuesText: t('analysis-results.explorer.group-values-text'),
                      groupPropertiesText: t('analysis-results.explorer.group-properties-text'),
                      operatorsText: t('analysis-results.explorer.operators-text'),
                      operationAndText: t('analysis-results.explorer.operation-and-text'),
                      operationOrText: t('analysis-results.explorer.operation-or-text'),
                      operatorLessText: t('analysis-results.explorer.operator-less-text'),
                      operatorLessOrEqualText: t('analysis-results.explorer.operator-less-or-equal-text'),
                      operatorGreaterText: t('analysis-results.explorer.operator-greater-text'),
                      operatorGreaterOrEqualText: t('analysis-results.explorer.operator-greater-or-equal-text'),
                      operatorContainsText: t('analysis-results.explorer.operator-contains-text'),
                      operatorDoesNotContainText: t('analysis-results.explorer.operator-does-not-contain-text'),
                      operatorEqualsText: t('analysis-results.explorer.operator-equals-text'),
                      operatorDoesNotEqualText: t('analysis-results.explorer.operator-does-not-equal-text'),
                      editTokenHeader: t('analysis-results.explorer.edit-token-header'),
                      propertyText: t('analysis-results.explorer.property-text'),
                      operatorText: t('analysis-results.explorer.operator-text'),
                      valueText: t('analysis-results.explorer.value-text'),
                      cancelActionText: t('common.actions.cancel'),
                      applyActionText: t('analysis-results.explorer.apply-action-text'),
                      allPropertiesLabel: t('analysis-results.explorer.all-properties-label'),
                      tokenLimitShowMore: t('analysis-results.explorer.token-limit-show-more'),
                      tokenLimitShowFewer: t('analysis-results.explorer.token-limit-show-fewer'),
                      clearFiltersText: t('analysis-results.explorer.clear-filters-text'),
                      removeTokenButtonAriaLabel: (token) => t('analysis-results.explorer.remove-token-aria-label', { propertyLabel: token.propertyLabel, operator: token.operator, value: token.value }),
                      enteredTextLabel: (text) => t('analysis-results.explorer.entered-text-label', { text }),
                    }}
                  />

                  {hasActiveFilters && (
                    <div className="active-filters">
                      <Button variant="link" onClick={clearAllFilters}>{t('analysis-results.explorer.clear-all-filters')}</Button>
                    </div>
                  )}

                  {/* Pagination at top */}
                  {displayItems.length > PAGE_SIZE && (
                    <Box float="right">
                      <Pagination
                        currentPageIndex={currentPage}
                        pagesCount={Math.ceil(displayItems.length / PAGE_SIZE)}
                        onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
                        ariaLabels={explorerPaginationLabels}
                      />
                    </Box>
                  )}


                  {/* ---- Access Pattern view ---- */}
                  {browseMode === 'pattern' && (
                    <Table
                      columnDefinitions={patternColumnDefinitions}
                      items={paginatedItems}
                      loading={loading}
                      variant="embedded"
                      selectionType="single"
                      ariaLabels={explorerSelectionLabels(t('analysis-results.explorer.table-patterns'))}
                      selectedItems={selectedItems}
                      onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
                      empty={
                        <Box textAlign="center" padding="l">
                          <Box variant="strong">{t('analysis-results.explorer.no-matching-patterns')}</Box>
                          <Box variant="p" color="text-body-secondary">{t('analysis-results.explorer.try-adjusting-filters')}</Box>
                        </Box>
                      }
                    />
                  )}


                  {/* ---- Source Table view ---- */}
                  {browseMode === 'source' && (
                    <Table
                      columnDefinitions={sourceTableColumnDefinitions}
                      items={paginatedItems}
                      loading={loading}
                      variant="embedded"
                      selectionType="single"
                      ariaLabels={explorerSelectionLabels(t('analysis-results.explorer.table-sources'))}
                      selectedItems={selectedItems}
                      onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
                      empty={
                        <Box textAlign="center" padding="l">
                          <Box variant="strong">{t('analysis-results.explorer.no-matching-source-tables')}</Box>
                          <Box variant="p" color="text-body-secondary">{t('analysis-results.explorer.try-adjusting-filters')}</Box>
                        </Box>
                      }
                    />
                  )}

                  {/* Bottom pagination */}
                  {displayItems.length > PAGE_SIZE && (
                    <Box float="right">
                      <Pagination
                        currentPageIndex={currentPage}
                        pagesCount={Math.ceil(displayItems.length / PAGE_SIZE)}
                        onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
                        ariaLabels={explorerPaginationLabels}
                      />
                    </Box>
                  )}

                  {/* ============================================ */}
                  {/* Cache layer section (#429 part 2)             */}
                  {/* ============================================ */}
                  {/* The cache layer's own access patterns/key designs, as a
                      distinct group after the owner rows above -- in both
                      browse modes -- instead of mixed into the owner table
                      or left out of the explorer entirely (#361/#405 only
                      excluded them from the owner total/pie). Hidden
                      whenever the current filters leave no cache-layer rows
                      (e.g. the engine filter is set to an owner engine only),
                      same as the owner table's own empty state. */}
                  {browseMode === 'pattern' && filteredCacheLayerPatterns.length > 0 && (
                    <Box margin={{ top: 'l' }}>
                      <Header
                        variant="h3"
                        counter={`(${filteredCacheLayerPatterns.length})`}
                      >
                        {t('analysis-results.explorer.cache-layer-section-title', {
                          engine: ENGINE_LABELS[cacheOverlay?.engine] || cacheOverlay?.engine,
                        })}
                      </Header>
                      <Table
                        columnDefinitions={patternColumnDefinitions}
                        items={filteredCacheLayerPatterns}
                        variant="embedded"
                        selectionType="single"
                        ariaLabels={explorerSelectionLabels(t('analysis-results.explorer.table-cache-patterns'))}
                        selectedItems={selectedItems}
                        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
                      />
                    </Box>
                  )}

                  {browseMode === 'source' && cacheSourceTableGroups.length > 0 && (
                    <Box margin={{ top: 'l' }}>
                      <Header
                        variant="h3"
                        counter={`(${cacheSourceTableGroups.length})`}
                      >
                        {t('analysis-results.explorer.cache-layer-section-title', {
                          engine: ENGINE_LABELS[cacheOverlay?.engine] || cacheOverlay?.engine,
                        })}
                      </Header>
                      <Table
                        columnDefinitions={sourceTableColumnDefinitions}
                        items={cacheSourceTableGroups}
                        variant="embedded"
                        selectionType="single"
                        ariaLabels={explorerSelectionLabels(t('analysis-results.explorer.table-cache-sources'))}
                        selectedItems={selectedItems}
                        onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
                      />
                    </Box>
                  )}
                </SpaceBetween>
              </Container>
            )}

            {/* Sources of truth and replicas (#992): each table read on a
                replica, its CDC direction and its replica reads, with the
                eventual-consistency risk stated once. #1232: collapsed, after
                the access pattern explorer. */}
            <SourcesOfTruthCallout synthesis={synthesis} t={t} />

            {/* Risk Assessment (#373): synthesis.risk_assessment, including
                any CRITICAL risk, with one tab per severity (always present,
                even empty) and the risks the assignment already resolved.
                An UNRATED tab (#270 round-1 review) joins them only when a
                risk's severity is none of the four known ones -- matching
                the deck's "only show UNRATED when present" rule, rather than
                an always-empty fifth tab on every ordinary run. */}
            {!(openRisks.length > 0 || resolvedRisksList.length > 0 || overallRiskLevel) && (
              <NoOpenRisksSection
                synthesis={resultsData?.synthesis}
                caveat={resultsData?.no_open_risks_caveat}
                title={t('analysis-results.risk-assessment.title')}
              />
            )}
            {(openRisks.length > 0 || resolvedRisksList.length > 0 || overallRiskLevel) && (
              <Container header={<Header variant="h2" description={t('analysis-results.risk-assessment.description')}>{t('analysis-results.risk-assessment.title')}</Header>}>
                <SpaceBetween size="m">
                  {overallRiskLevel && (
                    <StatusIndicator type={riskSeverityStatus(overallRiskLevel)}>
                      {t('analysis-results.risk-assessment.overall-risk')}: {overallRiskLevel}
                    </StatusIndicator>
                  )}
                  <NoOpenRisksNote
                    synthesis={resultsData?.synthesis}
                    shownRisks={openRisks}
                    caveat={resultsData?.no_open_risks_caveat}
                  />
                  <Tabs
                    tabs={[
                      ...RISK_SEVERITIES,
                      ...((risksBySeverity[UNRATED] || []).length > 0 ? [UNRATED] : []),
                    ].map((severity) => {
                      const risksAtSeverity = risksBySeverity[severity] || [];
                      const severityCount = openRiskCountAt(synthesis, severity, risksAtSeverity);
                      const status = riskSeverityStatus(severity);
                      return {
                        id: severity,
                        label: t('analysis-results.risk-assessment.tab-label', { severity, count: severityCount }),
                        content: risksAtSeverity.length === 0 ? (
                          <Box color="text-body-secondary" padding="s">
                            {t('analysis-results.risk-assessment.no-risks-at-severity')}
                          </Box>
                        ) : (
                          <SpaceBetween size="s">
                            {risksAtSeverity.map((risk, idx) => {
                              const { engine, body } = splitRiskDescription(risk.description);
                              const showMitigation = risk.mitigation
                                && !mitigationRepeatsDescription(risk.mitigation, body);
                              const affectedTables = Array.isArray(risk.affected_tables) ? risk.affected_tables : [];
                              return (
                                <Box
                                  key={risk.risk_id || idx}
                                  padding={{ vertical: 'xs', horizontal: 's' }}
                                  style={{ borderLeft: `4px solid ${RISK_BORDER_COLOR[status]}`, backgroundColor: '#fff', borderRadius: '4px' }}
                                >
                                  <SpaceBetween size="xxs">
                                    <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                                      <StatusIndicator type={status}>{severity}</StatusIndicator>
                                      {engine !== '(general)' && (
                                        <Badge color={ENGINE_BADGE_COLORS[engine] || 'grey'}>
                                          {ENGINE_LABELS[engine] || engine}
                                        </Badge>
                                      )}
                                      {risk.risk_id && (
                                        <Box fontSize="body-s" color="text-body-secondary">{risk.risk_id}</Box>
                                      )}
                                    </SpaceBetween>
                                    <Box fontSize="body-s">{body}</Box>
                                    {showMitigation && (
                                      <Box fontSize="body-s" color="text-body-secondary">
                                        <b>{t('analysis-results.risk-assessment.mitigation')}:</b> {risk.mitigation}
                                      </Box>
                                    )}
                                    {affectedTables.length > 0 && (
                                      <Box fontSize="body-s" color="text-body-secondary">
                                        {t('analysis-results.risk-assessment.affected-tables')}: {affectedTables.join(', ')}
                                      </Box>
                                    )}
                                    {Array.isArray(risk.details) && risk.details.length > 0 && (
                                      <ul className="risk-details" style={{ margin: 0, paddingLeft: '1.25em' }}>
                                        {risk.details.map((detail, i) => (
                                          <li key={i}><Box fontSize="body-s" color="text-body-secondary">{detail}</Box></li>
                                        ))}
                                      </ul>
                                    )}
                                  </SpaceBetween>
                                </Box>
                              );
                            })}
                          </SpaceBetween>
                        ),
                      };
                    })}
                  />
                  {resolvedRisksList.length > 0 && (
                    <ExpandableSection headerText={
                      resolvedRisksBySeverityText(synthesis, t)
                        ? t('analysis-results.risk-assessment.resolved-count-by-severity', { count: resolvedRiskCountOf(synthesis, resolvedRisksList), severities: resolvedRisksBySeverityText(synthesis, t) })
                        : t('analysis-results.risk-assessment.resolved-count', { count: resolvedRiskCountOf(synthesis, resolvedRisksList) })
                    }>
                      <SpaceBetween size="s">
                        {resolvedRisksList.map((risk, idx) => {
                          const { body } = splitRiskDescription(risk.description);
                          const fromEngine = ENGINE_LABELS[risk.engine] || risk.engine;
                          const toEngine = ENGINE_LABELS[risk.resolved_on] || risk.resolved_on;
                          return (
                            <Box key={risk.risk_id || idx} padding={{ vertical: 'xs', horizontal: 's' }}>
                              <SpaceBetween size="xxs">
                                <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                                  <StatusIndicator type={riskSeverityStatus(risk.severity)}>{risk.severity}</StatusIndicator>
                                  {risk.resolved_on ? (
                                    <Box fontSize="body-s" color="text-body-secondary">
                                      {t('analysis-results.risk-assessment.resolved-where', { from: fromEngine, to: toEngine })}
                                    </Box>
                                  ) : (
                                    <Box fontSize="body-s" color="text-body-secondary">{fromEngine}</Box>
                                  )}
                                </SpaceBetween>
                                <Box fontSize="body-s">{body}</Box>
                                {risk.reason && (
                                  <Box fontSize="body-s" color="text-body-secondary">
                                    {t('analysis-results.risk-assessment.resolved-reason', { reason: risk.reason })}
                                  </Box>
                                )}
                                {Array.isArray(risk.affected_tables) && risk.affected_tables.length > 0 && (
                                  <Box fontSize="body-s" color="text-body-secondary">
                                    {t('analysis-results.risk-assessment.affected-tables')}: {risk.affected_tables.join(', ')}
                                  </Box>
                                )}
                              </SpaceBetween>
                            </Box>
                          );
                        })}
                      </SpaceBetween>
                    </ExpandableSection>
                  )}
                </SpaceBetween>
              </Container>
            )}


            {/* Engine-specific Trade-offs */}
            {Object.entries(engineTradeOffs).some(([, tradeOffList]) => tradeOffList.length > 0) && (
              <Container
                header={<Header variant="h2">{t('analysis-results.tradeoffs.title')}</Header>}
              >
                <Tabs
                  tabs={Object.entries(engineTradeOffs)
                    .filter(([, tradeOffList]) => tradeOffList.length > 0)
                    .map(([engine, tradeoffs]) => {
                      // Separate PE notes from design decisions
                      const normalized = tradeoffs.map(tradeOff =>
                        typeof tradeOff === 'object' && tradeOff !== null && tradeOff.description
                          ? tradeOff
                          : { description: String(tradeOff), impact: '', source_tables: [], target_tables: [], query_ids: [] }
                      );
                      const peNotes = normalized.filter(tradeOff => tradeOff.description.startsWith('[PE note]'));
                      const decisions = normalized.filter(tradeOff => !tradeOff.description.startsWith('[PE note]'));

                      return {
                        id: engine,
                        label: tradeOffTabLabel(engine, tradeoffs.length, t, ENGINE_LABELS),
                        content: (
                          <SpaceBetween size="m">
                            {/* Design Decisions */}
                            {decisions.length > 0 && (
                              <SpaceBetween size="xs">
                                {decisions.map((tradeOff, idx) => (
                                  <Box key={idx} padding={{ vertical: 'xs', horizontal: 's' }}
                                    style={{ borderLeft: '3px solid #0972d3', backgroundColor: '#f2f8fd', borderRadius: '4px' }}>
                                    <SpaceBetween size="xxs">
                                      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                                        <Icon name="status-positive" />
                                        <Link variant="primary" fontSize="body-s" fontWeight="bold" onFollow={() => {}} external={false}>
                                          <span style={{ textDecoration: 'underline' }}>{tradeOff.description}</span>
                                        </Link>
                                      </SpaceBetween>
                                      {tradeOff.impact && (
                                        <Box fontSize="body-s" color="text-body-secondary">{tradeOff.impact}</Box>
                                      )}
                                      {(tradeOff.source_tables?.length > 0 || tradeOff.target_tables?.length > 0) && (
                                        <Box fontSize="body-s" padding={{ left: 'xl' }}>
                                          <Link variant="secondary" fontSize="body-s" onFollow={() => {}}>{t('analysis-results.tradeoffs.mapping')}</Link>
                                          <Box margin={{ top: 'xxxs' }}>
                                            <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
                                              {tradeOff.source_tables?.length > 0 && (
                                                <SpaceBetween direction="horizontal" size="xxs">
                                                  {tradeOff.source_tables.map((table, ti) => (
                                                    <span key={ti} style={{
                                                      display: 'inline-block',
                                                      padding: '1px 4px',
                                                      fontSize: '10px',
                                                      backgroundColor: '#0972d3',
                                                      color: '#fff',
                                                      borderRadius: '2px',
                                                      fontWeight: '500'
                                                    }}>
                                                      {table}
                                                    </span>
                                                  ))}
                                                </SpaceBetween>
                                              )}
                                              {tradeOff.source_tables?.length > 0 && tradeOff.target_tables?.length > 0 && (
                                                <span style={{ fontSize: '11px', fontWeight: 'bold', padding: '0 2px' }}>→</span>
                                              )}
                                              {tradeOff.target_tables?.length > 0 && (
                                                <SpaceBetween direction="horizontal" size="xxs">
                                                  {tradeOff.target_tables.map((table, ti) => (
                                                    <span key={ti} style={{
                                                      display: 'inline-block',
                                                      padding: '1px 4px',
                                                      fontSize: '10px',
                                                      backgroundColor: '#d13212',
                                                      color: '#fff',
                                                      borderRadius: '2px',
                                                      fontWeight: '500'
                                                    }}>
                                                      {table}
                                                    </span>
                                                  ))}
                                                </SpaceBetween>
                                              )}
                                            </SpaceBetween>
                                          </Box>
                                        </Box>
                                      )}
                                      {tradeOff.query_ids?.length > 0 && (
                                        <Box fontSize="body-s" margin={{ top: 's' }} padding={{ left: 'xl' }}>
                                          <Link variant="secondary" fontSize="body-s" onFollow={() => {}}>{t('analysis-results.tradeoffs.sql-ids')}</Link>
                                          <Box margin={{ top: 'xxxs' }}>
                                            <SpaceBetween direction="horizontal" size="xxs">
                                              {tradeOff.query_ids.map((qid, qi) => (
                                                <span key={qi}
                                                  role="button"
                                                  tabIndex={0}
                                                  aria-label={t('analysis-results.query-routing.open-label', { id: qid })}
                                                  onClick={() => fetchQueryJourney(qid)}
                                                  onKeyDown={(e) => {
                                                    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fetchQueryJourney(qid); }
                                                  }}
                                                  style={{
                                                    display: 'inline-block',
                                                    padding: '1px 4px',
                                                    fontSize: '10px',
                                                    fontFamily: 'monospace',
                                                    backgroundColor: '#e9ebed',
                                                    borderRadius: '2px',
                                                    cursor: 'pointer',
                                                    transition: 'background-color 0.15s',
                                                  }}
                                                  onMouseEnter={e => { e.target.style.backgroundColor = '#0972d3'; e.target.style.color = '#fff'; }}
                                                  onMouseLeave={e => { e.target.style.backgroundColor = '#e9ebed'; e.target.style.color = ''; }}
                                                  onFocus={e => { e.target.style.backgroundColor = '#0972d3'; e.target.style.color = '#fff'; }}
                                                  onBlur={e => { e.target.style.backgroundColor = '#e9ebed'; e.target.style.color = ''; }}
                                                  title={`View query history for ${qid}`}
                                                >
                                                  {qid}
                                                </span>
                                              ))}
                                            </SpaceBetween>
                                          </Box>
                                        </Box>
                                      )}
                                    </SpaceBetween>
                                  </Box>
                                ))}
                              </SpaceBetween>
                            )}

                            {/* Principal Engineer Notes */}
                            {peNotes.length > 0 && (
                              <ExpandableSection
                                headerText={`Principal Engineer notes (${peNotes.length})`}
                                variant="footer"
                                defaultExpanded={false}
                              >
                                <SpaceBetween size="xs">
                                  {peNotes.map((tradeOff, idx) => (
                                    <Box key={idx} padding={{ vertical: 'xs', horizontal: 's' }}
                                      style={{ borderLeft: '3px solid #ff9900', backgroundColor: '#fff8e6', borderRadius: '4px' }}>
                                      <SpaceBetween size="xxs">
                                        <div style={{ display: 'flex', alignItems: 'flex-start', gap: '8px' }}>
                                          <Badge color="blue">{idx + 1}</Badge>
                                          <Box fontSize="body-s" style={{ flex: 1 }}>{tradeOff.description.replace(/^\[PE note\]\s*/, '')}</Box>
                                        </div>
                                        {tradeOff.impact && (
                                          <Box fontSize="body-s" color="text-body-secondary" padding={{ left: 'xl' }}>{tradeOff.impact}</Box>
                                        )}
                                      </SpaceBetween>
                                    </Box>
                                  ))}
                                </SpaceBetween>
                              </ExpandableSection>
                            )}
                          </SpaceBetween>
                        )
                      };
                    })
                  }
                />
              </Container>
            )}

            {/* Table design (#478; "Aurora design" until #1232, every section
                collapsed when the page opens): Aurora's schema design has no
                access_patterns, so it never shows up in the explorer above --
                this section is its own view of what the design already has:
                tables, DDL, optimizations and the queries the assignment
                routed here. */}
            {auroraDesigns.length > 0 && (
              <Container
                id="table-design-section"
                header={
                  <Header
                    variant="h2"
                    description={t('analysis-results.aurora-design.description')}
                  >
                    {t('analysis-results.aurora-design.title')}
                  </Header>
                }
              >
                <Tabs
                  tabs={auroraDesigns.map(design => {
                    const queryCount = design.queryGroups.reduce((sum, g) => sum + g.queries.length, 0);
                    return {
                      id: design.engine,
                      label: t('analysis-results.aurora-design.tab-label', {
                        name: design.displayName,
                        count: designTableCountOf(synthesis, design.engine, design.tables),
                      }),
                      content: (
                        <SpaceBetween size="l">
                          {design.migrationStrategy && (
                            <Box fontSize="body-s" color="text-body-secondary">
                              {t(
                                `analysis-results.aurora-design.migration-strategy-${design.migrationStrategy}`,
                                { defaultValue: design.migrationStrategy }
                              )}
                            </Box>
                          )}

                          <ExpandableSection
                            headerText={t('analysis-results.aurora-design.tables-header', { count: design.tables.length })}
                            defaultExpanded={false}
                          >
                            {design.tables.length === 0 ? (
                              <Box color="text-body-secondary" fontSize="body-s">{t('analysis-results.aurora-design.no-tables')}</Box>
                            ) : (
                              <SpaceBetween size="s">
                                {design.tables.map(table => (
                                  <ExpandableSection
                                    key={table.tableName}
                                    headerText={table.tableName}
                                    variant="footer"
                                    defaultExpanded={false}
                                  >
                                    <SpaceBetween size="s">
                                      <Table
                                        variant="embedded"
                                        columnDefinitions={[
                                          { id: 'name', header: t('analysis-results.aurora-design.col-column'), cell: c => c.name },
                                          { id: 'type', header: t('analysis-results.aurora-design.col-aurora-type'), cell: c => c.auroraType },
                                        ]}
                                        items={table.columns}
                                        ariaLabels={{
                                          tableLabel: t('analysis-results.aurora-design.columns-table-label', {
                                            engine: design.displayName,
                                            table: table.tableName,
                                          }),
                                        }}
                                      />
                                      <KeyValuePairs
                                        columns={1}
                                        items={[
                                          {
                                            label: t('analysis-results.aurora-design.primary-key'),
                                            value: table.primaryKey.join(', ') || '—',
                                          },
                                          ...(table.indexes.length > 0 ? [{
                                            label: t('analysis-results.aurora-design.indexes', { count: table.indexes.length }),
                                            value: (
                                              <SpaceBetween size="xxs">
                                                {table.indexes.map((ix, idx) => (
                                                  <Box key={idx} fontFamily="monospace" fontSize="body-s">{ix}</Box>
                                                ))}
                                              </SpaceBetween>
                                            ),
                                          }] : []),
                                          ...(table.foreignKeys.length > 0 ? [{
                                            label: t('analysis-results.aurora-design.foreign-keys', { count: table.foreignKeys.length }),
                                            value: (
                                              <SpaceBetween size="xxs">
                                                {table.foreignKeys.map((fk, idx) => (
                                                  <Box key={idx} fontFamily="monospace" fontSize="body-s">{fk}</Box>
                                                ))}
                                              </SpaceBetween>
                                            ),
                                          }] : []),
                                        ]}
                                      />
                                    </SpaceBetween>
                                  </ExpandableSection>
                                ))}
                              </SpaceBetween>
                            )}
                          </ExpandableSection>

                          {design.ddl && (
                            <ExpandableSection
                              headerText={t('analysis-results.aurora-design.ddl-header')}
                              defaultExpanded={false}
                            >
                              {aceLoading ? (
                                <Box textAlign="center" padding="l">
                                  <StatusIndicator type="loading">{t('analysis-results.query-journey.loading-editor')}</StatusIndicator>
                                </Box>
                              ) : (
                                <CodeEditor
                                  ace={ace}
                                  language="sql"
                                  value={design.ddl}
                                  preferences={{ wrapLines: false, theme: 'cloud_editor_dark' }}
                                  editorContentHeight={400}
                                  i18nStrings={codeEditorI18nStrings(t, {
                                    loadingState: t('analysis-results.query-journey.loading'),
                                    errorState: t('analysis-results.query-journey.error'),
                                    errorStateRecovery: t('common.actions.retry'),
                                  })}
                                  readOnly
                                />
                              )}
                            </ExpandableSection>
                          )}

                          {design.optimizations.length > 0 && (
                            <ExpandableSection
                              headerText={t('analysis-results.aurora-design.optimizations-header', { count: design.optimizations.length })}
                              defaultExpanded={false}
                            >
                              <SpaceBetween size="xs">
                                {design.optimizations.map((opt, idx) => (
                                  <Box key={idx} padding={{ vertical: 'xs', horizontal: 's' }}
                                    style={{ borderLeft: '3px solid #0972d3', backgroundColor: '#f2f8fd', borderRadius: '4px' }}>
                                    <SpaceBetween size="xxs">
                                      <Box fontSize="body-s" fontWeight="bold">
                                        {opt.target ? `${opt.category ? `${optimizationLabel(opt.category)}: ` : ''}${opt.target}` : optimizationLabel(opt.category)}
                                      </Box>
                                      <Box fontSize="body-s">{opt.recommendation}</Box>
                                      {opt.rationale && (
                                        <Box fontSize="body-s" color="text-body-secondary">{opt.rationale}</Box>
                                      )}
                                    </SpaceBetween>
                                  </Box>
                                ))}
                              </SpaceBetween>
                            </ExpandableSection>
                          )}

                          <ExpandableSection
                            headerText={t('analysis-results.aurora-design.queries-header', { count: queryCount })}
                            defaultExpanded={false}
                          >
                            {design.queryGroups.length === 0 ? (
                              <Box color="text-body-secondary" fontSize="body-s">{t('analysis-results.aurora-design.no-queries')}</Box>
                            ) : (
                              <SpaceBetween size="s">
                                {design.queryGroups.map(group => (
                                  <ExpandableSection
                                    key={group.sourceTable}
                                    headerText={t('analysis-results.aurora-design.source-table-header', {
                                      table: group.sourceTable,
                                      count: group.queries.length,
                                    })}
                                    variant="footer"
                                    defaultExpanded={false}
                                  >
                                    <Table
                                      variant="embedded"
                                      columnDefinitions={[
                                        {
                                          id: 'excerpt',
                                          header: t('analysis-results.aurora-design.col-query'),
                                          cell: q => <Box fontFamily="monospace" fontSize="body-s">{q.excerpt}</Box>,
                                        },
                                        {
                                          id: 'type',
                                          header: t('analysis-results.aurora-design.col-type'),
                                          cell: q => q.queryType || '—',
                                        },
                                        {
                                          id: 'rps',
                                          header: t('analysis-results.aurora-design.col-calls-per-second'),
                                          cell: q => (typeof q.callsPerSecond === 'number' ? q.callsPerSecond.toFixed(2) : '—'),
                                        },
                                        {
                                          id: 'reason',
                                          header: t('analysis-results.aurora-design.col-reason'),
                                          cell: q => q.reason || '—',
                                        },
                                      ]}
                                      items={group.queries}
                                      ariaLabels={{
                                        tableLabel: t('analysis-results.aurora-design.queries-table-label', {
                                          engine: design.displayName,
                                          table: group.sourceTable,
                                        }),
                                      }}
                                    />
                                  </ExpandableSection>
                                ))}
                              </SpaceBetween>
                            )}
                          </ExpandableSection>
                        </SpaceBetween>
                      ),
                    };
                  })}
                />
              </Container>
            )}

          </SpaceBetween>
        }
        contentType="default"
        toolsHide
      />

      {/* Pattern Detail Modal */}
      <Modal
        visible={showPatternModal}
        onDismiss={handleCloseModal}
        header={
          selectedPattern
            ? `Pattern: ${selectedPattern.id?.slice(0, 12)}`
            : selectedSourceTable
            ? `Source Table: ${selectedSourceTable.table}`
            : 'Details'
        }
        size="max"
      >
        {selectedPattern && renderPatternDetail(selectedPattern)}
        {selectedSourceTable && renderSourceTableDetail(selectedSourceTable)}
      </Modal>

      {/* Query Journey Modal */}
      <Modal
        visible={showQueryJourneyModal}
        onDismiss={handleCloseQueryJourneyModal}
        header={`Query Journey: ${selectedQueryId || 'Loading...'}`}
        size="max"
      >
        {queryJourneyLoading ? (
          <Box textAlign="center" padding="xxl">
            <StatusIndicator type="loading">{t('analysis-results.query-journey.loading')}</StatusIndicator>
          </Box>
        ) : queryJourneyData?.error ? (
          <Box textAlign="center" padding="l">
            <StatusIndicator type="error">{queryJourneyData.error}</StatusIndicator>
          </Box>
        ) : (
          <Tabs
            tabs={[
              {
                id: 'general',
                label: 'General Information',
                content: (
                  <SpaceBetween size="m">
                    <KeyValuePairs
                      columns={2}
                      items={[
                        {
                          label: 'SQL ID',
                          value: (
                            <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                              <Box fontFamily="monospace" fontSize="body-s">
                                {selectedQueryId}
                              </Box>
                              <CopyToClipboard
                                copyButtonAriaLabel={t('common.actions.copy-sql-id')}
                                copyErrorText={t('common.errors.copy-failed')}
                                copySuccessText={t('common.success.copied')}
                                textToCopy={selectedQueryId}
                                variant="icon"
                              />
                            </SpaceBetween>
                          )
                        },
                        {
                          label: 'Query Type',
                          value: queryJourneyData?.source?.query_type || '—'
                        },
                        {
                          label: 'Tables Accessed',
                          value: (queryJourneyData?.source?.tables_accessed || []).join(', ') || '—'
                        },
                        {
                          label: 'Frequency (per hour)',
                          value: queryJourneyData?.source?.frequency_per_hour?.toFixed(2) || '—'
                        },
                        {
                          label: 'Calls per Second',
                          value: queryJourneyData?.source?.calls_per_second?.toFixed(4) || '—'
                        },
                        {
                          label: 'Assigned Engine',
                          value: queryJourneyData?.assignment?.assigned_engine || '—'
                        },
                        {
                          label: 'Workload Fit',
                          value: queryJourneyData?.assignment?.confidence ? (
                            <SpaceBetween size="xxxs">
                              <Box>{`${queryJourneyData.assignment.confidence}%`}</Box>
                              <Box fontSize="body-s" color="text-status-inactive">
                                {WORKLOAD_FIT_DEFINITION}
                              </Box>
                            </SpaceBetween>
                          ) : '—'
                        },
                        {
                          label: 'In Scope',
                          value: queryJourneyData?.assignment?.in_scope ? 'Yes' : 'No'
                        }
                      ]}
                    />
                    {queryJourneyData?.source?.query_text && (
                      <Container header={<Header variant="h3">{t('analysis-results.pattern-detail.query-text')}</Header>}>
                        <Box fontSize="body-s" fontFamily="monospace" padding="s" style={{
                          backgroundColor: '#f2f3f3',
                          borderRadius: '4px',
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-word'
                        }}>
                          {queryJourneyData.source.query_text}
                        </Box>
                      </Container>
                    )}
                  </SpaceBetween>
                )
              },
              {
                id: 'performance',
                label: 'Performance',
                content: (
                  <KeyValuePairs
                    columns={3}
                    items={
                      queryJourneyData?.source?.performance
                        ? Object.entries(queryJourneyData.source.performance).map(([key, value]) => ({
                            label: key.split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' '),
                            value: typeof value === 'number' ? value.toFixed(3) : (value?.toString() || '—')
                          }))
                        : [{ label: 'No performance data', value: '—' }]
                    }
                  />
                )
              },
              {
                id: 'characteristics',
                label: 'Characteristics',
                content: (
                  <KeyValuePairs
                    columns={3}
                    items={
                      queryJourneyData?.source?.characteristics
                        ? Object.entries(queryJourneyData.source.characteristics).map(([key, value]) => ({
                            label: key.split('_').map(word => word.charAt(0).toUpperCase() + word.slice(1)).join(' '),
                            value: typeof value === 'boolean'
                              ? (value ? 'Yes' : 'No')
                              : Array.isArray(value)
                              ? value.join(', ') || '—'
                              : (value?.toString() || '—')
                          }))
                        : [{ label: 'No characteristics data', value: '—' }]
                    }
                  />
                )
              },
              {
                id: 'why-this-engine',
                label: t('analysis-results.query-trace.tab'),
                content: (
                  <QueryDecisionTrace
                    trace={queryTrace.data}
                    error={queryTrace.error}
                    loading={queryJourneyLoading}
                    t={t}
                  />
                )
              },
              {
                id: 'json',
                label: 'JSON',
                content: aceLoading ? (
                  <Box textAlign="center" padding="l">
                    <StatusIndicator type="loading">{t('analysis-results.query-journey.loading-editor')}</StatusIndicator>
                  </Box>
                ) : (
                  <CodeEditor
                    ace={ace}
                    language="json"
                    value={JSON.stringify(queryJourneyData, null, 2)}
                    preferences={{
                      wrapLines: false,
                      theme: 'cloud_editor_dark'
                    }}
                    editorContentHeight={600}
                    i18nStrings={codeEditorI18nStrings(t, {
                      loadingState: t('analysis-results.query-journey.loading'),
                      errorState: t('analysis-results.query-journey.error'),
                      errorStateRecovery: t('common.actions.retry')
                    })}
                    loading={queryJourneyLoading}
                    readOnly
                  />
                )
              }
            ]}
          />
        )}
      </Modal>

    </>
  );
});

export default AnalysisResultsPage;
