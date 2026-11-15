// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useNavigate } from 'react-router-dom';
import AppLayoutToolbar from '@cloudscape-design/components/app-layout';
import BreadcrumbGroup from '@cloudscape-design/components/breadcrumb-group';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SideNavigation from '@cloudscape-design/components/side-navigation';
import SpaceBetween from '@cloudscape-design/components/space-between';
import AppHeader from './AppHeader';
import { SideNavigationConfigurations } from '../config/GlobalConfigurations';

// The one not-found state of every job route (#2307).
export default function AssessmentNotFound({ jobId }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [navigationOpen, setNavigationOpen] = useState(false);

  return (
    <>
      <div id="top-nav">
        <AppHeader />
      </div>
      <AppLayoutToolbar
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
              { href: '/', text: t('dashboard.breadcrumb.home') },
              { href: '/dashboard', text: t('dashboard.breadcrumb.dashboard') },
            ]}
          />
        }
        content={
          <Container header={<Header variant="h1">{t('assessment-not-found.title')}</Header>}>
            <SpaceBetween size="m">
              <Box>{t('assessment-not-found.body', { id: jobId })}</Box>
              <Button variant="primary" onClick={() => navigate('/dashboard')}>
                {t('assessment-not-found.go-to-assessments')}
              </Button>
            </SpaceBetween>
          </Container>
        }
      />
    </>
  );
}
