// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

//##-- React Events
import { useState, useEffect, memo, useCallback, useRef, useMemo } from 'react';
import { useTranslation } from 'react-i18next';

//##-- AWS UI Objects
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import useLayoutAriaLabels from '../hooks/useLayoutAriaLabels';
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import CopyToClipboard from "@cloudscape-design/components/copy-to-clipboard";
import Box from "@cloudscape-design/components/box";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import ButtonDropdown from "@cloudscape-design/components/button-dropdown";
import SideNavigation from "@cloudscape-design/components/side-navigation";
import Flashbar from "@cloudscape-design/components/flashbar";
import Alert from "@cloudscape-design/components/alert";
import Modal from "@cloudscape-design/components/modal";



//##-- Custom Objects
import { SideNavigationConfigurations, createLabelFunction, EmptyState } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";
import { failureFlash } from "../utils/requestFailure";
import CustomTable from "../components/CustomTable";
import { getStatusType as sharedGetStatusType, getStatusText as sharedGetStatusText, isAlreadyFinishedMessage } from "../utils/jobStatus";
import { buildDashboardActionItems } from "../utils/dashboardActions";
import { assessmentHref, copyJobIdLabel, databaseLabel } from "../utils/assessmentRows";
import { formatDuration, UNKNOWN_DURATION } from "../utils/duration";
import { formatCount, formatPercent, formatHours, NO_FIGURE } from "../utils/format";

// The id of the assessments list, the target of /dashboard#assessments (#2393).
const ASSESSMENTS_SECTION_ID = 'assessments';





//--|#######################|
//--|#######################| Main Page  |#######################
//--|#######################|



