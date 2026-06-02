//##-- React Events
import { useState, useEffect, memo, useCallback, useRef, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams } from 'react-router-dom';

//##-- AWS UI Objects
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
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Spinner from "@cloudscape-design/components/spinner";
import Alert from "@cloudscape-design/components/alert";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Select from "@cloudscape-design/components/select";
import CodeEditor from "@cloudscape-design/components/code-editor";
import Modal from "@cloudscape-design/components/modal";



//##-- Custom Objects
import { SideNavigationConfigurations, ApiConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";
import ProgressState02 from "../components/ProgressState-02";
import ChartSankey from "../components/ChartSankey-01";





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

const ENGINE_COLORS_HEX = {
  dynamodb: '#3184e8',
  documentdb: '#1d8102',
  elasticache: '#d13212',
  opensearch: '#2ea597',
  neptune: '#7d2105',
  keyspaces: '#8b6ccb',
  aurora: '#ec7211',
};



const JobMonitoringSummaryPage = memo(() => {

  //##-- Get jobId from URL parameters
  const { jobId } = useParams();

  const { t } = useTranslation();


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

  //-- Agent filter for artifacts
  const [selectedArtifact, setSelectedArtifact] = useState(null);
  const [artifactContent, setArtifactContent] = useState('Loading artifact...');
  const [artifactLoading, setArtifactLoading] = useState(false);

  //-- Expanded artifact modal state
  const [showExpandedArtifact, setShowExpandedArtifact] = useState(false);

  //-- Auto-dismiss complete alert
  const [completeDismissed, setCompleteDismissed] = useState(false);

  //-- Ace editor state
  const [ace, setAce] = useState(null);
  const [aceLoading, setAceLoading] = useState(true);

  //-- Use ref for last update timestamp (doesn't need re-render)
  const lastUpdateTimestamp = useRef(null);





  //--|#######################| Handle Section  |#######################


  //##-- Flashbar message handler
  const addFlashbarMessage = useCallback((message) => {
    setFlashbarItems(prevItems => [...prevItems, message]);
  }, []);

  const handleFlashbarDismiss = useCallback((itemId) => {
    setFlashbarItems(prevItems => prevItems.filter(item => item.id !== itemId));
  }, []);


  //##-- Artifact handlers
  const handleArtifactChange = useCallback(({ detail }) => {
    setSelectedArtifact(detail.selectedOption);
  }, []);

  const handleDownloadLogs = useCallback(() => {
    console.log('Downloading artifact for job:', jobId);
    // TODO: Implement download artifact functionality
  }, [jobId]);

  const handleCopyLogs = useCallback(() => {
    console.log('Copying artifact to clipboard');
    if (artifactContent) {
      navigator.clipboard.writeText(artifactContent);
    }
  }, [artifactContent]);

  const handleExpandArtifact = useCallback(() => {
    setShowExpandedArtifact(true);
  }, []);

  const handleCloseExpandedArtifact = useCallback(() => {
    setShowExpandedArtifact(false);
  }, []);




  //--|#######################| Gather Information Section  |#######################

  //##-- Fetch artifact content
  const fetchArtifactContent = useCallback(async (artifactPath) => {
    if (!artifactPath) return;

    setArtifactLoading(true);
    setArtifactContent('Loading artifact...');

    try {
      const apiManager = new ApiManager();
      const apiCalls = [
        {
          id: 'get-artifact',
          path: artifactPath,
          method: 'GET',
          params: {}
        }
      ];

      const results = await apiManager.execute(apiCalls);
      console.log('Artifact data:', results);

      if (results['get-artifact']?.error) {
        setArtifactContent('No artifacts found.');
      } else if (results['get-artifact']?.success) {
        // Format the artifact content as JSON
        const content = JSON.stringify(results['get-artifact'], null, 2); // nosemgrep: no-stringify-keys - display only, not a React key prop
        setArtifactContent(content);
      } else {
        setArtifactContent('No artifacts found.');
      }
    } catch (error) {
      console.error('Error fetching artifact:', error);
      setArtifactContent('No artifacts found.');
    } finally {
      setArtifactLoading(false);
    }
  }, []);

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
        const result = results['get-job-details'];
        const errorMessage = result.error?.message || 'Failed to load job details';
        const statusCode = result.status || 'Unknown';
        const apiUrl = `${ApiConfigurations.baseUrl}assessments/${jobId}`;

        addFlashbarMessage({
          type: 'error',
          header: `API Error (Status: ${statusCode})`,
          content: `Failed to fetch '${apiUrl}': ${errorMessage}`,
          dismissible: true,
          id: `error-${Date.now()}`
        });
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
        const result = results['get-agent-details'];
        const errorMessage = result.error?.message || 'Failed to load agent details';
        const statusCode = result.status || 'Unknown';
        const apiUrl = `${ApiConfigurations.baseUrl}assessments/${jobId}/agents`;

        addFlashbarMessage({
          type: 'error',
          header: `API Error (Status: ${statusCode})`,
          content: `Failed to fetch '${apiUrl}': ${errorMessage}`,
          dismissible: true,
          id: `error-${Date.now()}`
        });
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
      const errorDetails = error.message || 'Failed to load job details';

      addFlashbarMessage({
        type: 'error',
        header: 'Unexpected Error',
        content: `An unexpected error occurred: ${errorDetails}. Please check your network connection and try again.`,
        dismissible: true,
        id: `error-${Date.now()}`
      });
    } finally {
      setLoading(false);
      setInitialLoad(false);
    }
  }, [jobId, addFlashbarMessage, initialLoad]);


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
              topTablesBySize: [...tables].sort((a, b) => (b.size_mb || 0) - (a.size_mb || 0)).slice(0, 3),
              totalRows: tables.reduce((sum, t) => sum + (t.row_count || 0), 0),
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
            const signals = res.triage.signals || [];
            const selected = res.triage.selected_agents || [];
            const skipped = res.triage.skipped_agents || [];
            setTriageInsights({
              signalCount: signals.length,
              topSignals: signals.slice(0, 5).map(s => ({
                name: s.signal,
                queryCount: s.query_ids?.length || 0,
                primaryTarget: s.targets?.[0] || null,
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
    if (nodeId && nodeId !== 'patterns') {
      // Open pattern analysis in new tab
      window.open(`/analysis/patterns/${jobId}?target=${nodeId}`, '_blank');
    }
  }, [jobId]);




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


  //##-- Fetch artifact when selection changes
  useEffect(() => {
    if (selectedArtifact?.path) {
      fetchArtifactContent(selectedArtifact.path);
    }
  }, [selectedArtifact, fetchArtifactContent]);


  //##-- Load Ace editor
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


  //--## Pipeline stages list with customer-facing labels
  //   Collect → Analyse → Select → Design → Review
  const pipelineStepsList = useMemo(() => [
    'Collect',
    'Analyse',
    'Select',
    'Design',
    'Review'
  ], []);

  //--## Which stages require human input
  const humanStages = useMemo(() => new Set(['Select', 'Review']), []);

  //--## Map API stage names to customer-facing labels
  const stageNameMapping = useMemo(() => ({
    'RunCollector': 'Collect',
    'RunRefereeTriage': 'Analyse',
    'LoadTriageOutput': 'Analyse',
    'RunAnalysis': 'Analyse',
    'RunAssignmentResolution': 'Analyse',
    'RunRealityCheck': 'Analyse',
    'UpdateAssignmentVersionAfterRealityCheck': 'Analyse',
    'WaitForAssignmentApproval': 'Select',
    'RunSchemaDesign': 'Design',
    'RunRefereeSynthesis': 'Review',
    'LoadSynthesisOutput': 'Review'
  }), []);


  //--## Get current pipeline step from job data
  const currentPipelineStep = useMemo(() => {
    if (!jobData?.progress?.stages || jobData.progress.stages.length === 0) {
      return null;
    }
    // Get the last stage from the stages array
    const stages = jobData.progress.stages;
    const lastStage = stages[stages.length - 1];
    const apiStageName = lastStage?.name || null;

    // Map API stage name to customer-facing label
    return apiStageName ? (stageNameMapping[apiStageName] || apiStageName) : null;
  }, [jobData, stageNameMapping]);


  //--## Calculate completed stages based on pipeline stages
  const completedStagesCount = useMemo(() => {
    if (!jobData?.progress?.stages || jobData.progress.stages.length === 0) {
      return 0;
    }

    // Get all API stage names (both completed and in-progress)
    const allApiStageNames = jobData.progress.stages.map(stage => stage.name);

    // Check which customer-facing stages are completed
    const completedCustomerStages = new Set();

    // For each pipeline stage, check if it should be counted as completed
    pipelineStepsList.forEach((customerStage) => {
      // Find all API stages that map to this customer stage
      const apiStagesForThisCustomer = Object.entries(stageNameMapping)
        .filter(([_, mappedName]) => mappedName === customerStage)
        .map(([apiName, _]) => apiName);

      // Check if any of these API stages exist and are completed
      const hasCompletedApiStage = apiStagesForThisCustomer.some(apiStageName => {
        const apiStage = jobData.progress.stages.find(s => s.name === apiStageName);
        return apiStage && apiStage.status === 'completed';
      });

      // Special case: Assignment Review might not be in API if auto-approved
      // Count it as completed if Schema Design (the next stage) is completed
      if (customerStage === 'Assignment Review' && !apiStagesForThisCustomer.some(name => allApiStageNames.includes(name))) {
        const schemaDesignCompleted = jobData.progress.stages.some(
          s => s.name === 'RunSchemaDesign' && s.status === 'completed'
        );
        if (schemaDesignCompleted) {
          completedCustomerStages.add(customerStage);
        }
      } else if (hasCompletedApiStage) {
        completedCustomerStages.add(customerStage);
      }
    });

    return completedCustomerStages.size;
  }, [jobData, stageNameMapping, pipelineStepsList]);

  //--## Total stages is always the full pipeline
  const totalStagesCount = useMemo(() => {
    return pipelineStepsList.length;
  }, [pipelineStepsList]);

  //--## Check if job is complete
  //    NOTE: jobData.status is the HTTP status (200) because ApiManager spreads
  //    the response and overwrites the API's "status" field. Use progress data instead:
  //    the job is complete when the last pipeline stage (Review) is completed.
  const isJobComplete = useMemo(() => {
    if (!jobData?.progress?.stages) return false;
    const stages = jobData.progress.stages;
    const lastStage = stages[stages.length - 1];
    const lastMapped = lastStage?.name ? stageNameMapping[lastStage.name] : null;
    return lastMapped === 'Review' && lastStage?.status === 'completed';
  }, [jobData, stageNameMapping]);

  //--## Auto-dismiss complete alert after 60 seconds
  useEffect(() => {
    if (isJobComplete && !completeDismissed) {
      const timer = setTimeout(() => setCompleteDismissed(true), 60000);
      return () => clearTimeout(timer);
    }
  }, [isJobComplete, completeDismissed]);

  //--## Check if waiting for assignment approval
  const isWaitingForApproval = useMemo(() => {
    return jobData?.progress?.current_stage === 'WaitForAssignmentApproval';
  }, [jobData]);

  //--## Check if schema design is in progress (uses same mapping as progress bar)
  const isDesigning = useMemo(() => {
    return currentPipelineStep === 'Design';
  }, [currentPipelineStep]);


  //--## Build ranking from post-reality-check assignment distribution only
  const ranking = useMemo(() => {
    if (assignmentDistribution) {
      return Object.entries(assignmentDistribution)
        .sort(([, a], [, b]) => b - a)
        .map(([engine, count]) => ({
          target: engine,
          access_patterns: count,
        }));
    }
    return [];
  }, [assignmentDistribution]);


  //--## Artifact options for combobox
  const artifactOptions = useMemo(() => {
    const staticArtifacts = [
      { label: "Collector", value: "collector", path: `assessments/${jobId}/collector` },
      { label: "Referee Triage", value: "triage", path: `assessments/${jobId}/triage` },
      { label: "Schema Design", value: "schema-design", path: `assessments/${jobId}/schema-designs` }
    ];

    const dynamicArtifacts = [];
    if (jobData?.synthesis?.ranking) {
      jobData.synthesis.ranking.forEach(item => {
        dynamicArtifacts.push({
          label: `Analysis (${item.target})`,
          value: `analysis-${item.target}`,
          path: `assessments/${jobId}/analysis/${item.target}`
        });
      });
    }

    return [...staticArtifacts, ...dynamicArtifacts];
  }, [jobId, jobData]);


  //##-- Set default artifact selection
  useEffect(() => {
    if (artifactOptions.length > 0 && !selectedArtifact) {
      setSelectedArtifact(artifactOptions[0]); // Select "Collector" by default
    }
  }, [artifactOptions, selectedArtifact]);


  //--## Generate Sankey chart data from ranking
  const sankeyData = useMemo(() => {
    if (ranking.length === 0) {
      // Return demo data if no ranking available
      return {
        nodes: [
          { id: "patterns" },
          { id: "dynamodb" },
          { id: "elasticache" },
          { id: "documentdb" }
        ],
        links: [
          { source: "patterns", target: "dynamodb", value: 4 },
          { source: "patterns", target: "elasticache", value: 20 },
          { source: "patterns", target: "documentdb", value: 10 }
        ]
      };
    }

    // Build nodes: root "patterns" + all targets from ranking
    const nodes = [{ id: "patterns" }];
    const links = [];

    ranking.forEach((item) => {
      // Add target node
      nodes.push({ id: item.target });

      // Add link from patterns to target with access_patterns as value
      links.push({
        source: "patterns",
        target: item.target,
        value: item.access_patterns || 0
      });
    });

    return { nodes, links };
  }, [ranking]);




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
                  <div style={{ display: 'flex', alignItems: 'center', height: '100%' }}>
                    <a
                      href={`/analysis/assignments/${jobId}`}
                      style={{
                        display: 'inline-block',
                        padding: '8px 20px',
                        fontSize: '14px',
                        fontWeight: '700',
                        color: '#0f1b2a',
                        backgroundColor: '#ec7211',
                        borderRadius: '6px',
                        textDecoration: 'none',
                        cursor: 'pointer',
                        transition: 'background-color 0.15s ease',
                        whiteSpace: 'nowrap',
                      }}
                      onMouseEnter={e => e.target.style.backgroundColor = '#f09030'}
                      onMouseLeave={e => e.target.style.backgroundColor = '#ec7211'}
                    >
                      {t("job-summary.alert.approval-action")}
                    </a>
                  </div>
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

            {/* Report ready alert — auto-dismisses after 60s */}
            {isJobComplete && !completeDismissed && (
              <Alert
                type="success"
                header={t("job-summary.alert.complete-header")}
                dismissible
                onDismiss={() => setCompleteDismissed(true)}
              >
                {t("job-summary.alert.complete-body")}
              </Alert>
            )}

            <Header
              variant="h1"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button iconName="refresh" variant="normal" onClick={handleRefresh} loading={loading} />
                  {isJobComplete && (
                    <Button variant="primary" href={`/analysis/results-v2/${jobId}`}>
                      {t("job-summary.actions.view-results")}
                    </Button>
                  )}
                </SpaceBetween>
              }
            >
              {t("job-summary.header.title")}
            </Header>

            <Container>
              {jobData && (
                <ProgressState02
                  stepsList={pipelineStepsList}
                  stepCurrent={currentPipelineStep}
                  humanStages={humanStages}
                  jobId={jobId}
                  jobIdLabel={t("job-summary.progress.job-id-label")}
                  status={isJobComplete ? 'success' : 'in-progress'}
                  statusLine={
                    <span style={{ fontSize: '13px', color: 'var(--dbm-text-secondary)' }}>
                      {jobData.source_database_type} — {jobData.database_name} — {formatTimeAgo(jobData.created_at)}
                    </span>
                  }
                />
              )}
            </Container>

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
                        {collectorInsights.totalRows > 1000000
                          ? `${(collectorInsights.totalRows / 1000000).toFixed(1)}M`
                          : collectorInsights.totalRows > 1000
                            ? `${(collectorInsights.totalRows / 1000).toFixed(1)}K`
                            : collectorInsights.totalRows.toLocaleString()}
                      </Box>
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
                        color: sig.primaryTarget ? ENGINE_COLORS_HEX[sig.primaryTarget] : 'var(--dbm-text-primary)',
                        background: sig.primaryTarget
                          ? `${ENGINE_COLORS_HEX[sig.primaryTarget]}18`
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
                  <ChartSankey
                    width={900}
                    height={280}
                    data={sankeyData}
                    onNodeClick={handleSankeyNodeClick}
                  />
                </Container>
              </div>
            )}

            {/* Developer tools - raw artifacts (hidden for now, kept in code) */}
            {false && <Container
              header={
                <Header
                  actions={
                    <SpaceBetween direction="horizontal" size="xs">
                      <Select
                        selectedOption={selectedArtifact}
                        onChange={handleArtifactChange}
                        options={artifactOptions}
                        placeholder={t('job-monitoring.artifacts.select-placeholder')}
                        filteringType="auto"
                      />
                      <Button
                        iconName="download"
                        variant="normal"
                        onClick={handleDownloadLogs}
                      >
                        {t('job-monitoring.artifacts.download-button')}
                      </Button>
                    </SpaceBetween>
                  }
                  description={t('job-monitoring.artifacts.description')}
                  variant="h2"
                >
                  {t('job-monitoring-summary.artifacts.title')}
                </Header>
              }
             
            >
              <ExpandableSection
                headerText={t('job-monitoring.artifacts.view-section')}
                variant="footer"
                defaultExpanded={false}
               
              >
                <SpaceBetween size="xs">
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button
                      iconName="expand"
                      variant="normal"
                      onClick={handleExpandArtifact}
                    >
                      {t('job-monitoring.artifacts.expand-button')}
                    </Button>
                    <Button
                      iconName="copy"
                      variant="normal"
                      onClick={handleCopyLogs}
                    >
                      {t('job-monitoring.artifacts.copy-button')}
                    </Button>
                  </SpaceBetween>
                  {aceLoading ? (
                    <Box textAlign="center" padding="l">
                      <StatusIndicator type="loading">{t('job-monitoring.artifacts.loading-editor')}</StatusIndicator>
                    </Box>
                  ) : (
                    <CodeEditor
                      ace={ace}
                      language="json"
                      value={artifactContent}
                      preferences={{
                        wrapLines: false,
                        theme: 'cloud_editor_dark'
                      }}
                      editorContentHeight={800}
                      i18nStrings={{
                        loadingState: "Loading artifact...",
                        errorState: "Error loading artifact",
                        errorStateRecovery: "Retry"
                      }}
                      loading={artifactLoading}
                      readOnly
                    />
                  )}
                </SpaceBetween>
              </ExpandableSection>
            </Container>}

          </SpaceBetween>
        }
        contentType="default"
        toolsHide
      />

      <Modal
        visible={showExpandedArtifact}
        onDismiss={handleCloseExpandedArtifact}
        header={`Artifact: ${selectedArtifact?.label || 'Unknown'}`}
        closeAriaLabel="Close modal"
        size="max"
      >
        {aceLoading ? (
          <Box textAlign="center" padding="xxl">
            <StatusIndicator type="loading">{t('job-monitoring.artifacts.loading-editor')}</StatusIndicator>
          </Box>
        ) : (
          <CodeEditor
            ace={ace}
            language="json"
            value={artifactContent}
            preferences={{
              wrapLines: false,
              theme: 'cloud_editor_dark'
            }}
            editorContentHeight={600}
            i18nStrings={{
              loadingState: "Loading artifact...",
              errorState: "Error loading artifact",
              errorStateRecovery: "Retry"
            }}
            loading={artifactLoading}
            readOnly
          />
        )}
      </Modal>
    </>
  );
});

export default JobMonitoringSummaryPage;
