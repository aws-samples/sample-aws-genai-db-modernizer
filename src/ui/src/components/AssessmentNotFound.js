// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import AppLayoutToolbar from '@cloudscape-design/components/app-layout';
import useLayoutAriaLabels from '../hooks/useLayoutAriaLabels';
import BreadcrumbGroup from '@cloudscape-design/components/breadcrumb-group';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SideNavigation from '@cloudscape-design/components/side-navigation';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import AppHeader from './AppHeader';
import { SideNavigationConfigurations } from '../config/GlobalConfigurations';

// The page chrome these states share (also the not-found page, #2393): header, navigation and a breadcrumb that
// ends on the page the reader is on.
export function JobRouteShell({ lastCrumb, children }) {
  const { t } = useTranslation();
  const layoutAriaLabels = useLayoutAriaLabels();
  const [navigationOpen, setNavigationOpen] = useState(false);
  return (
    <>
      <div id="top-nav">
        <AppHeader />
      </div>
      <AppLayoutToolbar
        ariaLabels={layoutAriaLabels}
        headerSelector="#top-nav"
        navigationOpen={navigationOpen}
        onNavigationChange={({ detail }) => setNavigationOpen(detail.open)}
        navigation={
          <SideNavigation
            activeHref="/dashboard"
            header={SideNavigationConfigurations.header}
            items={SideNavigationConfigurations.items}
          />
        }
        toolsHide
        breadcrumbs={
          <BreadcrumbGroup
            items={[
              { href: '/dashboard', text: t('common.breadcrumb.assessments') },
              { href: '#', text: lastCrumb },
            ]}
          />
        }
        content={children}
      />
    </>
  );
}

// Shown while a job route's lookup is pending (#2307), so the page is never blank.
export function AssessmentLookupPending() {
  const { t } = useTranslation();
  return (
    <JobRouteShell lastCrumb={t('assessment-lookup.breadcrumb')}>
      <Container>
        <div role="status">
          <StatusIndicator type="loading">{t('assessment-lookup.loading')}</StatusIndicator>
        </div>
      </Container>
    </JobRouteShell>
  );
}

// The one not-found state of every job route (#2307).
export default function AssessmentNotFound({ jobId }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <JobRouteShell lastCrumb={t('assessment-not-found.breadcrumb')}>
      <Container header={<Header variant="h1">{t('assessment-not-found.title')}</Header>}>
        <SpaceBetween size="m">
          <Box>{t('assessment-not-found.body', { id: jobId })}</Box>
          <Button
            variant="primary"
            href="/dashboard"
            onFollow={(event) => {
              event.preventDefault();
              navigate('/dashboard');
            }}
          >
            {t('assessment-not-found.go-to-assessments')}
          </Button>
        </SpaceBetween>
      </Container>
    </JobRouteShell>
  );
}
