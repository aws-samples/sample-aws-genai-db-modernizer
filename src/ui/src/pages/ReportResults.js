import { useState, useEffect, memo, useCallback, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useCollection } from '@cloudscape-design/collection-hooks';

//##-- AWS UI Objects
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Container from "@cloudscape-design/components/container";
import Box from "@cloudscape-design/components/box";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Flashbar from "@cloudscape-design/components/flashbar";
import Badge from "@cloudscape-design/components/badge";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Table from "@cloudscape-design/components/table";
import Alert from "@cloudscape-design/components/alert";
import Tabs from "@cloudscape-design/components/tabs";
import Link from "@cloudscape-design/components/link";
import Pagination from "@cloudscape-design/components/pagination";
import TextFilter from "@cloudscape-design/components/text-filter";
import Button from "@cloudscape-design/components/button";
import ExpandableSection from "@cloudscape-design/components/expandable-section";

//##-- Custom Objects
import { SideNavigationConfigurations, ApiConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";
import SectionSeparator from "../components/SectionSeparator";
import { buildReportHtml, normalizeTradeoff, riskHasContent } from "../utils/ReportHtmlExport";
import { splitRankingByRole, formatCacheLayerLine } from "../utils/cacheLayer";
import { analysisConfidence, confidenceAlertText, confidenceText, hasRoutedConfidence } from "../utils/rankingConfidence";



const ENGINE_COLORS = {
  dynamodb: 'blue',
  documentdb: 'green',
  elasticache: 'red',
  opensearch: 'blue',
  neptune: 'red',
  keyspaces: 'blue',
  aurora: 'green',
};



// Inline trade-offs shown inside access pattern groups
const InlineTradeoffs = memo(({ tradeoffs }) => {
  if (!tradeoffs || tradeoffs.length === 0) return null;
  return (
    <SpaceBetween size="xs">
      {tradeoffs.map((tradeOff, idx) => (
        <Box key={idx} padding={{ vertical: 'xs', horizontal: 's' }}
          className="inline-tradeoff"
          style={{ borderLeft: '3px solid #0972d3', backgroundColor: '#f2f8fd', borderRadius: '4px' }}>
          <SpaceBetween size="xxs">
            <Box fontSize="body-s" fontWeight="bold">
              <Badge color={ENGINE_COLORS[tradeOff.engine] || 'blue'}>{tradeOff.engine}</Badge>
              {' '}{tradeOff.description}
            </Box>
            {tradeOff.impact && (
              <Box fontSize="body-s" color="text-body-secondary">
                {tradeOff.impact}
              </Box>
            )}
            {(tradeOff.source_tables.length > 0 || tradeOff.target_tables.length > 0) && (
              <Box fontSize="body-s" color="text-status-inactive">
                {tradeOff.source_tables.length > 0 && <span>{tradeOff.source_tables.join(', ')}</span>}
                {tradeOff.source_tables.length > 0 && tradeOff.target_tables.length > 0 && <span> → </span>}
                {tradeOff.target_tables.length > 0 && <span>{tradeOff.target_tables.join(', ')}</span>}
              </Box>
            )}
          </SpaceBetween>
        </Box>
      ))}
    </SpaceBetween>
  );
});

// Component for Schema Design tables (DynamoDB/DocumentDB)
const SchemaDesignTable = memo(({ tables }) => {
  const { t } = useTranslation();
  const { items, collectionProps, paginationProps, filterProps } = useCollection(
    tables,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.schema-table.no-tables')}</Box></Box>,
        noMatch: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('common.labels.no-matches')}</Box></Box>
      }
    }
  );

  return (
    <Table
      {...collectionProps} // nosemgrep: react-props-spreading
      columnDefinitions={[
        {
          id: 'target_table',
          header: t('report-results.schema-table.col-target-table'),
          cell: item => <Box fontFamily="monospace" fontWeight="bold">{item.table_name}</Box>
        },
        {
          id: 'design_pattern',
          header: t('report-results.schema-table.col-design-pattern'),
          cell: item => <Badge>{item.aggregate_pattern}</Badge>
        },
        {
          id: 'source_tables',
          header: t('report-results.schema-table.col-source-tables'),
          cell: item => (
            <Box fontSize="body-s">
              {item.source_tables?.join(', ') || 'N/A'}
            </Box>
          )
        },
        {
          id: 'gsis',
          header: t('report-results.schema-table.col-gsis'),
          cell: item => item.gsi_count || 0,
          width: 80
        },
        {
          id: 'est_items',
          header: t('report-results.schema-table.col-est-items'),
          cell: item => (item.item_count || 0).toLocaleString(),
          width: 120
        },
        {
          id: 'avg_item_size',
          header: t('report-results.schema-table.col-avg-item-size'),
          cell: item => `${item.item_size_bytes || 0} B`,
          width: 120
        }
      ]}
      items={items}
      variant="embedded"
      filter={
        <TextFilter
          {...filterProps} // nosemgrep: react-props-spreading
          filteringPlaceholder={t('report-results.schema-table.filter-placeholder')}
          filteringText={filterProps.filteringText}
          countText={`${items.length} ${items.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
        />
      }
      pagination={<Pagination {...paginationProps} />} // nosemgrep: react-props-spreading
    />
  );
});

// Component for OpenSearch indexes
const OpenSearchIndexTable = memo(({ tables }) => {
  const { t } = useTranslation();
  const { items, collectionProps, paginationProps, filterProps } = useCollection(
    tables,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.opensearch-table.no-indexes')}</Box></Box>,
        noMatch: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('common.labels.no-matches')}</Box></Box>
      }
    }
  );

  return (
    <Table
      {...collectionProps} // nosemgrep: react-props-spreading
      columnDefinitions={[
        {
          id: 'target_index',
          header: t('report-results.opensearch-table.col-target-index'),
          cell: item => <Box fontFamily="monospace" fontWeight="bold">{item.table_name}</Box>
        },
        {
          id: 'design_pattern',
          header: t('report-results.opensearch-table.col-design-pattern'),
          cell: item => <Badge>{item.aggregate_pattern}</Badge>
        },
        {
          id: 'source_tables',
          header: t('report-results.opensearch-table.col-source-tables'),
          cell: item => (
            <Box fontSize="body-s">
              {item.source_tables?.join(', ') || 'N/A'}
            </Box>
          )
        },
        {
          id: 'shards',
          header: t('report-results.opensearch-table.col-shards'),
          cell: item => item.shards || 0,
          width: 80
        },
        {
          id: 'replicas',
          header: t('report-results.opensearch-table.col-replicas'),
          cell: item => item.replicas || 0,
          width: 80
        },
        {
          id: 'fields',
          header: t('report-results.opensearch-table.col-fields'),
          cell: item => item.field_count || 0,
          width: 80
        }
      ]}
      items={items}
      variant="embedded"
      filter={
        <TextFilter
          {...filterProps} // nosemgrep: react-props-spreading
          filteringPlaceholder={t('report-results.opensearch-table.filter-placeholder')}
          filteringText={filterProps.filteringText}
          countText={`${items.length} ${items.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
        />
      }
      pagination={<Pagination {...paginationProps} />} // nosemgrep: react-props-spreading
    />
  );
});

