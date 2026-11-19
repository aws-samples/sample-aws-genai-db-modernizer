// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

//##-- React
import { useState, useEffect, memo, useCallback, useMemo, useRef } from 'react';
import { useParams, useLocation } from 'react-router-dom';
import { useTranslation } from 'react-i18next';

//##-- AWS UI Objects
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import useLayoutAriaLabels from "../hooks/useLayoutAriaLabels";
import ProseText from '../components/ProseText';
import { CardBox } from '../components/StyledBlocks';
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Flashbar from "@cloudscape-design/components/flashbar";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Alert from "@cloudscape-design/components/alert";
import Spinner from "@cloudscape-design/components/spinner";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import CodeEditor from "@cloudscape-design/components/code-editor";
import StatusIndicator from "@cloudscape-design/components/status-indicator";

//##-- Custom
import { SideNavigationConfigurations, ApiConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";
import QueryFlow from "../components/QueryFlow";
import SummaryFallbackNotice from "../components/SummaryFallbackNotice";
import QueryRoutingTable from "../components/QueryRoutingTable";
import ExploreGraphButton from "../components/ExploreGraphButton";
import useGraphExplorerFlag from "../hooks/useGraphExplorerFlag";
import CloseCallCallout from "../components/CloseCallCallout";
import UnservableQueriesCallout from "../components/UnservableQueriesCallout";
import DynamoDBMergeConflictsCallout from "../components/DynamoDBMergeConflictsCallout";
import DynamoDBMergeReviewsCallout from "../components/DynamoDBMergeReviewsCallout";
import RelatedSectionPointer, { unservedPointers } from "../components/RelatedSectionPointer";
import NoOpenRisksNote from "../components/NoOpenRisksNote";
import PinRuleNote from "../components/PinRuleNote";
import NoOpenRisksSection from "../components/NoOpenRisksSection";
import UnreviewedMovesNote from "../components/UnreviewedMovesNote";
import MigrationWavePlan from "../components/MigrationWavePlan";
import StepGateEvidence from "../components/StepGateEvidence";
import { schemaSectionIdOf } from "../utils/adoptionPaths";
import { engineFromSearch, linkedEngineTarget, linkedSectionId, activeDesignEngine } from "../utils/resultsLinks";
import useResultsTabs, { ResultsSectionNav, useSectionsOnPage } from "../hooks/useResultsTabs";
import { RESULTS_TABS, hashId, onThisPageAnchors, tabLabel } from "../utils/resultsTabs";
import ResultsOnThisPage from "../components/ResultsOnThisPage";
import { designBodyState, explorerAvailability } from "../utils/sectionStates";
import { failureError, failureFlash, failureKind, failureReason, reportFailure, upsertFlash } from "../utils/requestFailure";
import { schemaDesignsRefused } from "../utils/schemaDesignsLoad";
import ExecutiveSummary from "../components/ExecutiveSummary";
import TableOwnersCallout from "../components/TableOwnersCallout";
import DeliverablesPanel from "../components/DeliverablesPanel";
import AssumptionsPanel from "../components/AssumptionsPanel";
import CostBreakdown from "../components/CostBreakdown";
import TradeOffDecisions from "../components/TradeOffDecisions";
import TradeOffsEmptyNote from "../components/TradeOffsEmptyNote";
import { buildQueryRoutingRows, queryPatternLinks } from "../utils/queryRouting";
import QueryDetailPanel from "../components/QueryDetailPanel";
import SourceTablesSection from "../components/SourceTablesSection";
import { cacheLayerPatterns, ownerAccessPatterns, patternsByQueryId, shortTable, sourceNameMapper, sourceTableRows } from "../utils/accessPatterns";
import AuroraDesignTables, { AuroraLeftOutAlert } from "../components/AuroraDesignTables";
import AuroraTableDetailPanel from "../components/AuroraTableDetailPanel";
import { getCacheOverlay, ownerDistribution, formatCacheLayerLine, cacheChangeSentence, targetEngineEntries, resolveCostBreakdown, cacheAccessPatternCount, addCacheOverlayNode } from "../utils/cacheLayer";
import { summaryFallbackInfo } from "../utils/summaryFallback";
import { fallbackWithheldReason } from "../utils/fallbackWithheldReason";
import { PROCEDURE_INTERNAL_GROUP, buildAuroraDesigns, designTableKey, isAuroraEngine, reasonTextFor } from "../utils/auroraDesign";
import { codeEditorI18nStrings } from "../utils/codeEditorI18n";
import { RISK_SEVERITIES, UNRATED, filterRisksWithContent, groupRisksBySeverity, riskKey, riskSeverityLabel, riskSeverityStatus, sharedMitigationRef, splitRiskDescription } from "../utils/riskAssessment";
import { costBaselineStats } from "../utils/tcoAnalysis";
// #358: engine display names come from the shared mapping (checked against
// src/shared/engine_names.py by tests/unit/report/test_engine_names_js_sync.py)
// rather than this page's own hand-kept copy.
import { ENGINE_LABELS } from "../utils/engineNames";
import { formatRate } from "../utils/format";
import EngineBadge from "../components/EngineBadge";
import RiskCard from "../components/RiskCard";
import { optimizationLabel } from "../utils/optimizationLabels";
import { accessPatternTotals, assumptionItemsOf, cacheLayerFactsOf, costScopeLabelOf, defaultRiskSeverity, derivedCopySyncOf, designLeftOutOf, finalEngineCounts, noEngineInScopeOf, openRiskCountAt, overallRiskLevelOf, placedQueryCountOf, procedureInternalQueryCountOf, procedureInternalRoutineCountOn, procedureInternalRoutineCountsOf, queryPatternCountsOf, rateWindowRiskNoteOf, resolvedRiskCountOf, resolvedRisksBySeverityText, routingHistoryRecorded, routingKindsById, tableQueryRowsOf, targetEngineEntriesOf, tradeOffsByTab, tradeOffTabLabel, tradeOffTabsShown, tradeOffTopicGroupsOf } from "../utils/reportFacts";

// ============================================
// Constants
// ============================================

// GET /assessments/{job_id}/query-journeys returns at most 200 a page (#822).
const JOURNEY_PAGE_SIZE = 200;
const JOURNEY_MAX_PAGES = 50;



// ============================================
// Component
// ============================================

const AnalysisResultsPage = memo(() => {
  const { t } = useTranslation();
  const { jobId } = useParams();
  const { search: locationSearch, hash: locationHash } = useLocation();
  // #2300: the Table design tab a link names, read before the first render so the tabs
  // are controlled from the start.
  const [riskTab, setRiskTab] = useState(null);
  // #2322: which risks the reader opened, by risk key; it lives here so a tab switch keeps it.
  const [riskOpen, setRiskOpen] = useState({});
  const onToggleRisk = useCallback((id, expanded) => setRiskOpen((o) => ({ ...o, [id]: expanded })), []);
  const [designTab, setDesignTab] = useState(() => engineFromSearch(locationSearch));
  const [navigationOpen, setNavigationOpen] = useState(false);
  // The experimental graph explorer (#308): its entry shows only when the server flag is on.
  const graphExplorer = useGraphExplorerFlag();
  const [resultsData, setResultsData] = useState(null);
  const [schemaDesigns, setSchemaDesigns] = useState([]);
  // #2308: the design files could not be read, so an empty design is "not available", not "none".
  const [designsLoadFailed, setDesignsLoadFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [flashbarItems, setFlashbarItems] = useState([]);
  // #2313: the tabs follow the URL (?tab=, or the tab of the section a hash names).
  const { activeTab, selectTab, goToSection } = useResultsTabs({ ready: !loading && !!resultsData });
  const [tabsRoot, setTabsRoot] = useState(null);
  const sectionsOnPage = useSectionsOnPage(activeTab, tabsRoot);
  const onThisPage = onThisPageAnchors(activeTab, sectionsOnPage, t);
  // #2313 review: the unserved queries (Plan) and the merge's unserved access patterns
  // and decisions (Risks and decisions) point at each other.
  const relatedPointers = unservedPointers(resultsData?.synthesis);
  // A tab with one section or none needs no navigation.
  const toolsHidden = onThisPage.length < 2;
  // "On this page" opens with the page on a wide screen; on a narrow one it would cover it.
  const [toolsOpen, setToolsOpen] = useState(
    () => typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      && window.matchMedia('(min-width: 1400px)').matches,
  );

  // #2446: the property filter token a ?engine= link opens the query table with.
  const [linkedQueryFilter, setLinkedQueryFilter] = useState(null);

  // Query detail split panel state (#2319)
  const [splitPanelOpen, setSplitPanelOpen] = useState(false);
  const [queryJourneyData, setQueryJourneyData] = useState(null);
  const [queryJourneyLoading, setQueryJourneyLoading] = useState(false);
  // #339: the journey's "Why this engine" section (graph/queries/{id}/trace)
  const [queryTrace, setQueryTrace] = useState({ data: null, error: null });
  const [selectedQueryId, setSelectedQueryId] = useState(null);
  // #2480 review: raised when a query opens from the source table dialog, so the panel takes focus.
  const [panelFocusRequest, setPanelFocusRequest] = useState(0);
  // #2325: the Table design row shown in the split panel ({ engine, tableName }), or null.
  const [designSelection, setDesignSelection] = useState(null);
  // #822: every query's journey (the query-journeys list), for the query
  // routing table that opens a trace without schema design.
  const [queryJourneyList, setQueryJourneyList] = useState({ items: [], loading: true, error: null, truncated: false });
  // #957: the job's deliverables/ folder, for the download panel.
  const [deliverables, setDeliverables] = useState({ files: [], loading: true, error: null });

  // Ace editor state
  const [ace, setAce] = useState(null);
  const [aceLoading, setAceLoading] = useState(true);

  const addFlashbarMessage = useCallback((message) => {
    setFlashbarItems(prevItems => upsertFlash(prevItems, message));
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
      ]);

      if (results['results']?.success) {
        setResultsData(results['results']);
        setFlashbarItems([]);
      } else if (results['results']?.error) {
        addFlashbarMessage(failureFlash(t, results['results'], { header: t('analysis-results.error.failed-to-load'), context: 'results', id: 'results-load-failed', onRetry: () => fetchData() }));
      }

      if (results['schemas']?.success) {
        setSchemaDesigns(results['schemas'].schema_designs || []);
      }
      setDesignsLoadFailed(!results['schemas']?.success);
      if (schemaDesignsRefused(results['schemas'])) {
        reportFailure(results['schemas'], 'schema-designs');
        addFlashbarMessage({ type: 'error', header: t('analysis-results.error.schema-designs-refused'), content: failureReason(results['schemas'], t) || t('analysis-results.error.schema-designs-refused-detail'), dismissible: true, id: `error-schemas-${Date.now()}` });
      }

    } catch (error) {
      addFlashbarMessage(failureFlash(t, error, { header: t('analysis-results.error.failed-to-load'), context: 'results', id: 'results-load-failed', onRetry: () => fetchData() }));
    } finally {
      setLoading(false);
    }
  }, [jobId, addFlashbarMessage, t]);

  useEffect(() => { fetchData(); }, [jobId]); // eslint-disable-line react-hooks/exhaustive-deps

  // #1991: an assumptions change re-ran synthesis and the deliverables: reload both.
  const [deliverablesRound, setDeliverablesRound] = useState(0);
  const reloadAfterAssumptionChange = useCallback(() => {
    fetchData();
    setDeliverablesRound(n => n + 1);
  }, [fetchData]);

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
        if (!first?.success) throw failureError(first, 'query-journeys');
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
            if (!rest[id]?.success) throw failureError(rest[id], 'query-journeys');
            items = items.concat(rest[id].items || []);
          });
        }
        if (!cancelled) setQueryJourneyList({ items, loading: false, error: null, truncated: totalPages > pages });
      } catch (error) {
        if (!cancelled) setQueryJourneyList({ items: [], loading: false, error: failureKind(error), truncated: false });
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
        if (!res?.success) throw failureError(res, 'deliverables');
        if (!cancelled) setDeliverables({ files: res.files || [], loading: false, error: null });
      } catch (error) {
        if (!cancelled) setDeliverables({ files: [], loading: false, error: failureKind(error) });
      }
    };
    load();
    return () => { cancelled = true; };
  }, [jobId, deliverablesRound]);

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
  // #296 cache overlay: a top-level synthesis.cache_overlay signals that
  // ElastiCache is a cache layer here, not an owner -- defensively strip it
  // from the owner distribution so it can't show up as its own Sankey/target
  // engine node with a workload share. Legacy reports with no cache_overlay
  // (ElastiCache still a real owner) are unaffected. Plain consts (not
  // useMemo) to match synthesis above, which is the same.
  const cacheOverlay = getCacheOverlay(synthesis);
  const afterDist = ownerDistribution(finalEngineCounts(synthesis), !!cacheOverlay);
  // #1252: the designed cached reads, from the facts (the reads the ElastiCache design covers).
  const cacheFacts = cacheLayerFactsOf(synthesis);
  const cacheChange = cacheChangeSentence(cacheFacts, { t });
  const cacheLayerLine = formatCacheLayerLine(
    cacheFacts
      ? { query_count: cacheFacts.designed, call_share_percent: cacheFacts.designedCallShare }
      : cacheOverlay,
    { t, kind: 'designed' },
  );

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
    () => targetEngineEntriesOf(synthesis) ?? targetEngineEntries(afterDist, cacheOverlay),
    [synthesis, afterDist, cacheOverlay]
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
  const overallRiskLevel = overallRiskLevelOf(synthesis);
  const openRisks = useMemo(
    () => filterRisksWithContent(riskAssessment.risks),
    [riskAssessment]
  );
  const resolvedRisksList = useMemo(
    () => filterRisksWithContent(riskAssessment.resolved_risks),
    [riskAssessment]
  );
  const risksBySeverity = useMemo(() => groupRisksBySeverity(openRisks), [openRisks]);
  const riskTabIds = [
    ...RISK_SEVERITIES,
    ...((risksBySeverity[UNRATED] || []).length > 0 ? [UNRATED] : []),
  ];

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

  // Build Sankey from the final engine counts (facts.engine_query_counts, #2358)
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
  const layoutAriaLabels = useLayoutAriaLabels();


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

  // #361: the owner engines' patterns; the cache layer's are cacheLayerPatterns below.
  const allAccessPatterns = useMemo(
    () => ownerAccessPatterns(activeDesigns, cacheOverlay, auroraReasonByPatternId),
    [activeDesigns, cacheOverlay, auroraReasonByPatternId],
  );

  // #429 part 2: the cache layer's own access patterns and key designs, tagged
  // isCacheLayer. No-op without a cache_overlay.
  const cacheLayerPatternList = useMemo(
    () => cacheLayerPatterns(activeDesigns, cacheOverlay),
    [activeDesigns, cacheOverlay],
  );

  // #2319: the query table's rows, with each query's destination tables and its
  // cache engine from the schema design's patterns (a dash when none was designed).
  const queryRoutingRows = useMemo(
    () => buildQueryRoutingRows(
      queryJourneyList.items,
      routingKindsById(resultsData?.synthesis),
      ENGINE_LABELS,
      queryPatternLinks(allAccessPatterns, cacheLayerPatternList),
    ),
    [queryJourneyList.items, resultsData, allAccessPatterns, cacheLayerPatternList],
  );
  // #2446: each query's access patterns and cache key designs, for its detail panel.
  const patternsOfQuery = useMemo(
    () => patternsByQueryId(allAccessPatterns, cacheLayerPatternList),
    [allAccessPatterns, cacheLayerPatternList],
  );

  // Owner rows followed by the cache-layer rows -- the explorer's combined
  // browsing set. Everything that must stay owner-only (the "Access
  // patterns" stat, the engine pie) keeps reading allAccessPatterns/
  // totalAccessPatterns directly instead of this.
  const explorerPatterns = useMemo(
    () => [...allAccessPatterns, ...cacheLayerPatternList],
    [allAccessPatterns, cacheLayerPatternList]
  );
  // #2446: the Data model tab's source tables (the explorer's "By source table" view).
  const sourceTables = useMemo(
    () => sourceTableRows(allAccessPatterns, cacheLayerPatternList),
    [allAccessPatterns, cacheLayerPatternList],
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
    () => buildAuroraDesigns(activeDesigns, synthesis?.query_groups, tableQueryRowsOf(synthesis), designLeftOutOf(synthesis)),
    [activeDesigns, synthesis]
  );
  // #2325: the design table the split panel shows, with its engine's display name.
  const designTable = useMemo(() => {
    const design = designSelection && auroraDesigns.find(d => d.engine === designSelection.engine);
    const table = design?.tables.find(tb => tb.key === designSelection.key);
    return table ? { table, engineName: design.displayName } : null;
  }, [designSelection, auroraDesigns]);

  // Destination table definitions lookup: destName → { engine, def, allSourceTables }
  const destTableLookup = useMemo(() => {
    const lookup = {};
    const shorten = sourceNameMapper(activeDesigns);
    activeDesigns.forEach(design => {
      const engine = design.target_type;
      const content = design.content || {};
      (content.table_definitions || []).forEach(t => {
        const name = t.schema_name ? designTableKey(t.table_name, t.schema_name) : t.table_name;
        lookup[`${engine}::${name}`] = {
          engine,
          name,
          def: t,
          allSourceTables: (t.source_tables || []).map(shorten),
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

  // #429 part 2: the explorer's own combined total (owner rows + cache-layer
  // rows), used for its header count ("64 (54 owned + 10 cache layer)") --
  // kept separate from totalAccessPatterns, which stays owner-only.
  const explorerTotal = patternTotals.listed;
  // #2308: the report counts patterns the page holds no row for: say so, do not show an empty table.
  const explorerState = explorerAvailability({
    expectedCount: explorerTotal,
    shownCount: explorerPatterns.length,
  });


  // ============================================
  // Handlers
  // ============================================

  const fetchQueryJourney = useCallback(async (queryId, options) => {
    setDesignSelection(null);
    setSelectedQueryId(queryId);
    if (options && options.focusPanel) setPanelFocusRequest(n => n + 1);
    setSplitPanelOpen(true);
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
        setQueryTrace({ data: null, error: reportFailure(trace, 'query-trace') });
      }

      if (result['query-journey']?.success) {
        setQueryJourneyData(result['query-journey']);
      } else if (result['query-journey']?.error) {
        setQueryJourneyData({ error: reportFailure(result['query-journey'], 'query-journey') });
      } else {
        setQueryJourneyData({ error: 'not-found' });
      }
    } catch (error) {
      console.error('Error fetching query journey:', error);
      setQueryJourneyData({ error: reportFailure(error, 'query-journey') });
    } finally {
      setQueryJourneyLoading(false);
    }
  }, [jobId, t]);

  const closeDesignDetail = useCallback(() => {
    setDesignSelection(null);
    setSplitPanelOpen(false);
  }, []);

  // One split panel serves the page: a table opens it in place of a query, and back.
  const selectDesignTable = useCallback((engine, key) => {
    setSelectedQueryId(null);
    setQueryJourneyData(null);
    setQueryTrace({ data: null, error: null });
    setDesignSelection({ engine, key });
    setSplitPanelOpen(true);
  }, []);

  const handleCloseQueryDetail = useCallback(() => {
    setSplitPanelOpen(false);
    setQueryJourneyData(null);
    setQueryTrace({ data: null, error: null });
    setSelectedQueryId(null);
  }, []);



  // #2300: a link from the Sankey (or an old pattern-analysis link) names an
  // engine (?engine=) and a section (#hash). Once the page has its data, filter
  // the query table to that engine (#2446; its Table design tab for a relational one
  // with a design), say so when the plan has no part for it or its design has not
  // run, then open the section's tab (#2313), scroll to the section and move focus
  // there. Done once per page load, so the user's own filter changes stand.
  const linkedEngineHandled = useRef(false);
  useEffect(() => {
    if (loading || linkedEngineHandled.current) return;
    linkedEngineHandled.current = true;
    const engine = engineFromSearch(locationSearch);
    const target = linkedEngineTarget(
      engine,
      explorerPatterns.map(ap => ap.engine),
      auroraDesigns.map(d => d.engine),
      Object.keys(afterDist || {}),
    );
    // #2313: the section's tab opens first (a section on a closed tab is not in the DOM).
    const sectionId = linkedSectionId(target, locationHash);
    if (target === 'queries' || target === 'not-designed') {
      // The cache layer owns no query: its link lists the queries it caches.
      setLinkedQueryFilter(engine === cacheOverlay?.engine
        ? { propertyKey: 'cached', value: 'yes' }
        : { propertyKey: 'engine', value: engine });
    }
    if (target === 'absent' || target === 'not-designed') {
      addFlashbarMessage({
        type: 'info',
        header: t(`analysis-results.engine-link.${target}`, { engine: ENGINE_LABELS[engine] || engine }),
        dismissible: true,
        id: `engine-link-${engine}`,
      });
    }
    // useResultsTabs lands on the hash's section already; a linked engine's section can
    // differ from it (its Table design).
    if (sectionId && sectionId !== hashId(locationHash)) goToSection(sectionId);
  }, [loading, locationSearch, locationHash, explorerPatterns, auroraDesigns, afterDist, cacheOverlay, addFlashbarMessage, goToSection, t]);



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
          // Controlled from the first render (Cloudscape decides that once, and the page
          // keeps this layout when its results arrive), so On this page can open then.
          toolsOpen={false}
          toolsHide
          // The query detail's split panel is controlled from the first render for the
          // same reason, so it can open once the results are in (#2319).
          splitPanelOpen={false}
          onSplitPanelToggle={() => {}}
        />
      </>
    );
  }


  // ============================================
  // Main render
  // ============================================

  // #2313: one tab per reader question, each in the order of the decision: what to do
  // and what it costs, how to do it, the evidence, and what can go wrong. The sections
  // are the ones the page always had, moved, not changed; RESULTS_TABS
  // (utils/resultsTabs.js) lists which tab holds which, for the URL and On this page.
  const tabContent = {
    summary: (
      <SpaceBetween size="l">
        {/* Executive Summary (#2315): the decision in the header, five figures, then the
            summary's paragraphs under "Why". Every figure is a report.json fact. */}
        <ExecutiveSummary
          synthesis={synthesis}
          t={t}
          why={(
            <SpaceBetween size="m">
              {/* #1403: one paragraph per part (recommendation, risk, decision), as
                  the engineering report prints them. */}
              <Box variant="div" fontSize="body-m">
                {synthesis?.summary ? <ProseText text={synthesis.summary} paragraphs /> : 'No summary available.'}
              </Box>
              <PinRuleNote note={resultsData?.pin_rule_note} />
              {summaryFallback.isFallback && (
                <SummaryFallbackNotice
                  text={t('analysis-results.executive-summary.fallback-notice', {
                    reason: fallbackWithheldReason(summaryFallback.warnings),
                  })}
                />
              )}
            </SpaceBetween>
          )}
          statsProps={{
            projectedCost,
            costBaseline,
            retiredEngines: synthesis?.facts?.retired_in_use_engines,
            engineScope: synthesis?.facts?.engine_scope,
            noEngineInScope: noEngineInScopeOf(synthesis),
            priceList: synthesis?.facts?.price_list,
            costScopeLabel: costScopeLabelOf(synthesis),
            cacheLayerLine,
            // #1252: the cache candidates the design does not cover stay on the tile as
            // a warning, as the executive report prints them in its body.
            cacheChange,
            // The longer notes sit behind the Target engines' Info link: #399 why the
            // cache layer is cache-aside (facts.cache_layer.role_sentence) and #1891 per
            // non-Aurora engine its statements on temporary objects that follow their routine
            // (facts.query_group_counts.procedure_internal_routine_counts).
            targetEngineNotes: [
              cacheLayerLine && cacheFacts?.roleSentence,
              ...Object.entries(procedureInternalRoutineCountsOf(synthesis))
                .filter(([engine, count]) => count > 0 && !isAuroraEngine(engine))
                .map(([engine, count]) => t('analysis-results.procedure-internal-routine-on', {
                  engine: ENGINE_LABELS[engine] || engine,
                  count,
                })),
            ],
            targetEngines: (
              <SpaceBetween direction="horizontal" size="xxs">
                {/* #358: the cache layer is listed here too (labeled distinctly,
                    e.g. "ElastiCache (cache layer)") -- it owns no queries, but it is
                    still a target engine the deliverables name and price. */}
                {targetEngines.map(({ engine, isCacheLayer }) => (
                  <EngineBadge key={engine} engine={engine}>
                    {ENGINE_LABELS[engine] || engine}
                    {isCacheLayer ? t('cache-layer.engine-badge-suffix', { defaultValue: ' (cache layer)' }) : ''}
                  </EngineBadge>
                ))}
              </SpaceBetween>
            ),
          }}
        />

        {/* Cost Breakdown (#2316: total first, engines in a table, notes in one expandable) */}
        {costBreakdown.length > 0 && (
          <CostBreakdown
            synthesis={synthesis}
            costBreakdown={costBreakdown}
            projectedCost={projectedCost}
            cacheEngine={cacheOverlay?.engine}
            t={t}
          />
        )}

        {/* Query Flow (Sankey) */}
        {sankeyData && (
          <Container id="query-flow-section" header={
            <Header variant="h2" description={t('analysis-results.query-flow.description')}>
              {t('analysis-results.query-flow.title')}
            </Header>
          }>
            <QueryFlow
              total={placedQueryCountOf(synthesis)}
              height={Math.max(250, Object.keys(afterDist).length * 120)}
              data={sankeyData}
            />
          </Container>
        )}

        {/* #1991: the assumptions a customer can change, re-running only what a
            change affects (the server checks and records it). */}
        <AssumptionsPanel jobId={jobId} t={t} onChanged={reloadAfterAssumptionChange} items={assumptionItemsOf(synthesis)} />
      </SpaceBetween>
    ),
    plan: (
      <SpaceBetween size="l">
        {/* Migration roadmap (#957): report.json's waves, the same ones the
            PDF and the engineering report show. */}
        <MigrationWavePlan
          synthesis={synthesis}
          t={t}
          schemaSectionId={schemaSectionIdOf(totalAccessPatterns > 0 || cachePatternCount > 0, auroraDesigns.length > 0)}
        />
        <StepGateEvidence synthesis={synthesis} t={t} />

        {/* Queries on a fallback engine (#729): where each landed and why,
            its gates, a redesign hint and the assignment review that
            re-runs. Warning first, as in both reports. */}
        <UnservableQueriesCallout
          synthesis={resultsData?.synthesis}
          jobId={jobId}
          onOpen={fetchQueryJourney}
          t={t}
        />
        <RelatedSectionPointer target={relatedPointers.toRisks} t={t} />

        {/* Close calls (#730, #2318): queries a kept engine could serve almost
            as well, one table row each, with the way into each one's decision
            trace and the assignment review that moves them. */}
        <CloseCallCallout
          synthesis={resultsData?.synthesis}
          jobId={jobId}
          onOpen={fetchQueryJourney}
          queries={queryRoutingRows}
          t={t}
        />

        {/* Reality-check moves no model reviewed (#746): one line when any. */}
        <UnreviewedMovesNote synthesis={resultsData?.synthesis} t={t} />
      </SpaceBetween>
    ),
    queries: (
      <SpaceBetween size="l">
        {/* Query table (#822, #2319): every query, open, with a property
            filter; the selected query's detail opens in the split panel.
            #2446: the tab's one list. The access pattern explorer's pattern and
            cache tables folded into it: a query's patterns open in its panel, and
            the by-source-table view is on the Data model tab. */}
        <QueryRoutingTable
          rows={queryRoutingRows}
          queryPatterns={queryPatternCountsOf(synthesis)}
          loading={queryJourneyList.loading}
          error={queryJourneyList.error}
          hasLedger={routingHistoryRecorded(resultsData?.synthesis)}
          truncated={queryJourneyList.truncated}
          selectedId={selectedQueryId}
          onSelect={fetchQueryJourney}
          linkedFilter={linkedQueryFilter}
          t={t}
        />
      </SpaceBetween>
    ),
    'data-model': (
      <SpaceBetween size="l">
        {/* Table owners (#212, #2321): each mapped table's owner (its source
            of truth), the engines that read a CDC copy of it, the reads a
            replica serves and its cache, as the engineering report's
            Migration map and Sources of truth state them, with the
            eventual-consistency risk once behind an Info link. Collapsed. */}
        <TableOwnersCallout synthesis={synthesis} t={t} />

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
              activeTabId={activeDesignEngine(designTab, auroraDesigns.map(d => d.engine))}
              onChange={({ detail }) => {
                // The panel shows one engine's table: it closes when the engine changes.
                if (designSelection && designSelection.engine !== detail.activeTabId) closeDesignDetail();
                setDesignTab(detail.activeTabId);
              }}
              tabs={auroraDesigns.map(design => {
                const queryCount = design.queryGroups.reduce((sum, g) => sum + g.queries.length, 0);
                // #2308: the tab and the body read one count, and say why a design is empty.
                const designState = designBodyState({
                  hasDesign: design.hasDesign,
                  loadedCount: design.tables.length,
                  synthesis,
                  engine: design.engine,
                  designsLoadFailed,
                });
                const hasTableCount = designState.state === 'ok' || designState.state === 'partial';
                return {
                  id: design.engine,
                  label: hasTableCount
                    ? t('analysis-results.aurora-design.tab-label', { name: design.displayName, count: designState.count })
                    : t(
                      designState.state === 'empty'
                        ? 'analysis-results.aurora-design.tab-label'
                        : `analysis-results.aurora-design.tab-label-${designState.state}`,
                      designState.state === 'empty'
                        ? { name: design.displayName, count: 0 }
                        : { name: design.displayName },
                    ),
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

                      {designState.longer && (
                        <Alert type="info" header={t('analysis-results.aurora-design.longer-title')}>
                          {t('analysis-results.aurora-design.longer-body', { count: designState.count, loaded: designState.loaded })}
                        </Alert>
                      )}
                      {designState.state === 'partial' && (
                        <Alert type="info" header={t('analysis-results.aurora-design.partial-title')}>
                          {t('analysis-results.aurora-design.partial-body', { loaded: designState.loaded, count: designState.count })}
                        </Alert>
                      )}
                      {(designState.state === 'none' || designState.state === 'unavailable') && (
                        <Alert
                          type={designState.state === 'none' ? 'info' : 'warning'}
                          header={t(`analysis-results.aurora-design.${designState.state}-title`)}
                        >
                          {t(
                            designState.state === 'unavailable' && !designState.count
                              ? 'analysis-results.aurora-design.unavailable-body-nocount'
                              : `analysis-results.aurora-design.${designState.state}-body`,
                            { count: designState.count },
                          )}
                        </Alert>
                      )}

                      {designState.state !== 'none' && designState.state !== 'unavailable' && (
                        <AuroraLeftOutAlert leftOut={design.leftOut} t={t} />
                      )}

                      {designState.state !== 'none' && designState.state !== 'unavailable' && (
                      <>
                        {design.tables.length === 0 ? (
                          <Box color="text-body-secondary" fontSize="body-s">{t('analysis-results.aurora-design.no-tables')}</Box>
                        ) : (
                          <AuroraDesignTables
                            design={design}
                            count={designState.count}
                            selectedKey={designSelection?.engine === design.engine ? designSelection.key : null}
                            onSelect={(key) => selectDesignTable(design.engine, key)}
                            t={t}
                          />
                        )}
                      </>
                      )}

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
                              <CardBox key={idx} tone="info">
                                <SpaceBetween size="xxs">
                                  <Box fontSize="body-s" fontWeight="bold">
                                    {opt.target ? `${opt.category ? `${optimizationLabel(opt.category)}: ` : ''}${opt.target}` : optimizationLabel(opt.category)}
                                  </Box>
                                  <Box fontSize="body-s"><ProseText text={opt.recommendation} /></Box>
                                  {opt.rationale && (
                                    <Box fontSize="body-s" color="text-body-secondary"><ProseText text={opt.rationale} /></Box>
                                  )}
                                </SpaceBetween>
                              </CardBox>
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
                            {procedureInternalQueryCountOf(synthesis) > 0 &&
                              design.queryGroups.some(g => g.sourceTable === PROCEDURE_INTERNAL_GROUP) && (
                              <Box color="text-body-secondary" fontSize="body-s">
                                {t('analysis-results.aurora-design.procedure-internal', {
                                  count: procedureInternalQueryCountOf(synthesis),
                                })}
                              </Box>
                            )}
                            {procedureInternalRoutineCountOn(synthesis, design.engine) > 0 && (
                              <Box color="text-body-secondary" fontSize="body-s">
                                {t('analysis-results.aurora-design.procedure-internal-routine', {
                                  count: procedureInternalRoutineCountOn(synthesis, design.engine),
                                })}
                              </Box>
                            )}
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
                                      minWidth: 240,
                                    },
                                    {
                                      id: 'type',
                                      header: t('analysis-results.aurora-design.col-type'),
                                      cell: q => q.queryType || '—',
                                      width: 100,
                                    },
                                    {
                                      id: 'rps',
                                      header: t('analysis-results.aurora-design.col-calls-per-second'),
                                      cell: q => formatRate(q.callsPerSecond),
                                      width: 100,
                                    },
                                    {
                                      id: 'reason',
                                      header: t('analysis-results.aurora-design.col-reason'),
                                      cell: q => (q.reason ? <ProseText text={q.reason} /> : '—'),
                                      minWidth: 240,
                                    },
                                  ]}
                                  wrapLines
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

        {/* Source tables (#2446): the access pattern explorer's "By source
            table" view, each source table's destination designs with their keys
            and indexes; a pattern's queries open in the query detail panel. */}
        {(totalAccessPatterns > 0 || cachePatternCount > 0) && (
          <SourceTablesSection
            rows={sourceTables}
            availability={explorerState}
            expectedCount={explorerTotal}
            shownCount={explorerPatterns.length}
            cacheEngine={cacheOverlay?.engine}
            destTableLookup={destTableLookup}
            onSelectQuery={fetchQueryJourney}
            t={t}
          />
        )}
      </SpaceBetween>
    ),
    risks: (
      <SpaceBetween size="l">
        <div id="risk-assessment-section">
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
                    {t('analysis-results.risk-assessment.overall-risk', { severity: riskSeverityLabel(overallRiskLevel, t).toLowerCase() })}
                  </StatusIndicator>
                )}
                {/* #1991: the rate-driven risks rest on a window that is not representative */}
                {rateWindowRiskNoteOf(synthesis) && (
                  <Box fontSize="body-s" color="text-status-warning">
                    <span data-figure="rate-window-scenario-risks">{rateWindowRiskNoteOf(synthesis)}.</span>
                  </Box>
                )}
                <NoOpenRisksNote
                  synthesis={resultsData?.synthesis}
                  shownRisks={openRisks}
                  caveat={resultsData?.no_open_risks_caveat}
                />
                <Tabs
                  activeTabId={
                    riskTabIds.includes(riskTab)
                      ? riskTab
                      : defaultRiskSeverity(synthesis, risksBySeverity, riskTabIds)
                  }
                  onChange={({ detail }) => setRiskTab(detail.activeTabId)}
                  tabs={riskTabIds.map((severity) => {
                    const risksAtSeverity = risksBySeverity[severity] || [];
                    const severityCount = openRiskCountAt(synthesis, severity, risksAtSeverity);
                    return {
                      id: severity,
                      label: t('analysis-results.risk-assessment.tab-label', { severity: riskSeverityLabel(severity, t), count: severityCount }),
                      content: risksAtSeverity.length === 0 ? (
                        <Box color="text-body-secondary" padding="s">
                          {t('analysis-results.risk-assessment.no-risks-at-severity')}
                        </Box>
                      ) : (
                        <SpaceBetween size="s">
                          {risksAtSeverity.map((risk, idx) => {
                            const key = riskKey(severity, risk, idx);
                            return (
                              <RiskCard
                                key={key}
                                id={key}
                                risk={risk}
                                severity={severity}
                                expanded={riskOpen[key] ?? false}
                                onToggle={onToggleRisk}
                                headingLevel="h3"
                                engineLabel={ENGINE_LABELS[splitRiskDescription(risk.description).engine]}
                                mitigationRef={sharedMitigationRef(resultsData?.mitigation_refs, risk)}
                                copySync={derivedCopySyncOf(synthesis, risk.risk_id)}
                                t={t}
                              />
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
                                <StatusIndicator type={riskSeverityStatus(risk.severity)}>{riskSeverityLabel(risk.severity, t)}</StatusIndicator>
                                {risk.resolved_on ? (
                                  <Box fontSize="body-s" color="text-body-secondary">
                                    {t('analysis-results.risk-assessment.resolved-where', { from: fromEngine, to: toEngine })}
                                  </Box>
                                ) : (
                                  <Box fontSize="body-s" color="text-body-secondary">{fromEngine}</Box>
                                )}
                              </SpaceBetween>
                              <Box fontSize="body-s"><ProseText text={body} /></Box>
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
        </div>

        {/* DynamoDB merge conflicts (#802): the access patterns the merged
            design does not serve, as both reports state them. */}
        <DynamoDBMergeConflictsCallout synthesis={resultsData?.synthesis} t={t} />
        <RelatedSectionPointer target={relatedPointers.toRisks === 'dynamodb-merge-conflicts' ? relatedPointers.toPlan : null} t={t} />

        {/* Engine-specific Trade-offs */}
        {tradeOffTabsShown(engineTradeOffs).length > 0 && (
          <Container
            id="tradeoffs-section"
            header={<Header variant="h2">{t('analysis-results.tradeoffs.title')}</Header>}
          >
            <Tabs
              tabs={tradeOffTabsShown(engineTradeOffs)
                .map(([engine, tradeoffs]) => {
                  // Separate PE notes from design decisions. Only the fallback below
                  // (a tab the facts list no topic groups for, e.g. a design the report
                  // does not carry) reads the `[PE note]` marker out of the text;
                  // otherwise the facts' review-note group is the PE notes (#1221).
                  const normalized = tradeoffs.map(tradeOff =>
                    typeof tradeOff === 'object' && tradeOff !== null && tradeOff.description
                      ? tradeOff
                      : { description: String(tradeOff), impact: '', source_tables: [], target_tables: [], query_ids: [] }
                  );
                  const peNotes = normalized.filter(tradeOff => tradeOff.description.startsWith('[PE note]'));
                  const decisions = normalized.filter(tradeOff => !tradeOff.description.startsWith('[PE note]'));

                  // #1221: the design decisions by topic, as facts.trade_off_topics groups
                  // them (labels and counts too), the design review notes apart; a tab with
                  // no groups (a design the report does not list) lists them as before.
                  const topicGroups = tradeOffTopicGroupsOf(resultsData?.synthesis, engine);
                  const shownPeNotes = topicGroups ? topicGroups.reviewNotes : peNotes;
                  const peNoteCount = topicGroups ? topicGroups.reviewNoteCount : peNotes.length;
                  // One label with the engineering report: the facts' own, and the same
                  // words in the fallback's string.
                  const peNotesHeader = topicGroups
                    ? t('analysis-results.tradeoffs.topic-heading', { label: topicGroups.reviewNoteLabel, count: peNoteCount })
                    : t('analysis-results.trade-offs.pe-notes-header', { count: peNoteCount });
                  return {
                    id: engine,
                    label: tradeOffTabLabel(engine, tradeoffs.length, t, ENGINE_LABELS),
                    content: (
                      <SpaceBetween size="m">
                        {/* #2324: a design with no trade-off left says so. */}
                        <TradeOffsEmptyNote engine={engine} tradeOffs={tradeoffs} engineLabels={ENGINE_LABELS} t={t} />
                        {/* Design Decisions */}
                        <TradeOffDecisions
                          groups={topicGroups ? topicGroups.topics : null}
                          decisions={decisions}
                          engine={engine}
                          onOpenQuery={fetchQueryJourney}
                          t={t}
                        />

                        {/* Principal Engineer Notes */}
                        {shownPeNotes.length > 0 && (
                          <ExpandableSection
                            headerText={peNotesHeader}
                            variant="footer"
                            defaultExpanded={false}
                          >
                            <SpaceBetween size="xs">
                              {shownPeNotes.map((tradeOff, idx) => (
                                <CardBox key={idx} tone="note">
                                  <SpaceBetween size="xxs">
                                    <div style={{ display: 'flex', alignItems: 'flex-start', gap: '8px' }}>
                                      <Badge color="blue">{idx + 1}</Badge>
                                      <Box fontSize="body-s" className="dbm-fill"><ProseText text={tradeOff.description.replace(/^\[PE note\]\s*/, '')} /></Box>
                                    </div>
                                    {tradeOff.impact && (
                                      <Box fontSize="body-s" color="text-body-secondary" padding={{ left: 'xl' }}><ProseText text={tradeOff.impact} /></Box>
                                    )}
                                  </SpaceBetween>
                                </CardBox>
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

        {/* DynamoDB merge-review decisions (#1780): the authoritative home the
            merge chose for each overlap and its derived copies, as both reports
            state them. */}
        <DynamoDBMergeReviewsCallout synthesis={resultsData?.synthesis} t={t} />
        <RelatedSectionPointer target={relatedPointers.toRisks === 'dynamodb-merge-reviews' ? relatedPointers.toPlan : null} t={t} />
      </SpaceBetween>
    ),
  };

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
          <ResultsSectionNav.Provider value={goToSection}>
            <SpaceBetween size="l">
              {flashbarItems.length > 0 && (
                <Flashbar items={flashbarItems.map(item => ({ ...item, onDismiss: () => handleFlashbarDismiss(item.id) }))} />
              )}

              <Header
                variant="h1"
                actions={
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button iconName="refresh" variant="normal" onClick={fetchData} loading={loading} ariaLabel={t('common.actions.refresh')} />
                    {graphExplorer.enabled && <ExploreGraphButton jobId={jobId} enabled={graphExplorer.enabled} />}
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
                onRetry={() => setDeliverablesRound(n => n + 1)}
                baseUrl={ApiConfigurations.baseUrl}
                t={t}
              />

              <div ref={setTabsRoot}>
                <Tabs
                  ariaLabel={t('analysis-results.tabs.aria-label')}
                  // A narrow screen scrolls the tab list with two icon buttons (#2458, #2446).
                  i18nStrings={{
                    scrollLeftAriaLabel: t('analysis-results.tabs.scroll-left'),
                    scrollRightAriaLabel: t('analysis-results.tabs.scroll-right'),
                  }}
                  activeTabId={activeTab}
                  onChange={({ detail }) => selectTab(detail.activeTabId)}
                  tabs={RESULTS_TABS.map(tab => ({
                    id: tab.id,
                    label: tabLabel(t, tab, synthesis),
                    // A tab once opened stays mounted, so what the reader expanded or
                    // filtered there is as they left it.
                    contentRenderStrategy: 'lazy',
                    content: tabContent[tab.id],
                  }))}
                />
              </div>
            </SpaceBetween>
          </ResultsSectionNav.Provider>
        }
        contentType="default"
        tools={<ResultsOnThisPage anchors={onThisPage} onGo={goToSection} t={t} />}
        toolsOpen={toolsOpen && !toolsHidden}
        onToolsChange={({ detail }) => setToolsOpen(detail.open)}
        toolsHide={toolsHidden}
        splitPanel={designTable ? (
          <AuroraTableDetailPanel table={designTable.table} engineName={designTable.engineName} t={t} />
        ) : selectedQueryId ? (
          <QueryDetailPanel
            queryId={selectedQueryId}
            journey={queryJourneyData}
            loading={queryJourneyLoading}
            trace={queryTrace}
            patterns={patternsOfQuery.get(selectedQueryId) || []}
            schemaDesigns={synthesis.schema_designs}
            availability={explorerState}
            focusRequest={panelFocusRequest}
            t={t}
          />
        ) : undefined}
        splitPanelOpen={splitPanelOpen && Boolean(designTable || selectedQueryId)}
        onSplitPanelToggle={({ detail }) => {
          if (detail.open) setSplitPanelOpen(true);
          else if (designTable) closeDesignDetail();
          else handleCloseQueryDetail();
        }}
      />


    </>
  );
});

export default AnalysisResultsPage;
