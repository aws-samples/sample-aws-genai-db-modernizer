//##-- React Events
import { useState, useEffect, memo, useCallback, useRef, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useParams, useNavigate } from 'react-router-dom';

//##-- AWS UI Objects
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import ProgressBar from "@cloudscape-design/components/progress-bar";
import Box from "@cloudscape-design/components/box";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Flashbar from "@cloudscape-design/components/flashbar";
import Table from "@cloudscape-design/components/table";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Steps from "@cloudscape-design/components/steps";
import Select from "@cloudscape-design/components/select";
import Modal from "@cloudscape-design/components/modal";
import CodeEditor from "@cloudscape-design/components/code-editor";
import Popover from "@cloudscape-design/components/popover";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";



//##-- Custom Objects
import { SideNavigationConfigurations, ApiConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";





//--|#######################|
//--|#######################| Main Page  |#######################
//--|#######################|



const JobMonitoringPage = memo(() => {

  const { t } = useTranslation();

  //##-- Get jobId from URL parameters
  const { jobId } = useParams();

  //##-- Navigation hook
  const navigate = useNavigate();


  //--|#######################| State Management Section  |#######################

  //-- Refresh Interval
  const [refreshInterval] = useState(20000); // Refresh every 20 seconds

  //-- Variable for Navigation Panel
  const [navigationOpen, setNavigationOpen] = useState(false);

  //--######## Data State

  const [jobData, setJobData] = useState(null);
  const [agentData, setAgentData] = useState(null);
  const [executionHistory, setExecutionHistory] = useState(null);
  const [expandedHistoryItems, setExpandedHistoryItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [initialLoad, setInitialLoad] = useState(true);

  //-- Flashbar state for error messages
  const [flashbarItems, setFlashbarItems] = useState([]);

  //-- Agent filter for logs
  const [selectedArtifact, setSelectedArtifact] = useState(null);
  const [artifactContent, setArtifactContent] = useState('Loading artifact...');
  const [artifactLoading, setArtifactLoading] = useState(false);

  //-- Cancel job modal state
  const [showCancelModal, setShowCancelModal] = useState(false);
  const [cancellingJob, setCancellingJob] = useState(false);

  //-- Expanded artifact modal state
  const [showExpandedArtifact, setShowExpandedArtifact] = useState(false);

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
        // Auto-expand Map states on first load
        if (!executionHistory) {
          const mapStates = (results['get-execution-history'].states || [])
            .filter(s => s.type === 'Map')
            .map((s, idx) => ({ ...s, _id: `${s.name}-${s.type}-${(results['get-execution-history'].states || []).indexOf(s)}` }));
          setExpandedHistoryItems(mapStates);
        }
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


  //##-- Action handlers
  const handleRefresh = useCallback(() => {
    gatherJobInformation();
  }, [gatherJobInformation]);

  const handleDownloadLogs = useCallback(() => {
    console.log('Downloading logs for job:', jobId);
    // TODO: Implement download logs functionality
  }, [jobId]);

  const handleArtifactChange = useCallback(({ detail }) => {
    setSelectedArtifact(detail.selectedOption);
  }, []);

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


  //##-- Cancel job handlers
  const handleCancelJobClick = useCallback(() => {
    setShowCancelModal(true);
  }, []);

  const handleCancelModalDismiss = useCallback(() => {
    setShowCancelModal(false);
  }, []);

  const handleCancelJobConfirm = useCallback(async () => {
    setCancellingJob(true);

    try {
      const apiManager = new ApiManager();

      const apiCalls = [
        {
          id: 'delete-assessment',
          path: `assessments/${jobId}`,
          method: 'DELETE',
          params: {}
        }
      ];

      const results = await apiManager.execute(apiCalls);
      console.log('Cancel job result:', results);

      if (results['delete-assessment']?.error) {
        const result = results['delete-assessment'];
        const errorMessage = result.error?.message || 'Failed to cancel job';
        const statusCode = result.status || 'Unknown';
        const apiUrl = `${ApiConfigurations.baseUrl}assessments/${jobId}`;

        addFlashbarMessage({
          type: 'error',
          header: `API Error (Status: ${statusCode})`,
          content: `Failed to cancel job at '${apiUrl}': ${errorMessage}`,
          dismissible: true,
          id: `error-${Date.now()}`
        });
      } else if (results['delete-assessment']?.success) {
        addFlashbarMessage({
          type: 'success',
          header: 'Job Cancelled',
          content: `Job ${jobId} has been cancelled successfully. Redirecting to dashboard...`,
          dismissible: true,
          id: `success-${Date.now()}`
        });

        // Navigate to dashboard after 2 seconds
        setTimeout(() => {
          navigate('/dashboard');
        }, 2000);
      }

    } catch (error) {
      console.error('Error cancelling job:', error);
      const errorDetails = error.message || 'Failed to cancel job';

      addFlashbarMessage({
        type: 'error',
        header: 'Unexpected Error',
        content: `An unexpected error occurred: ${errorDetails}. Please check your network connection and try again.`,
        dismissible: true,
        id: `error-${Date.now()}`
      });
    } finally {
      setCancellingJob(false);
      setShowCancelModal(false);
    }
  }, [jobId, addFlashbarMessage, navigate]);




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


  const getStatusType = useCallback((status) => {
          const statusMap = {
            COMPLETED: "success",
            SUCCEEDED: "success",
            completed: "success",
            success: "success",
            RUNNING: "in-progress",
            running: "in-progress",
            "in-progress": "in-progress",
            FAILED: "error",
            failed: "error",
            error: "error",
            PENDING: "pending",
            pending: "pending",
          };
          return statusMap[status] || "info";
  }, []);




  const getStatusText = useCallback((status) => {
          if (!status) return 'Unknown';
          const textMap = {
            COMPLETED: "Completed",
            SUCCEEDED: "Succeeded",
            completed: "Completed",
            RUNNING: "Running",
            running: "Running",
            FAILED: "Failed",
            failed: "Failed",
            PENDING: "Pending",
            pending: "Pending",
          };
          return textMap[status] || status.charAt(0).toUpperCase() + status.slice(1).toLowerCase();
  }, []);




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
    { href: "/", text: t("job-monitoring.breadcrumb.home") },
    { href: "/dashboard", text: t("job-monitoring.breadcrumb.dashboard") },
    { href: `/analysis/monitor/${jobId}`, text: jobId || t("job-monitoring.breadcrumb.job-monitor") }
  ], [jobId, t]);


  //--## Stage table columns
  const stageColumnDefinitions = useMemo(() => [
    {
      id: 'agent',
      header: t("job-monitoring.agents.col-agent-name"),
      cell: item => item.agent_name || 'Unknown',
      minWidth: 180
    },
    {
      id: 'status',
      header: t("job-monitoring.agents.col-status"),
      cell: item => (
        <StatusIndicator type={getStatusType(item.status)}>
          {getStatusText(item.status)}
        </StatusIndicator>
      ),
      minWidth: 140
    },
    {
      id: 'duration',
      header: t("job-monitoring.agents.col-duration"),
      cell: item => formatDuration(item.duration_seconds),
      minWidth: 100
    },
    {
      id: 'details',
      header: t("job-monitoring.agents.col-details"),
      cell: item => {
        const short = item.details || '-';
        const summary = item.artifact_summary;
        if (!summary || typeof summary !== 'object' || Object.keys(summary).length === 0) {
          return short;
        }
        const pairs = Object.entries(summary)
          .filter(([, v]) => v !== null && v !== undefined)
          .map(([key, value]) => ({
            label: key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
            value: Array.isArray(value) ? value.join(', ') || 'None' : String(value),
          }));
        return (
          <Popover
            dismissButton={false}
            position="top"
            size="large"
            triggerType="text"
            content={<KeyValuePairs columns={2} items={pairs} />}
          >
            {short}
          </Popover>
        );
      },
      minWidth: 200
    }
  ], [getStatusType, getStatusText, formatDuration, t]);


  //--## Stage table items
  const stageItems = useMemo(() => {
    const agents = agentData?.agents || [];

    // Remove duplicates based on agent_name and started_at
    const uniqueAgents = agents.reduce((acc, agent) => {
      const key = `${agent.agent_name}-${agent.started_at}`;
      if (!acc.has(key)) {
        acc.set(key, agent);
      }
      return acc;
    }, new Map());

    // Convert back to array and add unique ID
    return Array.from(uniqueAgents.values()).map((agent, index) => ({
      ...agent,
      uniqueId: `${agent.agent_name}-${index}`
    }));
  }, [agentData]);


  //--## Pipeline steps for visual representation
  const pipelineSteps = useMemo(() => {
    if (!agentData?.agents) return [];

    return agentData.agents.map(agent => {
      let details = getStatusText(agent.status);

      if (agent.status === 'completed' && agent.duration_seconds) {
        details = `Completed in ${formatDuration(agent.duration_seconds)}`;
        if (agent.details) {
          details += ` • ${agent.details}`;
        }
      } else if (agent.status === 'running' && agent.duration_seconds) {
        details = `Running for ${formatDuration(agent.duration_seconds)}`;
        if (agent.details) {
          details += ` • ${agent.details}`;
        }
      } else if (agent.status === 'pending') {
        details = agent.details || 'Waiting to start';
      } else if (agent.details) {
        details = agent.details;
      }

      return {
        header: agent.agent_name || 'Unknown Agent',
        status: getStatusType(agent.status),
        details: details
      };
    });
  }, [agentData, getStatusType, getStatusText, formatDuration]);


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


  //--## Agent status table component
  const agentStatusTable = useMemo(() => (
    <Table
      columnDefinitions={stageColumnDefinitions}
      contentDensity="comfortable"
      items={stageItems}
      loading={loading}
      trackBy="uniqueId"
      variant="borderless"
      empty={
        <Box textAlign="center" color="inherit">
          <Box padding={{ bottom: "s" }} variant="p" color="inherit">
            {t("job-monitoring.agents.no-agents")}
          </Box>
        </Box>
      }

    />
  ), [stageColumnDefinitions, stageItems, loading, t]);




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
            activeHref={`/analysis/monitor/${jobId}`}
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
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button iconName="refresh" variant="normal" onClick={handleRefresh} loading={loading}>
                  </Button>
                  <Button iconName="download" variant="normal" onClick={handleDownloadLogs}>
                    {t("job-monitoring.actions.download-logs")}
                  </Button>
                  {jobData?.progress?.percent_complete !== 100 && (
                    <Button iconName="close" variant="normal" onClick={handleCancelJobClick}>
                      {t("job-monitoring.actions.cancel-job")}
                    </Button>
                  )}
                  {jobData?.progress?.percent_complete === 100 && (
                    <Button iconName="status-positive" variant="primary" href={`/analysis/results-v2/${jobId}`}>
                      {t("job-monitoring.actions.view-results")}
                    </Button>
                  )}
                </SpaceBetween>
              }
              description={t("job-monitoring.header.description")}
              variant="h1"
            >
              {t("job-monitoring.header.title", { jobId })}
            </Header>

            <Container
              header={
                <Header
                  description={t("job-monitoring.pipeline.description")}
                  variant="h2"
                >
                  {t("job-monitoring.pipeline.title")}
                </Header>
              }

            >
              <SpaceBetween size="l">
                <ProgressBar
                  additionalInfo={jobData?.progress?.estimated_remaining_seconds
                    ? t("job-monitoring.pipeline.estimated-completion", { minutes: Math.ceil(jobData.progress.estimated_remaining_seconds / 60) })
                    : (calculateProgress() === 100 ? t("job-monitoring.pipeline.analysis-complete") : '')}
                  description={jobData ? t("job-monitoring.pipeline.analyzing", { type: jobData.source_database_type }) : t("job-monitoring.pipeline.loading")}
                  label={t("job-monitoring.pipeline.progress-label")}
                  status={loading && !jobData ? 'in-progress' : (calculateProgress() === 100 ? 'success' : 'in-progress')}
                  value={calculateProgress()}
                />
                <Box
                  color="text-label"
                  padding={{
                    top: "xs",
                  }}
                >
                  <ColumnLayout columns={4} variant="text-grid">
                    <Box>
                      <Box variant="awsui-key-label">{t("job-monitoring.pipeline.col-job-id")}</Box>
                      <Box>{jobData?.job_id || jobId}</Box>
                    </Box>
                    <Box>
                      <Box variant="awsui-key-label">{t("job-monitoring.pipeline.col-source-database")}</Box>
                      <Box>{jobData ? `${jobData.source_database_type} - ${jobData.database_name}` : t("common.labels.loading")}</Box>
                    </Box>
                    <Box>
                      <Box variant="awsui-key-label">{t("job-monitoring.pipeline.col-started")}</Box>
                      <Box>{jobData ? formatTimeAgo(jobData.created_at) : 'N/A'}</Box>
                    </Box>
                    <Box>
                      <Box variant="awsui-key-label">{t("job-monitoring.pipeline.col-stages-completed")}</Box>
                      <Box>
                        {agentData?.agents
                          ? `${agentData.agents.filter(a => a.status === 'completed').length} of ${agentData.agents.length}`
                          : '0 of 0'}
                      </Box>
                    </Box>
                  </ColumnLayout>
                </Box>
              </SpaceBetween>
            </Container>

            <Container
              header={
                <Header
                  description={t("job-monitoring.agents.description")}
                  variant="h2"
                >
                  {t("job-monitoring.agents.title")}
                </Header>
              }

            >
              {agentStatusTable}
            </Container>

            <Container
              header={
                <Header
                  description={t("job-monitoring.execution-history.description")}
                  variant="h2"
                >
                  {t("job-monitoring.execution-history.title")}
                </Header>
              }

            >
              {executionHistory?.states ? (() => {
                // Assign unique IDs and flatten children for expandable rows
                let idCounter = 0;
                const childMap = {};

                const assignIds = (items) => items.map(item => {
                  const withId = { ...item, _id: `state-${idCounter++}` };
                  if (item.children && item.children.length > 0) {
                    const children = assignIds(item.children);
                    childMap[withId._id] = children;
                  }
                  return withId;
                });

                const topLevel = assignIds(executionHistory.states);

                return (
                  <Table
                    columnDefinitions={[
                      {
                        id: 'name',
                        header: t("job-monitoring.execution-history.col-name"),
                        cell: item => item.name,
                        minWidth: 180,
                      },
                      {
                        id: 'type',
                        header: t("job-monitoring.execution-history.col-type"),
                        cell: item => item.type,
                        minWidth: 100,
                      },
                      {
                        id: 'status',
                        header: t("job-monitoring.execution-history.col-status"),
                        cell: item => (
                          <StatusIndicator type={
                            item.status === 'completed' ? 'success' :
                            item.status === 'in-progress' ? 'in-progress' :
                            item.status === 'failed' ? 'error' : 'pending'
                          }>
                            {item.status === 'completed' ? t("job-monitoring.execution-history.status-succeeded") :
                             item.status === 'in-progress' ? t("job-monitoring.execution-history.status-in-progress") :
                             item.status === 'failed' ? t("job-monitoring.execution-history.status-failed") : t("job-monitoring.execution-history.status-pending")}
                          </StatusIndicator>
                        ),
                        minWidth: 120,
                      },
                      {
                        id: 'duration',
                        header: t("job-monitoring.execution-history.col-duration"),
                        cell: item => item.duration_seconds != null
                          ? formatDuration(item.duration_seconds)
                          : '-',
                        minWidth: 100,
                      },
                      {
                        id: 'started_after',
                        header: t("job-monitoring.execution-history.col-started-after"),
                        cell: item => item.started_after_seconds != null
                          ? formatDuration(item.started_after_seconds)
                          : '-',
                        minWidth: 100,
                      },
                    ]}
                    items={topLevel}
                    expandableRows={{
                      getItemChildren: item => childMap[item._id] || [],
                      isItemExpandable: item => (childMap[item._id] || []).length > 0,
                      expandedItems: expandedHistoryItems,
                      onExpandableItemToggle: ({ detail }) => {
                        const item = detail.item;
                        const isExpanded = detail.expanded;
                        setExpandedHistoryItems(prev =>
                          isExpanded
                            ? [...prev, item]
                            : prev.filter(i => i._id !== item._id)
                        );
                      },
                    }}
                    trackBy="_id"
                    variant="embedded"
                    stripedRows
                    wrapLines={false}
                  />
                );
              })() : <Box color="text-body-secondary">{t("job-monitoring.execution-history.loading")}</Box>}
            </Container>

            <Container
              header={
                <Header
                  actions={
                    <SpaceBetween direction="horizontal" size="xs">
                      <Select
                        selectedOption={selectedArtifact}
                        onChange={handleArtifactChange}
                        options={artifactOptions}
                        placeholder={t("job-monitoring.artifacts.select-placeholder")}
                        filteringType="auto"
                      />
                      <Button
                        iconName="download"
                        variant="normal"
                        onClick={handleDownloadLogs}
                      >
                        {t("job-monitoring.artifacts.download-button")}
                      </Button>
                    </SpaceBetween>
                  }
                  description={t("job-monitoring.artifacts.description")}
                  variant="h2"
                >
                  {t("job-monitoring.artifacts.title")}
                </Header>
              }

            >
              <ExpandableSection
                headerText={t("job-monitoring.artifacts.view-section")}
                variant="footer"

              >
                <SpaceBetween size="xs">
                  <SpaceBetween direction="horizontal" size="xs">
                    <Button
                      iconName="expand"
                      variant="normal"
                      onClick={handleExpandArtifact}
                    >
                      {t("job-monitoring.artifacts.expand-button")}
                    </Button>
                    <Button
                      iconName="copy"
                      variant="normal"
                      onClick={handleCopyLogs}
                    >
                      {t("job-monitoring.artifacts.copy-button")}
                    </Button>
                  </SpaceBetween>
                  {aceLoading ? (
                    <Box textAlign="center" padding="l">
                      <StatusIndicator type="loading">{t("job-monitoring.artifacts.loading-editor")}</StatusIndicator>
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
                        loadingState: t("job-monitoring.artifacts.code-editor.loading"),
                        errorState: t("job-monitoring.artifacts.code-editor.error"),
                        errorStateRecovery: t("job-monitoring.artifacts.code-editor.error-recovery")
                      }}
                      loading={artifactLoading}
                      readOnly
                    />
                  )}
                </SpaceBetween>
              </ExpandableSection>
            </Container>

          </SpaceBetween>
        }
        contentType="default"
        toolsHide
      />

      <Modal
        visible={showCancelModal}
        onDismiss={handleCancelModalDismiss}
        header={t("job-monitoring.cancel-modal.header")}
        closeAriaLabel={t("job-monitoring.cancel-modal.close-aria")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={handleCancelModalDismiss}>
                {t("job-monitoring.cancel-modal.keep-running")}
              </Button>
              <Button variant="primary" onClick={handleCancelJobConfirm} loading={cancellingJob}>
                {t("job-monitoring.cancel-modal.confirm")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            {t("job-monitoring.cancel-modal.confirm-question")}
          </Box>
          <Box color="text-status-warning">
            <SpaceBetween size="xs">
              <Box variant="strong">{t("job-monitoring.cancel-modal.job-id", { jobId })}</Box>
              <Box>{t("job-monitoring.cancel-modal.warning")}</Box>
            </SpaceBetween>
          </Box>
        </SpaceBetween>
      </Modal>

      <Modal
        visible={showExpandedArtifact}
        onDismiss={handleCloseExpandedArtifact}
        header={t("job-monitoring.artifacts.modal-header", { label: selectedArtifact?.label || 'Unknown' })}
        closeAriaLabel={t("job-monitoring.cancel-modal.close-aria")}
        size="max"
      >
        {aceLoading ? (
          <Box textAlign="center" padding="xxl">
            <StatusIndicator type="loading">{t("job-monitoring.artifacts.loading-editor")}</StatusIndicator>
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
              loadingState: t("job-monitoring.artifacts.code-editor.loading"),
              errorState: t("job-monitoring.artifacts.code-editor.error"),
              errorStateRecovery: t("job-monitoring.artifacts.code-editor.error-recovery")
            }}
            loading={artifactLoading}
            readOnly
          />
        )}
      </Modal>
    </>
  );
});

export default JobMonitoringPage;