const ComponentPage = memo(() => {

  const { t } = useTranslation();
  const layoutAriaLabels = useLayoutAriaLabels();

  //--|#######################| Static Variables Section  |#######################

  //--## Table variables
  const [visibleTableColumns] = useState(['databaseName', 'jobId', 'status', 'created', 'duration']);

  const tableColumnsList = useMemo(() => {
        return  [
            {id: 'databaseName', header: t('dashboard.table.col-database'), cell: item => (<Link href={assessmentHref(item.jobId, item.status)} variant="primary">{databaseLabel(item.databaseName, item.jobId)}</Link>), ariaLabel: createLabelFunction(t('dashboard.table.col-database')), sortingField: 'databaseName'},
            {id: 'jobId', header: t('dashboard.table.col-job-id'), cell: item => (<SpaceBetween direction="horizontal" size="xxs" alignItems="center"><Box fontFamily="monospace" fontSize="body-s">{item.jobId}</Box><CopyToClipboard variant="icon" textToCopy={item.jobId} copyButtonAriaLabel={copyJobIdLabel(t, item.databaseName, item.jobId)} copySuccessText={t('common.success.copied')} copyErrorText={t('common.errors.copy-failed')} /></SpaceBetween>), ariaLabel: createLabelFunction(t('dashboard.table.col-job-id')), sortingField: 'jobId'},
            {id: 'status', header: t('dashboard.table.col-status'), cell: item => (<StatusIndicator type={getStatusType(item.status)}>{item.statusText}</StatusIndicator>), ariaLabel: createLabelFunction(t('dashboard.table.col-status')), sortingField: 'status'},
            {id: 'created', header: t('dashboard.table.col-created'), cell: item => item.created, ariaLabel: createLabelFunction(t('dashboard.table.col-created')), sortingField: 'created'},
            {id: 'duration', header: t('dashboard.table.col-duration'), cell: item => item.duration, ariaLabel: createLabelFunction(t('dashboard.table.col-duration')), sortingField: 'duration'},
        ];

    }, [t]);


    //--|#######################| State Managemet Section  |#######################

  //-- Refresh Interval
  const [refreshInterval] = useState(30000); // Default 30 seconds

  //-- Variable for Navigation Panel
  const [navigationOpen, setNavigationOpen] = useState(false);

  //--######## Data State

  const [assessments, setAssessments] = useState([]);
  // null until the stats call succeeds: an unloaded figure is never shown as 0 (#2306).
  const [dashboardStats, setDashboardStats] = useState(null);
  // True while the last load of the assessments or the stats failed (#2306).
  const [loadFailed, setLoadFailed] = useState(false);
  const loadFailedRef = useRef(false);
  // Which call failed: a failed call's old figures are hidden, not left beside the alert.
  const [assessmentsFailed, setAssessmentsFailed] = useState(false);
  const [statsFailed, setStatsFailed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [initialLoad, setInitialLoad] = useState(true);

  //-- Table state
  const [selectedItems, setSelectedItems] = useState([]);

  //-- Flashbar state for error messages
  const [flashbarItems, setFlashbarItems] = useState([]);

  //-- Cancel modal state
  const [showCancelModal, setShowCancelModal] = useState(false);
  const [cancellingJob, setCancellingJob] = useState(false);
  const [jobToCancel, setJobToCancel] = useState(null);

  //-- Delete-artifacts modal state (#529)
  const [showDeleteArtifactsModal, setShowDeleteArtifactsModal] = useState(false);
  const [deletingArtifacts, setDeletingArtifacts] = useState(false);
  const [jobToDeleteArtifacts, setJobToDeleteArtifacts] = useState(null);

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
          if (initialLoad || loadFailedRef.current) {
            setLoading(true);
          }
          let failed = false;

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
            if (!results['get-assessments']?.success) {
              // One alert with a retry covers this (#2306); the request URL never reaches the reader.
              failed = true;
              // The old rows would sit beside "did not load": show none, and no count.
              setAssessments([]);
              setSelectedItems([]);
              setAssessmentsFailed(true);
            } else {
              setAssessmentsFailed(false);
              const newAssessments = results['get-assessments'].assessments || [];
              setAssessments(newAssessments);

              // Restore selection after data refresh
              if (selectedJobIds.length > 0) {
                // Transform new assessments to table items format to find matches
                // eslint-disable-next-line no-use-before-define
                const newTableItems = newAssessments.map(assessment => ({
                  jobId: assessment.job_id,
                  databaseName: assessment.database_name,
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
            if (!results['get-dashboard-stats']?.success) {
              failed = true;
              setStatsFailed(true);
            } else {
              setStatsFailed(false);
              const stats = results['get-dashboard-stats'];
              setDashboardStats({
                total_assessments: stats.total_assessments ?? null,
                active_jobs: stats.active_jobs ?? null,
                success_rate_percent: stats.success_rate_percent ?? null,
                average_duration_hours: stats.average_duration_hours ?? null,
                completed_today: stats.completed_today ?? null,
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
            failed = true;
          } finally {
            loadFailedRef.current = failed;
            setLoadFailed(failed);
            setLoading(false);
            setInitialLoad(false);
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
      window.open(`/analysis/results/${jobId}`, '_blank');
    } else if (detail.id === 'cancel') {
      if (selectedItems.length === 0) {
        addFlashbarMessage({
          type: 'warning',
          header: t('dashboard.cancel-modal.no-selection-header'),
          content: t('dashboard.cancel-modal.no-selection-content'),
          dismissible: true,
          id: `warning-${Date.now()}`
        });
        return;
      }

      // For now, handle single cancellation (first selected item)
      setJobToCancel(selectedItems[0]);
      setShowCancelModal(true);
    } else if (detail.id === 'delete-artifacts') {
      if (selectedItems.length === 0) {
        addFlashbarMessage({
          type: 'warning',
          header: t('dashboard.delete-artifacts-modal.no-selection-header'),
          content: t('dashboard.delete-artifacts-modal.no-selection-content'),
          dismissible: true,
          id: `warning-${Date.now()}`
        });
        return;
      }

      // For now, handle a single deletion (first selected item)
      setJobToDeleteArtifacts(selectedItems[0]);
      setShowDeleteArtifactsModal(true);
    }
  }, [selectedItems, addFlashbarMessage, t]);


  //##-- Cancel modal handlers
  const handleCancelModalDismiss = useCallback(() => {
    setShowCancelModal(false);
    setJobToCancel(null);
  }, []);


  const handleCancelJobConfirm = useCallback(async () => {
    if (!jobToCancel) return;

    setCancellingJob(true);

    try {
      const apiManager = new ApiManager();

      const apiCalls = [
        {
          id: 'cancel-assessment',
          path: `assessments/${jobToCancel.jobId}`,
          method: 'DELETE',
          params: {}
        }
      ];

      const results = await apiManager.execute(apiCalls);
      console.log('Cancel job result:', results);

      if (results['cancel-assessment']?.error) {
        addFlashbarMessage(failureFlash(t, results['cancel-assessment'], { header: t('dashboard.cancel-modal.failed-header'), context: 'cancel-assessment', variant: 'action' }));
      } else if (results['cancel-assessment']?.success) {
        // The backend returns 200 for both an actual cancellation and a
        // no-op on a job that was already finished -- the response's
        // `status` field can't tell those apart (and ApiManager overwrites
        // it with the HTTP status code anyway), so this reads the message
        // instead (#526 round 1 review).
        if (isAlreadyFinishedMessage(results['cancel-assessment']?.message)) {
          addFlashbarMessage({
            type: 'info',
            header: t('dashboard.cancel-modal.already-finished-header'),
            content: t('dashboard.cancel-modal.already-finished-content', { jobId: jobToCancel.jobId }),
            dismissible: true,
            id: `info-${Date.now()}`
          });
        } else {
          addFlashbarMessage({
            type: 'success',
            header: t('dashboard.cancel-modal.success-header'),
            content: t('dashboard.cancel-modal.success-content', { jobId: jobToCancel.jobId }),
            dismissible: true,
            id: `success-${Date.now()}`
          });
        }

        // Clear selection and refresh data
        setSelectedItems([]);
        gatherDashboardData();
      }

    } catch (error) {
      console.error('Error cancelling analysis:', error);
      addFlashbarMessage(failureFlash(t, error, { header: t('dashboard.cancel-modal.failed-header'), context: 'cancel-assessment', variant: 'action' }));
    } finally {
      setCancellingJob(false);
      setShowCancelModal(false);
      setJobToCancel(null);
    }
  }, [jobToCancel, addFlashbarMessage, gatherDashboardData, t]);


  //##-- Delete-artifacts modal handlers (#529)
  const handleDeleteArtifactsModalDismiss = useCallback(() => {
    setShowDeleteArtifactsModal(false);
    setJobToDeleteArtifacts(null);
  }, []);


  const handleDeleteArtifactsConfirm = useCallback(async () => {
    if (!jobToDeleteArtifacts) return;

    setDeletingArtifacts(true);

    try {
      const apiManager = new ApiManager();
      const apiPath = `assessments/${jobToDeleteArtifacts.jobId}/artifacts?database_name=${encodeURIComponent(jobToDeleteArtifacts.databaseName || '')}`;

      const apiCalls = [
        {
          id: 'delete-artifacts',
          path: apiPath,
          method: 'DELETE',
          params: {}
        }
      ];

      const results = await apiManager.execute(apiCalls);
      console.log('Delete artifacts result:', results);

      if (results['delete-artifacts']?.error) {
        addFlashbarMessage(failureFlash(t, results['delete-artifacts'], { header: t('dashboard.delete-artifacts-modal.failed-header'), context: 'delete-artifacts', variant: 'action' }));
      } else if (results['delete-artifacts']?.success) {
        const removedCount = (results['delete-artifacts'].removed || []).length;

        addFlashbarMessage({
          type: 'success',
          header: t('dashboard.delete-artifacts-modal.success-header'),
          content: t('dashboard.delete-artifacts-modal.success-content', {
            count: removedCount,
            jobId: jobToDeleteArtifacts.jobId,
          }),
          dismissible: true,
          id: `success-${Date.now()}`
        });

        // Clear selection and refresh data -- the job's folder is gone, so
        // it disappears from the list on the next load.
        setSelectedItems([]);
        gatherDashboardData();
      }

    } catch (error) {
      console.error('Error deleting artifacts:', error);
      addFlashbarMessage(failureFlash(t, error, { header: t('dashboard.delete-artifacts-modal.failed-header'), context: 'delete-artifacts', variant: 'action' }));
    } finally {
      setDeletingArtifacts(false);
      setShowDeleteArtifactsModal(false);
      setJobToDeleteArtifacts(null);
    }
  }, [jobToDeleteArtifacts, addFlashbarMessage, gatherDashboardData, t]);




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


  const getStatusType = useCallback((status) => sharedGetStatusType(status), []);




  const getStatusText = useCallback((status) => sharedGetStatusText(t, status), [t]);




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



  //--##  Status figures: one container, plain values (#2306). A figure that has
  //-- not loaded is a loading indicator or a dash, never 0; the values are
  //-- formatted by utils/format.js and computed by the API (ADR-032).
  const statValue = useCallback((format) => {
    if (dashboardStats === null || statsFailed) {
      return loading
        ? <StatusIndicator type="loading">{t("common.labels.loading")}</StatusIndicator>
        : NO_FIGURE;
    }
    return <Box variant="awsui-value-large">{format(dashboardStats)}</Box>;
  }, [dashboardStats, statsFailed, loading, t]);

  const statusItems = useMemo(() => [
        { label: t("dashboard.status.total-analyses"), value: statValue(s => formatCount(s.total_assessments)) },
        { label: t("dashboard.status.active-jobs"), value: statValue(s => formatCount(s.active_jobs)) },
        { label: t("dashboard.status.success-rate"), value: statValue(s => formatPercent(s.success_rate_percent)) },
        { label: t("dashboard.status.average-time"), value: statValue(s => (s.average_duration_hours === null ? UNKNOWN_DURATION : formatHours(s.average_duration_hours))) },
        { label: t("dashboard.activity.last-analysis"), value: statValue(s => (s.last_analysis_at ? formatTimeAgo(s.last_analysis_at) : NO_FIGURE)) },
        { label: t("dashboard.activity.completed-today"), value: statValue(s => formatCount(s.completed_today)) },
  ], [t, statValue, formatTimeAgo]);



  //-- Table items (transformed data)
  const tableItems = useMemo(() =>
                    assessments.map(assessment => ({
                                                    jobId: assessment.job_id,
                                                    databaseName: assessment.database_name,
                                                    sourceDatabase: `${assessment.source_database_type} - ${assessment.database_name}`,
                                                    status: assessment.status,
                                                    statusText: getStatusText(assessment.status),
                                                    created: formatDateTime(assessment.created_at),
                                                    duration: formatDuration(assessment.duration_seconds),
                    }))
  , [assessments, getStatusText, formatDateTime]);


  //-- Action items for ButtonDropdown
  const actionItems = useMemo(
    () => buildDashboardActionItems(t, selectedItems),
    [t, selectedItems],
  );


  //-- /dashboard#assessments (the not-found page's link, #2393) scrolls to the list
  //-- and moves focus there once the page has rendered.
  const assessmentsRef = useRef(null);
  useEffect(() => {
    if (window.location.hash === `#${ASSESSMENTS_SECTION_ID}` && assessmentsRef.current) {
      assessmentsRef.current.scrollIntoView?.();
      assessmentsRef.current.focus({ preventScroll: true });
    }
  }, [loading]);

  //-- Memoized table component to prevent unnecessary re-renders
  const tableComponent = useMemo(() => (
    <CustomTable
      columnsTable={tableColumnsList}
      visibleContent={visibleTableColumns}
      dataset={tableItems}
      loading={loading}
      showCount={!loading && !assessmentsFailed}
      emptyState={assessmentsFailed
        ? <EmptyState title={t("dashboard.table.unavailable")} />
        : (
          <EmptyState
            title={t("dashboard.table.empty-title")}
            subtitle={t("dashboard.table.empty-body")}
            action={<Button variant="primary" href="/analysis/create">{t("dashboard.header.new-analysis")}</Button>}
          />
        )}
      title={t("dashboard.table.title")}
      description={t("dashboard.table.description")}
      onSelectionItem={handleSelectionChange}
      selectedListItems={selectedItems}
      extendedTableProperties={{
        variant: "embedded",
        loading: loading,
        wrapLines: true,
        ariaLabels: {
          tableLabel: t("dashboard.table.title"),
          selectionGroupLabel: t("dashboard.table.selection-group"),
          itemSelectionLabel: (_, item) => t("dashboard.table.select-item", { name: databaseLabel(item.databaseName, item.jobId) }),
        },
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
  ), [t, tableColumnsList, visibleTableColumns, tableItems, loading, assessmentsFailed, handleSelectionChange, selectedItems, actionItems, handleActionsClick]);





  //--|#######################| Render Section  |#######################

  return (
    <>
      <AppHeader />
      <AppLayoutToolbar
        ariaLabels={layoutAriaLabels}
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



                      {loadFailed && (
                        // Cloudscape's Alert has no live region; this wrapper makes screen readers announce it.
                        <div role="alert">
                          <Alert
                            type="error"
                            header={t("dashboard.error.title")}
                            action={<Button loading={loading} onClick={gatherDashboardData}>{t("common.actions.retry")}</Button>}
                          >
                            {t("dashboard.error.body")}
                          </Alert>
                        </div>
                      )}

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
                      <KeyValuePairs columns={3} items={statusItems} />
                    </Container>


                    <div id={ASSESSMENTS_SECTION_ID} ref={assessmentsRef} tabIndex={-1} className="dashboard-assessments">
                      <Container>
                        {tableComponent}
                      </Container>
                    </div>


          </SpaceBetween>
        }
        contentType="dashboard"
        toolsHide
      />

      <Modal
        visible={showCancelModal}
        onDismiss={handleCancelModalDismiss}
        header={t("dashboard.cancel-modal.header")}
        closeAriaLabel={t("dashboard.cancel-modal.close-aria")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={handleCancelModalDismiss}>
                {t("dashboard.cancel-modal.keep-job")}
              </Button>
              <Button variant="primary" onClick={handleCancelJobConfirm} loading={cancellingJob}>
                {t("dashboard.cancel-modal.confirm")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            {t("dashboard.cancel-modal.confirm-question")}
          </Box>
          {jobToCancel && (
            <Box color="text-status-warning">
              <SpaceBetween size="xs">
                <Box variant="strong">{t("dashboard.cancel-modal.job-id")}{jobToCancel.jobId}</Box>
                <Box variant="strong">{t("dashboard.cancel-modal.database")}{jobToCancel.sourceDatabase}</Box>
                <Box variant="strong">{t("dashboard.cancel-modal.status")}{jobToCancel.statusText}</Box>
                <Box>{t("dashboard.cancel-modal.warning")}</Box>
              </SpaceBetween>
            </Box>
          )}
        </SpaceBetween>
      </Modal>

      <Modal
        visible={showDeleteArtifactsModal}
        onDismiss={handleDeleteArtifactsModalDismiss}
        header={t("dashboard.delete-artifacts-modal.header")}
        closeAriaLabel={t("dashboard.delete-artifacts-modal.close-aria")}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={handleDeleteArtifactsModalDismiss}>
                {t("dashboard.delete-artifacts-modal.keep-job")}
              </Button>
              <Button variant="primary" onClick={handleDeleteArtifactsConfirm} loading={deletingArtifacts}>
                {t("dashboard.delete-artifacts-modal.confirm")}
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <Box>
            {t("dashboard.delete-artifacts-modal.confirm-question")}
          </Box>
          {jobToDeleteArtifacts && (
            <Box color="text-status-warning">
              <SpaceBetween size="xs">
                <Box variant="strong">{t("dashboard.delete-artifacts-modal.job-id")}{jobToDeleteArtifacts.jobId}</Box>
                <Box variant="strong">{t("dashboard.delete-artifacts-modal.folder")}{jobToDeleteArtifacts.databaseName}/{jobToDeleteArtifacts.jobId}/</Box>
                <Box variant="strong">{t("dashboard.delete-artifacts-modal.status")}{jobToDeleteArtifacts.statusText}</Box>
                <Box>{t("dashboard.delete-artifacts-modal.warning")}</Box>
              </SpaceBetween>
            </Box>
          )}
        </SpaceBetween>
      </Modal>
    </>
  );
});

export default ComponentPage;
