// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

//##-- React Events
import { useState, useEffect, memo, useCallback, useRef, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, useNavigate } from 'react-router-dom';

//##-- AWS UI Objects
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import useLayoutAriaLabels from '../hooks/useLayoutAriaLabels';
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import Box from "@cloudscape-design/components/box";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Flashbar from "@cloudscape-design/components/flashbar";
import Badge from "@cloudscape-design/components/badge";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Spinner from "@cloudscape-design/components/spinner";
import Alert from "@cloudscape-design/components/alert";



//##-- Custom Objects
import { SideNavigationConfigurations } from "../config/GlobalConfigurations";
import UnservableAssignmentNotice from "../components/UnservableAssignmentNotice";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";
import { failureFlash, upsertFlash } from "../utils/requestFailure";
import AssessmentSummary, { AssessmentId } from "../components/AssessmentSummary";
import PipelineSteps from "../components/PipelineSteps";
import QueryFlow from "../components/QueryFlow";
import { stepOfStage } from "../utils/pipelineSteps";
import { getCacheOverlay, addCacheOverlayNode, ownerDistribution } from "../utils/cacheLayer";
import { finalEngineCounts, placedQueryCountOf } from "../utils/reportFacts";
import { resultsEngineHref } from "../utils/resultsLinks";
import { engineHex } from "../utils/engineColors";
import { collectorRowTotals, totalRowsText } from '../utils/collectorRowTotals';





//--|#######################|
//--|#######################| Main Page  |#######################
//--|#######################|



