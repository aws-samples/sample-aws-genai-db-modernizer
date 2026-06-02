//##-- React Events
import { useState, useCallback, memo } from 'react';
import { useTranslation } from 'react-i18next';

//##-- AWS UI Objects
import AppLayoutToolbar from "@cloudscape-design/components/app-layout";
import BreadcrumbGroup from "@cloudscape-design/components/breadcrumb-group";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Header from "@cloudscape-design/components/header";
import Button from "@cloudscape-design/components/button";
import Container from "@cloudscape-design/components/container";
import FormField from "@cloudscape-design/components/form-field";
import Input from "@cloudscape-design/components/input";
import Select from "@cloudscape-design/components/select";
import Textarea from "@cloudscape-design/components/textarea";

import SideNavigation from "@cloudscape-design/components/side-navigation";
import Flashbar from "@cloudscape-design/components/flashbar";

//##-- Custom Objects
import { SideNavigationConfigurations, ApiConfigurations } from "../config/GlobalConfigurations";
import AppHeader from "../components/AppHeader";
import ApiManager from "../classes/ApiManager";

const DebugPage = memo(() => {

  const { t } = useTranslation();

  //--|#######################| State Management Section  |#######################

  //-- Variable for Navigation Panel
  const [navigationOpen, setNavigationOpen] = useState(false);

  //-- API Call Parameters
  const [method, setMethod] = useState({ label: 'GET', value: 'GET' });
  const [path, setPath] = useState('assessments');
  const [parameters, setParameters] = useState('{\n  "limit": 25,\n  "offset": 0\n}');

  //-- Response State
  const [response, setResponse] = useState('');
  const [loading, setLoading] = useState(false);

  //-- Flashbar state
  const [flashbarItems, setFlashbarItems] = useState([]);



  //--|#######################| Handle Section  |#######################

  //##-- Flashbar message handler
  const addFlashbarMessage = useCallback((message) => {
    setFlashbarItems(prevItems => [...prevItems, message]);
  }, []);

  const handleFlashbarDismiss = useCallback((itemId) => {
    setFlashbarItems(prevItems => prevItems.filter(item => item.id !== itemId));
  }, []);


  //##-- Execute API Call
  const handleExecuteApiCall = useCallback(async () => {
    setLoading(true);
    setFlashbarItems([]); // Clear previous messages

    try {
      // Parse parameters JSON
      let params = {};
      if (parameters.trim()) {
        try {
          params = JSON.parse(parameters);
        } catch (parseError) {
          addFlashbarMessage({
            type: 'error',
            header: 'Invalid JSON',
            content: `Failed to parse parameters: ${parseError.message}`,
            dismissible: true,
            id: `error-${Date.now()}`
          });
          setLoading(false);
          return;
        }
      }

      // Build API call
      const apiCalls = [
        {
          id: 'debug-call',
          path: path,
          method: method.value,
          params: params
        }
      ];

      // Execute API call
      const apiManager = new ApiManager();
      const results = await apiManager.execute(apiCalls);

      // Format response
      const formattedResponse = JSON.stringify(results['debug-call'], null, 2); // nosemgrep: no-stringify-keys - display only, not a React key prop
      setResponse(formattedResponse);

      // Check for errors
      if (results['debug-call']?.error) {
        const result = results['debug-call'];
        const errorMessage = result.error?.message || 'API call failed';
        const statusCode = result.status || 'Unknown';
        const apiUrl = `${ApiConfigurations.baseUrl}${path}`;

        addFlashbarMessage({
          type: 'error',
          header: `API Error (Status: ${statusCode})`,
          content: `Failed to fetch '${apiUrl}': ${errorMessage}`,
          dismissible: true,
          id: `error-${Date.now()}`
        });
      } else if (results['debug-call']?.success) {
        addFlashbarMessage({
          type: 'success',
          header: 'Success',
          content: `API call completed successfully (Status: ${results['debug-call'].status || 200})`,
          dismissible: true,
          id: `success-${Date.now()}`
        });
      }

    } catch (error) {
      console.error('Error executing API call:', error);
      const errorDetails = error.message || 'Failed to execute API call';

      setResponse(JSON.stringify({
        error: errorDetails,
        stack: error.stack
      }, null, 2));

      addFlashbarMessage({
        type: 'error',
        header: 'Unexpected Error',
        content: `An unexpected error occurred: ${errorDetails}`,
        dismissible: true,
        id: `error-${Date.now()}`
      });
    } finally {
      setLoading(false);
    }
  }, [method, path, parameters, addFlashbarMessage]);


  //##-- Clear response
  const handleClearResponse = useCallback(() => {
    setResponse('');
    setFlashbarItems([]);
  }, []);



  //--|#######################| Variables & Configuration Section  |#######################

  const methodOptions = [
    { label: 'GET', value: 'GET' },
    { label: 'POST', value: 'POST' },
    { label: 'PUT', value: 'PUT' },
    { label: 'DELETE', value: 'DELETE' },
    { label: 'PATCH', value: 'PATCH' }
  ];



  //--|#######################| Render Section  |#######################

  return (
    <>
      <AppHeader />
      <AppLayoutToolbar
        disableContentPaddings={false}
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        breadcrumbs={
          <BreadcrumbGroup
            items={[
              { href: "/", text: t('debug.breadcrumb.home') },
              { href: "/debug", text: t('debug.breadcrumb.api-debug') }
            ]}
          />
        }
        navigation={
          <SideNavigation
            activeHref="/debug"
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
              description={t('debug.header.description')}
            >
              {t('debug.header.title')}
            </Header>

            <Container
              header={
                <Header variant="h2">
                  {t('debug.request.title')}
                </Header>
              }
            >
              <SpaceBetween size="m">

                <FormField
                  label={t('debug.request.method-label')}
                  description={t('debug.request.method-description')}
                >
                  <Select
                    selectedOption={method}
                    onChange={({ detail }) => setMethod(detail.selectedOption)}
                    options={methodOptions}
                    placeholder={t('debug.request.method-placeholder')}
                  />
                </FormField>

                <FormField
                  label={t('debug.request.path-label')}
                  description={`${t('debug.config.base-url-label')} ${ApiConfigurations.baseUrl}`}
                >
                  <Input
                    value={path}
                    onChange={({ detail }) => setPath(detail.value)}
                    placeholder="e.g., assessments or assessments/123"
                  />
                </FormField>

                <FormField
                  label={t('debug.request.params-label')}
                  description={t('debug.request.params-description')}
                  constraintText={t('debug.request.params-constraint')}
                >
                  <Textarea
                    value={parameters}
                    onChange={({ detail }) => setParameters(detail.value)}
                    placeholder='{\n  "limit": 25,\n  "offset": 0\n}'
                    rows={8}
                  />
                </FormField>

                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    variant="primary"
                    onClick={handleExecuteApiCall}
                    loading={loading}
                    disabled={!path}
                  >
                    {t('debug.actions.execute')}
                  </Button>
                  <Button
                    variant="normal"
                    onClick={handleClearResponse}
                    disabled={!response}
                  >
                    {t('debug.actions.clear-response')}
                  </Button>
                </SpaceBetween>

              </SpaceBetween>
            </Container>

            <Container
              header={
                <Header
                  variant="h2"
                  description={t('debug.response.description')}
                >
                  {t('debug.response.title')}
                </Header>
              }
            >
              <Textarea
                value={response || '// Response will appear here after executing the API call'}
                readOnly
                rows={20}
                disabled={loading}
                placeholder="Response will appear here after executing the API call"
              />
            </Container>

            <Container
              header={
                <Header variant="h2">
                  {t('debug.config.title')}
                </Header>
              }
            >
              <SpaceBetween size="xs">
                <div><strong>{t('debug.config.base-url-label')}</strong> {ApiConfigurations.baseUrl}</div>
                <div><strong>{t('debug.config.mode-label')}</strong> {ApiConfigurations.mode}</div>
                <div><strong>{t('debug.config.timeout-label')}</strong> {ApiConfigurations.timeout}ms</div>
              </SpaceBetween>
            </Container>

          </SpaceBetween>
        }
        contentType="default"
        toolsHide
      />
    </>
  );
});

export default DebugPage;
