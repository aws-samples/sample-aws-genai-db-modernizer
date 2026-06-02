//##-- React
import { useState, useEffect, memo, useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams } from 'react-router-dom';

//##-- Cloudscape
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Box from "@cloudscape-design/components/box";
import Badge from "@cloudscape-design/components/badge";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Flashbar from "@cloudscape-design/components/flashbar";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import Spinner from "@cloudscape-design/components/spinner";

//##-- Custom
import { SideNavigationConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";
import ChartSankey from "../components/ChartSankey-01";


const ENGINE_COLORS = {
  dynamodb: 'blue',
  documentdb: 'green',
  elasticache: 'red',
  opensearch: 'grey',
  neptune: 'red',
  keyspaces: 'blue',
  aurora: 'green',
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


const AnalysisResultsPage = memo(() => {
  const { t } = useTranslation();
  const { jobId } = useParams();
  const [navigationOpen, setNavigationOpen] = useState(false);
  const [resultsData, setResultsData] = useState(null);
  const [schemaDesigns, setSchemaDesigns] = useState([]);
  const [loading, setLoading] = useState(true);
  const [flashbarItems, setFlashbarItems] = useState([]);

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
    } catch (error) {
      addFlashbarMessage({ type: 'error', header: t('common.labels.error'), content: error.message, dismissible: true, id: `error-${Date.now()}` });
    } finally {
      setLoading(false);
    }
  }, [jobId, addFlashbarMessage, t]);

  useEffect(() => { fetchData(); }, [jobId]); // eslint-disable-line react-hooks/exhaustive-deps

  // -- Derived data --
  const synthesis = resultsData?.synthesis || {};
  const tcoAnalysis = synthesis?.tco_analysis || {};
  const realityCheck = synthesis?.reality_check || {};
  const afterDist = realityCheck?.after_distribution || {};

  // Filter schema designs to only engines with actual content (not skipped)
  const activeDesigns = useMemo(() => {
    return schemaDesigns.filter(d => {
      const c = d.content || {};
      return c.table_definitions?.length > 0 || c.index_designs?.length > 0 || c.collection_designs?.length > 0;
    });
  }, [schemaDesigns]);

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
    { href: "/", text: t('dashboard.breadcrumb.home') },
    { href: "/dashboard", text: t('dashboard.breadcrumb.dashboard') },
    { href: `/analysis/monitor/summary/${jobId}`, text: t('job-summary.breadcrumb.assessment') },
    { href: `/analysis/results/${jobId}`, text: t('analysis-results.breadcrumb.results') }
  ], [jobId, t]);

  // -- Loading state --
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

  // -- Render helpers --
  const renderDynamoDBDesign = (content) => {
    const tables = content.table_definitions || [];
    const patterns = content.access_patterns || [];
    return (
      <SpaceBetween size="m">
        <ColumnLayout columns={4} variant="text-grid">
          <Box>
            <Box variant="awsui-key-label">{t('analysis-results.dynamodb.tables')}</Box>
            <Box fontSize="heading-m" fontWeight="bold">{tables.length}</Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">{t('analysis-results.dynamodb.gsis')}</Box>
            <Box fontSize="heading-m" fontWeight="bold">{tables.reduce((sum, tbl) => sum + (tbl.gsis?.length || 0), 0)}</Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">{t('analysis-results.executive-summary.access-patterns')}</Box>
            <Box fontSize="heading-m" fontWeight="bold">{patterns.length}</Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">{t('analysis-results.dynamodb.unsupported')}</Box>
            <Box fontSize="heading-m" fontWeight="bold">{(content.unsupported_patterns || []).length}</Box>
          </Box>
        </ColumnLayout>

        <Table
          columnDefinitions={[
            { id: "name", header: t('analysis-results.dynamodb.col-table'), cell: item => <Box fontWeight="bold">{item.table_name}</Box>, width: 180 },
            { id: "pk", header: t('analysis-results.dynamodb.col-partition-key'), cell: item => <Box fontFamily="monospace" fontSize="body-s">{item.partition_key?.attribute_name} ({item.partition_key?.attribute_type})</Box>, width: 160 },
            { id: "sk", header: t('analysis-results.dynamodb.col-sort-key'), cell: item => item.sort_key ? <Box fontFamily="monospace" fontSize="body-s">{item.sort_key.attribute_name} ({item.sort_key.attribute_type})</Box> : '—', width: 160 },
            { id: "gsis", header: t('analysis-results.dynamodb.col-gsis'), cell: item => item.gsis?.length || 0, width: 60 },
            { id: "sources", header: t('analysis-results.dynamodb.col-source-tables'), cell: item => (
              <Box whiteSpace="normal" fontSize="body-s" color="text-body-secondary">
                {(item.source_tables || []).map(src => src.split('.').pop()).join(', ')}
              </Box>
            )},
            { id: "pattern", header: t('analysis-results.dynamodb.col-design-pattern'), cell: item => item.design_pattern?.replace(/_/g, ' ') || item.aggregate_pattern?.replace(/_/g, ' ') || '—', width: 160 },
          ]}
          items={tables}
          variant="embedded"
          wrapLines
        />

        {/* Access patterns */}
        {patterns.length > 0 && (
          <ExpandableSection headerText={t('analysis-results.dynamodb.access-patterns-expandable', { count: patterns.length })} defaultExpanded={false} variant="footer">
            <Table
              columnDefinitions={[
                { id: "id", header: t('analysis-results.dynamodb.col-pattern'), cell: item => <Box fontWeight="bold" fontSize="body-s">{item.pattern_id || item.pattern_name}</Box>, width: 120 },
                { id: "op", header: t('analysis-results.dynamodb.col-operation'), cell: item => item.operation, width: 100 },
                { id: "table", header: t('analysis-results.dynamodb.col-table'), cell: item => item.table_name, width: 150 },
                { id: "key", header: t('analysis-results.dynamodb.col-key-condition'), cell: item => <Box fontFamily="monospace" fontSize="body-s" whiteSpace="normal">{item.key_condition || item.key_expression}</Box> },
                { id: "desc", header: t('analysis-results.dynamodb.col-description'), cell: item => <Box whiteSpace="normal" fontSize="body-s">{item.description}</Box> },
              ]}
              items={patterns}
              variant="embedded"
              wrapLines
            />
          </ExpandableSection>
        )}
      </SpaceBetween>
    );
  };

  const renderOpenSearchDesign = (content) => {
    const indexes = content.index_designs || [];
    const streams = content.data_stream_designs || [];
    const patterns = content.access_patterns || [];
    return (
      <SpaceBetween size="m">
        <ColumnLayout columns={4} variant="text-grid">
          <Box>
            <Box variant="awsui-key-label">{t('analysis-results.opensearch.indexes')}</Box>
            <Box fontSize="heading-m" fontWeight="bold">{indexes.length}</Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">{t('analysis-results.opensearch.data-streams')}</Box>
            <Box fontSize="heading-m" fontWeight="bold">{streams.length}</Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">{t('analysis-results.executive-summary.access-patterns')}</Box>
            <Box fontSize="heading-m" fontWeight="bold">{patterns.length}</Box>
          </Box>
          <Box>
            <Box variant="awsui-key-label">{t('analysis-results.dynamodb.unsupported')}</Box>
            <Box fontSize="heading-m" fontWeight="bold">{(content.unsupported_patterns || []).length}</Box>
          </Box>
        </ColumnLayout>

        {indexes.length > 0 && (
          <Table
            columnDefinitions={[
              { id: "name", header: t('analysis-results.opensearch.col-index'), cell: item => <Box fontWeight="bold">{item.index_name}</Box>, width: 180 },
              { id: "shards", header: t('analysis-results.opensearch.col-shards'), cell: item => item.settings?.number_of_shards || '—', width: 80 },
              { id: "replicas", header: t('analysis-results.opensearch.col-replicas'), cell: item => item.settings?.number_of_replicas ?? '—', width: 80 },
              { id: "fields", header: t('analysis-results.opensearch.col-fields'), cell: item => Object.keys(item.mappings?.properties || {}).length, width: 80 },
              { id: "sources", header: t('analysis-results.opensearch.col-source-tables'), cell: item => (
                <Box whiteSpace="normal" fontSize="body-s" color="text-body-secondary">
                  {(item.source_tables || []).map(src => src.split('.').pop()).join(', ')}
                </Box>
              )},
            ]}
            items={indexes}
            variant="embedded"
            wrapLines
          />
        )}

        {patterns.length > 0 && (
          <ExpandableSection headerText={t('analysis-results.opensearch.access-patterns-expandable', { count: patterns.length })} defaultExpanded={false} variant="footer">
            <Table
              columnDefinitions={[
                { id: "id", header: t('analysis-results.opensearch.col-pattern'), cell: item => <Box fontWeight="bold" fontSize="body-s">{item.pattern_id || item.pattern_name}</Box>, width: 120 },
                { id: "op", header: t('analysis-results.opensearch.col-operation'), cell: item => item.operation || item.http_method, width: 100 },
                { id: "index", header: t('analysis-results.opensearch.col-index-col'), cell: item => item.index || item.table_name, width: 150 },
                { id: "desc", header: t('analysis-results.opensearch.col-description'), cell: item => <Box whiteSpace="normal" fontSize="body-s">{item.description}</Box> },
              ]}
              items={patterns}
              variant="embedded"
              wrapLines
            />
          </ExpandableSection>
        )}
      </SpaceBetween>
    );
  };

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
                  <Button iconName="refresh" variant="normal" onClick={fetchData} loading={loading} />
                  <Button href={`/analysis/monitor/summary/${jobId}`}>{t('analysis-results.actions.back-to-assessment')}</Button>
                </SpaceBetween>
              }
            >
              {t('analysis-results.header.title')}
            </Header>

            {/* Executive Summary */}
            <Container header={<Header variant="h2">{t('analysis-results.executive-summary.title')}</Header>}>
              <SpaceBetween size="m">
                <Box variant="p" fontSize="body-m">
                  {synthesis?.summary || 'No summary available.'}
                </Box>
                <ColumnLayout columns={4} variant="text-grid">
                  <Box>
                    <Box variant="awsui-key-label">{t('analysis-results.executive-summary.database')}</Box>
                    <Box fontSize="heading-m" fontWeight="bold">{synthesis?.database_name || '—'}</Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">{t('analysis-results.executive-summary.target-engines')}</Box>
                    <Box fontSize="heading-m">
                      <SpaceBetween direction="horizontal" size="xxs">
                        {Object.keys(afterDist).map(engine => (
                          <Badge key={engine} color={ENGINE_COLORS[engine] || 'grey'}>{ENGINE_LABELS[engine] || engine}</Badge>
                        ))}
                      </SpaceBetween>
                    </Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">{t('analysis-results.executive-summary.projected-cost')}</Box>
                    <Box fontSize="heading-m" fontWeight="bold">
                      {tcoAnalysis?.projected_monthly_cost != null
                        ? `$${tcoAnalysis.projected_monthly_cost.toFixed(2)}/mo`
                        : '—'}
                    </Box>
                  </Box>
                  <Box>
                    <Box variant="awsui-key-label">{t('analysis-results.executive-summary.access-patterns')}</Box>
                    <Box fontSize="heading-m" fontWeight="bold">
                      {Object.values(afterDist).reduce((a, b) => a + b, 0) || '—'}
                    </Box>
                  </Box>
                </ColumnLayout>
              </SpaceBetween>
            </Container>

            {/* Query Flow (Sankey) */}
            {sankeyData && (
              <Container header={
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

            {/* TCO Breakdown */}
            {tcoAnalysis?.cost_breakdown?.length > 0 && (
              <Container header={<Header variant="h2" description={t('analysis-results.cost-breakdown.description')}>{t('analysis-results.cost-breakdown.title')}</Header>}>
                <SpaceBetween size="m">
                  <ColumnLayout columns={tcoAnalysis.cost_breakdown.length} variant="text-grid">
                    {tcoAnalysis.cost_breakdown
                      .filter(cb => afterDist[cb.database] != null)
                      .map((cb, idx) => (
                        <Box key={idx} textAlign="center">
                          <Badge color={ENGINE_COLORS[cb.database] || 'grey'}>{ENGINE_LABELS[cb.database] || cb.database}</Badge>
                          <Box fontSize="display-l" fontWeight="bold" margin={{ top: 'xs' }}>
                            ${cb.monthly_cost_usd?.toFixed(2)}
                          </Box>
                          <Box fontSize="body-s" color="text-body-secondary">/month · {cb.pricing_mode}</Box>
                        </Box>
                      ))}
                  </ColumnLayout>
                  {tcoAnalysis.assumptions?.length > 0 && (
                    <Box fontSize="body-s" color="text-body-secondary">
                      {tcoAnalysis.assumptions.join(' · ')}
                    </Box>
                  )}
                </SpaceBetween>
              </Container>
            )}

            {/* Schema Designs */}
            {activeDesigns.length > 0 && (
              <Container header={
                <Header variant="h2" description={t('analysis-results.schema-designs.description')}>
                  {t('analysis-results.schema-designs.title')}
                </Header>
              }>
                <Tabs
                  tabs={activeDesigns.map(design => {
                    const engine = design.target_type;
                    const content = design.content || {};
                    return {
                      id: engine,
                      label: (
                        <SpaceBetween direction="horizontal" size="xxs">
                          <Badge color={ENGINE_COLORS[engine] || 'grey'}>{ENGINE_LABELS[engine] || engine}</Badge>
                        </SpaceBetween>
                      ),
                      content: engine === 'dynamodb'
                        ? renderDynamoDBDesign(content)
                        : engine === 'opensearch'
                          ? renderOpenSearchDesign(content)
                          : <Box padding="m" color="text-body-secondary">{t('analysis-results.schema-designs.viewer-not-available', { engine })}</Box>
                    };
                  })}
                />
              </Container>
            )}

            {/* Additional Information */}
            {(synthesis?.trade_offs?.length > 0 || realityCheck?.recommendations?.length > 0) && (
              <ExpandableSection headerText={t('analysis-results.additional-info.title')} variant="container" defaultExpanded={false}>
                <SpaceBetween size="m">
                  {realityCheck?.recommendations?.length > 0 && (
                    <Box>
                      <Box variant="awsui-key-label" margin={{ bottom: 'xs' }}>{t('analysis-results.additional-info.recommendations')}</Box>
                      <ul style={{ margin: 0, paddingLeft: '20px' }}>
                        {realityCheck.recommendations.map((rec, idx) => (
                          <li key={idx} style={{ marginBottom: '4px' }}>
                            <Box fontSize="body-s">{rec}</Box>
                          </li>
                        ))}
                      </ul>
                    </Box>
                  )}
                  {synthesis?.trade_offs?.length > 0 && (
                    <Box>
                      <Box variant="awsui-key-label" margin={{ bottom: 'xs' }}>{t('analysis-results.additional-info.trade-offs')}</Box>
                      <ul style={{ margin: 0, paddingLeft: '20px' }}>
                        {synthesis.trade_offs.map((tradeOff, idx) => (
                          <li key={idx} style={{ marginBottom: '4px' }}>
                            <Box fontSize="body-s">{tradeOff}</Box>
                          </li>
                        ))}
                      </ul>
                    </Box>
                  )}
                </SpaceBetween>
              </ExpandableSection>
            )}

          </SpaceBetween>
        }
        contentType="default"
        toolsHide
      />
    </>
  );
});

export default AnalysisResultsPage;
