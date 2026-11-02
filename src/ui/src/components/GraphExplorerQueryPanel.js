// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Box from '@cloudscape-design/components/box';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Spinner from '@cloudscape-design/components/spinner';
import { waveTags } from '../utils/graphExplorer';

function Field({ label, children }) {
  return (
    <div>
      <Box variant="awsui-key-label">{label}</Box>
      <div>{children ?? '-'}</div>
    </div>
  );
}

/**
 * A query's details (the API's /explore/query answer). Every figure is the
 * API's; customer text (SQL, reasons, table names) is rendered as text.
 */
export default function GraphExplorerQueryPanel({ t, detail, loading, error, embedded = false }) {
  if (!detail && !loading && !error) return null;
  const body = (
    <>
      {loading && <Spinner />}
      {error && <Box color="text-status-error">{t('graph-explorer.errors.query')}</Box>}
      {detail && (
        <SpaceBetween size="m">
          <ColumnLayout columns={embedded ? 2 : 4} variant="text-grid">
            <Field label={t('graph-explorer.query.id')}>
              <span style={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>{detail.query_id}</span>
            </Field>
            <Field label={t('graph-explorer.query.operation')}>{detail.operation}</Field>
            <Field label={t('graph-explorer.query.cps')}>
              {detail.calls_per_second == null ? null : detail.calls_per_second.toLocaleString()}
            </Field>
            <Field label={t('graph-explorer.query.engine')}>{detail.engine_label}</Field>
            <Field label={t('graph-explorer.query.waves')}>
              {waveTags(detail, t).join(' · ') || null}
            </Field>
            <Field label={t('graph-explorer.query.class')}>
              {detail.access_pattern_class}
              {detail.access_pattern_reason && (
                <Box variant="small" color="text-body-secondary">{detail.access_pattern_reason}</Box>
              )}
            </Field>
            <Field label={t('graph-explorer.query.unit')}>
              {detail.unit_size ? t('graph-explorer.query.unit-size', { count: detail.unit_size }) : null}
            </Field>
            <Field label={t('graph-explorer.query.tables')}>
              {(detail.accesses || []).map((a) => (
                <div key={a.table}>
                  {a.table}
                  {a.access ? ` (${t(`graph-explorer.access.${a.access}`)})` : ''}
                </div>
              ))}
            </Field>
          </ColumnLayout>
          <Field label={t('graph-explorer.query.why')}>{detail.assignment_reason}</Field>
          <Field label={t('graph-explorer.query.sql')}>
            {detail.sql_preview && <Box variant="code">{detail.sql_preview}</Box>}
          </Field>
        </SpaceBetween>
      )}
    </>
  );
  // Embedded in a query card: no frame of its own, two columns.
  if (embedded) return <div>{body}</div>;
  return (
    <Container header={<Header variant="h2">{detail ? detail.label : t('graph-explorer.query.title')}</Header>}>
      {body}
    </Container>
  );
}
