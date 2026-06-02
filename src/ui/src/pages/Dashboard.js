//##-- React Events
import { useState, useEffect, memo, useCallback, useRef, useMemo } from 'react';
import { useTranslation } from 'react-i18next';

//##-- AWS UI Objects
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import ButtonDropdown from "@cloudscape-design/components/button-dropdown";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Flashbar from "@cloudscape-design/components/flashbar";
import Modal from "@cloudscape-design/components/modal";



//##-- Custom Objects
import { SideNavigationConfigurations, createLabelFunction, ApiConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";
import CustomTable from "../components/CustomTable";





//--|#######################|
//--|#######################| Main Page  |#######################
//--|#######################|



const ComponentPage = memo(() => {

  const { t } = useTranslation();

  //--|#######################| Static Variables Section  |#######################

  //--## Table variables
  const [visibleTableColumns] = useState(['jobId', 'status', 'created', 'duration']);

  const tableColumnsList = useMemo(() => {
        return  [
            {id: 'jobId', header: t('dashboard.table.col-job-id'), cell: item => (<Link href={`/analysis/monitor/summary/${item.jobId}`} variant="primary">{item.jobId}</Link>), ariaLabel: createLabelFunction('jobId'), sortingField: 'jobId'},
            {id: 'status', header: t('dashboard.table.col-status'), cell: item => (<StatusIndicator type={getStatusType(item.status)}>{item.statusText}</StatusIndicator>), ariaLabel: createLabelFunction('Status'), sortingField: 'status'},
            {id: 'created', header: t('dashboard.table.col-created'), cell: item => item.created, ariaLabel: createLabelFunction('created'), sortingField: 'created'},
            {id: 'duration', header: t('dashboard.table.col-duration'), cell: item => item.duration, ariaLabel: createLabelFunction('duration'), sortingField: 'duration'},
        ];

    }, [t]);


    //--|#######################| State Managemet Section  |#######################

  //-- Refresh Interval
  const [refreshInterval] = useState(30000); // Default 30 seconds

  //-- Variable for Navigation Panel
  const [navigationOpen, setNavigationOpen] = useState(false);

  //--######## Data State

  const [assessments, setAssessments] = useState([]);
  const [dashboardStats, setDashboardStats] = useState({
    total_assessments: 0,
    active_jobs: 0,
    success_rate_percent: 0,
    average_duration_hours: 0,
    completed_today: 0,
    last_analysis_at: null
  });
  const [loading, setLoading] = useState(true);
  const [initialLoad, setInitialLoad] = useState(true);

  //-- Table state
  const [selectedItems, setSelectedItems] = useState([]);

  //-- Flashbar state for error messages
  const [flashbarItems, setFlashbarItems] = useState([]);

  //-- Delete modal state
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [deletingJob, setDeletingJob] = useState(false);
  const [jobToDelete, setJobToDelete] = useState(null);

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

  //##-- Gather dashboard data
  const gatherDashboardData = useCallback(async () => {
          // Only show loading spinner on initial load, not on background refreshes
          if (initialLoad) {
            setLoading(true);
          }

          // Save currently selected job IDs before refresh
          const selectedJobIds = selectedItems.map(item => item.jobId);

          try {
            const apiManager = new ApiManager();

            const apiCalls = [
              {
                id: 'get-assessments',
                path: 'assessments',
                method: 'GET',
                params: { limit: 25, offset: 0 }
              },
              {
                id: 'get-dashboard-stats',
                path: 'dashboard/stats',
                method: 'GET',
                params: {}
              }
            ];

            const results = await apiManager.execute(apiCalls);
            console.log(results);

            // Handle assessments response
            if (results['get-assessments']?.error) {
              const result = results['get-assessments'];
              const errorMessage = result.error?.message || 'Failed to load assessments';
              const statusCode = result.status || 'Unknown';
              const apiUrl = `${ApiConfigurations.baseUrl}assessments`;

              addFlashbarMessage({
                type: 'error',
                header: `API Error (Status: ${statusCode})`,
                content: `Failed to fetch '${apiUrl}': ${errorMessage}`,
                dismissible: true,
                id: `error-${Date.now()}`
              });
            } else if (results['get-assessments']?.success) {
              const newAssessments = results['get-assessments'].assessments || [];
              setAssessments(newAssessments);

              // Restore selection after data refresh
              if (selectedJobIds.length > 0) {
                // Transform new assessments to table items format to find matches
                // eslint-disable-next-line no-use-before-define
                const newTableItems = newAssessments.map(assessment => ({
                  jobId: assessment.job_id,
                  sourceDatabase: `${assessment.source_database_type} - ${assessment.database_name}`,
                  status: assessment.status,
                  statusText: getStatusText(assessment.status),
                  created: formatDateTime(assessment.created_at),
                  duration: formatDuration(assessment.duration_seconds),
                }));

                // Find and restore previously selected items
                const restoredSelection = newTableItems.filter(item =>
                  selectedJobIds.includes(item.jobId)
                );

                if (restoredSelection.length > 0) {
                  setSelectedItems(restoredSelection);
                }
              }
            }

            // Handle dashboard stats response
            if (results['get-dashboard-stats']?.error) {
              const result = results['get-dashboard-stats'];
              const errorMessage = result.error?.message || 'Failed to load dashboard stats';
              const statusCode = result.status || 'Unknown';
              const apiUrl = `${ApiConfigurations.baseUrl}dashboard/stats`;

              addFlashbarMessage({
                type: 'error',
                header: `API Error (Status: ${statusCode})`,
                content: `Failed to fetch '${apiUrl}': ${errorMessage}`,
                dismissible: true,
                id: `error-${Date.now()}`
              });
            } else if (results['get-dashboard-stats']?.success) {
              const stats = results['get-dashboard-stats'];
              setDashboardStats({
                total_assessments: stats.total_assessments || 0,
                active_jobs: stats.active_jobs || 0,
                success_rate_percent: stats.success_rate_percent || 0,
                average_duration_hours: stats.average_duration_hours || 0,
                completed_today: stats.completed_today || 0,
                last_analysis_at: stats.last_analysis_at || null
              });
            }

            // Clear flashbar on full success
            if (results['get-assessments']?.success && results['get-dashboard-stats']?.success) {
              setFlashbarItems([]);
            }

            lastUpdateTimestamp.current = new Date().toISOString();

          } catch (error) {
            console.error('Error loading dashboard data:', error);
            const errorDetails = error.message || 'Failed to load dashboard data';

            addFlashbarMessage({
              type: 'error',
              header: 'Unexpected Error',
              content: `An unexpected error occurred: ${errorDetails}. Please check your network connection and try again.`,
              dismissible: true,
              id: `error-${Date.now()}`
            });
          } finally {
            if (initialLoad) {
              setLoading(false);
              setInitialLoad(false);
            }
    }
  }, [addFlashbarMessage, selectedItems, initialLoad]);


  const handleSelectionChange = useCallback((item) => {
        setSelectedItems(item);
  }, []);


  //##-- Actions dropdown handler
  const handleActionsClick = useCallback(({ detail }) => {
    if (detail.id === 'view-results') {
      if (selectedItems.length === 0) {
        addFlashbarMessage({
          type: 'warning',
          header: 'No Selection',
          content: 'Please select an analysis to view results',
          dismissible: true,
          id: `warning-${Date.now()}`
        });
        return;
      }

      // Open results page in new tab
      const jobId = selectedItems[0].jobId;
      window.open(`/analysis/results-v2/${jobId}`, '_blank');
    } else if (detail.id === 'view-analysis') {
      if (selectedItems.length === 0) {
        addFlashbarMessage({
          type: 'warning',
          header: 'No Selection',
          content: 'Please select an analysis to view engine analysis',
          dismissible: true,
          id: `warning-${Date.now()}`
        });
        return;
      }

      const jobId = selectedItems[0].jobId;
      window.open(`/analysis/engine-analysis/${jobId}`, '_blank');
    } else if (detail.id === 'delete') {
      if (selectedItems.length === 0) {
        addFlashbarMessage({
          type: 'warning',
          header: 'No Selection',
          content: 'Please select at least one analysis to delete',
          dismissible: true,
          id: `warning-${Date.now()}`
        });
        return;
      }

      // For now, handle single deletion (first selected item)
      setJobToDelete(selectedItems[0]);
      setShowDeleteModal(true);
    } else if (detail.id === 'stop') {
      addFlashbarMessage({
        type: 'info',
        header: 'Stop Analysis',
        content: 'Stop analysis functionality coming soon',
        dismissible: true,
        id: `info-${Date.now()}`
      });
    }
  }, [selectedItems, addFlashbarMessage]);


  //##-- Delete modal handlers
  const handleDeleteModalDismiss = useCallback(() => {
    setShowDeleteModal(false);
    setJobToDelete(null);
  }, []);


  const handleDeleteConfirm = useCallback(async () => {
    if (!jobToDelete) return;

    setDeletingJob(true);

    try {
      const apiManager = new ApiManager();

      const apiCalls = [
        {
          id: 'delete-assessment',
          path: `assessments/${jobToDelete.jobId}`,
          method: 'DELETE',
          params: {}
        }
      ];

      const results = await apiManager.execute(apiCalls);
      console.log('Delete job result:', results);

      if (results['delete-assessment']?.error) {
        const result = results['delete-assessment'];
        const errorMessage = result.error?.message || 'Failed to delete analysis';
        const statusCode = result.status || 'Unknown';
        const apiUrl = `${ApiConfigurations.baseUrl}assessments/${jobToDelete.jobId}`;

        addFlashbarMessage({
          type: 'error',
          header: `API Error (Status: ${statusCode})`,
          content: `Failed to delete analysis at '${apiUrl}': ${errorMessage}`,
          dismissible: true,
          id: `error-${Date.now()}`
        });
      } else if (results['delete-assessment']?.success) {
        addFlashbarMessage({
          type: 'success',
          header: 'Analysis Deleted',
          content: `Analysis ${jobToDelete.jobId} has been deleted successfully`,
          dismissible: true,
          id: `success-${Date.now()}`
        });

        // Clear selection and refresh data
        setSelectedItems([]);
        gatherDashboardData();
      }

    } catch (error) {
      console.error('Error deleting analysis:', error);
      const errorDetails = error.message || 'Failed to delete analysis';

      addFlashbarMessage({
        type: 'error',
        header: 'Unexpected Error',
        content: `An unexpected error occurred: ${errorDetails}. Please check your network connection and try again.`,
        dismissible: true,
        id: `error-${Date.now()}`
      });
    } finally {
      setDeletingJob(false);
      setShowDeleteModal(false);
      setJobToDelete(null);
    }
  }, [jobToDelete, addFlashbarMessage, gatherDashboardData]);




  //--|#######################| Initialization Section  |#######################


  //##-- Initial page load
  useEffect(() => {
    gatherDashboardData();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);


  //##-- Auto-refresh interval
  useEffect(() => {
    if (!refreshInterval) return;

    const intervalId = setInterval(() => {
      gatherDashboardData();
    }, refreshInterval);

    return () => clearInterval(intervalId);
  }, [refreshInterval, gatherDashboardData]);




  //--|#######################| Utility Functions Section  |#######################


  const getStatusType = useCallback((status) => {
          const statusMap = {
            COMPLETED: "success",
            SUCCEEDED: "success",
            RUNNING: "in-progress",
            FAILED: "error",
            PENDING: "pending",
          };
          return statusMap[status] || "info";
  }, []);




  const getStatusText = useCallback((status) => {
          const textMap = {
            COMPLETED: "Completed",
            SUCCEEDED: "Succeeded",
            RUNNING: "Running",
            FAILED: "Failed",
            PENDING: "Pending",
          };
          return textMap[status] || status;
  }, []);




  const formatDuration = useCallback((seconds) => {
          if (!seconds) return '-';
          const hours = Math.floor(seconds / 3600);
          const minutes = Math.floor((seconds % 3600) / 60);
          return `${hours}h ${minutes}m`;
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


  const formatDateTime = useCallback((dateString) => {
          if (!dateString) return 'N/A';
          const date = new Date(dateString);
          return date.toLocaleString('en-US', {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
          });
  }, []);






  //--|#######################| OnEvent Change Variable Section|#######################



  //--##  Recent activity items configuration
  const recentActivityItems = useMemo(() => [
        { label: t("dashboard.activity.last-analysis"), value: dashboardStats.last_analysis_at ? formatTimeAgo(dashboardStats.last_analysis_at) : 'N/A' },
        { label: t("dashboard.activity.active-jobs"), value: String(dashboardStats.active_jobs) },
        { label: t("dashboard.activity.completed-today"), value: String(dashboardStats.completed_today) },
  ], [t, dashboardStats, formatTimeAgo]);



  //-- Table items (transformed data)
  const tableItems = useMemo(() =>
                    assessments.map(assessment => ({
                                                    jobId: assessment.job_id,
                                                    sourceDatabase: `${assessment.source_database_type} - ${assessment.database_name}`,
                                                    status: assessment.status,
                                                    statusText: getStatusText(assessment.status),
                                                    created: formatDateTime(assessment.created_at),
                                                    duration: formatDuration(assessment.duration_seconds),
                    }))
  , [assessments, getStatusText, formatDateTime, formatDuration]);


  //-- Action items for ButtonDropdown
  const actionItems = useMemo(() => {
    return [
      {
        text: t("dashboard.actions.view-results"),
        id: "view-results"
      },
      {
        text: t("dashboard.actions.view-analysis"),
        id: "view-analysis"
      },
      {
        text: t("dashboard.actions.stop-analysis"),
        id: "stop"
      },
      {
        text: t("dashboard.actions.delete-analysis"),
        id: "delete"
      }
    ];
  }, [t]);


  //-- Memoized table component to prevent unnecessary re-renders
  const tableComponent = useMemo(() => (
    <CustomTable
      columnsTable={tableColumnsList}
      visibleContent={visibleTableColumns}
      dataset={tableItems}
      loading={loading}
      title={t("dashboard.table.title")}
      description={t("dashboard.table.description")}
      onSelectionItem={handleSelectionChange}
      selectedListItems={selectedItems}
      extendedTableProperties={{
        variant: "embedded",
        loading: loading,
      }}
      tableActions={
        <ButtonDropdown
          items={actionItems}
          variant="primary"
          onItemClick={handleActionsClick}
          disabled={selectedItems.length === 0}
        >
          {t("dashboard.actions.label")}
        </ButtonDropdown>
      }
    />
  ), [t, tableColumnsList, visibleTableColumns, tableItems, loading, handleSelectionChange, selectedItems, actionItems, handleActionsClick]);





  //--|#######################| Render Section  |#######################

  return (
    <>
      <AppHeader />
      <AppLayoutToolbar
        disableContentPaddings={false}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        breadcrumbs={<BreadcrumbGroup items={[
                                              {
                                                href: "#db-mod-dash",
                                                text: t("dashboard.breadcrumb.home"),
                                              },
                                              {
                                                href: "#db-mod-dash",
                                                text: t("dashboard.breadcrumb.dashboard"),
                                              },
                                            ]}
                      />
        }
        navigation={
          <SideNavigation
            activeHref="/dashboard"
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
                                    <Button
                                      iconName="add-plus"
                                      href="/analysis/create"
                                      variant="primary"
                                    >
                                      {t("dashboard.header.new-analysis")}
                                    </Button>
                                  }
                        description={t("dashboard.header.description")}
                        variant="h1"
                      >
                          {t("dashboard.header.title")}
                      </Header>



                    <Container
                      header={
                        <Header
                          description={t("dashboard.status.description")}
                          variant="h2"
                        >
                          {t("dashboard.status.title")}
                        </Header>
                      }
                     
                    >
                      <KeyValuePairs columns={4} items={[
                        { label: t("dashboard.status.total-analyses"), type: "pair", value: (<Link fontSize="display-l" variant="awsui-value-large">{dashboardStats.total_assessments}</Link>)},
                        { label: t("dashboard.status.active-jobs"), type: "pair", value: (<Link fontSize="display-l" variant="awsui-value-large">{dashboardStats.active_jobs}</Link>) },
                        { label: t("dashboard.status.success-rate"), type: "pair", value: (<Box fontSize="display-l" fontWeight="light">{dashboardStats.success_rate_percent.toFixed(1)}%</Box>) },
                        { label: t("dashboard.status.average-time"), type: "pair", value: (<Box fontSize="display-l" fontWeight="light">{dashboardStats.average_duration_hours.toFixed(1)}h</Box>) },
                      ]} />
                    </Container>


                    <Container
                        fitHeight
                        header={<Header variant="h2">{t("dashboard.activity.title")}</Header>}
                       
                    >
                          <KeyValuePairs columns={3} items={recentActivityItems} />
                    </Container>

                    <Container>
                      {tableComponent}
                    </Container>


          </SpaceBetween>
        }
        contentType="dashboard"
        toolsHide
      />

      <Modal
        visible={showDeleteModal}
        onDismiss={handleDeleteModalDismiss}
        header={t("dashboard.delete-modal.header")}
        closeAriaLabel="Close modal"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={handleDeleteModalDismiss}>
                {t("common.actions.cancel")}
              </Button>
              <Button variant="primary" onClick={handleDeleteConfirm} loading={deletingJob}>
                {t("common.actions.delete")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            {t("dashboard.delete-modal.confirm-question")}
          </Box>
          {jobToDelete && (
            <Box color="text-status-warning">
              <SpaceBetween size="xs">
                <Box variant="strong">{t("dashboard.delete-modal.job-id")}{jobToDelete.jobId}</Box>
                <Box variant="strong">{t("dashboard.delete-modal.database")}{jobToDelete.sourceDatabase}</Box>
                <Box variant="strong">{t("dashboard.delete-modal.status")}{jobToDelete.statusText}</Box>
                <Box>{t("dashboard.delete-modal.warning")}</Box>
              </SpaceBetween>
            </Box>
          )}
        </SpaceBetween>
      </Modal>
    </>
  );
});

export default ComponentPage;
