//##-- React Events
import { useState, useEffect, memo, useCallback, useRef, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, useNavigate } from 'react-router-dom';

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
import Table from "@cloudscape-design/components/table";
import Tabs from "@cloudscape-design/components/tabs";
import Button from "@cloudscape-design/components/button";
import PieChart from "@cloudscape-design/components/pie-chart";
import Popover from "@cloudscape-design/components/popover";
import SegmentedControl from "@cloudscape-design/components/segmented-control";

//##-- Custom Objects
import { SideNavigationConfigurations, ApiConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";



//--|#######################|
//--|#######################| Main Page  |#######################
//--|#######################|



const ENGINE_COLORS = {
  dynamodb: 'blue',
  documentdb: 'green',
  elasticache: 'red',
  opensearch: 'grey',
  neptune: 'red',
  keyspaces: 'blue',
  aurora: 'green',
};



const PatternAnalysisPage = memo(() => {

  const { t } = useTranslation();

  //##-- Get jobId from URL parameters
  const { jobId } = useParams();
  const navigate = useNavigate();

  //##-- Get target from URL query params
  const searchParams = new URLSearchParams(window.location.search);
  const target = searchParams.get('target');


  //--|#######################| State Management Section  |#######################

  //-- Variable for Navigation Panel
  const [navigationOpen, setNavigationOpen] = useState(false);

  //--######## Data State
  const [triageData, setTriageData] = useState(null);
  const [collectorData, setCollectorData] = useState(null);
  const [analysisData, setAnalysisData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [initialLoad, setInitialLoad] = useState(true);

  //-- Flashbar state for error messages
  const [flashbarItems, setFlashbarItems] = useState([]);




  //--|#######################| Handle Section  |#######################


  //##-- Flashbar message handler
  const addFlashbarMessage = useCallback((message) => {
    setFlashbarItems(prevItems => [...prevItems, message]);
  }, []);

  const handleFlashbarDismiss = useCallback((itemId) => {
    setFlashbarItems(prevItems => prevItems.filter(item => item.id !== itemId));
  }, []);




  //--|#######################| Gather Information Section  |#######################

  //##-- Gather triage and collector information
  const gatherPatternData = useCallback(async () => {
    if (!jobId) {
      addFlashbarMessage({
        type: 'error',
        header: t('pattern-analysis.error.invalid-job-id'),
        content: t('pattern-analysis.error.no-job-id'),
        dismissible: true,
        id: `error-${Date.now()}`
      });
      setLoading(false);
      setInitialLoad(false);
      return;
    }

    if (initialLoad) {
      setLoading(true);
    }

    try {
      const apiManager = new ApiManager();

      const apiCalls = [
        {
          id: 'get-triage',
          path: `assessments/${jobId}/triage`,
          method: 'GET',
          params: {}
        },
        {
          id: 'get-collector',
          path: `assessments/${jobId}/collector`,
          method: 'GET',
          params: {}
        },
        {
          id: 'get-analysis',
          path: `assessments/${jobId}/analysis/${target}`,
          method: 'GET',
          params: {}
        }
      ];

      const results = await apiManager.execute(apiCalls);
      console.log('Pattern data:', results);

      // Handle triage response
      if (results['get-triage']?.error) {
        const result = results['get-triage'];
        const errorMessage = result.error?.message || t('pattern-analysis.error.api-error-content', { apiUrl: '', errorMessage: '' });
        const statusCode = result.status || 'Unknown';
        const apiUrl = `${ApiConfigurations.baseUrl}assessments/${jobId}/triage`;

        addFlashbarMessage({
          type: 'error',
          header: t('pattern-analysis.error.api-error-header', { statusCode }),
          content: t('pattern-analysis.error.api-error-content', { apiUrl, errorMessage }),
          dismissible: true,
          id: `error-${Date.now()}`
        });
      } else if (results['get-triage']?.success) {
        setTriageData(results['get-triage']);
      }

      // Handle collector response
      if (results['get-collector']?.error) {
        const result = results['get-collector'];
        const errorMessage = result.error?.message || t('pattern-analysis.error.api-error-content', { apiUrl: '', errorMessage: '' });
        const statusCode = result.status || 'Unknown';
        const apiUrl = `${ApiConfigurations.baseUrl}assessments/${jobId}/collector`;

        addFlashbarMessage({
          type: 'error',
          header: t('pattern-analysis.error.api-error-header', { statusCode }),
          content: t('pattern-analysis.error.api-error-content', { apiUrl, errorMessage }),
          dismissible: true,
          id: `error-${Date.now()}`
        });
      } else if (results['get-collector']?.success) {
        setCollectorData(results['get-collector']);
      }

      // Handle analysis response
      if (results['get-analysis']?.error) {
        const result = results['get-analysis'];
        const errorMessage = result.error?.message || t('pattern-analysis.error.api-error-content', { apiUrl: '', errorMessage: '' });
        const statusCode = result.status || 'Unknown';
        const apiUrl = `${ApiConfigurations.baseUrl}assessments/${jobId}/analysis/${target}`;

        addFlashbarMessage({
          type: 'error',
          header: t('pattern-analysis.error.api-error-header', { statusCode }),
          content: t('pattern-analysis.error.api-error-content', { apiUrl, errorMessage }),
          dismissible: true,
          id: `error-${Date.now()}`
        });
      } else if (results['get-analysis']?.success) {
        setAnalysisData(results['get-analysis']);
      }

      // Clear flashbar if all calls succeeded
      if (results['get-triage']?.success && results['get-collector']?.success && results['get-analysis']?.success) {
        setFlashbarItems([]);
      }

    } catch (error) {
      console.error('Error loading pattern data:', error);
      const errorDetails = error.message || 'Failed to load pattern data';

      addFlashbarMessage({
        type: 'error',
        header: t('pattern-analysis.error.unexpected-header'),
        content: t('pattern-analysis.error.unexpected-content', { errorDetails }),
        dismissible: true,
        id: `error-${Date.now()}`
      });
    } finally {
      setLoading(false);
      setInitialLoad(false);
    }
  }, [jobId, addFlashbarMessage, initialLoad, target, t]);




  //--|#######################| Initialization Section  |#######################


  //##-- Initial page load
  useEffect(() => {
    gatherPatternData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);




  //--|#######################| Utility Functions Section  |#######################


  const formatNumber = useCallback((num) => {
    if (!num && num !== 0) return '-';
    return num.toLocaleString();
  }, []);


  const formatSignalName = useCallback((signalName) => {
    if (!signalName) return '';
    // Convert snake_case to Title Case
    return signalName
      .split('_')
      .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
      .join(' ');
  }, []);




  //--|#######################| OnEvent Change Variable Section|#######################


  //--## Breadcrumb items
  const breadcrumbItems = useMemo(() => [
    { href: "/", text: t('dashboard.breadcrumb.home') },
    { href: "/dashboard", text: t('dashboard.breadcrumb.dashboard') },
    { href: `/analysis/monitor/summary/${jobId}`, text: jobId || t('pattern-analysis.breadcrumb.job') },
    { href: `/analysis/patterns/${jobId}?target=${target}`, text: t('pattern-analysis.breadcrumb.patterns', { target }) }
  ], [jobId, target, t]);


  //--## Get available engines from triage
  const availableEngines = useMemo(() => {
    if (!triageData?.selected_agents) return [];
    return triageData.selected_agents.map(agent => agent.agent_type);
  }, [triageData]);


  //--## Handle engine switch
  const handleEngineChange = useCallback((newTarget) => {
    navigate(`/analysis/patterns/${jobId}?target=${newTarget}`);
  }, [navigate, jobId]);


  //--## Filter signals by target
  const filteredSignals = useMemo(() => {
    if (!triageData?.signals || !target) return [];

    const filtered = triageData.signals.filter(signal =>
      signal.targets && signal.targets.includes(target)
    );

    // Sort by query_count descending (highest first)
    return filtered.sort((a, b) => (b.query_count || 0) - (a.query_count || 0));
  }, [triageData, target]);


  //--## Get query details by query_id
  const getQueryDetails = useCallback((queryId) => {
    if (!collectorData?.queries?.query_patterns) return null;
    return collectorData.queries.query_patterns.find(q => q.query_id === queryId);
  }, [collectorData]);


  //--## Get engine overview metrics
  const engineMetrics = useMemo(() => {
    if (!target || !analysisData) return null;

    // Calculate stats from analysis data (similar to useEngineAnalysis hook)
    const recs = analysisData.table_recommendations || [];
    const scores = recs.map(r => r.confidence_score);
    const avgConfidence = scores.length > 0 ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : 0;

    const monthlyCost = analysisData.cost_estimate?.monthly_cost_usd || 0;
    const patternsDetected = analysisData.workload_analysis?.patterns_detected?.length || 0;
    const antiPatterns = analysisData.workload_analysis?.anti_patterns_detected?.length || 0;

    // Get unique query IDs from patterns
    const queriesCovered = new Set(
      (analysisData.workload_analysis?.patterns_detected || []).flatMap(p => p.query_ids || [])
    ).size;

    return {
      avgConfidence,
      monthlyCost,
      tablesAnalyzed: recs.length,
      queriesCovered,
      patterns: patternsDetected,
      antiPatterns
    };
  }, [target, analysisData]);


  //--## Prepare donut chart data for patterns
  const patternChartData = useMemo(() => {
    if (!filteredSignals || filteredSignals.length === 0) return [];

    return filteredSignals.map((signal, index) => ({
      title: formatSignalName(signal.signal),
      value: signal.query_count || 0,
      color: `hsl(${(index * 360) / filteredSignals.length}, 70%, 50%)`
    }));
  }, [filteredSignals, formatSignalName]);


  //--## Query table columns
  const queryColumnDefinitions = useMemo(() => [
    {
      id: 'query_id',
      header: t('engine-analysis.table.col-query-id'),
      cell: item => (
        <Box fontFamily="monospace" fontSize="body-s">
          {item.query_id?.substring(0, 12)}...
        </Box>
      ),
      minWidth: 140
    },
    {
      id: 'type',
      header: t('common.labels.type'),
      cell: item => <Badge>{item.query_type || 'UNKNOWN'}</Badge>,
      minWidth: 100
    },
    {
      id: 'sql',
      header: t('engine-analysis.table.col-sql'),
      cell: item => {
        const truncatedSql = item.query_text?.substring(0, 80) || '';
        const isTruncated = item.query_text && item.query_text.length > 80;

        return (
          <Popover
            dismissButton={false}
            position="top"
            size="large"
            triggerType="custom"
            content={
              <Box fontFamily="monospace" fontSize="body-s" padding="s">
                <pre style={{
                  margin: 0,
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                  maxWidth: '600px'
                }}>
                  {item.query_text}
                </pre>
              </Box>
            }
          >
            <Box
              fontFamily="monospace"
              fontSize="body-s"
              style={{ cursor: isTruncated ? 'pointer' : 'default' }}
            >
              {truncatedSql}{isTruncated && '...'}
            </Box>
          </Popover>
        );
      },
      minWidth: 300
    },
    {
      id: 'tables',
      header: t('engine-analysis.table.col-tables'),
      cell: item => (
        <SpaceBetween direction="horizontal" size="xs">
          {(item.tables_accessed || []).map((table, idx) => (
            <Badge key={idx} color="grey">{table.split('.').pop()}</Badge>
          ))}
        </SpaceBetween>
      ),
      minWidth: 200
    },
    {
      id: 'rps',
      header: t('engine-analysis.table.col-rps'),
      cell: item => formatNumber(item.calls_per_second?.toFixed(1)),
      minWidth: 80
    }
  ], [formatNumber, t]);


  //--## Create tabs for patterns
  const patternTabs = useMemo(() => {
    const tabs = filteredSignals.map((signal, index) => ({
      id: `pattern-${index}`,
      label: `${formatSignalName(signal.signal)} (${signal.query_count || 0})`,
      content: (
        <SpaceBetween size="m">
          <Box>
            <Box variant="awsui-key-label">{t('pattern-analysis.signal.evidence')}</Box>
            <Box>{signal.evidence}</Box>
          </Box>

          <ColumnLayout columns={2} variant="text-grid">
            <Box>
              <Box variant="awsui-key-label">{t('pattern-analysis.signal.query-count')}</Box>
              <Badge color="blue">{signal.query_count || 0}</Badge>
            </Box>
            <Box>
              <Box variant="awsui-key-label">{t('pattern-analysis.signal.target-engines')}</Box>
              <SpaceBetween direction="horizontal" size="xs">
                {(signal.targets || []).map((engine, idx) => (
                  <Badge key={idx} color={ENGINE_COLORS[engine] || 'grey'}>{engine}</Badge>
                ))}
              </SpaceBetween>
            </Box>
          </ColumnLayout>

          {signal.table_ids && signal.table_ids.length > 0 && (
            <Box>
              <Box variant="awsui-key-label">{t('engine-analysis.signal.related-tables')}</Box>
              <SpaceBetween direction="horizontal" size="xs">
                {signal.table_ids.map((tableId, idx) => (
                  <Badge key={idx} color="grey">{tableId.split('.').pop()}</Badge>
                ))}
              </SpaceBetween>
            </Box>
          )}

          {signal.query_ids && signal.query_ids.length > 0 && (
            <Box>
              <Box variant="awsui-key-label" padding={{ bottom: 's' }}>{t('pattern-analysis.signal.queries')}</Box>
              <Table
                columnDefinitions={queryColumnDefinitions}
                items={signal.query_ids.map(qid => getQueryDetails(qid)).filter(Boolean)}
                variant="embedded"
                empty={
                  <Box textAlign="center" color="inherit" padding="s">
                    {t('pattern-analysis.signal.no-query-details')}
                  </Box>
                }
              />
            </Box>
          )}
        </SpaceBetween>
      )
    }));

    // Add anti-patterns tab if there are any
    const antiPatterns = analysisData?.workload_analysis?.anti_patterns_detected || [];
    if (antiPatterns.length > 0) {
      tabs.push({
        id: 'anti-patterns',
        // nosemgrep: missing-template-string-indicator
        label: `⚠️ ${t('pattern-analysis.anti-patterns.tab-label', { count: antiPatterns.length })}`,
        content: (
          <SpaceBetween size="l">
            {antiPatterns.map((antiPattern, index) => (
              <Container key={index}>
                <SpaceBetween size="m">
                  <Box>
                    <Box variant="h3" color="text-status-warning">
                      {formatSignalName(antiPattern.anti_pattern_id || antiPattern.pattern_id)}
                    </Box>
                  </Box>

                  <Box>
                    <Box variant="awsui-key-label">{t('pattern-analysis.anti-patterns.why-problematic')}</Box>
                    <Box>{antiPattern.rationale || antiPattern.evidence || t('pattern-analysis.anti-patterns.default-rationale')}</Box>
                  </Box>

                  {antiPattern.concerns && antiPattern.concerns.length > 0 && (
                    <Box>
                      <Box variant="awsui-key-label">{t('pattern-analysis.anti-patterns.concerns')}</Box>
                      <SpaceBetween size="xs">
                        {antiPattern.concerns.map((concern, idx) => (
                          <Box key={idx} color="text-status-warning">• {concern}</Box>
                        ))}
                      </SpaceBetween>
                    </Box>
                  )}

                  {antiPattern.recommendations && antiPattern.recommendations.length > 0 && (
                    <Box>
                      <Box variant="awsui-key-label">{t('pattern-analysis.anti-patterns.alternative-approaches')}</Box>
                      <SpaceBetween size="xs">
                        {antiPattern.recommendations.map((rec, idx) => (
                          <Box key={idx}>• {rec}</Box>
                        ))}
                      </SpaceBetween>
                    </Box>
                  )}

                  <ColumnLayout columns={2} variant="text-grid">
                    <Box>
                      <Box variant="awsui-key-label">{t('pattern-analysis.anti-patterns.severity')}</Box>
                      <Badge color="red">{antiPattern.severity || 'HIGH'}</Badge>
                    </Box>
                    <Box>
                      <Box variant="awsui-key-label">{t('pattern-analysis.anti-patterns.affected-queries')}</Box>
                      <Badge color="grey">{antiPattern.query_ids?.length || 0}</Badge>
                    </Box>
                  </ColumnLayout>

                  {antiPattern.table_ids && antiPattern.table_ids.length > 0 && (
                    <Box>
                      <Box variant="awsui-key-label">{t('engine-analysis.signal.related-tables')}</Box>
                      <SpaceBetween direction="horizontal" size="xs">
                        {antiPattern.table_ids.map((tableId, idx) => (
                          <Badge key={idx} color="grey">{tableId.split('.').pop()}</Badge>
                        ))}
                      </SpaceBetween>
                    </Box>
                  )}

                  {antiPattern.query_ids && antiPattern.query_ids.length > 0 && (
                    <Box>
                      <Box variant="awsui-key-label" padding={{ bottom: 's' }}>{t('pattern-analysis.anti-patterns.affected-queries')}</Box>
                      <Table
                        columnDefinitions={queryColumnDefinitions}
                        items={antiPattern.query_ids.map(qid => getQueryDetails(qid)).filter(Boolean)}
                        variant="embedded"
                        empty={
                          <Box textAlign="center" color="inherit" padding="s">
                            {t('pattern-analysis.signal.no-query-details')}
                          </Box>
                        }
                      />
                    </Box>
                  )}
                </SpaceBetween>
              </Container>
            ))}
          </SpaceBetween>
        )
      });
    }

    return tabs;
  }, [filteredSignals, queryColumnDefinitions, getQueryDetails, formatSignalName, analysisData, t]);




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
            activeHref={`/analysis/patterns/${jobId}`}
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
                  onDismiss: () => handleFlashbarDismiss(item.id)
                }))}
              />
            )}

            <Header
              variant="h1"
              description={t('pattern-analysis.header.description')}
            >
              {t('pattern-analysis.header.title', { target })}
            </Header>

            {/* Engine Selector */}
            {availableEngines.length > 1 && (
              <Container>
                <SegmentedControl
                  selectedId={target}
                  onChange={({ detail }) => handleEngineChange(detail.selectedId)}
                  label={t('pattern-analysis.engine-selector.label')}
                  options={availableEngines.map(engine => ({
                    id: engine,
                    text: engine,
                    iconName: engine === target ? 'check' : undefined
                  }))}
                />
              </Container>
            )}

            {/* Engine Overview */}
            {engineMetrics && (
              <Container
                header={
                  <Header
                    variant="h2"
                    description={t('pattern-analysis.overview.description')}
                  >
                    {t('pattern-analysis.overview.title')}
                  </Header>
                }
              >
                <ColumnLayout columns={2} variant="default">
                  <Box>
                    <PieChart
                      data={patternChartData}
                      variant="donut"
                      size="medium"
                      innerMetricValue={filteredSignals.reduce((sum, s) => sum + (s.query_count || 0), 0).toString()}
                      innerMetricDescription={t('pattern-analysis.chart.total-queries')}
                      hideFilter
                      hideLegend={false}
                      legendTitle={t('pattern-analysis.chart.legend-title')}
                      empty={
                        <Box textAlign="center" color="inherit">
                          <Box variant="p" color="inherit">{t('common.labels.no-data')}</Box>
                        </Box>
                      }
                      noMatch={
                        <Box textAlign="center" color="inherit">
                          <Box variant="p" color="inherit">{t('pattern-analysis.chart.no-matching-data')}</Box>
                        </Box>
                      }
                    />
                  </Box>

                  <ColumnLayout columns={2} variant="text-grid">
                    <Box>
                      <Box variant="awsui-key-label">{t('engine-analysis.summary.avg-confidence')}</Box>
                      <Box fontSize="heading-xl" fontWeight="bold">{engineMetrics.avgConfidence}%</Box>
                    </Box>

                    <Box>
                      <Box variant="awsui-key-label">{t('engine-analysis.summary.monthly-cost')}</Box>
                      <Box fontSize="heading-xl" fontWeight="bold">${engineMetrics.monthlyCost.toFixed(2)}</Box>
                    </Box>

                    <Box>
                      <Box variant="awsui-key-label">{t('engine-analysis.summary.tables-analyzed')}</Box>
                      <Box fontSize="heading-xl" fontWeight="bold">{engineMetrics.tablesAnalyzed}</Box>
                    </Box>

                    <Box>
                      <Box variant="awsui-key-label">{t('engine-analysis.summary.queries-covered')}</Box>
                      <Box fontSize="heading-xl" fontWeight="bold">{engineMetrics.queriesCovered}</Box>
                    </Box>

                    <Box>
                      <Box variant="awsui-key-label">{t('engine-analysis.summary.patterns')}</Box>
                      <Box fontSize="heading-xl" fontWeight="bold">{engineMetrics.patterns}</Box>
                    </Box>

                    <Box>
                      <Box variant="awsui-key-label">{t('engine-analysis.summary.anti-patterns')}</Box>
                      <Box fontSize="heading-xl" fontWeight="bold" color={engineMetrics.antiPatterns > 0 ? 'text-status-warning' : 'inherit'}>
                        {engineMetrics.antiPatterns > 0 && '⚠️ '}{engineMetrics.antiPatterns}
                      </Box>
                    </Box>
                  </ColumnLayout>
                </ColumnLayout>
              </Container>
            )}

            {/* Signals/Patterns */}
            <Container
              header={
                <Header variant="h2" description={t('pattern-analysis.detected-patterns.description', { count: filteredSignals.length, target })}>
                  {t('pattern-analysis.detected-patterns.title')}
                </Header>
              }
            >
              {loading ? (
                <Box textAlign="center" padding="l">{t('pattern-analysis.detected-patterns.loading')}</Box>
              ) : filteredSignals.length === 0 ? (
                <Box textAlign="center" padding="l" color="text-body-secondary">
                  {t('pattern-analysis.detected-patterns.empty', { target })}
                </Box>
              ) : (
                <Tabs tabs={patternTabs} />
              )}
            </Container>

          </SpaceBetween>
        }
        contentType="default"
        toolsHide
      />
    </>
  );
});

export default PatternAnalysisPage;
