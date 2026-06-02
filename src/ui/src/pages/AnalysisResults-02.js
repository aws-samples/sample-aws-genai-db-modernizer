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
import { SideNavigationConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";
import ChartSankey from "../components/ChartSankey-01";
import { generateHTMLReport } from "../utils/ExportReport";


// ============================================
// Constants
// ============================================

const ENGINE_BADGE_COLORS = {
  dynamodb: 'blue',
  documentdb: 'green',
  elasticache: 'red',
  opensearch: 'grey',
  neptune: 'red',
  keyspaces: 'blue',
  aurora: 'green',
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

const ENGINE_LABELS = {
  dynamodb: 'DynamoDB',
  documentdb: 'DocumentDB',
  opensearch: 'OpenSearch',
  elasticache: 'ElastiCache',
  neptune: 'Neptune',
  keyspaces: 'Keyspaces',
  aurora: 'Aurora',
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
  const [selectedQueryId, setSelectedQueryId] = useState(null);

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
      addFlashbarMessage({ type: 'error', header: t('analysis-results-v2.error.invalid-job-id'), content: t('analysis-results-v2.error.no-job-id'), dismissible: true, id: `error-${Date.now()}` });
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
        addFlashbarMessage({ type: 'error', header: t('analysis-results-v2.error.failed-to-load'), content: results['results'].error?.message || 'Unknown error', dismissible: true, id: `error-${Date.now()}` });
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
  const realityCheck = synthesis?.reality_check || {};
  const afterDist = realityCheck?.after_distribution || {};

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

  // Projected cost: sum only surviving engines
  const costBreakdown = useMemo(() => {
    const all = synthesis?.tco_analysis?.cost_breakdown || [];
    return all.filter(cb => afterDist[cb.database] != null);
  }, [synthesis, afterDist]);

  const projectedCost = useMemo(() => {
    return costBreakdown.reduce((sum, cb) => sum + (cb.monthly_cost_usd || 0), 0);
  }, [costBreakdown]);

  // Build Sankey from after_distribution
  const sankeyData = useMemo(() => {
    if (Object.keys(afterDist).length === 0) return null;
    const nodes = [{ id: 'queries' }];
    const links = [];
    Object.entries(afterDist).forEach(([engine, count]) => {
      nodes.push({ id: engine });
      links.push({ source: 'queries', target: engine, value: count });
    });
    return { nodes, links };
  }, [afterDist]);

  const breadcrumbItems = useMemo(() => [
    { href: "/", text: t('analysis-results-v2.breadcrumb.home') },
    { href: "/dashboard", text: t('analysis-results-v2.breadcrumb.dashboard') },
    { href: `/analysis/monitor/summary/${jobId}`, text: t('analysis-results-v2.breadcrumb.assessment') },
    { href: `/analysis/results-v2/${jobId}`, text: t('analysis-results-v2.breadcrumb.results') }
  ], [jobId, t]);


  // ============================================
  // Unified access patterns (all engines)
  // ============================================

  const allAccessPatterns = useMemo(() => {
    const patterns = [];

    activeDesigns.forEach(design => {
      const engine = design.target_type;
      const content = design.content || {};

      // Build table/index lookup for detail rendering
      const tableLookup = {};
      (content.table_definitions || []).forEach(t => { tableLookup[t.table_name] = t; });
      (content.index_designs || []).forEach(idx => { tableLookup[idx.index_name] = idx; });
      (content.collection_designs || []).forEach(col => { tableLookup[col.collection_name || col.name] = col; });

      (content.access_patterns || []).forEach(ap => {
        const destName = ap.table_name || ap.key_pattern || ap.index_or_stream || ap.index || ap.collection || '—';
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
          description: ap.description || ap.name || '',
          queryIds: ap.source_query_ids || ap.query_ids || [],
          patternGroup: ap.pattern_group || null,
          designRps: ap.design_rps || null,
          itemSize: ap.item_size_bytes || null,
          avgItems: ap.avg_items_returned || null,
          inScope: ap.in_scope !== false,
          outOfScopeReason: ap.out_of_scope_reason || null,
          // OpenSearch specific
          opensearchDsl: ap.opensearch_dsl || null,
          sourceQuery: ap.source_query || null,
        });
      });
    });

    return patterns;
  }, [activeDesigns]);

  // Engine trade-offs lookup
  const engineTradeOffs = useMemo(() => {
    const map = {};
    activeDesigns.forEach(d => {
      map[d.target_type] = d.content?.trade_offs || [];
    });
    return map;
  }, [activeDesigns]);

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

  // Total access patterns
  const totalAccessPatterns = allAccessPatterns.length;


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

    allAccessPatterns.forEach(ap => {
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
  }, [allAccessPatterns]);

  // Apply all filters (engine bar, op donut, property filter)
  const filteredPatterns = useMemo(() => {
    let items = [...allAccessPatterns];

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
  }, [allAccessPatterns, engineFilter, opFilter, filterQuery]);


  // ============================================
  // Source table view grouping
  // ============================================

  const sourceTableGroups = useMemo(() => {
    const groups = {};

    filteredPatterns.forEach(ap => {
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
    filteredPatterns.forEach(ap => {
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
  }, [filteredPatterns]);

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

  // Engine distribution for pie chart
  const filteredEngineDist = useMemo(() => {
    const counts = {};
    filteredPatterns.forEach(ap => {
      counts[ap.engine] = (counts[ap.engine] || 0) + 1;
    });
    return counts;
  }, [filteredPatterns]);


  // ============================================
  // Pagination
  // ============================================

  const displayItems = browseMode === 'pattern' ? filteredPatterns : sourceTableGroups;
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

    try {
      const apiManager = new ApiManager();
      const result = await apiManager.execute([
        { id: 'query-journey', path: `assessments/${jobId}/query-journeys/${queryId}`, method: 'GET', params: {} }
      ]);

      if (result['query-journey']?.success) {
        setQueryJourneyData(result['query-journey']);
      } else if (result['query-journey']?.error) {
        setQueryJourneyData({ error: result['query-journey'].error.message || 'Failed to load query journey' });
      } else {
        setQueryJourneyData({ error: 'No data available' });
      }
    } catch (error) {
      console.error('Error fetching query journey:', error);
      setQueryJourneyData({ error: error.message || 'Failed to load query journey' });
    } finally {
      setQueryJourneyLoading(false);
    }
  }, [jobId]);

  const handleCloseQueryJourneyModal = useCallback(() => {
    setShowQueryJourneyModal(false);
    setQueryJourneyData(null);
    setSelectedQueryId(null);
  }, []);

  // Export progress modal state
  const [showExportProgress, setShowExportProgress] = useState(false);
  const [exportProgress, setExportProgress] = useState({ current: 0, total: 4, message: '' });

  const handleExportToHTML = useCallback(async () => {
    try {
      setShowExportProgress(true);
      setExportProgress({ current: 0, total: 4, message: 'Starting export...' });

      const apiManager = new ApiManager();

      // Prepare all API calls
      const apiCalls = [
        {
          id: 'results',
          path: `/assessments/${jobId}/results`,
          method: 'GET'
        },
        {
          id: 'schema-designs',
          path: `/assessments/${jobId}/schema-designs`,
          method: 'GET'
        },
        {
          id: 'query-journeys',
          path: `/assessments/${jobId}/query-journeys?page=1&page_size=1000`,
          method: 'GET'
        }
      ];

      // Execute all API calls
      setExportProgress({ current: 1, total: 4, message: 'Fetching data from APIs...' });
      const responses = await apiManager.execute(apiCalls);

      // Check for errors
      if (!responses.results?.success) {
        throw new Error('Failed to fetch analysis results');
      }
      if (!responses['schema-designs']?.success) {
        throw new Error('Failed to fetch schema designs');
      }

      // Query journeys is optional - log warning if it fails but continue
      let queryJourneysData = null;
      if (responses['query-journeys']?.success) {
        queryJourneysData = responses['query-journeys'];
      } else {
        console.warn('Failed to fetch query journeys:', responses['query-journeys']?.error);
      }

      // Step 4: Generate HTML
      setExportProgress({ current: 3, total: 4, message: 'Generating HTML report...' });

      const exportData = {
        results: responses.results,
        schemaDesigns: responses['schema-designs']?.schema_designs || [],
        collector: collectorData,
        jobId: jobId,
        exportDate: new Date().toISOString(),
        queryJourneys: queryJourneysData,
      };

      // Generate HTML content
      const htmlContent = generateHTMLReport(exportData);

      // Create and download file
      setExportProgress({ current: 4, total: 4, message: 'Downloading file...' });
      const blob = new Blob([htmlContent], { type: 'text/html' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `analysis-report-${jobId}-${new Date().toISOString().split('T')[0]}.html`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);

      setShowExportProgress(false);

      addFlashbarMessage({
        type: 'success',
        header: 'Export Successful',
        content: 'Analysis report has been exported to HTML',
        dismissible: true,
        id: `success-${Date.now()}`
      });
    } catch (error) {
      console.error('Error exporting to HTML:', error);
      setShowExportProgress(false);

      addFlashbarMessage({
        type: 'error',
        header: 'Export Failed',
        content: error.message || 'Failed to export analysis report',
        dismissible: true,
        id: `error-${Date.now()}`
      });
    }
  }, [jobId, collectorData, addFlashbarMessage]);

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
      header: t('analysis-results-v2.explorer.col-pattern'),
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
      header: t('analysis-results-v2.explorer.col-operation'),
      cell: item => item.operation,
      sortingField: 'operation',
      width: 150
    },
    {
      id: 'engine',
      header: t('analysis-results-v2.explorer.col-engine'),
      cell: item => (
        <Badge color={ENGINE_BADGE_COLORS[item.engine] || 'grey'}>
          {ENGINE_LABELS[item.engine] || item.engine}
        </Badge>
      ),
      sortingField: 'engine',
      width: 140
    },
    {
      id: 'source_tables',
      header: t('analysis-results-v2.explorer.col-source-tables'),
      cell: item => item.sourceTables.join(', '),
      width: 250
    },
    {
      id: 'destination',
      header: t('analysis-results-v2.explorer.col-destination'),
      cell: item => item.gsiName ? `${item.destTable} (GSI: ${item.gsiName})` : item.destTable,
      sortingField: 'destTable',
      width: 200
    },
    {
      id: 'description',
      header: t('analysis-results-v2.explorer.col-description'),
      cell: item => item.description || '—',
      width: 300
    }
  ], [t, handlePatternClick]);

  const sourceTableColumnDefinitions = useMemo(() => [
    {
      id: 'table_name',
      header: t('analysis-results-v2.explorer.col-source-table'),
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
      header: t('analysis-results-v2.explorer.col-engines'),
      cell: item => (
        <SpaceBetween direction="horizontal" size="xxs">
          {[...item.engines].map(e => (
            <Badge key={e} color={ENGINE_BADGE_COLORS[e] || 'grey'}>
              {ENGINE_LABELS[e] || e}
            </Badge>
          ))}
        </SpaceBetween>
      ),
      width: 200
    },
    {
      id: 'dest_tables',
      header: t('analysis-results-v2.explorer.col-destination-tables'),
      cell: item => (
        <Box fontSize="body-s">{[...item.destTables].join(', ')}</Box>
      ),
      width: 250
    },
    {
      id: 'patterns',
      header: t('analysis-results-v2.explorer.col-patterns'),
      cell: item => (
        <Badge>{item.patterns.length}</Badge>
      ),
      sortingField: 'patterns.length',
      width: 100
    },
    {
      id: 'convergence',
      header: t('analysis-results-v2.explorer.col-convergence'),
      cell: item => (
        item.convergesFrom.size > 0 ? (
          <Badge color="blue">{t('analysis-results-v2.explorer.merged-badge', { count: item.convergesFrom.size })}</Badge>
        ) : '—'
      ),
      width: 150
    },
    {
      id: 'operations',
      header: t('analysis-results-v2.explorer.col-operations'),
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
  ], [t, handleSourceTableClick]);


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
    if (ap.engine) metricsItems.push({ label: t('analysis-results-v2.pattern-detail.engine'), value: <Badge color={ENGINE_BADGE_COLORS[ap.engine] || 'grey'}>{ENGINE_LABELS[ap.engine] || ap.engine}</Badge> });
    if (ap.operation) metricsItems.push({ label: t('analysis-results-v2.pattern-detail.operation'), value: ap.operation });
    if (ap.destTable) metricsItems.push({ label: t('analysis-results-v2.pattern-detail.destination-table'), value: ap.destTable });
    if (ap.gsiName) metricsItems.push({ label: t('analysis-results-v2.pattern-detail.gsi'), value: ap.gsiName });
    if (ap.sourceTables?.length > 0) metricsItems.push({ label: t('analysis-results-v2.pattern-detail.source-tables'), value: ap.sourceTables.join(', ') });
    if (ap.designRps != null) metricsItems.push({ label: t('analysis-results-v2.pattern-detail.design-rps'), value: ap.designRps });
    if (ap.itemSize != null) metricsItems.push({ label: t('analysis-results-v2.pattern-detail.item-size'), value: `${ap.itemSize} bytes` });
    if (ap.avgItems != null) metricsItems.push({ label: t('analysis-results-v2.pattern-detail.avg-items-returned'), value: ap.avgItems });
    if (ap.patternGroup) metricsItems.push({ label: t('analysis-results-v2.pattern-detail.pattern-group'), value: ap.patternGroup });

    return (
      <SpaceBetween size="l">
        {/* Description */}
        {ap.description && (
          <Container>
            <Box fontSize="body-m">{ap.description}</Box>
          </Container>
        )}

        {/* Pattern Metrics */}
        <Container header={<Header variant="h3">{t('analysis-results-v2.pattern-detail.title')}</Header>}>
          <KeyValuePairs
            columns={3}
            items={metricsItems}
          />
        </Container>

        {/* Source queries */}
        {sourceQueries.length > 0 && (
          <Container header={<Header variant="h3">{t('analysis-results-v2.pattern-detail.source-queries', { count: sourceQueries.length })}</Header>}>
            <Table
              columnDefinitions={[
                {
                  id: 'query_id',
                  header: t('analysis-results-v2.pattern-detail.query-id'),
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
                  header: t('analysis-results-v2.pattern-detail.type'),
                  cell: (item) => <Badge>{item.query_type || t('common.labels.sql')}</Badge>,
                  width: 80
                },
                {
                  id: 'calls_per_second',
                  header: t('analysis-results-v2.pattern-detail.calls-per-sec'),
                  cell: (item) => item.calls_per_second?.toFixed(2) || '—',
                  width: 100
                },
                {
                  id: 'tables',
                  header: t('analysis-results-v2.pattern-detail.tables'),
                  cell: (item) => item.tables_accessed?.map(shortTable).join(', ') || '—',
                  width: 200
                },
                {
                  id: 'query_text',
                  header: t('analysis-results-v2.pattern-detail.query-text'),
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
          <Container header={<Header variant="h3">{t('analysis-results-v2.pattern-detail.source-sql')}</Header>}>
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
          <Container header={<Header variant="h3">{t('analysis-results-v2.pattern-detail.target-access-pattern')}</Header>}>
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
          <Container header={<Header variant="h3">{t('analysis-results-v2.source-table.gsi-prefix')}{ap.gsiName}</Header>}>
            {(ap.destDef.gsis || ap.destDef.gsi_indexes || [])
              .filter(g => (g.gsi_name || g.index_name) === ap.gsiName)
              .map((gsi, i) => {
                const pk = Array.isArray(gsi.partition_key) ? gsi.partition_key[0] : gsi.partition_key;
                const sk = Array.isArray(gsi.sort_key) ? gsi.sort_key[0] : gsi.sort_key;

                const gsiItems = [
                  { label: t('analysis-results-v2.pattern-detail.partition-key'), value: pk ? `${pk.attribute_name} (${pk.attribute_type})` : '—' },
                  { label: t('analysis-results-v2.pattern-detail.projection'), value: gsi.projection || t('common.labels.all') }
                ];

                if (sk) {
                  gsiItems.splice(1, 0, { label: t('analysis-results-v2.pattern-detail.sort-key'), value: `${sk.attribute_name} (${sk.attribute_type})` });
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
              <strong>{t('analysis-results-v2.pattern-detail.out-of-scope-reason')}:</strong> {ap.outOfScopeReason}
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
        label: ENGINE_LABELS[engine] || engine,
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
                          label: t('analysis-results-v2.source-table.table'),
                          value: dg.destTable
                        },
                        {
                          label: t('analysis-results-v2.source-table.patterns'),
                          value: dg.patterns.length
                        },
                        ...(pk ? [{
                          label: t('analysis-results-v2.pattern-detail.partition-key'),
                          value: <><code>{pk.attribute_name}</code> ({pk.attribute_type})</>
                        }] : []),
                        ...(sk ? [{
                          label: t('analysis-results-v2.pattern-detail.sort-key'),
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
                          {t('analysis-results-v2.source-table.no-patterns')}
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
    return engines.map(([engine, count]) => ({
      label: `${ENGINE_LABELS[engine] || engine} (${count})`,
      value: engine
    }));
  }, [filteredEngineDist]);

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
          breadcrumbs={<BreadcrumbGroup items={breadcrumbItems} />}
          navigation={
            <SideNavigation
              activeHref={`/analysis/results-v2/${jobId}`}
              header={SideNavigationConfigurations.header}
              items={SideNavigationConfigurations.items}
            />
          }
          content={
            <Box textAlign="center" padding={{ top: 'xxxl' }}>
              <Spinner size="large" />
              <Box margin={{ top: 'm' }} color="text-body-secondary">{t('analysis-results-v2.states.loading')}</Box>
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
        breadcrumbs={<BreadcrumbGroup items={breadcrumbItems} />}
        navigation={
          <SideNavigation
            activeHref={`/analysis/results-v2/${jobId}`}
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
                  <Button iconName="refresh" variant="normal" onClick={fetchData} loading={loading} />
                  <Button iconName="download" variant="normal" onClick={handleExportToHTML}>
                    {t('analysis-results-v2.actions.export-to-html')}
                  </Button>
                  <Button href={`/analysis/monitor/summary/${jobId}`}>{t('analysis-results-v2.actions.back-to-assessment')}</Button>
                </SpaceBetween>
              }
            >
              {t('analysis-results-v2.header.title')}
            </Header>

            {/* Executive Summary */}
            <Container header={<Header variant="h2">{t('analysis-results-v2.executive-summary.title')}</Header>}>
              <SpaceBetween size="m">
                <Box variant="p" fontSize="body-m">
                  {synthesis?.summary || 'No summary available.'}
                </Box>
                <ColumnLayout columns={4} variant="text-grid">
                  <Box>
                    <Box variant="awsui-key-label">{t('analysis-results-v2.executive-summary.database')}</Box>
                    <Box fontSize="heading-m" fontWeight="bold">{synthesis?.database_name || '—'}</Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">{t('analysis-results-v2.executive-summary.target-engines')}</Box>
                    <Box fontSize="heading-m">
                      <SpaceBetween direction="horizontal" size="xxs">
                        {Object.keys(afterDist).map(engine => (
                          <Badge key={engine} color={ENGINE_BADGE_COLORS[engine] || 'grey'}>{ENGINE_LABELS[engine] || engine}</Badge>
                        ))}
                      </SpaceBetween>
                    </Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">{t('analysis-results-v2.executive-summary.projected-cost')}</Box>
                    <Box fontSize="heading-m" fontWeight="bold">
                      {projectedCost > 0 ? `$${projectedCost.toFixed(2)}/mo` : '—'}
                    </Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">{t('analysis-results-v2.executive-summary.access-patterns')}</Box>
                    <Box fontSize="heading-m" fontWeight="bold">
                      {totalAccessPatterns > 0 ? totalAccessPatterns : Object.values(afterDist).reduce((a, b) => a + b, 0) || '—'}
                    </Box>
                  </Box>
                </ColumnLayout>
              </SpaceBetween>
            </Container>

            {/* Query Flow (Sankey) */}
            {sankeyData && (
              <Container header={
                <Header variant="h2" description={t('analysis-results-v2.query-flow.description')}>
                  {t('analysis-results-v2.query-flow.title')}
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
              <Container header={<Header variant="h2" description={t('analysis-results-v2.cost-breakdown.description')}>{t('analysis-results-v2.cost-breakdown.title')}</Header>}>
                <SpaceBetween size="m">
                  <ColumnLayout columns={costBreakdown.length} variant="text-grid">
                    {costBreakdown.map((cb, idx) => (
                      <Box key={idx} textAlign="center">
                        <Badge color={ENGINE_BADGE_COLORS[cb.database] || 'grey'}>{ENGINE_LABELS[cb.database] || cb.database}</Badge>
                        <Box fontSize="display-l" fontWeight="bold" margin={{ top: 'xs' }}>
                          ${cb.monthly_cost_usd?.toFixed(2)}
                        </Box>
                        <Box fontSize="body-s" color="text-body-secondary">/month · {cb.pricing_mode}</Box>
                      </Box>
                    ))}
                  </ColumnLayout>
                  {synthesis?.tco_analysis?.assumptions?.length > 0 && (
                    <Box fontSize="body-s" color="text-body-secondary">
                      {synthesis.tco_analysis.assumptions.join(' · ')}
                    </Box>
                  )}
                </SpaceBetween>
              </Container>
            )}


            {/* ============================================ */}
            {/* Access Pattern Explorer                      */}
            {/* ============================================ */}
            {totalAccessPatterns > 0 && (
              <Container
                id="access-patterns-section"
                header={
                  <Header
                    variant="h2"
                    counter={`(${filteredPatterns.length}${filteredPatterns.length !== totalAccessPatterns ? ` of ${totalAccessPatterns}` : ''})`}
                    description={t('analysis-results-v2.explorer.description')}
                    actions={
                      <SpaceBetween direction="horizontal" size="xs">
                        <SegmentedControl
                          selectedId={browseMode}
                          onChange={({ detail }) => { setBrowseMode(detail.selectedId); setCurrentPage(1); setSelectedItems([]); }}
                          options={[
                            { id: 'pattern', text: t('analysis-results-v2.explorer.by-access-pattern') },
                            { id: 'source', text: t('analysis-results-v2.explorer.by-source-table') },
                          ]}
                        />
                      </SpaceBetween>
                    }
                  >
                    {t('analysis-results-v2.explorer.title')}
                  </Header>
                }
              >
                <SpaceBetween size="m">

                  {/* Visual navigators: engine bar + operation donut */}
                  <ColumnLayout columns={2}>
                    <Box>
                      <Box variant="awsui-key-label" margin={{ bottom: 'xs' }}>{t('analysis-results-v2.explorer.filter-by-engine')}</Box>
                      {renderEngineBar()}
                    </Box>
                    <Box>
                      <Box variant="awsui-key-label" margin={{ bottom: 'xs' }}>{t('analysis-results-v2.explorer.filter-by-operation-type')}</Box>
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
                    filteringPlaceholder={t('analysis-results-v2.explorer.filter-placeholder')}
                    countText={t('analysis-results-v2.explorer.count-text', { count: filteredPatterns.length })}
                    expandToViewport
                    i18nStrings={{
                      filteringAriaLabel: t('analysis-results-v2.explorer.filtering-aria-label'),
                      dismissAriaLabel: t('analysis-results-v2.explorer.dismiss-aria-label'),
                      filteringPlaceholder: t('analysis-results-v2.explorer.filter-placeholder'),
                      groupValuesText: t('analysis-results-v2.explorer.group-values-text'),
                      groupPropertiesText: t('analysis-results-v2.explorer.group-properties-text'),
                      operatorsText: t('analysis-results-v2.explorer.operators-text'),
                      operationAndText: t('analysis-results-v2.explorer.operation-and-text'),
                      operationOrText: t('analysis-results-v2.explorer.operation-or-text'),
                      operatorLessText: t('analysis-results-v2.explorer.operator-less-text'),
                      operatorLessOrEqualText: t('analysis-results-v2.explorer.operator-less-or-equal-text'),
                      operatorGreaterText: t('analysis-results-v2.explorer.operator-greater-text'),
                      operatorGreaterOrEqualText: t('analysis-results-v2.explorer.operator-greater-or-equal-text'),
                      operatorContainsText: t('analysis-results-v2.explorer.operator-contains-text'),
                      operatorDoesNotContainText: t('analysis-results-v2.explorer.operator-does-not-contain-text'),
                      operatorEqualsText: t('analysis-results-v2.explorer.operator-equals-text'),
                      operatorDoesNotEqualText: t('analysis-results-v2.explorer.operator-does-not-equal-text'),
                      editTokenHeader: t('analysis-results-v2.explorer.edit-token-header'),
                      propertyText: t('analysis-results-v2.explorer.property-text'),
                      operatorText: t('analysis-results-v2.explorer.operator-text'),
                      valueText: t('analysis-results-v2.explorer.value-text'),
                      cancelActionText: t('common.actions.cancel'),
                      applyActionText: t('analysis-results-v2.explorer.apply-action-text'),
                      allPropertiesLabel: t('analysis-results-v2.explorer.all-properties-label'),
                      tokenLimitShowMore: t('analysis-results-v2.explorer.token-limit-show-more'),
                      tokenLimitShowFewer: t('analysis-results-v2.explorer.token-limit-show-fewer'),
                      clearFiltersText: t('analysis-results-v2.explorer.clear-filters-text'),
                      removeTokenButtonAriaLabel: (token) => t('analysis-results-v2.explorer.remove-token-aria-label', { propertyLabel: token.propertyLabel, operator: token.operator, value: token.value }),
                      enteredTextLabel: (text) => t('analysis-results-v2.explorer.entered-text-label', { text }),
                    }}
                  />

                  {hasActiveFilters && (
                    <div className="active-filters">
                      <Button variant="link" onClick={clearAllFilters}>{t('analysis-results-v2.explorer.clear-all-filters')}</Button>
                    </div>
                  )}

                  {/* Pagination at top */}
                  {displayItems.length > PAGE_SIZE && (
                    <Box float="right">
                      <Pagination
                        currentPageIndex={currentPage}
                        pagesCount={Math.ceil(displayItems.length / PAGE_SIZE)}
                        onChange={({ detail }) => setCurrentPage(detail.currentPageIndex)}
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
                      selectedItems={selectedItems}
                      onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
                      empty={
                        <Box textAlign="center" padding="l">
                          <Box variant="strong">{t('analysis-results-v2.explorer.no-matching-patterns')}</Box>
                          <Box variant="p" color="text-body-secondary">{t('analysis-results-v2.explorer.try-adjusting-filters')}</Box>
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
                      selectedItems={selectedItems}
                      onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
                      empty={
                        <Box textAlign="center" padding="l">
                          <Box variant="strong">{t('analysis-results-v2.explorer.no-matching-source-tables')}</Box>
                          <Box variant="p" color="text-body-secondary">{t('analysis-results-v2.explorer.try-adjusting-filters')}</Box>
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
                      />
                    </Box>
                  )}
                </SpaceBetween>
              </Container>
            )}


            {/* Engine-specific Trade-offs */}
            {Object.entries(engineTradeOffs).some(([, tradeOffList]) => tradeOffList.length > 0) && (
              <Container
                header={<Header variant="h2">{t('analysis-results-v2.tradeoffs.title')}</Header>}
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
                        label: `${ENGINE_LABELS[engine] || engine} (${tradeoffs.length})`,
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
                                          <Link variant="secondary" fontSize="body-s" onFollow={() => {}}>{t('analysis-results-v2.tradeoffs.mapping')}</Link>
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
                                          <Link variant="secondary" fontSize="body-s" onFollow={() => {}}>{t('analysis-results-v2.tradeoffs.sql-ids')}</Link>
                                          <Box margin={{ top: 'xxxs' }}>
                                            <SpaceBetween direction="horizontal" size="xxs">
                                              {tradeOff.query_ids.map((qid, qi) => (
                                                <span key={qi}
                                                  onClick={() => fetchQueryJourney(qid)}
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
                                                  title={`View query journey for ${qid}`}
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
            <StatusIndicator type="loading">{t('analysis-results-v2.query-journey.loading')}</StatusIndicator>
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
                          label: 'Confidence',
                          value: queryJourneyData?.assignment?.confidence ? `${queryJourneyData.assignment.confidence}%` : '—'
                        },
                        {
                          label: 'In Scope',
                          value: queryJourneyData?.assignment?.in_scope ? 'Yes' : 'No'
                        }
                      ]}
                    />
                    {queryJourneyData?.source?.query_text && (
                      <Container header={<Header variant="h3">{t('analysis-results-v2.pattern-detail.query-text')}</Header>}>
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
                id: 'json',
                label: 'JSON',
                content: aceLoading ? (
                  <Box textAlign="center" padding="l">
                    <StatusIndicator type="loading">{t('analysis-results-v2.query-journey.loading-editor')}</StatusIndicator>
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
                    i18nStrings={{
                      loadingState: t('analysis-results-v2.query-journey.loading'),
                      errorState: t('analysis-results-v2.query-journey.error'),
                      errorStateRecovery: t('common.actions.retry')
                    }}
                    loading={queryJourneyLoading}
                    readOnly
                  />
                )
              }
            ]}
          />
        )}
      </Modal>

      {/* Export Progress Modal */}
      <Modal
        visible={showExportProgress}
        header="Exporting Report"
        size="medium"
        closeAriaLabel="Close"
      >
        <SpaceBetween size="l">
          <Box textAlign="center">
            <StatusIndicator type="loading">
              {exportProgress.message}
            </StatusIndicator>
          </Box>
          <Box>
            <div style={{
              width: '100%',
              height: '8px',
              backgroundColor: '#e9ebed',
              borderRadius: '4px',
              overflow: 'hidden'
            }}>
              <div style={{
                width: `${(exportProgress.current / exportProgress.total) * 100}%`,
                height: '100%',
                backgroundColor: '#0972d3',
                transition: 'width 0.3s ease'
              }} />
            </div>
          </Box>
          <Box textAlign="center" color="text-body-secondary">
            {t('analysis-results-v2.export.progress-step', { current: exportProgress.current, total: exportProgress.total })}
          </Box>
        </SpaceBetween>
      </Modal>
    </>
  );
});

export default AnalysisResultsPage;
