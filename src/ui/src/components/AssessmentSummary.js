// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import Box from '@cloudscape-design/components/box';
import Container from '@cloudscape-design/components/container';
import CopyToClipboard from '@cloudscape-design/components/copy-to-clipboard';
import Header from '@cloudscape-design/components/header';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import EngineBadge from './EngineBadge';
import { copyJobIdLabel } from '../utils/assessmentRows';
import { getCacheOverlay, ownerDistribution, targetEngineEntries } from '../utils/cacheLayer';
import { displayEngine } from '../utils/engineNames';
import { NO_FIGURE, formatMonthlyMoney } from '../utils/format';
import { finalEngineCounts, overallRiskLevelOf, projectedMonthlyCostOf, targetEngineEntriesOf } from '../utils/reportFacts';
import { riskSeverityLabel, riskSeverityStatus } from '../utils/riskAssessment';

/** The assessment id with a copy button, named for the database it belongs to. */
export function AssessmentId({ jobId, databaseName }) {
  const { t } = useTranslation();
  return (
    <SpaceBetween direction="horizontal" size="xxs" alignItems="center">
      <Box fontFamily="monospace" fontSize="body-s">{jobId}</Box>
      <CopyToClipboard
        variant="icon"
        textToCopy={jobId}
        copyButtonAriaLabel={copyJobIdLabel(t, databaseName, jobId)}
        copySuccessText={t('common.success.copied')}
        copyErrorText={t('common.errors.copy-failed')}
      />
    </SpaceBetween>
  );
}

/**
 * What a finished assessment found, at a glance (#2317): the database, its source engine,
 * the target engines, the projected monthly cost and the overall risk. Every figure is a
 * report fact read as stored (ADR-032); a figure the report does not state shows a dash.
 */
export default function AssessmentSummary({ jobId, jobData, synthesis }) {
  const { t } = useTranslation();
  const targets = useMemo(() => {
    const overlay = getCacheOverlay(synthesis);
    const afterDist = ownerDistribution(finalEngineCounts(synthesis), !!overlay);
    return targetEngineEntriesOf(synthesis) ?? targetEngineEntries(afterDist, overlay);
  }, [synthesis]);
  const risk = overallRiskLevelOf(synthesis);
  const databaseName = jobData?.database_name;
  const source = jobData?.source_database_label || jobData?.source_database_type;

  return (
    <Container header={<Header variant="h2">{t('job-summary.summary.title')}</Header>}>
      <KeyValuePairs
        columns={3}
        items={[
          { label: t('job-summary.summary.database'), value: databaseName || NO_FIGURE },
          { label: t('job-summary.summary.source'), value: source || NO_FIGURE },
          {
            label: t('job-summary.summary.targets'),
            value: targets.length === 0 ? NO_FIGURE : (
              <SpaceBetween direction="horizontal" size="xxs">
                {targets.map(({ engine, isCacheLayer }) => (
                  <EngineBadge key={engine} engine={engine}>
                    {displayEngine(engine)}
                    {isCacheLayer ? t('cache-layer.engine-badge-suffix') : ''}
                  </EngineBadge>
                ))}
              </SpaceBetween>
            ),
          },
          { label: t('job-summary.summary.cost'), value: formatMonthlyMoney(projectedMonthlyCostOf(synthesis)) },
          {
            label: t('job-summary.summary.risk'),
            value: risk
              ? <StatusIndicator type={riskSeverityStatus(risk)}>{riskSeverityLabel(risk, t)}</StatusIndicator>
              : NO_FIGURE,
          },
          { label: t('job-summary.progress.job-id-label'), value: <AssessmentId jobId={jobId} databaseName={databaseName} /> },
        ]}
      />
    </Container>
  );
}