// Component for Query Classification access patterns with inline trade-offs
const AccessPatternTable = memo(({ accessPatterns, tradeoffs = [] }) => {
  const { t } = useTranslation();
  const { items, collectionProps, paginationProps, filterProps } = useCollection(
    accessPatterns,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.access-pattern-table.no-patterns')}</Box></Box>,
        noMatch: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('common.labels.no-matches')}</Box></Box>
      }
    }
  );

  // Build a lookup: query_id -> list of trade-offs that reference it
  const tradeoffsByQuery = useMemo(() => {
    const map = {};
    tradeoffs.forEach(tradeOff => {
      (tradeOff.query_ids || []).forEach(qid => {
        if (!map[qid]) map[qid] = [];
        map[qid].push(tradeOff);
      });
    });
    return map;
  }, [tradeoffs]);

  return (
    <SpaceBetween size="s">
      <Table
        {...collectionProps} // nosemgrep: react-props-spreading
        columnDefinitions={[
          {
            id: 'pattern_id',
            header: t('report-results.access-pattern-table.col-pattern-id'),
            cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.pattern_id || 'N/A'}</Box>
          },
          {
            id: 'operation',
            header: t('report-results.access-pattern-table.col-operation'),
            cell: item => <Badge>{item.operation || 'N/A'}</Badge>
          },
          {
            id: 'table',
            header: t('report-results.access-pattern-table.col-table'),
            cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.table_name || 'N/A'}</Box>
          },
          {
            id: 'rps',
            header: t('report-results.access-pattern-table.col-design-rps'),
            cell: item => item.design_rps?.toFixed(2) || '0'
          },
          {
            id: 'description',
            header: t('report-results.access-pattern-table.col-description'),
            cell: item => {
              const matched = tradeoffsByQuery[item.source_query_id] || tradeoffsByQuery[item.pattern_id] || [];
              return (
                <SpaceBetween size="xs">
                  <Box fontSize="body-s">{item.description || 'N/A'}</Box>
                  {matched.length > 0 && matched.map((tradeOff, i) => (
                    <Box key={i} padding={{ left: 's' }} style={{ borderLeft: '2px solid #0972d3' }}>
                      <Box fontSize="body-s" color="text-status-info" fontWeight="bold">{tradeOff.description}</Box>
                      {tradeOff.impact && <Box fontSize="body-s" color="text-body-secondary">{tradeOff.impact}</Box>}
                    </Box>
                  ))}
                </SpaceBetween>
              );
            }
          }
        ]}
        items={items}
        variant="embedded"
        wrapLines
        filter={
          <TextFilter
            {...filterProps} // nosemgrep: react-props-spreading
            filteringPlaceholder={t('report-results.access-pattern-table.filter-placeholder')}
            filteringText={filterProps.filteringText}
            countText={`${items.length} ${items.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
          />
        }
        pagination={<Pagination {...paginationProps} />} // nosemgrep: react-props-spreading
      />
    </SpaceBetween>
  );
});



const ReportResultsPage = memo(() => {

  const { t } = useTranslation();
  const { jobId } = useParams();

  //--|#######################| State Management Section  |#######################

  const [navigationOpen, setNavigationOpen] = useState(false);
  const [resultsData, setResultsData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [flashbarItems, setFlashbarItems] = useState([]);
  const [exporting, setExporting] = useState(false);



  //--|#######################| Handle Section  |#######################

  const addFlashbarMessage = useCallback((message) => {
    setFlashbarItems(prevItems => [...prevItems, message]);
  }, []);

  const handleFlashbarDismiss = useCallback((itemId) => {
    setFlashbarItems(prevItems => prevItems.filter(item => item.id !== itemId));
  }, []);

  // Export to HTML with all sections
  const exportToHTML = useCallback(() => {
    setExporting(true);

    try {
      // Every report.json value is HTML-escaped inside buildReportHtml (#242).
      const html = buildReportHtml({ resultsData, jobId, t });

      const dataBlob = new Blob([html], { type: 'text/html' });
      const url = URL.createObjectURL(dataBlob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `database-modernization-report-${jobId}.html`;
      link.click();
      URL.revokeObjectURL(url);

      addFlashbarMessage({
        type: 'success',
        header: t('report-results.export.success-header'),
        content: t('report-results.export.success-content'),
        dismissible: true,
        id: `success-${Date.now()}`
      });
    } catch (error) {
      addFlashbarMessage({
        type: 'error',
        header: t('report-results.export.error-header'),
        content: t('report-results.export.error-content', { message: error.message }),
        dismissible: true,
        id: `error-${Date.now()}`
      });
    } finally {
      setExporting(false);
    }
  }, [resultsData, jobId, addFlashbarMessage, t]);



  //--|#######################| Gather Information Section  |#######################

  const gatherResults = useCallback(async () => {
    if (!jobId) {
      addFlashbarMessage({
        type: 'error',
        header: t('report-results.error.invalid-job-id'),
        content: t('report-results.error.no-job-id'),
        dismissible: true,
        id: `error-${Date.now()}`
      });
      setLoading(false);
      return;
    }

    setLoading(true);

    try {
      const apiManager = new ApiManager();

      const apiCalls = [{
        id: 'get-results',
        path: `assessments/${jobId}/results`,
        method: 'GET',
        params: {}
      }];

      const results = await apiManager.execute(apiCalls);

      if (results['get-results']?.error) {
        const result = results['get-results'];
        const errorMessage = result.error?.message || 'Failed to load results';
        const statusCode = result.status || 'Unknown';
        const apiUrl = `${ApiConfigurations.baseUrl}assessments/${jobId}/results`;

        addFlashbarMessage({
          type: 'error',
          header: t('report-results.error.api-error-header', { statusCode }),
          content: t('report-results.error.api-error-content', { apiUrl, errorMessage }),
          dismissible: true,
          id: `error-${Date.now()}`
        });
      } else if (results['get-results']?.success) {
        setResultsData(results['get-results']);
        setFlashbarItems([]);
      }

    } catch (error) {
      console.error('Error loading results:', error);
      addFlashbarMessage({
        type: 'error',
        header: t('report-results.error.unexpected-header'),
        content: t('report-results.error.unexpected-content', { message: error.message }),
        dismissible: true,
        id: `error-${Date.now()}`
      });
    } finally {
      setLoading(false);
    }
  }, [jobId, addFlashbarMessage, t]);



  //--|#######################| Initialization Section  |#######################

  useEffect(() => {
    gatherResults();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);



  //--|#######################| Data Processing Section  |#######################

  const synthesis = resultsData?.synthesis || {};
  const triage = resultsData?.triage_summary || {};
  // #296 cache overlay: the ranking array may carry a trailing role: "cache_layer"
  // entry for ElastiCache (workload_percent 0, assigned_queries 0 by design --
  // it never owns a query). Split it out so it renders as a cache layer card
  // instead of a ranked owner with a misleading "0%".
  const { owners: ranking, cacheLayer } = splitRankingByRole(synthesis?.ranking);
  const cacheLayerLine = formatCacheLayerLine(cacheLayer, { t, withLabel: false });
  const queryGroups = synthesis?.query_groups || [];
  const tableMappings = synthesis?.table_mappings || [];
  const riskAssessment = synthesis?.risk_assessment || {};
  const tradeoffs = synthesis?.trade_offs || [];
  const tcoAnalysis = synthesis?.tco_analysis || {};
  const schemaDesigns = synthesis?.schema_designs || {};

  // Process risks from API - extract engine from description if available
  const processRisks = (apiRisks) => {
    if (!apiRisks || apiRisks.length === 0) return [];

    return apiRisks.filter(risk => riskHasContent(risk.description)).map(risk => {
      // Extract engine from description (format: [engine] type: description)
      const engineMatch = risk.description?.match(/^\[(\w+)\]/);
      const engine = engineMatch ? engineMatch[1] : null;

      // Remove engine prefix from description if present. The label between
      // "]" and ":" is \w+ for most risk types, but unsupported-pattern risks
      // now carry a humanised, possibly multi-word label (e.g. "unsupported
      // pattern", "text search" -- see src/shared/unsupported_pattern.py),
      // which \w+ would fail to match and leave the prefix un-stripped.
      // Only a short label (up to three words) is stripped, so a re-attributed risk
      // keeps its "Flagged by the X analysis for N queries now on Y: ..." attribution.
      const cleanDescription = risk.description
        ?.replace(/^\[\w+\]\s*/, '')
        .replace(/^[\w-]+(?:\s[\w-]+){0,2}:\s+/, '') || '';

      return {
        id: risk.risk_id,
        engine: engine,
        severity: risk.severity,
        risk_type: risk.risk_type,
        description: cleanDescription,
        mitigation: risk.mitigation?.replace(/^Implement as application logic:\s+/, '') || '',
        affected_tables: risk.affected_tables || []
      };
    });
  };

  const risks = processRisks(riskAssessment.risks);
  // Analysis risks the effective assignment resolved (risk_assessment.resolved_risks).
  const resolvedRisks = (riskAssessment.resolved_risks || [])
    .filter(risk => riskHasContent(risk.description))
    .map((risk, i) => ({
      key: `resolved-${i}`,
      engine: risk.engine || null,
      resolved_on: risk.resolved_on || null,
      severity: risk.severity,
      description: risk.description?.replace(/^\[\w+\]\s*/, '') || '',
      reason: risk.reason || '',
    }));
  const totalRisks = risks.length;
  const highSeverityRisks = risks.filter(r => r.severity === 'HIGH');
  const mediumSeverityRisks = risks.filter(r => r.severity === 'MEDIUM');
  const lowSeverityRisks = risks.filter(r => r.severity === 'LOW');



  //--|#######################| Breadcrumb Section  |#######################

  const breadcrumbItems = useMemo(() => [
    { href: "/", text: t('report-results.breadcrumb.home') },
    { href: "/dashboard", text: t('report-results.breadcrumb.dashboard') },
    { href: `/analysis/monitor/summary/${jobId}`, text: jobId || t('report-results.breadcrumb.job') },
    { href: `/analysis/report/${jobId}`, text: t('report-results.breadcrumb.report') }
  ], [jobId, t]);



  //--|#######################| Utility Functions  |#######################

  const scrollToSection = useCallback((sectionId) => {
    const element = document.getElementById(sectionId);
    if (element) {
      element.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }, []);

  const formatPatternName = useCallback((name) => {
    if (!name) return 'N/A';
    return name
      .split('_')
      .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
      .join(' ');
  }, []);

  // Normalize trade-offs (handles both structured objects and legacy strings)
  const processedTradeoffs = useMemo(() => {
    return tradeoffs.map(item => normalizeTradeoff(item));
  }, [tradeoffs]);

  const uniqueEngines = useMemo(() => {
    const engines = [...new Set(processedTradeoffs.map(item => item.engine))];
    return engines.sort();
  }, [processedTradeoffs]);

  // Group trade-offs by engine
  const tradeoffsByEngine = useMemo(() => {
    const grouped = {};
    uniqueEngines.forEach(engine => {
      grouped[engine] = processedTradeoffs.filter(item => item.engine === engine);
    });
    return grouped;
  }, [processedTradeoffs, uniqueEngines]);

  // Table Mappings collection with pagination and filtering
  const { items: tableMappingItems, collectionProps: tableMappingCollectionProps, paginationProps: tableMappingPaginationProps, filterProps: tableMappingFilterProps } = useCollection(
    tableMappings,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: (
          <Box textAlign="center" color="inherit">
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              {t('report-results.table-mappings.no-mappings')}
            </Box>
          </Box>
        ),
        noMatch: (
          <Box textAlign="center" color="inherit">
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              {t('common.labels.no-matches')}
            </Box>
          </Box>
        )
      }
    }
  );

  // High Severity Risks collection
  const { items: highRiskItems, collectionProps: highRiskCollectionProps, paginationProps: highRiskPaginationProps, filterProps: highRiskFilterProps } = useCollection(
    highSeverityRisks,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.risk-assessment.no-high-risks')}</Box></Box>,
        noMatch: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('common.labels.no-matches')}</Box></Box>
      }
    }
  );

  // Medium Severity Risks collection
  const { items: mediumRiskItems, collectionProps: mediumRiskCollectionProps, paginationProps: mediumRiskPaginationProps, filterProps: mediumRiskFilterProps } = useCollection(
    mediumSeverityRisks,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.risk-assessment.no-medium-risks')}</Box></Box>,
        noMatch: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('common.labels.no-matches')}</Box></Box>
      }
    }
  );

  // Low Severity Risks collection
  const { items: lowRiskItems, collectionProps: lowRiskCollectionProps, paginationProps: lowRiskPaginationProps, filterProps: lowRiskFilterProps } = useCollection(
    lowSeverityRisks,
    {
      pagination: { pageSize: 10 },
      sorting: {},
      filtering: {
        empty: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.risk-assessment.no-low-risks')}</Box></Box>,
        noMatch: <Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('common.labels.no-matches')}</Box></Box>
      }
    }
  );



  //--|#######################| Render Section  |#######################

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
            activeHref={`/analysis/report/${jobId}`}
            header={SideNavigationConfigurations.header}
            items={SideNavigationConfigurations.items}
          />
        }
        content={
          <SpaceBetween size="l">

            {flashbarItems.length > 0 && (
              <Flashbar
                items={flashbarItems.map(item => ({
                  ...item,
                  onDismiss: () => handleFlashbarDismiss(item.id)
                }))}
              />
            )}

            {loading ? (
              <Container>
                <Box textAlign="center" padding="xxl">
                  <Box variant="p" color="text-body-secondary">{t('report-results.states.loading')}</Box>
                </Box>
              </Container>
            ) : (
              <SpaceBetween size="l">

                {/* Hero Header */}
                <Container>
                  <SpaceBetween size="m">
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                      <Box>
                        <Box fontSize="display-l" fontWeight="bold">
                          {t('report-results.hero.title')}
                        </Box>
                        <Box variant="p" color="text-body-secondary">
                          {t('report-results.hero.subtitle', { sourceType: triage.source_database_type || t('report-results.hero.subtitle-database-fallback') })}
                        </Box>
                      </Box>
                      <Button
                        variant="primary"
                        iconName="download"
                        onClick={exportToHTML}
                        loading={exporting}
                      >
                        {t('report-results.hero.export-report')}
                      </Button>
                    </div>

                    <ColumnLayout columns={3} variant="text-grid" borders="vertical">
                      <div>
                        <Box variant="awsui-key-label">{t('report-results.hero.job-id')}</Box>
                        <Box fontFamily="monospace">{jobId || 'N/A'}</Box>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">{t('report-results.hero.source-database')}</Box>
                        <Box>{triage.database_name || synthesis.database_name || 'N/A'}</Box>
                      </div>
                      <div>
                        <Box variant="awsui-key-label">{t('report-results.hero.analysis-date')}</Box>
                        <Box>{synthesis.timestamp ? new Date(synthesis.timestamp).toLocaleString() : 'N/A'}</Box>
                      </div>
                    </ColumnLayout>
                  </SpaceBetween>
                </Container>

                {/* Table of Contents */}
                <Container
                  id="report-contents"
                  header={
                    <SectionSeparator
                      title={t('report-results.toc.title')}
                      description={t('report-results.toc.description')}
                      onTopClick={() => window.scrollTo({ top: 0, behavior: 'smooth' })}
                    />
                  }
                >
                  <ColumnLayout columns={2} variant="text-grid">
                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('executive-summary')}>
                        {t('report-results.toc.executive-summary')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.executive-summary-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('database-ranking')}>
                        {t('report-results.toc.database-ranking')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.database-ranking-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('table-mappings')}>
                        {t('report-results.toc.table-mappings')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.table-mappings-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('target-database-details')}>
                        {t('report-results.toc.target-database-details')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.target-database-details-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('schema-designs')}>
                        {t('report-results.toc.schema-designs')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.schema-designs-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('risk-assessment')}>
                        {t('report-results.toc.risk-assessment')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.risk-assessment-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('tradeoffs')}>
                        {t('report-results.toc.tradeoffs')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.tradeoffs-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('tco-analysis')}>
                        {t('report-results.toc.tco-analysis')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.tco-analysis-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('migration-roadmap')}>
                        {t('report-results.toc.migration-roadmap')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.migration-roadmap-desc')}
                      </Box>
                    </Box>

                    <Box>
                      <Link variant="primary" fontSize="body-m" onFollow={() => scrollToSection('query-classification')}>
                        {t('report-results.toc.query-classification')}
                      </Link>
                      <Box padding={{ top: 'xs' }} color="text-body-secondary" fontSize="body-s">
                        {t('report-results.toc.query-classification-desc')}
                      </Box>
                    </Box>
                  </ColumnLayout>
                </Container>

                {/* Executive Summary */}
                <Container
                  id="executive-summary"
                  header={
                    <SectionSeparator
                      title={t('report-results.executive-summary.title')}
                      description={t('report-results.executive-summary.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <SpaceBetween size="m">
                    <Box>{synthesis.summary || t('report-results.executive-summary.no-summary')}</Box>

                    {synthesis.summary_deterministic && (
                      <Box>
                        <Box variant="awsui-key-label">{t('report-results.executive-summary.key-metrics')}</Box>
                        <Box fontSize="body-s" color="text-body-secondary">
                          {synthesis.summary_deterministic}
                        </Box>
                      </Box>
                    )}

                    {triage.source_database_type && (
                      <ColumnLayout columns={3} variant="text-grid">
                        <Box>
                          <Box variant="awsui-key-label">{t('report-results.executive-summary.source-engine')}</Box>
                          <Box>{triage.source_database_type}</Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">{t('report-results.executive-summary.database-name')}</Box>
                          <Box>{triage.database_name || 'N/A'}</Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">{t('report-results.executive-summary.analysis-date')}</Box>
                          <Box>{new Date().toLocaleDateString()}</Box>
                        </Box>
                      </ColumnLayout>
                    )}
                  </SpaceBetween>
                </Container>

                {/* Database Ranking */}
                <Container
                  id="database-ranking"
                  header={
                    <SectionSeparator
                      title={t('report-results.ranking.title')}
                      description={t('report-results.ranking.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <ColumnLayout columns={3} variant="default" borders="vertical">
                    {ranking.map((item, index) => (
                      <Box key={index} padding="l">
                        <SpaceBetween size="m" alignItems="center">
                          <Box textAlign="center">
                            <Box variant="awsui-key-label">{t('report-results.ranking.rank', { rank: index + 1 })}</Box>
                          </Box>

                          <Box textAlign="center">
                            <Badge color={ENGINE_COLORS[item.target] || 'grey'}>{item.target}</Badge>
                          </Box>

                          <Box textAlign="center">
                            <Box fontSize="display-l" fontWeight="bold">{confidenceText(item, true)}</Box>
                            <Box variant="small" color="text-body-secondary">
                              {hasRoutedConfidence(item)
                                ? t('report-results.ranking.routed-confidence')
                                : t('report-results.ranking.confidence')}
                            </Box>
                            {hasRoutedConfidence(item) && analysisConfidence(item) !== null && (
                              <Box variant="small" color="text-body-secondary">
                                {t('report-results.ranking.analysis-confidence', { score: analysisConfidence(item) })}
                              </Box>
                            )}
                          </Box>

                          <Box textAlign="center">
                            <Box variant="awsui-key-label">{t('report-results.ranking.weight')}</Box>
                            <Box variant="small" color="text-body-secondary">{(item.weight * 100).toFixed(0)}%</Box>
                          </Box>

                          <Box textAlign="center">
                            {item.migration_complexity_avg === 'LOW' ? (
                              <Box color="text-status-success">
                                <Box fontSize="body-s">{t('report-results.ranking.schema-ready-check')}</Box>
                              </Box>
                            ) : (
                              <Box color="text-status-info">
                                <Box fontSize="body-s">{t('report-results.ranking.schema-ready')}</Box>
                              </Box>
                            )}
                          </Box>

                          <Box textAlign="center">
                            <Box variant="small" color="text-body-secondary">
                              {item.tables_analyzed || 0} tables · {item.access_patterns || 0} patterns · ${item.monthly_cost_usd?.toFixed(2) || '0'}/mo
                            </Box>
                          </Box>
                        </SpaceBetween>
                      </Box>
                    ))}
                  </ColumnLayout>

                  {/* #296: ElastiCache's cache_layer ranking entry is never an owner
                      (workload_percent 0, assigned_queries 0 by design), so it gets
                      its own card -- cached-read count and call share, not a rank. */}
                  {cacheLayer && (
                    <Box padding={{ top: 'l' }}>
                      <Box
                        padding="l"
                        style={{ borderLeft: '3px solid #d13212', backgroundColor: '#fef2f2', borderRadius: '4px' }}
                      >
                        <SpaceBetween direction="horizontal" size="l" alignItems="center">
                          <Badge color={ENGINE_COLORS[cacheLayer.target] || 'red'}>
                            {t('cache-layer.badge-label', { defaultValue: 'Cache layer' })}
                          </Badge>
                          <Box fontSize="body-m">{cacheLayerLine}</Box>
                        </SpaceBetween>
                      </Box>
                    </Box>
                  )}
                </Container>

                {/* Table Mappings */}
                <Container
                  id="table-mappings"
                  header={
                    <SectionSeparator
                      title={t('report-results.table-mappings.title', { count: tableMappings.length })}
                      description={t('report-results.table-mappings.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <Table
                    {...tableMappingCollectionProps} // nosemgrep: react-props-spreading
                    columnDefinitions={[
                      {
                        id: 'source',
                        header: t('report-results.table-mappings.col-source-table'),
                        cell: item => <Box fontFamily="monospace">{item.source_table}</Box>
                      },
                      {
                        id: 'target_db',
                        header: t('report-results.table-mappings.col-target-database'),
                        cell: item => <Badge color={ENGINE_COLORS[item.recommended_database] || 'grey'}>{item.recommended_database}</Badge>
                      },
                      {
                        id: 'target_table',
                        header: t('report-results.table-mappings.col-target-table'),
                        cell: item => <Box fontFamily="monospace">{item.target_table || 'N/A'}</Box>
                      },
                      {
                        id: 'pattern',
                        header: t('report-results.table-mappings.col-pattern'),
                        cell: item => <Badge color="blue">{formatPatternName(item.aggregate_pattern)}</Badge>
                      },
                      {
                        id: 'confidence',
                        header: t('report-results.table-mappings.col-confidence'),
                        cell: item => (
                          <ProgressBar
                            value={item.confidence_score || 0}
                            variant="standalone"
                            status={item.confidence_score >= 70 ? 'success' : 'in-progress'}
                          />
                        )
                      }
                    ]}
                    items={tableMappingItems}
                    filter={
                      <TextFilter
                        {...tableMappingFilterProps} // nosemgrep: react-props-spreading
                        filteringPlaceholder={t('report-results.table-mappings.filter-placeholder')}
                        filteringText={tableMappingFilterProps.filteringText}
                        countText={`${tableMappingItems.length} ${tableMappingItems.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
                      />
                    }
                    pagination={<Pagination {...tableMappingPaginationProps} />} // nosemgrep: react-props-spreading
                  />
                </Container>

                {/* Target Database Details */}
                <Container
                  id="target-database-details"
                  header={
                    <SectionSeparator
                      title={t('report-results.target-db-mapping.title')}
                      description={t('report-results.target-db-mapping.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <Tabs
                    tabs={ranking.map((item, index) => ({
                      id: item.target,
                      label: item.target,
                      content: (
                        <SpaceBetween size="m">
                          <Alert type="info">
                            {confidenceAlertText(item, t)}
                          </Alert>

                          <ColumnLayout columns={2} variant="text-grid">
                            <Box>
                              <Box variant="h4">{t('report-results.target-db-mapping.use-cases')}</Box>
                              <Box padding={{ top: 's' }}>
                                {item.target === 'dynamodb' && (
                                  <ul>
                                    <li>{t('report-results.target-db-mapping.dynamodb.use-case-1')}</li>
                                    <li>{t('report-results.target-db-mapping.dynamodb.use-case-2')}</li>
                                    <li>{t('report-results.target-db-mapping.dynamodb.use-case-3')}</li>
                                    <li>{t('report-results.target-db-mapping.dynamodb.use-case-4')}</li>
                                  </ul>
                                )}
                                {item.target === 'documentdb' && (
                                  <ul>
                                    <li>{t('report-results.target-db-mapping.documentdb.use-case-1')}</li>
                                    <li>{t('report-results.target-db-mapping.documentdb.use-case-2')}</li>
                                    <li>{t('report-results.target-db-mapping.documentdb.use-case-3')}</li>
                                    <li>{t('report-results.target-db-mapping.documentdb.use-case-4')}</li>
                                  </ul>
                                )}
                                {item.target === 'elasticache' && (
                                  <ul>
                                    <li>{t('report-results.target-db-mapping.elasticache.use-case-1')}</li>
                                    <li>{t('report-results.target-db-mapping.elasticache.use-case-2')}</li>
                                    <li>{t('report-results.target-db-mapping.elasticache.use-case-3')}</li>
                                    <li>{t('report-results.target-db-mapping.elasticache.use-case-4')}</li>
                                  </ul>
                                )}
                                {item.target === 'aurora' && (
                                  <ul>
                                    <li>{t('report-results.target-db-mapping.aurora.use-case-1')}</li>
                                    <li>{t('report-results.target-db-mapping.aurora.use-case-2')}</li>
                                    <li>{t('report-results.target-db-mapping.aurora.use-case-3')}</li>
                                    <li>{t('report-results.target-db-mapping.aurora.use-case-4')}</li>
                                  </ul>
                                )}
                              </Box>
                            </Box>

                            <Box>
                              <Box variant="h4">{t('report-results.target-db-mapping.migration-considerations')}</Box>
                              <Box padding={{ top: 's' }}>
                                <Box variant="awsui-key-label">{t('report-results.target-db-mapping.complexity')}</Box>
                                <Badge color={item.migration_complexity_avg === 'LOW' ? 'green' : item.migration_complexity_avg === 'MEDIUM' ? 'blue' : 'red'}>
                                  {item.migration_complexity_avg || 'UNKNOWN'}
                                </Badge>
                                <Box padding={{ top: 's' }}>
                                  <Box variant="awsui-key-label">{t('report-results.target-db-mapping.estimated-monthly-cost')}</Box>
                                  <Box fontSize="heading-l">${item.monthly_cost_usd?.toFixed(2) || '0.00'}</Box>
                                </Box>
                              </Box>
                            </Box>
                          </ColumnLayout>
                        </SpaceBetween>
                      )
                    }))}
                  />
                </Container>

                {/* Schema Designs */}
                <Container
                  id="schema-designs"
                  header={
                    <SectionSeparator
                      title={t('report-results.schema-designs.title')}
                      description={t('report-results.schema-designs.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <Tabs
                    tabs={Object.keys(schemaDesigns)
                      .filter(engine => schemaDesigns[engine]?.status === 'completed')
                      .map(engine => {
                        const design = schemaDesigns[engine];
                        return {
                          id: engine,
                          label: engine,
                          content: (
                            <SpaceBetween size="m">
                              {/* Validation Status */}
                              <Box>
                                {design.validation_passed ? (
                                  <Badge color="green">{t('report-results.schema-designs.validated')}</Badge>
                                ) : (
                                  <Badge color="red">{t('report-results.schema-designs.not-available')}</Badge>
                                )}
                              </Box>

                              {/* Summary Stats */}
                              <ColumnLayout columns={4} variant="text-grid">
                                <Box>
                                  <Box variant="awsui-key-label">{t('report-results.schema-designs.source-tables')}</Box>
                                  <Box fontSize="heading-l">{design.tables?.reduce((sum, tbl) => sum + (tbl.source_tables?.length || 0), 0) || 0}</Box>
                                </Box>
                                <Box>
                                  <Box variant="awsui-key-label">{t('report-results.schema-designs.target-tables')}</Box>
                                  <Box fontSize="heading-l">{design.tables?.length || 0}</Box>
                                </Box>
                                <Box>
                                  <Box variant="awsui-key-label">{t('report-results.schema-designs.total-gsis')}</Box>
                                  <Box fontSize="heading-l">{design.tables?.reduce((sum, tbl) => sum + (tbl.gsi_count || 0), 0) || 0}</Box>
                                </Box>
                                <Box>
                                  <Box variant="awsui-key-label">{t('report-results.schema-designs.access-patterns')}</Box>
                                  <Box fontSize="heading-l">{design.access_pattern_count || 0}</Box>
                                </Box>
                              </ColumnLayout>

                              {/* Tables */}
                              {design.tables && design.tables.length > 0 && engine !== 'opensearch' && (
                                <SchemaDesignTable tables={design.tables} />
                              )}

                              {/* For OpenSearch - show indexes with shards/replicas */}
                              {engine === 'opensearch' && design.tables && design.tables.length > 0 && (
                                <OpenSearchIndexTable tables={design.tables} />
                              )}

                              {/* Unsupported Patterns */}
                              {design.unsupported_patterns && design.unsupported_patterns.length > 0 && (
                                <Alert type="warning" header={t('report-results.schema-designs.unsupported-patterns', { count: design.unsupported_patterns.length })}>
                                  <SpaceBetween size="s">
                                    {design.unsupported_patterns.map((pattern, idx) => (
                                      <Box key={idx} fontSize="body-s">
                                        <Box fontWeight="bold">{pattern.pattern_type || 'Unknown'}</Box>
                                        <Box>{pattern.recommendation}</Box>
                                      </Box>
                                    ))}
                                  </SpaceBetween>
                                </Alert>
                              )}
                            </SpaceBetween>
                          )
                        };
                      })}
                  />
                </Container>

                {/* Risk Assessment */}
                <Container
                  id="risk-assessment"
                  header={
                    <SectionSeparator
                      title={t('report-results.risk-assessment.title', { count: totalRisks, level: riskAssessment.overall_risk_level || 'MEDIUM' })}
                      description={t('report-results.risk-assessment.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <SpaceBetween size="l">
                    {/* Overall Risk Summary */}
                    <Alert
                      type={riskAssessment.overall_risk_level === 'HIGH' ? 'error' : riskAssessment.overall_risk_level === 'LOW' ? 'success' : 'warning'}
                      header={t('report-results.risk-assessment.overall-risk-header', { level: riskAssessment.overall_risk_level || 'MEDIUM' })}
                    >
                      {t('report-results.risk-assessment.overall-risk-body', { level: riskAssessment.overall_risk_level || 'MEDIUM', count: totalRisks })}
                      {resolvedRisks.length > 0 && (
                        <> {t('report-results.risk-assessment.resolved-summary', { count: resolvedRisks.length })}</>
                      )}
                    </Alert>

                    {/* Detailed Risks - Tabs by Severity */}
                    <Tabs
                      tabs={[
                        {
                          id: 'high-severity',
                          label: t('report-results.risk-assessment.high-severity-tab', { count: highSeverityRisks.length }),
                          content: (
                            <Table
                              {...highRiskCollectionProps} // nosemgrep: react-props-spreading
                              columnDefinitions={[
                                {
                                  id: 'id',
                                  header: t('report-results.risk-assessment.col-id'),
                                  cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.id || 'N/A'}</Box>,
                                  width: 100
                                },
                                {
                                  id: 'engine',
                                  header: t('report-results.risk-assessment.col-engine'),
                                  cell: item => item.engine ? <Badge color={ENGINE_COLORS[item.engine] || 'blue'}>{item.engine}</Badge> : <Box>-</Box>,
                                  width: 120
                                },
                                {
                                  id: 'severity',
                                  header: t('report-results.risk-assessment.col-severity'),
                                  cell: item => (
                                    <Badge color="red">
                                      {item.severity}
                                    </Badge>
                                  ),
                                  width: 100
                                },
                                {
                                  id: 'type',
                                  header: t('report-results.risk-assessment.col-type'),
                                  cell: item => (
                                    <Box fontSize="body-s" fontWeight="bold">
                                      {item.risk_type || t('report-results.risk-assessment.type-technical')}
                                    </Box>
                                  ),
                                  width: 150
                                },
                                {
                                  id: 'description',
                                  header: t('report-results.risk-assessment.col-description'),
                                  cell: item => (
                                    <Box fontSize="body-s">
                                      {item.description}
                                      {item.affected_tables && item.affected_tables.length > 0 && (
                                        <Box padding={{ top: 'xs' }}>
                                          <Box variant="awsui-key-label">{t('report-results.risk-assessment.affected-tables')}</Box>
                                          <SpaceBetween direction="horizontal" size="xs">
                                            {item.affected_tables.map((table, idx) => (
                                              <Badge key={idx} color="grey">{table}</Badge>
                                            ))}
                                          </SpaceBetween>
                                        </Box>
                                      )}
                                    </Box>
                                  )
                                },
                                {
                                  id: 'mitigation',
                                  header: t('report-results.risk-assessment.col-mitigation'),
                                  cell: item => <Box fontSize="body-s">{item.mitigation}</Box>
                                }
                              ]}
                              items={highRiskItems}
                              variant="embedded"
                              wrapLines
                              filter={
                                <TextFilter
                                  {...highRiskFilterProps} // nosemgrep: react-props-spreading
                                  filteringPlaceholder={t('report-results.risk-assessment.filter-high')}
                                  filteringText={highRiskFilterProps.filteringText}
                                  countText={`${highRiskItems.length} ${highRiskItems.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
                                />
                              }
                              pagination={<Pagination {...highRiskPaginationProps} />} // nosemgrep: react-props-spreading
                            />
                          )
                        },
                        {
                          id: 'medium-severity',
                          label: t('report-results.risk-assessment.medium-severity-tab', { count: mediumSeverityRisks.length }),
                          content: (
                            <Table
                              {...mediumRiskCollectionProps} // nosemgrep: react-props-spreading
                              columnDefinitions={[
                                {
                                  id: 'id',
                                  header: t('report-results.risk-assessment.col-id'),
                                  cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.id || 'N/A'}</Box>,
                                  width: 100
                                },
                                {
                                  id: 'engine',
                                  header: t('report-results.risk-assessment.col-engine'),
                                  cell: item => item.engine ? <Badge color={ENGINE_COLORS[item.engine] || 'blue'}>{item.engine}</Badge> : <Box>-</Box>,
                                  width: 120
                                },
                                {
                                  id: 'severity',
                                  header: t('report-results.risk-assessment.col-severity'),
                                  cell: item => (
                                    <Badge color="blue">
                                      {item.severity}
                                    </Badge>
                                  ),
                                  width: 100
                                },
                                {
                                  id: 'type',
                                  header: t('report-results.risk-assessment.col-type'),
                                  cell: item => (
                                    <Box fontSize="body-s" fontWeight="bold">
                                      {item.risk_type || t('report-results.risk-assessment.type-technical')}
                                    </Box>
                                  ),
                                  width: 150
                                },
                                {
                                  id: 'description',
                                  header: t('report-results.risk-assessment.col-description'),
                                  cell: item => (
                                    <Box fontSize="body-s">
                                      {item.title && <Box fontWeight="bold" padding={{ bottom: 'xs' }}>{item.title}</Box>}
                                      {item.description}
                                      {item.impact && (
                                        <Box padding={{ top: 'xs' }} color="text-status-error" fontSize="body-s">
                                          {t('report-results.risk-assessment.impact-label')}{item.impact}
                                        </Box>
                                      )}
                                    </Box>
                                  )
                                },
                                {
                                  id: 'mitigation',
                                  header: t('report-results.risk-assessment.col-mitigation'),
                                  cell: item => <Box fontSize="body-s">{item.mitigation}</Box>
                                }
                              ]}
                              items={mediumRiskItems}
                              variant="embedded"
                              wrapLines
                              filter={
                                <TextFilter
                                  {...mediumRiskFilterProps} // nosemgrep: react-props-spreading
                                  filteringPlaceholder={t('report-results.risk-assessment.filter-medium')}
                                  filteringText={mediumRiskFilterProps.filteringText}
                                  countText={`${mediumRiskItems.length} ${mediumRiskItems.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
                                />
                              }
                              pagination={<Pagination {...mediumRiskPaginationProps} />} // nosemgrep: react-props-spreading
                            />
                          )
                        },
                        {
                          id: 'low-severity',
                          label: t('report-results.risk-assessment.low-severity-tab', { count: lowSeverityRisks.length }),
                          content: (
                            <Table
                              {...lowRiskCollectionProps} // nosemgrep: react-props-spreading
                              columnDefinitions={[
                                {
                                  id: 'id',
                                  header: t('report-results.risk-assessment.col-id'),
                                  cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.id || 'N/A'}</Box>,
                                  width: 100
                                },
                                {
                                  id: 'engine',
                                  header: t('report-results.risk-assessment.col-engine'),
                                  cell: item => item.engine ? <Badge color={ENGINE_COLORS[item.engine] || 'blue'}>{item.engine}</Badge> : <Box>-</Box>,
                                  width: 120
                                },
                                {
                                  id: 'severity',
                                  header: t('report-results.risk-assessment.col-severity'),
                                  cell: item => (
                                    <Badge color="grey">
                                      {item.severity}
                                    </Badge>
                                  ),
                                  width: 100
                                },
                                {
                                  id: 'type',
                                  header: t('report-results.risk-assessment.col-type'),
                                  cell: item => (
                                    <Box fontSize="body-s" fontWeight="bold">
                                      {item.risk_type || t('report-results.risk-assessment.type-technical')}
                                    </Box>
                                  ),
                                  width: 150
                                },
                                {
                                  id: 'description',
                                  header: t('report-results.risk-assessment.col-description'),
                                  cell: item => (
                                    <Box fontSize="body-s">
                                      {item.title && <Box fontWeight="bold" padding={{ bottom: 'xs' }}>{item.title}</Box>}
                                      {item.description}
                                      {item.impact && (
                                        <Box padding={{ top: 'xs' }} color="text-status-error" fontSize="body-s">
                                          {t('report-results.risk-assessment.impact-label')}{item.impact}
                                        </Box>
                                      )}
                                    </Box>
                                  )
                                },
                                {
                                  id: 'mitigation',
                                  header: t('report-results.risk-assessment.col-mitigation'),
                                  cell: item => <Box fontSize="body-s">{item.mitigation}</Box>
                                }
                              ]}
                              items={lowRiskItems}
                              variant="embedded"
                              wrapLines
                              filter={
                                <TextFilter
                                  {...lowRiskFilterProps} // nosemgrep: react-props-spreading
                                  filteringPlaceholder={t('report-results.risk-assessment.filter-low')}
                                  filteringText={lowRiskFilterProps.filteringText}
                                  countText={`${lowRiskItems.length} ${lowRiskItems.length === 1 ? t('common.labels.match') : t('common.labels.matches')}`}
                                />
                              }
                              pagination={<Pagination {...lowRiskPaginationProps} />} // nosemgrep: react-props-spreading
                            />
                          )
                        },
                        {
                          id: 'resolved',
                          label: t('report-results.risk-assessment.resolved-tab', { count: resolvedRisks.length }),
                          content: (
                            <Table
                              trackBy="key"
                              columnDefinitions={[
                                {
                                  id: 'engine',
                                  header: t('report-results.risk-assessment.col-engine'),
                                  cell: item => item.engine ? <Badge color={ENGINE_COLORS[item.engine] || 'blue'}>{item.engine}</Badge> : <Box>-</Box>,
                                  width: 120
                                },
                                {
                                  id: 'resolved-on',
                                  header: t('report-results.risk-assessment.col-resolved-on'),
                                  cell: item => item.resolved_on ? <Badge color={ENGINE_COLORS[item.resolved_on] || 'blue'}>{item.resolved_on}</Badge> : <Box>-</Box>,
                                  width: 120
                                },
                                {
                                  id: 'severity',
                                  header: t('report-results.risk-assessment.col-severity'),
                                  cell: item => <Badge color="grey">{item.severity}</Badge>,
                                  width: 100
                                },
                                {
                                  id: 'description',
                                  header: t('report-results.risk-assessment.col-description'),
                                  cell: item => <Box fontSize="body-s">{item.description}</Box>
                                },
                                {
                                  id: 'reason',
                                  header: t('report-results.risk-assessment.col-reason'),
                                  cell: item => <Box fontSize="body-s">{item.reason}</Box>
                                }
                              ]}
                              items={resolvedRisks}
                              variant="embedded"
                              wrapLines
                              empty={<Box textAlign="center" color="inherit"><Box padding={{ bottom: 's' }} variant="p" color="inherit">{t('report-results.risk-assessment.no-resolved-risks')}</Box></Box>}
                            />
                          )
                        }
                      ]}
                    />
                  </SpaceBetween>
                </Container>

                {/* Trade-offs and Design Decisions */}
                {processedTradeoffs.length > 0 && (
                <Container
                  id="tradeoffs"
                  header={
                    <SectionSeparator
                      title={t('report-results.tradeoffs.title', { count: processedTradeoffs.length })}
                      description={t('report-results.tradeoffs.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <SpaceBetween size="l">
                    {uniqueEngines.map(engine => {
                      const engineTradeoffs = tradeoffsByEngine[engine] || [];
                      // Separate PE notes from regular trade-offs
                      const peNotes = engineTradeoffs.filter(item => item.description.startsWith('[PE note]'));
                      const designDecisions = engineTradeoffs.filter(item => !item.description.startsWith('[PE note]'));
                      return (
                        <SpaceBetween key={engine} size="s">
                          <Box>
                            <Badge color={ENGINE_COLORS[engine] || 'blue'}>{engine}</Badge>
                            <Box variant="small" display="inline" padding={{ left: 'xs' }} color="text-body-secondary">
                              {t('report-results.tradeoffs.count', { count: engineTradeoffs.length })}
                            </Box>
                          </Box>

                          {/* Design decisions */}
                          {designDecisions.map((tradeOff, idx) => (
                            <Box key={idx} padding={{ vertical: 'xs', horizontal: 's' }}
                              style={{ borderLeft: '3px solid #0972d3', backgroundColor: '#f2f8fd', borderRadius: '4px' }}>
                              <SpaceBetween size="xxs">
                                <Box fontSize="body-s" fontWeight="bold">{tradeOff.description}</Box>
                                {tradeOff.impact && (
                                  <Box fontSize="body-s" color="text-body-secondary">{tradeOff.impact}</Box>
                                )}
                                {(tradeOff.source_tables.length > 0 || tradeOff.target_tables.length > 0) && (
                                  <Box fontSize="body-s" color="text-status-inactive">
                                    {tradeOff.source_tables.length > 0 && <span>{tradeOff.source_tables.join(', ')}</span>}
                                    {tradeOff.source_tables.length > 0 && tradeOff.target_tables.length > 0 && <span> → </span>}
                                    {tradeOff.target_tables.length > 0 && <span>{tradeOff.target_tables.join(', ')}</span>}
                                  </Box>
                                )}
                                {tradeOff.query_ids.length > 0 && (
                                  <Box fontSize="body-s" color="text-status-inactive">
                                    {t('report-results.tradeoffs.queries-label')}{tradeOff.query_ids.join(', ')}
                                  </Box>
                                )}
                              </SpaceBetween>
                            </Box>
                          ))}

                          {/* PE notes in a collapsible section */}
                          {peNotes.length > 0 && (
                            <ExpandableSection
                              headerText={t('report-results.tradeoffs.pe-notes-header', { count: peNotes.length })}
                              variant="footer"
                              defaultExpanded={false}
                            >
                              <SpaceBetween size="xs">
                                {peNotes.map((tradeOff, idx) => (
                                  <Box key={idx} padding={{ vertical: 'xs', horizontal: 's' }}
                                    style={{ borderLeft: '3px solid #ff9900', backgroundColor: '#fff8e6', borderRadius: '4px' }}>
                                    <SpaceBetween size="xxs">
                                      <Box fontSize="body-s">{tradeOff.description.replace(/^\[PE note\]\s*/, '')}</Box>
                                      {tradeOff.impact && (
                                        <Box fontSize="body-s" color="text-body-secondary">{tradeOff.impact}</Box>
                                      )}
                                    </SpaceBetween>
                                  </Box>
                                ))}
                              </SpaceBetween>
                            </ExpandableSection>
                          )}
                        </SpaceBetween>
                      );
                    })}
                  </SpaceBetween>
                </Container>
                )}

                {/* TCO Analysis */}
                {tcoAnalysis && (
                    <Container
                      id="tco-analysis"
                      header={
                        <SectionSeparator
                          title={t('report-results.tco.title')}
                          description={t('report-results.tco.description')}
                          onTopClick={() => scrollToSection('report-contents')}
                        />
                      }
                    >
                      <ColumnLayout columns={3} variant="text-grid">
                        <Box>
                          <Box variant="awsui-key-label">{t('report-results.tco.current-monthly-cost')}</Box>
                          <Box fontSize="heading-xl" fontWeight="bold">
                            ${tcoAnalysis.current_monthly_cost?.toFixed(2) || '0.00'}
                          </Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">{t('report-results.tco.projected-monthly-cost')}</Box>
                          <Box fontSize="heading-xl" fontWeight="bold" color="text-status-success">
                            ${tcoAnalysis.projected_monthly_cost?.toFixed(2) || '0.00'}
                          </Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">{t('report-results.tco.savings')}</Box>
                          <Box fontSize="heading-xl" fontWeight="bold" color={tcoAnalysis.savings_percent > 0 ? 'text-status-success' : 'inherit'}>
                            {tcoAnalysis.savings_percent?.toFixed(1) || '0'}%
                          </Box>
                        </Box>
                      </ColumnLayout>
                    </Container>
                )}

                {/* Migration Roadmap */}
                <Container
                  id="migration-roadmap"
                  header={
                    <SectionSeparator
                      title={t('report-results.roadmap.title')}
                      description={t('report-results.roadmap.description')}
                      onTopClick={() => scrollToSection('report-contents')}
                    />
                  }
                >
                  <ColumnLayout columns={4} variant="text-grid">
                    <Box>
                      <Badge color="green">{t('report-results.roadmap.phase-1-label')}</Badge>
                      <Box variant="h4" padding={{ top: 's' }}>{t('report-results.roadmap.phase-1-title')}</Box>
                      <Box padding={{ top: 's' }} fontSize="body-s">
                        <ul>
                          <li>{t('report-results.roadmap.phase-1-item-1')}</li>
                          <li>{t('report-results.roadmap.phase-1-item-2')}</li>
                          <li>{t('report-results.roadmap.phase-1-item-3')}</li>
                          <li>{t('report-results.roadmap.phase-1-item-4')}</li>
                        </ul>
                      </Box>
                      <Box variant="small" color="text-status-success">{t('report-results.roadmap.phase-1-timeline')}</Box>
                    </Box>

                    <Box>
                      <Badge color="blue">{t('report-results.roadmap.phase-2-label')}</Badge>
                      <Box variant="h4" padding={{ top: 's' }}>{t('report-results.roadmap.phase-2-title')}</Box>
                      <Box padding={{ top: 's' }} fontSize="body-s">
                        <ul>
                          <li>{t('report-results.roadmap.phase-2-item-1')}</li>
                          <li>{t('report-results.roadmap.phase-2-item-2')}</li>
                          <li>{t('report-results.roadmap.phase-2-item-3')}</li>
                          <li>{t('report-results.roadmap.phase-2-item-4')}</li>
                        </ul>
                      </Box>
                      <Box variant="small" color="text-status-info">{t('report-results.roadmap.phase-2-timeline')}</Box>
                    </Box>

                    <Box>
                      <Badge>{t('report-results.roadmap.phase-3-label')}</Badge>
                      <Box variant="h4" padding={{ top: 's' }}>{t('report-results.roadmap.phase-3-title')}</Box>
                      <Box padding={{ top: 's' }} fontSize="body-s">
                        <ul>
                          <li>{t('report-results.roadmap.phase-3-item-1')}</li>
                          <li>{t('report-results.roadmap.phase-3-item-2')}</li>
                          <li>{t('report-results.roadmap.phase-3-item-3')}</li>
                          <li>{t('report-results.roadmap.phase-3-item-4')}</li>
                        </ul>
                      </Box>
                      <Box variant="small">{t('report-results.roadmap.phase-3-timeline')}</Box>
                    </Box>

                    <Box>
                      <Badge color="green">{t('report-results.roadmap.phase-4-label')}</Badge>
                      <Box variant="h4" padding={{ top: 's' }}>{t('report-results.roadmap.phase-4-title')}</Box>
                      <Box padding={{ top: 's' }} fontSize="body-s">
                        <ul>
                          <li>{t('report-results.roadmap.phase-4-item-1')}</li>
                          <li>{t('report-results.roadmap.phase-4-item-2')}</li>
                          <li>{t('report-results.roadmap.phase-4-item-3')}</li>
                          <li>{t('report-results.roadmap.phase-4-item-4')}</li>
                        </ul>
                      </Box>
                      <Box variant="small" color="text-status-success">{t('report-results.roadmap.phase-4-timeline')}</Box>
                    </Box>
                  </ColumnLayout>
                </Container>

                {/* Query Classification */}
                {queryGroups.length > 0 && (
                    <Container
                      id="query-classification"
                      header={
                        <SectionSeparator
                          title={t('report-results.query-classification.title', { count: queryGroups.length })}
                          description={t('report-results.query-classification.description')}
                          onTopClick={() => scrollToSection('report-contents')}
                        />
                      }
                    >
                    <Tabs
                      tabs={queryGroups.map((group, index) => {
                        // Collect query IDs in this group to match trade-offs
                        const groupQueryIds = new Set([
                          ...(group.source_queries || []),
                          ...(group.access_patterns || []).map(ap => ap.source_query_id).filter(Boolean),
                          ...(group.access_patterns || []).map(ap => ap.pattern_id).filter(Boolean),
                        ]);
                        // Trade-offs that reference at least one query in this group
                        const groupTradeoffs = processedTradeoffs.filter(t =>
                          t.query_ids.length > 0 && t.query_ids.some(qid => groupQueryIds.has(qid))
                        );
                        // Trade-offs for this group's engines that have no query_ids (general trade-offs)
                        const groupEngines = new Set(group.engines || []);
                        const generalTradeoffs = processedTradeoffs.filter(t =>
                          t.query_ids.length === 0 && groupEngines.has(t.engine)
                        );

                        return {
                        id: `group-${index}`,
                        label: `${group.group_name} (${group.access_patterns?.length || 0})`,
                        content: (
                          <SpaceBetween size="m">
                            <ColumnLayout columns={3} variant="text-grid">
                              <Box>
                                <Box variant="awsui-key-label">{t('report-results.query-classification.target-engines')}</Box>
                                <SpaceBetween direction="horizontal" size="xs">
                                  {(group.engines || []).map((engine, idx) => (
                                    <Badge key={idx} color={ENGINE_COLORS[engine] || 'grey'}>{engine}</Badge>
                                  ))}
                                </SpaceBetween>
                              </Box>
                              <Box>
                                <Box variant="awsui-key-label">{t('report-results.query-classification.total-design-rps')}</Box>
                                <Box fontSize="heading-m">{group.total_design_rps?.toFixed(2) || '0'}</Box>
                              </Box>
                              <Box>
                                <Box variant="awsui-key-label">{t('report-results.query-classification.source-queries')}</Box>
                                <Box fontSize="heading-m">{group.source_queries?.length || 0}</Box>
                              </Box>
                            </ColumnLayout>

                            {group.access_patterns && group.access_patterns.length > 0 && (
                              <AccessPatternTable
                                accessPatterns={group.access_patterns}
                                tradeoffs={groupTradeoffs}
                              />
                            )}

                            {generalTradeoffs.length > 0 && (
                              <ExpandableSection
                                headerText={t('report-results.query-classification.general-tradeoffs', { count: generalTradeoffs.length })}
                                variant="footer"
                                defaultExpanded={false}
                              >
                                <InlineTradeoffs tradeoffs={generalTradeoffs} />
                              </ExpandableSection>
                            )}
                          </SpaceBetween>
                        )
                      };
                      })}
                    />
                  </Container>
                )}

              </SpaceBetween>
            )}

          </SpaceBetween>
        }
        contentType="default"
        toolsHide
      />
    </>
  );
});

export default ReportResultsPage;
