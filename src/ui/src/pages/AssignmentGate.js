//##-- React
import { useState, useEffect, memo, useCallback, useMemo } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

//##-- Cloudscape
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Box from "@cloudscape-design/components/box";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Flashbar from "@cloudscape-design/components/flashbar";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Badge from "@cloudscape-design/components/badge";
import Spinner from "@cloudscape-design/components/spinner";
import Alert from "@cloudscape-design/components/alert";
import Select from "@cloudscape-design/components/select";
import Table from "@cloudscape-design/components/table";
import PropertyFilter from "@cloudscape-design/components/property-filter";
import Pagination from "@cloudscape-design/components/pagination";
import Modal from "@cloudscape-design/components/modal";
import Tabs from "@cloudscape-design/components/tabs";
import ExpandableSection from "@cloudscape-design/components/expandable-section";

//##-- Custom
import { SideNavigationConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import SummaryFallbackNotice from "../components/SummaryFallbackNotice";
import PendingEngineChoices from "../components/PendingEngineChoices";
import ApiManager from "../classes/ApiManager";
import ChartSankey from "../components/ChartSankey";
import {
  buildAssignmentSummary,
  countTieUnresolvedQueries,
  effectiveEngines as computeEffectiveEngines,
  eliminatedEngines as computeEliminatedEngines,
  engineOptionsForAssignment,
  engineOptionsForQuery,
  survivingDistribution,
} from "../utils/assignmentSummary";
import { sourceEngineOf } from "../utils/sqlLocking";
import { ENGINE_STYLES, engineHex } from "../utils/engineColors";
import EngineBadge from "../components/EngineBadge";
import { getCacheOverlay, ownerDistribution, getQueryCacheInfo, formatCacheLayerLine, formatCachedByLine, isCacheEngine, buildOverrideList, addCacheOverlayNode } from "../utils/cacheLayer";
import { summaryFallbackInfo } from "../utils/summaryFallback";
import { reroutedQueryIds, rowMoveKind } from "../utils/postSchemaReroutes";
import Checkbox from "@cloudscape-design/components/checkbox";

import './AssignmentGate.css';


// ============================================
// Constants
// ============================================

// Colour, badge colour and label per engine, from the engine catalog (#492).
const ENGINE_COLORS = ENGINE_STYLES;

// #296: ElastiCache is a cache layer, never an owner, so it is not an engine
// option; each row has a "Cache with ElastiCache" toggle instead.
const ENGINE_OPTIONS = Object.entries(ENGINE_COLORS)
  .filter(([key]) => !isCacheEngine(key))
  .map(([key, val]) => ({
    label: val.label,
    value: key,
  }));

const PAGE_SIZE = 25;


// ============================================
// Helper: Engine badge component
// ============================================

// ============================================
// Helper: Distribution bar
// ============================================

function DistributionBar({ distribution, eliminated = [] }) {
  const total = Object.values(distribution).reduce((a, b) => a + b, 0);
  if (total === 0) return null;

  const sorted = Object.entries(distribution).sort((a, b) => b[1] - a[1]);

  return (
    <div className="distribution-bar">
      {sorted.map(([engine, count]) => {
        const pct = (count / total) * 100;
        const isEliminated = eliminated.includes(engine);
        const color = engineHex(engine);
        return (
          <div
            key={engine}
            className={`distribution-bar__segment ${isEliminated ? 'distribution-bar__segment--eliminated' : ''}`}
            style={{ width: `${pct}%`, backgroundColor: color }}
            title={`${engine}: ${count} queries (${pct.toFixed(1)}%)`}
          >
            <span>{count > 0 && `${engine} (${count})`}</span>
          </div>
        );
      })}
    </div>
  );
}


// ============================================
// Main Page Component
// ============================================

const AssignmentGatePage = memo(() => {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const { t } = useTranslation();

  // ---- State ----
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [resuming, setResuming] = useState(false);
  const [savingOverrides, setSavingOverrides] = useState(false);
  const [flashbarItems, setFlashbarItems] = useState([]);

  // Data
  const [realityCheck, setRealityCheck] = useState(null);
  const [assignmentData, setAssignmentData] = useState(null);
  const [phasesData, setPhasesData] = useState(null);
  const [triageData, setTriageData] = useState(null);
  const [collectorData, setCollectorData] = useState(null);
  const [databaseName, setDatabaseName] = useState('');

  // View mode
  const [viewMode, setViewMode] = useState('summary'); // 'summary' | 'advanced'

  // Advanced view state
  const [filterQuery, setFilterQuery] = useState({ tokens: [], operation: 'and' });
  const [currentPage, setCurrentPage] = useState(1);
  const [overrides, setOverrides] = useState({}); // { query_id: new_engine }
  const [cacheOverrides, setCacheOverrides] = useState({}); // { query_id: cached (bool) }
  const [expandedQueryIds, setExpandedQueryIds] = useState(new Set());
  const [showResumeModal, setShowResumeModal] = useState(false);
  const [sortColumn, setSortColumn] = useState(null); // column key
  const [sortDirection, setSortDirection] = useState('asc');


  // ---- Flashbar ----
  const addFlash = useCallback((type, header, content) => {
    const id = `flash-${Date.now()}`;
    setFlashbarItems(prev => [...prev, { type, header, content, dismissible: true, id }]);
  }, []);

  const dismissFlash = useCallback((itemId) => {
    setFlashbarItems(prev => prev.filter(i => i.id !== itemId));
  }, []);


  // ---- Data Fetching ----
  const fetchData = useCallback(async () => {
    if (!jobId) return;
    setLoading(true);

    try {
      const api = new ApiManager();

      // First get job details to resolve database_name
      const jobResult = await api.execute([
        { id: 'job', path: `assessments/${jobId}`, method: 'GET', params: {} }
      ]);

      const dbName = jobResult['job']?.database_name || '';
      setDatabaseName(dbName);

      if (!dbName) {
        addFlash('error', t('assignment-gate.flash.missing-db-name'), t('assignment-gate.flash.missing-db-name-detail'));
        setLoading(false);
        return;
      }

      // Now fetch everything in parallel
      const results = await api.execute([
        { id: 'reality-check', path: `assessments/${jobId}/reality-check`, method: 'GET', params: {} },
        { id: 'assignments', path: `assessments/${jobId}/assignments?database_name=${encodeURIComponent(dbName)}`, method: 'GET', params: {} },
        { id: 'phases', path: `assessments/${jobId}/phases`, method: 'GET', params: {} },
        { id: 'triage', path: `assessments/${jobId}/triage`, method: 'GET', params: {} },
        { id: 'collector', path: `assessments/${jobId}/collector`, method: 'GET', params: {} },
      ]);

      if (results['reality-check']?.success) {
        setRealityCheck(results['reality-check']);
      }
      if (results['assignments']?.success) {
        setAssignmentData(results['assignments']);
      }
      if (results['phases']?.success) {
        setPhasesData(results['phases']);
      }
      if (results['triage']?.success) {
        setTriageData(results['triage']);
      }
      if (results['collector']?.success) {
        setCollectorData(results['collector']);
      }

    } catch (error) {
      console.error('Error loading gate data:', error);
      addFlash('error', t('assignment-gate.flash.failed-to-load'), error.message);
    } finally {
      setLoading(false);
    }
  }, [jobId, addFlash, t]);


  useEffect(() => {
    fetchData();
  }, [fetchData]);


  // ---- Resume handler ----
  const handleResume = useCallback(async () => {
    setResuming(true);
    try {
      const api = new ApiManager();
      const results = await api.execute([
        {
          id: 'resume',
          path: `assessments/${jobId}/resume`,
          method: 'POST',
          params: { phase: 'assignment_review' }
        }
      ]);

      if (results['resume']?.success) {
        addFlash('success', t('assignment-gate.flash.pipeline-resumed'), t('assignment-gate.flash.pipeline-resumed-detail'));
        setShowResumeModal(false);
        // Navigate back to monitoring after a short delay
        setTimeout(() => navigate(`/analysis/monitor/summary/${jobId}`), 1500);
      } else {
        const msg = results['resume']?.error?.message || t('assignment-gate.flash.resume-failed-detail');
        addFlash('error', t('assignment-gate.flash.resume-failed'), msg);
      }
    } catch (error) {
      addFlash('error', t('assignment-gate.flash.resume-failed'), error.message);
    } finally {
      setResuming(false);
    }
  }, [jobId, addFlash, navigate, t]);


  // ---- Save overrides ----
  const handleSaveOverrides = useCallback(async () => {
    const overrideList = buildOverrideList(overrides, cacheOverrides);

    if (overrideList.length === 0) {
      addFlash('info', t('assignment-gate.flash.no-changes'), t('assignment-gate.flash.no-changes-detail'));
      return;
    }

    setSavingOverrides(true);
    try {
      const api = new ApiManager();
      const results = await api.execute([
        {
          id: 'save',
          path: `assessments/${jobId}/assignments?database_name=${encodeURIComponent(databaseName)}`,
          method: 'PUT',
          // #992: the version these edits were made on; the API refuses a stale edit.
          params: { overrides: overrideList, base_version: assignmentData?.assignment?.version }
        }
      ]);

      if (results['save']?.success) {
        addFlash('success', t('assignment-gate.flash.overrides-saved'), t('assignment-gate.flash.overrides-saved-detail', { count: overrideList.length }));
        setOverrides({});
        setCacheOverrides({});
        // Refresh data
        fetchData();
      } else {
        const msg = results['save']?.error?.message || t('assignment-gate.flash.save-failed-detail');
        addFlash('error', t('assignment-gate.flash.save-failed'), msg);
      }
    } catch (error) {
      addFlash('error', t('assignment-gate.flash.save-failed'), error.message);
    } finally {
      setSavingOverrides(false);
    }
  }, [jobId, overrides, cacheOverrides, addFlash, fetchData, t, assignmentData]);

  // ---- Save per-table engine choices (#1003) ----
  const handleSaveChoices = useCallback(async (body) => {
    setSavingOverrides(true);
    try {
      const api = new ApiManager();
      const results = await api.execute([
        {
          id: 'choices',
          path: `assessments/${jobId}/assignments?database_name=${encodeURIComponent(databaseName)}`,
          method: 'PUT',
          params: { ...body, base_version: assignmentData?.assignment?.version }
        }
      ]);
      if (results['choices']?.success) {
        addFlash('success', t('assignment-gate.flash.overrides-saved'), t('assignment-gate.flash.overrides-saved-detail', { count: (body.table_choices || []).length + (body.overrides || []).length }));
        fetchData();
      } else {
        const msg = results['choices']?.error?.message || t('assignment-gate.flash.save-failed-detail');
        addFlash('error', t('assignment-gate.flash.save-failed'), msg);
      }
    } catch (error) {
      addFlash('error', t('assignment-gate.flash.save-failed'), error.message);
    } finally {
      setSavingOverrides(false);
    }
  }, [jobId, databaseName, assignmentData, addFlash, fetchData, t]);

  // ---- Handle Sankey node click ----
  const handleSankeyNodeClick = useCallback((nodeId) => {
    if (nodeId && nodeId !== 'patterns') {
      window.open(`/analysis/patterns/${jobId}?target=${nodeId}`, '_blank');
    }
  }, [jobId]);


  // ---- Computed data ----

  const consolidations = useMemo(() => realityCheck?.consolidations || [], [realityCheck]);
  const patterns = useMemo(() => realityCheck?.architectural_patterns || [], [realityCheck]);
  const recommendations = useMemo(() => realityCheck?.recommendations || [], [realityCheck]);

  // #296 cache overlay: the assignment artifact carries an optional top-level
  // cache_overlay describing ElastiCache's cached-reads/call-share, separate
  // from the owner distributions below. Its presence is also the signal that
  // before/after_distribution are the new owners-only shape, so legacy
  // artifacts (no cache_overlay, ElastiCache still a real owner) keep
  // rendering exactly as before.
  const cacheOverlay = useMemo(() => getCacheOverlay(assignmentData?.assignment), [assignmentData]);
  // #381 review round 2: never offer the losing Aurora engine as a per-query
  // override target -- the server rejects it (LosingAuroraEngineOverride) with no
  // warning here otherwise.
  const engineOptions = useMemo(
    () => engineOptionsForAssignment(ENGINE_OPTIONS, assignmentData?.assignment),
    [assignmentData],
  );
  const beforeDist = useMemo(
    () => ownerDistribution(realityCheck?.before_distribution, !!cacheOverlay),
    [realityCheck, cacheOverlay],
  );
  const afterDist = useMemo(
    () => ownerDistribution(realityCheck?.after_distribution, !!cacheOverlay),
    [realityCheck, cacheOverlay],
  );
  const cacheLayerLine = useMemo(() => formatCacheLayerLine(cacheOverlay, { t }), [cacheOverlay, t]);

  // The current assignment's own in-scope engines (#235) -- a later customer
  // edit can be newer than the reality check's own before/after snapshot, so
  // elimination is always re-checked against this, not just realityCheck.
  const effectiveEngines = useMemo(
    () => computeEffectiveEngines(assignmentData?.assignment?.query_assignments),
    [assignmentData],
  );

  // Eliminated engines (#235: no in-scope query remains on them in the
  // current assignment) -- the reality check's settled eliminated_engines,
  // unioned with the first assignment's engines, filtered by effectiveEngines
  // so a later customer edit is reflected in both directions.
  const eliminatedEngines = useMemo(
    () => computeEliminatedEngines(realityCheck, beforeDist, effectiveEngines),
    [realityCheck, beforeDist, effectiveEngines],
  );

  // Every deliverable below (survivingEngines, the headline, the summary
  // sentence and the Sankey) reads this instead of afterDist directly, so an
  // eliminated engine's leftover out-of-scope query never renders as
  // surviving (#235).
  const survivingAfterDist = useMemo(
    () => survivingDistribution(afterDist, eliminatedEngines),
    [afterDist, eliminatedEngines],
  );

  // Surviving engines
  const survivingEngines = useMemo(
    () => Object.keys(survivingAfterDist),
    [survivingAfterDist],
  );

  // Table count from collector
  const tableCount = useMemo(() => {
    const tables = collectorData?.database_schema?.tables || collectorData?.schema?.tables || [];
    return tables.length;
  }, [collectorData]);

  // Build headline
  const headline = useMemo(() => {
    if (survivingEngines.length === 0) return 'Analyzing your workload...';
    const engineNames = survivingEngines.map(e => ENGINE_COLORS[e]?.label || e);
    if (engineNames.length === 1) return `Recommended architecture: ${engineNames[0]}`;
    const last = engineNames.pop();
    return `Recommended architecture: ${engineNames.join(', ')} and ${last}`;
  }, [survivingEngines]);

  // Build executive summary — the fact line (database, size) always leads; the
  // LLM-generated reality-check summary follows it when available (#250).
  // afterDist -> survivingAfterDist (#235): the deterministic "We map N to X"
  // sentence must not list an eliminated engine's leftover out-of-scope count.
  const executiveSummary = useMemo(() => buildAssignmentSummary({
    llmSummary: realityCheck?.executive_summary,
    databaseName,
    afterDist: survivingAfterDist,
    tableCount,
    patterns,
    cacheOverlay,
    engineLabel: (engine) => ENGINE_COLORS[engine]?.label || engine,
    t,
  }), [realityCheck, databaseName, survivingAfterDist, tableCount, patterns, cacheOverlay, t]);

  // #269: whether that LLM-generated reality-check summary was itself replaced
  // by its deterministic fallback (a grounding check rejected it against the
  // final records, #236) -- shown as a notice under the hero summary, with
  // the rejection warnings in an expandable detail.
  const summaryFallback = useMemo(() => summaryFallbackInfo(
    realityCheck?.executive_summary_source,
    realityCheck?.executive_summary_validation_warnings,
  ), [realityCheck]);

  // Sankey data from after_distribution. #330: the cache layer is added as a
  // node fed by its owner engines' flows (never a flow source itself), so
  // cache coverage is visible in the diagram instead of only in cacheLayerLine.
  const sankeyData = useMemo(() => {
    if (Object.keys(survivingAfterDist).length === 0) return null;

    const nodes = [{ id: 'queries' }];
    const links = [];

    Object.entries(survivingAfterDist).forEach(([engine, count]) => {
      nodes.push({ id: engine });
      links.push({ source: 'queries', target: engine, value: count });
    });

    return addCacheOverlayNode({ nodes, links }, cacheOverlay);
  }, [survivingAfterDist, cacheOverlay]);

  // Consolidated query IDs (for highlighting in advanced view)
  const consolidatedQueryIds = useMemo(() => {
    const ids = new Set();
    consolidations.forEach(c => {
      // We don't have individual query IDs in consolidations,
      // but we can mark queries whose assignment_reason contains "reality check"
    });
    return ids;
  }, [consolidations]);

  // Query assignments for advanced table
  const queryAssignments = useMemo(() => {
    return assignmentData?.assignment?.query_assignments || [];
  }, [assignmentData]);

  // #1003: the open ties grouped per table (built by the API), and the choices
  // --auto decided (still holding on this version).
  const pendingChoices = useMemo(() => assignmentData?.pending_choices || [], [assignmentData]);
  const autoDecided = useMemo(
    () => assignmentData?.assignment?.auto_decided_choices || [],
    [assignmentData],
  );

  // #629: queries the post-schema router moved after approval.
  const reroutedIds = useMemo(
    () => reroutedQueryIds(assignmentData?.assignment),
    [assignmentData],
  );

  // #503: queries an exact score tie left unresolved -- no ADR-031 rule
  // decided between the tied engines, so the customer sees a count here and
  // can pick a winner per query below (the "Reason" column names the
  // placeholder engine and the engines it tied with).
  const tieUnresolvedCount = useMemo(
    () => countTieUnresolvedQueries(queryAssignments),
    [queryAssignments]
  );

  // Collector query lookup: query_id → full query pattern object
  const queryDetailsMap = useMemo(() => {
    const map = {};
    const patterns = collectorData?.queries?.query_patterns || [];
    patterns.forEach(q => { map[q.query_id] = q; });
    return map;
  }, [collectorData]);

  // The source database's engine, from the collector payload: it sets the SQL
  // dialect the locking-read check reads (#1143).
  const sourceEngine = useMemo(() => sourceEngineOf(collectorData), [collectorData]);

  // Triage signal lookup: query_id → [{ signal, engine }]
  const querySignalsMap = useMemo(() => {
    const map = {};
    const signals = triageData?.signals || [];
    signals.forEach(sig => {
      const engine = (sig.targets || [])[0] || null;
      (sig.query_ids || []).forEach(qid => {
        if (!map[qid]) map[qid] = [];
        map[qid].push({ signal: sig.signal, engine });
      });
    });
    return map;
  }, [triageData]);

  // Toggle sort
  const toggleSort = useCallback((column) => {
    setSortColumn(prev => {
      if (prev === column) {
        setSortDirection(d => d === 'asc' ? 'desc' : 'asc');
        return column;
      }
      setSortDirection('asc');
      return column;
    });
    setCurrentPage(1);
  }, []);

  // Toggle row expansion
  const toggleQueryExpanded = useCallback((queryId) => {
    setExpandedQueryIds(prev => {
      const next = new Set(prev);
      if (next.has(queryId)) {
        next.delete(queryId);
      } else {
        next.add(queryId);
      }
      return next;
    });
  }, []);

  // Property filter definitions
  const filterProperties = useMemo(() => [
    {
      key: 'query_id',
      propertyLabel: 'Query ID',
      groupValuesLabel: 'Query ID values',
      operators: [':', '='],
    },
    {
      key: 'assigned_engine',
      propertyLabel: 'Engine',
      groupValuesLabel: 'Engine values',
      operators: ['=', '!='],
    },
    {
      key: 'source_table',
      propertyLabel: 'Table',
      groupValuesLabel: 'Table values',
      operators: ['=', '!=', ':'],
    },
    {
      key: 'query_type',
      propertyLabel: 'Query type',
      groupValuesLabel: 'Query type values',
      operators: ['=', '!='],
    },
    {
      key: 'assignment_reason',
      propertyLabel: 'Reason',
      groupValuesLabel: 'Reason values',
      operators: [':', '!:'],
    },
    {
      key: 'confidence',
      propertyLabel: 'Workload fit',
      groupValuesLabel: 'Workload fit values',
      operators: ['=', '!=', '>', '<', '>=', '<='],
    },
    {
      key: 'signal',
      propertyLabel: 'Signal',
      groupValuesLabel: 'Signal values',
      operators: ['=', ':'],
    },
    {
      key: 'moved',
      propertyLabel: 'Moved by reality check',
      groupValuesLabel: 'Moved values',
      operators: ['='],
    },
  ], []);

  // Build filter options from actual data
  const filterOptions = useMemo(() => {
    const engines = new Set();
    const tables = new Set();
    const types = new Set();
    const sigs = new Set();

    queryAssignments.forEach(q => {
      if (q.assigned_engine) engines.add(q.assigned_engine);
      (q.source_tables || []).forEach(t => tables.add(t.split('.').pop()));
      const detail = queryDetailsMap[q.query_id];
      if (detail?.query_type) types.add(detail.query_type);
      (querySignalsMap[q.query_id] || []).forEach(s => sigs.add(s.signal));
    });

    return [
      ...[...engines].sort().map(v => ({ propertyKey: 'assigned_engine', value: v })),
      ...[...tables].sort().map(v => ({ propertyKey: 'source_table', value: v })),
      ...[...types].sort().map(v => ({ propertyKey: 'query_type', value: v })),
      ...[...sigs].sort().map(v => ({ propertyKey: 'signal', value: v.replace(/_/g, ' ') })),
      { propertyKey: 'moved', value: 'yes' },
      { propertyKey: 'moved', value: 'no' },
    ];
  }, [queryAssignments, queryDetailsMap, querySignalsMap]);

  // Filtered queries using property filter tokens
  const filteredQueries = useMemo(() => {
    let items = [...queryAssignments];

    const { tokens, operation } = filterQuery;
    if (tokens.length === 0) return items;

    const matchToken = (q, token) => {
      // Free text token (no propertyKey)
      if (!token.propertyKey) {
        const text = token.value?.toLowerCase() || '';
        return (
          q.query_id?.toLowerCase().includes(text) ||
          q.assigned_engine?.toLowerCase().includes(text) ||
          q.assignment_reason?.toLowerCase().includes(text) ||
          (q.source_tables || []).some(t => t.toLowerCase().includes(text))
        );
      }

      const val = token.value?.toLowerCase() || '';
      const op = token.operator || '=';

      if (token.propertyKey === 'query_id') {
        const qid = q.query_id?.toLowerCase() || '';
        if (op === ':') return qid.includes(val);
        if (op === '=') return qid === val;
      }

      if (token.propertyKey === 'assigned_engine') {
        const engine = q.assigned_engine?.toLowerCase() || '';
        if (op === '=') return engine === val;
        if (op === '!=') return engine !== val;
      }

      if (token.propertyKey === 'source_table') {
        const tables = (q.source_tables || []).map(t => t.split('.').pop().toLowerCase());
        if (op === '=') return tables.some(t => t === val);
        if (op === '!=') return !tables.some(t => t === val);
        if (op === ':') return tables.some(t => t.includes(val));
      }

      if (token.propertyKey === 'query_type') {
        const detail = queryDetailsMap[q.query_id];
        const qtype = detail?.query_type?.toLowerCase() || '';
        if (op === '=') return qtype === val;
        if (op === '!=') return qtype !== val;
      }

      if (token.propertyKey === 'assignment_reason') {
        const reason = q.assignment_reason?.toLowerCase() || '';
        // #572 review suggestion 9: assignment_reason now names engines with
        // their display name ("Aurora MySQL"), not the raw id
        // ("aurora_mysql") -- a filter typed as the raw id must still match.
        const displayVal = ENGINE_COLORS[val]?.label?.toLowerCase();
        const hit = reason.includes(val) || (!!displayVal && reason.includes(displayVal));
        if (op === ':') return hit;
        if (op === '!:') return !hit;
      }

      if (token.propertyKey === 'confidence') {
        const conf = q.confidence ?? 0;
        const numVal = parseFloat(token.value);
        if (isNaN(numVal)) return true;
        if (op === '=') return conf === numVal;
        if (op === '!=') return conf !== numVal;
        if (op === '>') return conf > numVal;
        if (op === '<') return conf < numVal;
        if (op === '>=') return conf >= numVal;
        if (op === '<=') return conf <= numVal;
      }

      if (token.propertyKey === 'signal') {
        const sigs = (querySignalsMap[q.query_id] || []).map(s => s.signal.replace(/_/g, ' ').toLowerCase());
        if (op === '=') return sigs.some(s => s === val);
        if (op === ':') return sigs.some(s => s.includes(val));
      }

      if (token.propertyKey === 'moved') {
        const wasMoved = (q.assignment_reason || '').toLowerCase().includes('reality check');
        if (op === '=') return val === 'yes' ? wasMoved : !wasMoved;
      }

      return true;
    };

    items = items.filter(q => {
      if (operation === 'and') {
        return tokens.every(t => matchToken(q, t));
      }
      return tokens.some(t => matchToken(q, t));
    });

    return items;
  }, [queryAssignments, filterQuery, queryDetailsMap, querySignalsMap]);

  // Sorted queries
  const sortedQueries = useMemo(() => {
    if (!sortColumn) return filteredQueries;

    const sorted = [...filteredQueries];
    const dir = sortDirection === 'asc' ? 1 : -1;

    sorted.sort((a, b) => {
      let aVal, bVal;

      switch (sortColumn) {
        case 'engine':
          aVal = (overrides[a.query_id] || a.assigned_engine || '').toLowerCase();
          bVal = (overrides[b.query_id] || b.assigned_engine || '').toLowerCase();
          return dir * aVal.localeCompare(bVal);
        case 'confidence':
          aVal = a.confidence ?? 0;
          bVal = b.confidence ?? 0;
          return dir * (aVal - bVal);
        case 'tables':
          aVal = (a.source_tables || []).map(t => t.split('.').pop()).join(', ').toLowerCase();
          bVal = (b.source_tables || []).map(t => t.split('.').pop()).join(', ').toLowerCase();
          return dir * aVal.localeCompare(bVal);
        case 'signals':
          aVal = (querySignalsMap[a.query_id] || []).length;
          bVal = (querySignalsMap[b.query_id] || []).length;
          return dir * (aVal - bVal);
        case 'reason':
          aVal = (a.assignment_reason || '').toLowerCase();
          bVal = (b.assignment_reason || '').toLowerCase();
          return dir * aVal.localeCompare(bVal);
        default:
          return 0;
      }
    });

    return sorted;
  }, [filteredQueries, sortColumn, sortDirection, overrides, querySignalsMap]);

  const paginatedQueries = useMemo(() => {
    const start = (currentPage - 1) * PAGE_SIZE;
    return sortedQueries.slice(start, start + PAGE_SIZE);
  }, [sortedQueries, currentPage]);

  // Override count
  const overrideCount = new Set([...Object.keys(overrides), ...Object.keys(cacheOverrides)]).size;

  // Has pending overrides
  const hasPendingOverrides = overrideCount > 0;


  // ---- Breadcrumbs ----
  const breadcrumbItems = useMemo(() => [
    { href: "/", text: t('assignment-gate.breadcrumb.home') },
    { href: "/dashboard", text: t('assignment-gate.breadcrumb.dashboard') },
    { href: `/analysis/monitor/summary/${jobId}`, text: `Job ${jobId?.slice(0, 8)}...` },
    { href: `#`, text: t('assignment-gate.breadcrumb.assignment-review') }
  ], [jobId, t]);


  // ---- Render: Loading ----
  if (loading) {
    return (
      <>
        <AppHeader />
        <AppLayoutToolbar
          navigationOpen={navigationOpen}
          onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
          breadcrumbs={<BreadcrumbGroup items={breadcrumbItems} />}
          navigation={
            <SideNavigation
              activeHref={`/analysis/assignments/${jobId}`}
              header={SideNavigationConfigurations.header}
              items={SideNavigationConfigurations.items}
            />
          }
          content={
            <Box textAlign="center" padding={{ top: 'xxxl' }}>
              <SpaceBetween alignItems="center" direction="vertical" size="m">
                <Spinner size="large" />
                <Box variant="h3" color="text-body-secondary">{t('assignment-gate.states.loading')}</Box>
              </SpaceBetween>
            </Box>
          }
          toolsHide
        />
      </>
    );
  }


  // ---- Render: CTO Summary View ----
  const renderSummaryView = () => (
    <SpaceBetween size="l">

      {/* Hero Summary */}
      <Container>
        <div className="gate-hero">
          <div className="gate-hero__headline">{headline}</div>
          <div className="gate-hero__summary">{executiveSummary}</div>
          {summaryFallback.isFallback && (
            <SummaryFallbackNotice
              className="gate-hero__fallback-note"
              text={t('assignment-gate.summary.fallback-notice')}
              warnings={summaryFallback.warnings}
              detailsHeaderText={t('assignment-gate.summary.fallback-details-header')}
            />
          )}
        </div>
      </Container>

      {/* Query Flow (Sankey) */}
      {sankeyData && (
        <Container
          header={
            <Header variant="h2" description={t('assignment-gate.query-flow.description')}>
              {t('assignment-gate.query-flow.title')}
            </Header>
          }
        >
          <ChartSankey
            width={900}
            height={Math.max(250, Object.keys(survivingAfterDist).length * 120)}
            data={sankeyData}
            onNodeClick={handleSankeyNodeClick}
          />
          {/* #296/#330: ElastiCache never owns a query, so it's never a flow
              source -- the Sankey instead shows it as a node fed by its owner
              engines' flows (addCacheOverlayNode); this caption adds the
              overlay's call-share stat, which the diagram doesn't carry. */}
          {cacheLayerLine && (
            <Box padding={{ top: 's' }} textAlign="center" color="text-body-secondary" fontSize="body-s">
              {cacheLayerLine}
            </Box>
          )}
        </Container>
      )}

      {/* Consolidations & Architectural Patterns */}
      {(consolidations.length > 0 || patterns.length > 0) && (
        <ExpandableSection
          headerText={t('assignment-gate.optimization-details.header', {
            consolidations: t('assignment-gate.optimization-details.header-consolidation-count', { count: consolidations.length }),
            patterns: t('assignment-gate.optimization-details.header-pattern-count', { count: patterns.length }),
          })}
          variant="container"
          defaultExpanded={false}
        >
          <SpaceBetween size="l">
            {consolidations.length > 0 && (
              <Box>
                <Box variant="awsui-key-label" margin={{ bottom: 'xs' }}>{t('assignment-gate.optimization-details.engine-consolidations')}</Box>
                <SpaceBetween size="xs">
                  {consolidations.map((c, idx) => (
                    <Box key={idx} padding={{ vertical: 'xs', horizontal: 's' }}
                      style={{ borderLeft: '3px solid #d91515', backgroundColor: '#fef2f2', borderRadius: '4px' }}>
                      <SpaceBetween size="xxs">
                        <Box fontSize="body-s">
                          <Box display="inline">
                            <EngineBadge engine={c.from_engine} />
                          </Box>
                          <Box display="inline" padding={{ horizontal: 'xs' }}>→</Box>
                          <Box display="inline">
                            <EngineBadge engine={c.to_engine} />
                          </Box>
                          <Box display="inline" padding={{ left: 's' }} fontWeight="bold">
                            {t('assignment-gate.optimization-details.queries-moved', { count: c.query_count })}
                          </Box>
                        </Box>
                        <Box fontSize="body-s" color="text-body-secondary">{c.reason}</Box>
                      </SpaceBetween>
                    </Box>
                  ))}
                </SpaceBetween>
              </Box>
            )}
            {patterns.length > 0 && (
              <Box>
                <Box variant="awsui-key-label" margin={{ bottom: 'xs' }}>{t('assignment-gate.optimization-details.architectural-patterns')}</Box>
                <SpaceBetween size="xs">
                  {patterns.map((p, idx) => {
                    const engines = p.applies_to || {};
                    const engineList = [
                      ...(engines.write_engine ? [engines.write_engine] : []),
                      ...(engines.source_engine ? [engines.source_engine] : []),
                      ...(engines.read_engines || []),
                      ...(engines.view_engine ? [engines.view_engine] : []),
                    ];
                    return (
                      <Box key={idx} padding={{ vertical: 'xs', horizontal: 's' }}
                        style={{ borderLeft: '3px solid #0972d3', backgroundColor: '#f2f8fd', borderRadius: '4px' }}>
                        <SpaceBetween size="xxs">
                          <Box fontSize="body-s">
                            <Box fontWeight="bold" display="inline">{p.name}</Box>
                            {engineList.length > 0 && (
                              <Box display="inline" padding={{ left: 's' }}>
                                {engineList.map((e, i) => (
                                  <Box key={i} display="inline" padding={{ right: 'xxs' }}>
                                    <EngineBadge engine={e} />
                                  </Box>
                                ))}
                              </Box>
                            )}
                          </Box>
                          {p.when && (
                            <Box fontSize="body-s" color="text-body-secondary">
                              <Box fontWeight="bold" display="inline">{t('assignment-gate.optimization-details.when-label')} </Box>{p.when}
                            </Box>
                          )}
                          {p.example && (
                            <Box fontSize="body-s" color="text-body-secondary">{p.example}</Box>
                          )}
                        </SpaceBetween>
                      </Box>
                    );
                  })}
                </SpaceBetween>
              </Box>
            )}
          </SpaceBetween>
        </ExpandableSection>
      )}

      {/* #1003: one engine choice per table */}
      <PendingEngineChoices
        choices={pendingChoices}
        decided={autoDecided}
        engineLabel={(e) => ENGINE_COLORS[e]?.label || e}
        onSubmit={handleSaveChoices}
        saving={savingOverrides}
        t={t}
      />

      {/* Action Bar */}
      <Container>
        <div className="gate-actions">
          <Button
            variant="primary"
            onClick={() => setShowResumeModal(true)}
            loading={resuming}
            iconName="status-positive"
          >
            {t('assignment-gate.actions.continue-with-recommendation')}
          </Button>
          <button
            className="gate-actions__detail-link"
            onClick={() => setViewMode('advanced')}
          >
            {t('assignment-gate.actions.review-in-detail')} &rarr;
          </button>
        </div>
      </Container>
    </SpaceBetween>
  );


  // ---- Render: Advanced View ----
  const renderAdvancedView = () => (
    <SpaceBetween size="l">

      {/* Back to summary */}
      <Alert
        type="info"
        action={
          <Button variant="normal" onClick={() => setViewMode('summary')}>
            {t('assignment-gate.actions.back-to-summary')}
          </Button>
        }
      >
        {t('assignment-gate.advanced.info-alert-body')}
        {hasPendingOverrides && (
          <Box margin={{ top: 'xxs' }}>
            <StatusIndicator type="warning">{t('assignment-gate.advanced.unsaved-overrides', { count: overrideCount })}</StatusIndicator>
          </Box>
        )}
      </Alert>

      {/* #503: queries left as an unresolved tie between engines */}
      {tieUnresolvedCount > 0 && (
        <Alert type="warning" header={t('assignment-gate.advanced.tie-unresolved', { count: tieUnresolvedCount })} />
      )}

      {/* Overrides action bar */}
      {hasPendingOverrides && (
        <Alert type="warning" header={t('assignment-gate.advanced.pending-overrides-header', { count: overrideCount })}>
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="primary" onClick={handleSaveOverrides} loading={savingOverrides}>
              {t('assignment-gate.actions.save-overrides')}
            </Button>
            <Button variant="normal" onClick={() => { setOverrides({}); setCacheOverrides({}); }}>
              {t('assignment-gate.actions.discard-changes')}
            </Button>
          </SpaceBetween>
        </Alert>
      )}

      {/* Query Assignments */}
      <Container
        header={
          <Header
            variant="h2"
            counter={`(${filteredQueries.length})`}
            description={t('assignment-gate.table.description')}
            actions={
              <Pagination
                currentPageIndex={currentPage}
                pagesCount={Math.ceil(filteredQueries.length / PAGE_SIZE)}
                onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
              />
            }
          >
            {t('assignment-gate.table.title')}
          </Header>
        }
      >
        <SpaceBetween size="xxs">
          <PropertyFilter
            query={filterQuery}
            onChange={({ detail }) => { setFilterQuery(detail); setCurrentPage(1); }}
            filteringProperties={filterProperties}
            filteringOptions={filterOptions}
            filteringPlaceholder={t('assignment-gate.filter.placeholder')}
            countText={t('assignment-gate.filter.count-text', { count: filteredQueries.length })}
            expandToViewport
            i18nStrings={{
              filteringAriaLabel: t('assignment-gate.filter.aria-label'),
              dismissAriaLabel: t('assignment-gate.filter.dismiss-aria-label'),
              filteringPlaceholder: t('assignment-gate.filter.placeholder'),
              groupValuesText: t('assignment-gate.filter.group-values-text'),
              groupPropertiesText: t('assignment-gate.filter.group-properties-text'),
              operatorsText: t('assignment-gate.filter.operators-text'),
              operationAndText: t('assignment-gate.filter.operation-and-text'),
              operationOrText: t('assignment-gate.filter.operation-or-text'),
              operatorLessText: t('assignment-gate.filter.operator-less-text'),
              operatorLessOrEqualText: t('assignment-gate.filter.operator-less-or-equal-text'),
              operatorGreaterText: t('assignment-gate.filter.operator-greater-text'),
              operatorGreaterOrEqualText: t('assignment-gate.filter.operator-greater-or-equal-text'),
              operatorContainsText: t('assignment-gate.filter.operator-contains-text'),
              operatorDoesNotContainText: t('assignment-gate.filter.operator-does-not-contain-text'),
              operatorEqualsText: t('assignment-gate.filter.operator-equals-text'),
              operatorDoesNotEqualText: t('assignment-gate.filter.operator-does-not-equal-text'),
              editTokenHeader: t('assignment-gate.filter.edit-token-header'),
              propertyText: t('assignment-gate.filter.property-text'),
              operatorText: t('assignment-gate.filter.operator-text'),
              valueText: t('assignment-gate.filter.value-text'),
              cancelActionText: t('common.actions.cancel'),
              applyActionText: t('assignment-gate.filter.apply-action-text'),
              allPropertiesLabel: t('assignment-gate.filter.all-properties-label'),
              tokenLimitShowMore: t('assignment-gate.filter.token-limit-show-more'),
              tokenLimitShowFewer: t('assignment-gate.filter.token-limit-show-fewer'),
              clearFiltersText: t('assignment-gate.filter.clear-filters-text'),
              removeTokenButtonAriaLabel: (token) =>
                t('assignment-gate.filter.remove-token-aria-label', { propertyLabel: token.propertyLabel, operator: token.operator, value: token.value }),
              enteredTextLabel: (text) => t('assignment-gate.filter.entered-text-label', { text }),
            }}
          />

          {/* Column headers */}
          <div className="query-list-header">
            <div className="query-list-col query-list-col--query">{t('assignment-gate.table.col-query')}</div>
            <div className="query-list-col query-list-col--engine sortable-header" onClick={() => toggleSort('engine')}>
              {t('assignment-gate.table.col-engine')} {sortColumn === 'engine' && <span className="sort-arrow">{sortDirection === 'asc' ? '\u25B2' : '\u25BC'}</span>}
            </div>
            <div className="query-list-col query-list-col--confidence sortable-header" onClick={() => toggleSort('confidence')}>
              {t('assignment-gate.table.col-confidence')} {sortColumn === 'confidence' && <span className="sort-arrow">{sortDirection === 'asc' ? '\u25B2' : '\u25BC'}</span>}
            </div>
            <div className="query-list-col query-list-col--tables sortable-header" onClick={() => toggleSort('tables')}>
              {t('assignment-gate.table.col-tables')} {sortColumn === 'tables' && <span className="sort-arrow">{sortDirection === 'asc' ? '\u25B2' : '\u25BC'}</span>}
            </div>
            <div className="query-list-col query-list-col--signals sortable-header" onClick={() => toggleSort('signals')}>
              {t('assignment-gate.table.col-signals')} {sortColumn === 'signals' && <span className="sort-arrow">{sortDirection === 'asc' ? '\u25B2' : '\u25BC'}</span>}
            </div>
            <div className="query-list-col query-list-col--reason sortable-header" onClick={() => toggleSort('reason')}>
              {t('assignment-gate.table.col-reason')} {sortColumn === 'reason' && <span className="sort-arrow">{sortDirection === 'asc' ? '\u25B2' : '\u25BC'}</span>}
            </div>
          </div>

          {/* Query rows */}
          {paginatedQueries.length === 0 ? (
            <Box textAlign="center" padding="l">
              <Box variant="strong">{t('assignment-gate.table.no-matching-queries')}</Box>
              <Box variant="p" color="text-body-secondary">{t('assignment-gate.table.try-adjusting-filters')}</Box>
            </Box>
          ) : (
            paginatedQueries.map(item => {
              const isExpanded = expandedQueryIds.has(item.query_id);
              const currentEngine = overrides[item.query_id] || item.assigned_engine;
              const isOverridden = !!overrides[item.query_id] || item.query_id in cacheOverrides;
              const isCached = item.query_id in cacheOverrides ? cacheOverrides[item.query_id] : !!item.cache_engine;
              // #629: a row the post-schema router moved after approval gets its
              // own marker, distinct from a reality-check move.
              const moveKind = rowMoveKind(item, reroutedIds);
              const isMoved = moveKind !== null;
              // #503: mark the row the top banner's count is actually
              // about, driven by the structured
              // field, not the free-text reason.
              const isTied = (item.tied_engines || []).length > 0;
              const details = queryDetailsMap[item.query_id];
              const signals = querySignalsMap[item.query_id] || [];
              // #296: a cached read keeps its owner engine above but also carries
              // cache_engine/cache_pattern/cache_reason -- show it as a small
              // "cached by <engine>" indicator, never as its own owner row.
              const cacheInfo = getQueryCacheInfo(item);
              const cacheEngineLabel = cacheInfo ? (ENGINE_COLORS[cacheInfo.engine]?.label || cacheInfo.engine) : null;
              // #303: OpenSearch never owns a write -- hide it from this
              // row's picker rather than offer an override the server
              // rejects with no warning here.
              const rowEngineOptions = engineOptionsForQuery(
                engineOptions,
                details?.query_type,
                details?.query_text,
                sourceEngine,
              );

              return (
                <div key={item.query_id} className={`query-list-row ${isExpanded ? 'query-list-row--expanded' : ''} ${isMoved ? 'query-list-row--moved' : ''} ${isTied ? 'query-list-row--tied' : ''}`}>
                  {/* Collapsed row */}
                  <div className="query-list-row__summary" onClick={() => toggleQueryExpanded(item.query_id)}>
                    <div className="query-list-col query-list-col--query">
                      <span className="query-row-arrow">{isExpanded ? '\u25BC' : '\u25B6'}</span>
                      <span className="query-list-id">{item.query_id?.slice(0, 12)}...</span>
                      {moveKind === 'moved' && <span className="diff-badge">{t('assignment-gate.states.moved')}</span>}
                      {moveKind === 'rerouted' && (
                        <span className="diff-badge">{t('assignment-gate.states.moved-after-approval')}</span>
                      )}
                      {isOverridden && <span className="override-indicator">{t('assignment-gate.states.changed')}</span>}
                      {isTied && !isOverridden && (
                        <Badge color="grey">{t('assignment-gate.states.tied', { defaultValue: 'Tied' })}</Badge>
                      )}
                    </div>
                    <div className="query-list-col query-list-col--engine" onClick={e => e.stopPropagation()}>
                      <Select
                        selectedOption={{
                          label: ENGINE_COLORS[currentEngine]?.label || currentEngine,
                          value: currentEngine,
                        }}
                        onChange={({ detail: d }) => {
                          const newEngine = d.selectedOption.value;
                          if (newEngine === item.assigned_engine) {
                            setOverrides(prev => { const next = { ...prev }; delete next[item.query_id]; return next; });
                          } else {
                            setOverrides(prev => ({ ...prev, [item.query_id]: newEngine }));
                          }
                        }}
                        options={rowEngineOptions}
                        triggerVariant="option"
                        expandToViewport
                      />
                      <Checkbox
                        checked={isCached}
                        onChange={({ detail: d }) => {
                          if (d.checked === !!item.cache_engine) {
                            setCacheOverrides(prev => { const next = { ...prev }; delete next[item.query_id]; return next; });
                          } else {
                            setCacheOverrides(prev => ({ ...prev, [item.query_id]: d.checked }));
                          }
                        }}
                      >
                        {t('assignment-gate.table.cache-toggle', { defaultValue: 'Cache with ElastiCache' })}
                      </Checkbox>
                      {cacheInfo && (
                        <span
                          className="cache-overlay-indicator"
                          title={cacheInfo.reason || undefined}
                        >
                          {formatCachedByLine(cacheEngineLabel, { t })}
                        </span>
                      )}
                    </div>
                    <div className="query-list-col query-list-col--confidence">
                      {item.confidence ?? '-'}%
                    </div>
                    <div className="query-list-col query-list-col--tables">
                      {(item.source_tables || []).map(t => t.split('.').pop()).join(', ') || '-'}
                    </div>
                    <div className="query-list-col query-list-col--signals">
                      {signals.length > 0 ? (
                        <span className="signal-badges">
                          {signals.map((sig, i) => (
                            <span
                              key={i}
                              className="signal-badge"
                              style={sig.engine ? {
                                color: engineHex(sig.engine),
                                background: `${engineHex(sig.engine)}20`,
                              } : undefined}
                            >
                              {sig.signal.replace(/_/g, ' ')}
                            </span>
                          ))}
                        </span>
                      ) : '-'}
                    </div>
                    <div className="query-list-col query-list-col--reason">
                      {item.assignment_reason || '-'}
                    </div>
                  </div>

                  {/* Expanded detail — full width */}
                  {isExpanded && (
                    <div className="query-list-row__detail">
                      {details?.query_text ? (
                        <div className="query-detail-sql">
                          <Box variant="awsui-key-label" margin={{ bottom: 'xxs' }}>{t('assignment-gate.row-detail.sql-query')}</Box>
                          <pre className="query-detail-code">{details.query_text}</pre>
                        </div>
                      ) : (
                        <Box color="text-body-secondary" fontSize="body-s">{t('assignment-gate.row-detail.query-text-not-available')}</Box>
                      )}

                      {details && (
                        <div className="query-detail-meta">
                          <ColumnLayout columns={4} variant="text-grid">
                            <Box>
                              <Box variant="awsui-key-label">{t('assignment-gate.row-detail.type')}</Box>
                              <Box>{details.query_type || '-'}</Box>
                            </Box>
                            <Box>
                              <Box variant="awsui-key-label">{t('assignment-gate.row-detail.calls-per-sec')}</Box>
                              <Box>{details.calls_per_second?.toFixed(2) || '-'}</Box>
                            </Box>
                            <Box>
                              <Box variant="awsui-key-label">{t('assignment-gate.row-detail.avg-latency')}</Box>
                              <Box>{details.execution_time_ms_avg ? `${details.execution_time_ms_avg.toFixed(1)}ms` : '-'}</Box>
                            </Box>
                            <Box>
                              <Box variant="awsui-key-label">{t('assignment-gate.row-detail.tables-accessed')}</Box>
                              <Box>{(details.tables_accessed || []).map(tbl => tbl.split('.').pop()).join(', ') || '-'}</Box>
                            </Box>
                          </ColumnLayout>
                        </div>
                      )}

                      {details && (
                        <div className="query-detail-meta" style={{ marginTop: 8 }}>
                          <ColumnLayout columns={4} variant="text-grid">
                            <Box>
                              <Box variant="awsui-key-label">{t('assignment-gate.row-detail.joins')}</Box>
                              <Box>{details.has_joins ? `${t('common.labels.yes')} (${details.join_count})` : t('common.labels.no')}</Box>
                            </Box>
                            <Box>
                              <Box variant="awsui-key-label">{t('assignment-gate.row-detail.aggregations')}</Box>
                              <Box>{details.has_aggregations ? t('common.labels.yes') : t('common.labels.no')}</Box>
                            </Box>
                            <Box>
                              <Box variant="awsui-key-label">{t('assignment-gate.row-detail.text-search')}</Box>
                              <Box>{details.has_text_search ? (details.text_search_type || t('common.labels.yes')) : t('common.labels.no')}</Box>
                            </Box>
                            <Box>
                              <Box variant="awsui-key-label">{t('assignment-gate.row-detail.rows-examined')}</Box>
                              <Box>{details.rows_examined_avg?.toFixed(0) || '-'}</Box>
                            </Box>
                          </ColumnLayout>
                        </div>
                      )}

                      {signals.length > 0 && (
                        <div className="query-detail-signals">
                          <Box variant="awsui-key-label" margin={{ bottom: 'xxs', top: 'xs' }}>{t('assignment-gate.row-detail.triage-signals')}</Box>
                          <SpaceBetween direction="horizontal" size="xxs">
                            {signals.map((sig, i) => (
                              <EngineBadge key={i} engine={sig.engine}>
                                {sig.signal.replace(/_/g, ' ')}
                              </EngineBadge>
                            ))}
                          </SpaceBetween>
                        </div>
                      )}

                    </div>
                  )}
                </div>
              );
            })
          )}

        </SpaceBetween>
      </Container>

      {/* Approve and Continue */}
      <Container>
        <div className="gate-actions">
          <Button
            variant="primary"
            onClick={() => setShowResumeModal(true)}
            loading={resuming}
            disabled={hasPendingOverrides}
            iconName="status-positive"
          >
            {hasPendingOverrides ? t('assignment-gate.actions.save-overrides-first') : t('assignment-gate.actions.approve-and-continue')}
          </Button>
          <button
            className="gate-actions__detail-link"
            onClick={() => setViewMode('summary')}
          >
            &larr; {t('assignment-gate.actions.back-to-summary')}
          </button>
        </div>
      </Container>
    </SpaceBetween>
  );


  // ---- Main Render ----
  return (
    <>
      <AppHeader />
      <AppLayoutToolbar
        disableContentPaddings={false}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        breadcrumbs={<BreadcrumbGroup items={breadcrumbItems} />}
        navigation={
          <SideNavigation
            activeHref={`/analysis/assignments/${jobId}`}
            header={SideNavigationConfigurations.header}
            items={SideNavigationConfigurations.items}
          />
        }
        content={
          <SpaceBetween size="m">

            {flashbarItems.length > 0 && (
              <Flashbar
                items={flashbarItems.map(item => ({
                  ...item,
                  onDismiss: () => dismissFlash(item.id)
                }))}
              />
            )}

            <Header
              variant="h1"
              description=""
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button iconName="refresh" variant="normal" onClick={fetchData} />
                  {viewMode === 'summary' ? (
                    <Button variant="normal" onClick={() => setViewMode('advanced')}>
                      {t('assignment-gate.actions.advanced-review')}
                    </Button>
                  ) : (
                    <Button variant="normal" onClick={() => setViewMode('summary')}>
                      {t('assignment-gate.actions.summary-view')}
                    </Button>
                  )}
                </SpaceBetween>
              }
            >
              {t('assignment-gate.header.title')}
            </Header>

            {viewMode === 'summary' ? renderSummaryView() : renderAdvancedView()}

          </SpaceBetween>
        }
        contentType="default"
        toolsHide
      />

      {/* Resume Confirmation Modal */}
      <Modal
        visible={showResumeModal}
        onDismiss={() => setShowResumeModal(false)}
        header={t('assignment-gate.modal.header')}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowResumeModal(false)}>
                {t('common.actions.cancel')}
              </Button>
              <Button variant="primary" onClick={handleResume} loading={resuming}>
                {t('assignment-gate.modal.confirm')}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="s">
          <Box>
            {t('assignment-gate.modal.body')}
          </Box>
          <SpaceBetween direction="horizontal" size="xs">
            {survivingEngines.map(e => (
              <EngineBadge key={e} engine={e} />
            ))}
          </SpaceBetween>
          {eliminatedEngines.length > 0 && (
            <Box color="text-body-secondary" fontSize="body-s">
              {t('assignment-gate.modal.eliminated-engines', { engines: eliminatedEngines.map(e => ENGINE_COLORS[e]?.label || e).join(', ') })}
            </Box>
          )}
          {overrideCount > 0 && (
            <Alert type="info">
              {t('assignment-gate.modal.custom-overrides', { count: overrideCount })}
            </Alert>
          )}
        </SpaceBetween>
      </Modal>
    </>
  );
});

export default AssignmentGatePage;
