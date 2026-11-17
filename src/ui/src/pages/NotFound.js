// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useLocation, useNavigate } from 'react-router-dom';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { JobRouteShell } from '../components/AssessmentNotFound';

// The page for a path no route matches (#2393). It says so, names the path and
// links to the dashboard and to the assessments list on it (#assessments).
export default function NotFound() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { pathname } = useLocation();

  useEffect(() => {
    const previous = document.title;
    document.title = t('not-found.document-title');
    return () => {
      document.title = previous;
    };
  }, [t]);

  const link = (href) => ({
    href,
    onFollow: (event) => {
      event.preventDefault();
      navigate(href);
    },
  });

  return (
    <JobRouteShell lastCrumb={t('not-found.breadcrumb')}>
      <Container header={<Header variant="h1">{t('not-found.title')}</Header>}>
        <SpaceBetween size="m">
          <Box>{t('not-found.body', { path: pathname })}</Box>
          <SpaceBetween size="s" direction="horizontal">
            <Button variant="primary" {...link('/dashboard')}>{t('not-found.go-to-dashboard')}</Button>
            <Button {...link('/dashboard#assessments')}>{t('not-found.go-to-assessments')}</Button>
          </SpaceBetween>
        </SpaceBetween>
      </Container>
    </JobRouteShell>
  );
}