const JobMonitoringSummaryPage = memo(() => {

  //##-- Get jobId from URL parameters
  const { jobId } = useParams();
  const navigate = useNavigate();

  const { t } = useTranslation();
  const layoutAriaLabels = useLayoutAriaLabels();


  //--|#######################| State Management Section  |#######################

  //-- Refresh Interval
  const [refreshInterval] = useState(20000); // Refresh every 20 seconds

  //-- Variable for Navigation Panel
  const [navigationOpen, setNavigationOpen] = useState(false);

  //--######## Data State

  const [jobData, setJobData] = useState(null);
  const [agentData, setAgentData] = useState(null);
  const [executionHistory, setExecutionHistory] = useState(null);
  const [loading, setLoading] = useState(true);
  const [initialLoad, setInitialLoad] = useState(true);

  //-- Flashbar state for error messages
  const [flashbarItems, setFlashbarItems] = useState([]);

  //-- Progressive insight cards
  const [collectorInsights, setCollectorInsights] = useState(null);
  const [triageInsights, setTriageInsights] = useState(null);
  const [assignmentDistribution, setAssignmentDistribution] = useState(null);
  // #1329: the queries no engine in the customer's engine scope can serve.
  const [unservableQueries, setUnservableQueries] = useState([]);
  // #330: the cache_overlay off the same assignment fetch, so the Sankey can
  // show ElastiCache as a node fed by its owner engines' flows.
  const [cacheOverlay, setCacheOverlay] = useState(null);

  //-- Use ref for last update timestamp (doesn't need re-render)
  const lastUpdateTimestamp = useRef(null);





  //--|#######################| Handle Section  |#######################


  //##-- Flashbar message handler
  const addFlashbarMessage = useCallback((message) => {
    setFlashbarItems(prevItems => upsertFlash(prevItems, message));
  }, []);

  const handleFlashbarDismiss = useCallback((itemId) => {
    setFlashbarItems(prevItems => prevItems.filter(item => item.id !== itemId));
  }, []);


  //--|#######################| Gather Information Section  |#######################

  //##-- Gather job information
  const gatherJobInformation = useCallback(async () => {
    if (!jobId) {
      addFlashbarMessage({
        type: 'error',
        header: 'Invalid Job ID',
        content: 'No job ID provided in URL',
        dismissible: true,
        id: `error-${Date.now()}`
      });
      setLoading(false);
      setInitialLoad(false);
      return;
    }

    // Only show loading spinner on initial load
    if (initialLoad) {
      setLoading(true);
    }

    try {
      const apiManager = new ApiManager();

      const apiCalls = [
        {
          id: 'get-job-details',
          path: `assessments/${jobId}`,
          method: 'GET',
          params: {}
        },
        {
          id: 'get-agent-details',
          path: `assessments/${jobId}/agents`,
          method: 'GET',
          params: {}
        },
        {
          id: 'get-execution-history',
          path: `assessments/${jobId}/execution-history`,
          method: 'GET',
          params: {}
        },
        {
          id: 'get-results',
          path: `assessments/${jobId}/results`,
          method: 'GET',
          params: {}
        }
      ];

      const results = await apiManager.execute(apiCalls);
      console.log('Job details:', results);

      // Handle job details response
      if (results['get-job-details']?.error) {
        addFlashbarMessage(failureFlash(t, results['get-job-details'], { header: t('job-summary.error.job-details'), context: 'get-job-details', id: 'job-details-failed' }));
      } else if (results['get-job-details']?.success) {
        // Merge synthesis data from results into jobData
        const jobDetails = results['get-job-details'];
        if (results['get-results']?.success && results['get-results'].synthesis) {
          jobDetails.synthesis = results['get-results'].synthesis;
        }
        setJobData(jobDetails);
      }

      // Handle agent details response
      if (results['get-agent-details']?.error) {
        addFlashbarMessage(failureFlash(t, results['get-agent-details'], { header: t('job-summary.error.agent-details'), context: 'get-agent-details', id: 'agent-details-failed' }));
      } else if (results['get-agent-details']?.success) {
        setAgentData(results['get-agent-details']);
      }

      // Handle execution history response
      if (results['get-execution-history']?.success) {
        setExecutionHistory(results['get-execution-history']);
      }

      // Clear flashbar if both calls succeeded
      if (results['get-job-details']?.success && results['get-agent-details']?.success) {
        setFlashbarItems([]);
      }

      lastUpdateTimestamp.current = new Date().toISOString();

    } catch (error) {
      console.error('Error loading job details:', error);
      addFlashbarMessage(failureFlash(t, error, { header: t('job-summary.error.job-details'), context: 'job-summary', id: 'job-summary-failed' }));
    } finally {
      setLoading(false);
      setInitialLoad(false);
    }
  }, [jobId, addFlashbarMessage, initialLoad, t]);


  //##-- Fetch progressive insights when stages complete
  useEffect(() => {
    if (!jobData?.progress?.stages) return;
    const stages = jobData.progress.stages;

    // Fetch collector insights once collector completes
    const collectorDone = stages.some(s => s.name === 'RunCollector' && s.status === 'completed');
    if (collectorDone && !collectorInsights) {
      const api = new ApiManager();
      api.execute([{ id: 'collector', path: `assessments/${jobId}/collector`, method: 'GET', params: {} }])
        .then(res => {
          if (res.collector?.success) {
            const tables = res.collector.database_schema?.tables || [];
            const queries = res.collector.queries?.query_patterns || [];
            const meta = res.collector.metadata?.source_database || {};
            setCollectorInsights({
              tableCount: tables.length,
              queryCount: queries.length,
              sizeGb: meta.database_size_gb,
              engine: meta.engine,
              version: meta.version,
              // #1636: only tables with a collected size can be the largest.
              topTablesBySize: tables.filter(t => typeof t.size_mb === 'number').sort((a, b) => b.size_mb - a.size_mb).slice(0, 3),
              // #1636: a count the source does not have (null) is left out of the sum
              // and counted, so the total never treats an unknown table as empty.
              ...collectorRowTotals(tables),
            });
          }
        });
    }

    // Fetch triage insights once triage completes
    const triageDone = stages.some(s =>
      (s.name === 'RunRefereeTriage' || s.name === 'LoadTriageOutput') && s.status === 'completed'
    );
    if (triageDone && !triageInsights) {
      const api = new ApiManager();
      api.execute([{ id: 'triage', path: `assessments/${jobId}/triage`, method: 'GET', params: {} }])
        .then(res => {
          if (res.triage?.success) {
            const triageFacts = res.triage.facts || {};
            const selected = res.triage.selected_agents || [];
            const skipped = res.triage.skipped_agents || [];
            setTriageInsights({
              signalCount: triageFacts.signal_count ?? 0,
              topSignals: (triageFacts.top_signals || []).map(s => ({
                name: s.name,
                queryCount: s.query_count ?? 0,
                primaryTarget: s.primary_target ?? null,
              })),
              selectedEngines: selected.map(a => a.agent_type),
              skippedEngines: skipped.map(a => a.agent_type),
              confidence: res.triage.confidence_score,
            });
          }
        });
    }
    // Fetch assignment distribution only after reality check completes (post-consolidation)
    const assignmentDone = stages.some(s =>
      (s.name === 'UpdateAssignmentVersionAfterRealityCheck' || s.name === 'WaitForAssignmentApproval') && s.status === 'completed'
    );
    if (assignmentDone && !assignmentDistribution && jobData?.database_name) {
      const api = new ApiManager();
      api.execute([{
        id: 'assignments',
        path: `assessments/${jobId}/assignments?database_name=${encodeURIComponent(jobData.database_name)}`,
        method: 'GET',
        params: {}
      }])
        .then(res => {
          if (res.assignments?.success) {
            const qa = res.assignments.assignment?.query_assignments || [];
            const dist = {};
            qa.forEach(q => {
              dist[q.assigned_engine] = (dist[q.assigned_engine] || 0) + 1;
            });
            setAssignmentDistribution(dist);
            setUnservableQueries(res.assignments.assignment?.unservable_queries || []);
            setCacheOverlay(getCacheOverlay(res.assignments.assignment));
          }
        });
    }
  }, [jobData, jobId, collectorInsights, triageInsights, assignmentDistribution]);


  //##-- Action handlers
  const handleRefresh = useCallback(() => {
    gatherJobInformation();
  }, [gatherJobInformation]);


  //##-- Handle Sankey node click
  const handleSankeyNodeClick = useCallback((nodeId) => {
    if (nodeId && nodeId !== 'queries') {
      // #2300: the engine's part of the results page, in this tab.
      navigate(resultsEngineHref(jobId, nodeId));
    }
  }, [jobId, navigate]);




  //--|#######################| Initialization Section  |#######################


  //##-- Initial page load
  useEffect(() => {
    gatherJobInformation();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);


  //##-- Auto-refresh interval
  useEffect(() => {
    if (!refreshInterval) return;

    const intervalId = setInterval(() => {
      gatherJobInformation();
    }, refreshInterval);

    return () => clearInterval(intervalId);
  }, [refreshInterval, gatherJobInformation]);






  //--|#######################| Utility Functions Section  |#######################


  const formatDuration = useCallback((seconds) => {
    if (!seconds && seconds !== 0) return '-';
    const rounded = Math.ceil(seconds);
    const hours = Math.floor(rounded / 3600);
    const minutes = Math.floor((rounded % 3600) / 60);
    const secs = rounded % 60;
    if (hours > 0) return `${hours}h ${minutes}m`;
    if (minutes > 0) return `${minutes}m ${secs}s`;
    return `${secs}s`;
  }, []);




  const formatTimeAgo = useCallback((dateString) => {
    if (!dateString) return 'N/A';
    const date = new Date(dateString);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMins / 60);
    const diffDays = Math.floor(diffHours / 24);

    if (diffDays > 0) return `${diffDays} day${diffDays > 1 ? 's' : ''} ago`;
    if (diffHours > 0) return `${diffHours} hour${diffHours > 1 ? 's' : ''} ago`;
    if (diffMins > 0) return `${diffMins} minute${diffMins > 1 ? 's' : ''} ago`;
    return 'Just now';
  }, []);


  const calculateProgress = useCallback(() => {
    // Prefer execution history for accurate progress (includes Map/MapIteration states)
    if (executionHistory?.states) {
      const states = executionHistory.states;
      const total = states.length;
      const completed = states.filter(s => s.status === 'completed').length;
      return total > 0 ? Math.round((completed / total) * 100) : 0;
    }
    // Fallback to agent data
    if (!agentData?.agents) return 0;
    const agents = agentData.agents;
    const completedAgents = agents.filter(agent => agent.status === 'completed').length;
    const totalAgents = agents.length;
    return totalAgents > 0 ? Math.round((completedAgents / totalAgents) * 100) : 0;
  }, [executionHistory, agentData]);






  //--|#######################| OnEvent Change Variable Section|#######################


  //--## Breadcrumb items
  const breadcrumbItems = useMemo(() => [
    { href: "/", text: t("dashboard.breadcrumb.home") },
    { href: "/dashboard", text: t("dashboard.breadcrumb.dashboard") },
    { href: `/analysis/monitor/summary/${jobId}`, text: t("job-summary.breadcrumb.assessment") }
  ], [jobId, t]);


  //--## Get current pipeline step from job data
  const currentPipelineStep = useMemo(() => {
    if (!jobData?.progress?.stages || jobData.progress.stages.length === 0) {
      return null;
    }
    // Get the last stage from the stages array
    const stages = jobData.progress.stages;
    const lastStage = stages[stages.length - 1];
    const apiStageName = lastStage?.name || null;

    // The phase the stage belongs to (null for a stage no phase knows)
    return stepOfStage(apiStageName);
  }, [jobData]);


  //--## Check if job is complete
  //    NOTE: jobData.status is the HTTP status (200) because ApiManager spreads
  //    the response and overwrites the API's "status" field. Use progress data instead:
  //    the job is complete when the last pipeline stage (Review) is completed.
  const isJobComplete = useMemo(() => {
    if (!jobData?.progress?.stages) return false;
    const stages = jobData.progress.stages;
    const lastStage = stages[stages.length - 1];
    return stepOfStage(lastStage?.name) === 'review' && lastStage?.status === 'completed';
  }, [jobData]);

  //--## Check if waiting for assignment approval
  const isWaitingForApproval = useMemo(() => {
    return jobData?.progress?.current_stage === 'WaitForAssignmentApproval';
  }, [jobData]);

  //--## Check if schema design is in progress (uses same mapping as progress bar)
  const isDesigning = useMemo(() => {
    return currentPipelineStep === 'design';
  }, [currentPipelineStep]);


  //--## Build ranking from post-reality-check assignment distribution only
  // A finished job reads the final engine counts from its report facts, as the results page
  // does (#2358, ADR-032); only a running job, with no report, uses the assignment's own.
  const finalCounts = useMemo(() => {
    const synthesis = isJobComplete ? jobData?.synthesis : null;
    if (!synthesis) return null;
    return ownerDistribution(finalEngineCounts(synthesis), !!getCacheOverlay(synthesis));
  }, [isJobComplete, jobData]);
  const flowCacheOverlay = useMemo(
    () => (finalCounts ? getCacheOverlay(jobData.synthesis) : cacheOverlay),
    [finalCounts, jobData, cacheOverlay],
  );
  const flowTotal = finalCounts ? placedQueryCountOf(jobData.synthesis) : null;
  const ranking = useMemo(() => {
    const distribution = finalCounts || assignmentDistribution;
    if (distribution) {
      return Object.entries(distribution)
        .sort(([, a], [, b]) => b - a)
        .map(([engine, count]) => ({
          target: engine,
          access_patterns: count,
        }));
    }
    return [];
  }, [finalCounts, assignmentDistribution]);


  //--## Generate Sankey chart data from ranking
  const sankeyData = useMemo(() => {
    if (ranking.length === 0) return null;

    // Build nodes: the root "queries" + all targets from ranking
    const nodes = [{ id: "queries" }];
    const links = [];

    ranking.forEach((item) => {
      // Add target node
      nodes.push({ id: item.target });

      // Add link from the queries to the target with its query count as value
      links.push({
        source: "queries",
        target: item.target,
        value: item.access_patterns || 0
      });
    });

    // #330: show the cache layer in the diagram, fed by its owner engines'
    // flows, instead of leaving ElastiCache absent from it entirely.
    return addCacheOverlayNode({ nodes, links }, flowCacheOverlay);
  }, [ranking, flowCacheOverlay]);




  //--|#######################| Render Section  |#######################

  return (
    <>
      <AppHeader />
      <AppLayoutToolbar
        ariaLabels={layoutAriaLabels}
        disableContentPaddings={false}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        breadcrumbs={<BreadcrumbGroup items={breadcrumbItems} />}
        navigation={
          <SideNavigation
            activeHref={`/analysis/monitor/summary/${jobId}`}
            header={SideNavigationConfigurations.header}
            items={SideNavigationConfigurations.items}
          />
        }
        content={
          <SpaceBetween size="s">

            <div style={{ height: '4px' }} />

            {flashbarItems.length > 0 && (
              <Flashbar
                items={flashbarItems.map(item => ({
                  ...item,
                  dismissLabel: t("common.actions.dismiss"),
                  onDismiss: () => handleFlashbarDismiss(item.id)
                }))}
              />
            )}

            {/* Assignment Approval Alert */}
            {isWaitingForApproval && (
              <Alert
                type="warning"
                header={t("job-summary.alert.approval-header")}
                action={
                  <Button variant="primary" href={`/analysis/assignments/${jobId}`}>
                    {t("job-summary.alert.approval-action")}
                  </Button>
                }
              >
                {t("job-summary.alert.approval-body")}
              </Alert>
            )}

            {/* Schema design in progress */}
            {isDesigning && !isJobComplete && (
              <Alert type="info" header={t("job-summary.alert.designing-header")}>
                {t("job-summary.alert.designing-body")}
              </Alert>
            )}

            <Header
              variant="h1"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button iconName="refresh" variant="normal" ariaLabel={t("common.actions.refresh")} onClick={handleRefresh} loading={loading} />
                  {isJobComplete && (
                    <Button variant="primary" href={`/analysis/results/${jobId}`}>
                      {t("job-summary.actions.view-results")}
                    </Button>
                  )}
                </SpaceBetween>
              }
            >
              {t("job-summary.header.title")}
            </Header>

            {/* #2317: a finished job shows what it found; a running one its phases. */}
            {jobData && isJobComplete && (
              <AssessmentSummary jobId={jobId} jobData={jobData} synthesis={jobData.synthesis} />
            )}
            {jobData && !isJobComplete && (
              <Container
                header={
                  <Header
                    variant="h2"
                    description={[jobData.source_database_label || jobData.source_database_type, jobData.database_name, formatTimeAgo(jobData.created_at)].filter(Boolean).join(' · ')}
                  >
                    {t("job-summary.progress.title")}
                  </Header>
                }
              >
                <SpaceBetween size="m">
                  <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                    <Box variant="awsui-key-label">{t("job-summary.progress.job-id-label")}</Box>
                    <AssessmentId jobId={jobId} databaseName={jobData.database_name} />
                  </SpaceBetween>
                  <PipelineSteps current={currentPipelineStep} />
                </SpaceBetween>
              </Container>
            )}

            {/* Inline animation keyframes */}
            {/* nosemgrep: missing-template-string-indicator */}
            <style>{`
              @keyframes insightFadeIn {
                from { opacity: 0; transform: translateY(12px); }
                to { opacity: 1; transform: translateY(0); }
              }
            `}</style>

            {/* Database profile — compact row, visible before Sankey arrives */}
            {collectorInsights && ranking.length === 0 && (
              <div style={{ animation: 'insightFadeIn 0.6s ease-out' }}>
                <Container>
                  <ColumnLayout columns={4} variant="text-grid">
                    <Box>
                      <Box variant="awsui-key-label">{t("job-summary.collector.tables")}</Box>
                      <Box fontSize="heading-l" fontWeight="bold">{collectorInsights.tableCount}</Box>
                    </Box>
                    <Box>
                      <Box variant="awsui-key-label">{t("job-summary.collector.query-patterns")}</Box>
                      <Box fontSize="heading-l" fontWeight="bold">{collectorInsights.queryCount}</Box>
                    </Box>
                    <Box>
                      <Box variant="awsui-key-label">{t("job-summary.collector.total-rows")}</Box>
                      <Box fontSize="heading-l" fontWeight="bold">
                        {totalRowsText(collectorInsights) ?? t("job-summary.collector.total-rows-unknown")}
                      </Box>
                      {collectorInsights.unknownRowTables > 0 && collectorInsights.knownRowTables > 0 && (
                        <Box fontSize="body-s" color="text-body-secondary">
                          <span data-figure="unknown-row-tables">
                            {t("job-summary.collector.unknown-row-tables", { count: collectorInsights.unknownRowTables })}
                          </span>
                        </Box>
                      )}
                    </Box>
                    <Box>
                      <Box variant="awsui-key-label">{t("job-summary.collector.database-size")}</Box>
                      <Box fontSize="heading-l" fontWeight="bold">
                        {collectorInsights.sizeGb
                          ? (collectorInsights.sizeGb >= 1
                              ? `${collectorInsights.sizeGb.toFixed(1)} GB`
                              : `${(collectorInsights.sizeGb * 1024).toFixed(0)} MB`)
                          : '—'}
                      </Box>
                    </Box>
                  </ColumnLayout>
                </Container>
              </div>
            )}

            {/* Workload signals — compact row, visible before Sankey arrives */}
            {triageInsights && ranking.length === 0 && (
              <div style={{ animation: 'insightFadeIn 0.6s ease-out' }}>
                <Container>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                    <span style={{ fontSize: '13px', color: 'var(--dbm-text-secondary)', whiteSpace: 'nowrap' }}>
                      {triageInsights.signalCount} workload signals:
                    </span>
                    {triageInsights.topSignals.map((sig, i) => (
                      <span key={i} style={{
                        fontSize: '12px',
                        color: sig.primaryTarget ? engineHex(sig.primaryTarget) : 'var(--dbm-text-primary)',
                        background: sig.primaryTarget
                          ? `${engineHex(sig.primaryTarget)}18`
                          : 'var(--dbm-signal-badge-muted-bg)',
                        padding: '3px 8px',
                        borderRadius: '4px',
                      }}>
                        {sig.name.replace(/_/g, ' ')}
                        <span style={{ color: 'var(--dbm-text-muted)', marginLeft: '4px' }}>{sig.queryCount}</span>
                      </span>
                    ))}
                  </div>
                </Container>
              </div>
            )}

            {/* #1329: the queries with no engine in the scope, next to the query flow */}
            <UnservableAssignmentNotice unservable={unservableQueries} t={t} />

            {/* Query Flow — appears once assignment data is available, stays */}
            {ranking.length > 0 && (
              <div style={{ animation: 'insightFadeIn 0.6s ease-out' }}>
                <Container
                  header={
                    <Header
                      description={t("job-summary.query-flow.description")}
                      variant="h2"
                    >
                      {t("job-summary.query-flow.title")}
                    </Header>
                  }

                >
                  <QueryFlow
                    total={flowTotal}
                    height={280}
                    data={sankeyData}
                    // #2300: an engine is a link once the job's results exist, as "View results".
                    onNodeClick={isJobComplete ? handleSankeyNodeClick : undefined}
                    unavailableTitle={t('job-summary.query-flow.details-after-completion')}
                  />
                  {/* #2300: the reason as text too (a title shows on hover only). */}
                  {!isJobComplete && (
                    <Box padding={{ top: 's' }} textAlign="center" color="text-body-secondary" fontSize="body-s">
                      {t('job-summary.query-flow.details-after-completion')}
                    </Box>
                  )}
                </Container>
              </div>
            )}


          </SpaceBetween>
        }
        contentType="default"
        toolsHide
      />

    </>
  );
});

export default JobMonitoringSummaryPage;
