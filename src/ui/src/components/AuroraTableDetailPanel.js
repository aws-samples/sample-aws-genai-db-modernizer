// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import SplitPanel from '@cloudscape-design/components/split-panel';
import Header from '@cloudscape-design/components/header';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Table from '@cloudscape-design/components/table';
import { CodeBlock } from './StyledBlocks';
import ProseText from './ProseText';
import { formatCount, formatRate } from '../utils/format';

/**
 * One table of an Aurora design in the page's split panel (#2325): its columns, its
 * primary key, indexes and foreign keys, the queries the assignment routed to it with
 * the routing reason, and its own generated DDL. The DDL is read-only text in a code
 * block, never HTML, and every name renders through React's escaping. The
 * panel adds nothing the design does not hold.
 */

export function AuroraTableDetailBody({ table, engineName, t }) {
  const list = (items) => (
    <SpaceBetween size="xxs">
      {items.map((item, i) => (
        <Box key={`${i}-${item}`} fontFamily="monospace" fontSize="body-s">{item}</Box>
      ))}
    </SpaceBetween>
  );
  return (
    <SpaceBetween size="l">
      <KeyValuePairs
        columns={3}
        items={[
          { label: t('analysis-results.aurora-design.primary-key'), value: table.primaryKey.join(', ') || '—' },
          {
            label: t('analysis-results.aurora-design.indexes', { count: table.indexes.length }),
            value: table.indexes.length > 0 ? list(table.indexes) : '—',
          },
          {
            label: t('analysis-results.aurora-design.foreign-keys', { count: table.foreignKeys.length }),
            value: table.foreignKeys.length > 0 ? list(table.foreignKeys) : '—',
          },
        ]}
      />
      <Table
        variant="embedded"
        header={<Header variant="h3">{t('analysis-results.aurora-design.columns-title')}</Header>}
        columnDefinitions={[
          { id: 'name', header: t('analysis-results.aurora-design.col-column'), cell: c => c.name, width: 240 },
          { id: 'type', header: t('analysis-results.aurora-design.col-aurora-type'), cell: c => c.auroraType, width: 240 },
        ]}
        items={table.columns}
        wrapLines
        empty={<Box color="text-body-secondary">{t('analysis-results.aurora-design.no-columns')}</Box>}
        ariaLabels={{
          tableLabel: t('analysis-results.aurora-design.columns-table-label', { engine: engineName, table: table.tableName }),
        }}
      />
      <Table
        variant="embedded"
        header={<Header variant="h3" counter={`(${formatCount(table.queryCount)})`}>{t('analysis-results.aurora-design.queries-title')}</Header>}
        columnDefinitions={[
          {
            id: 'excerpt',
            header: t('analysis-results.aurora-design.col-query'),
            cell: q => <Box fontFamily="monospace" fontSize="body-s">{q.excerpt}</Box>,
            minWidth: 240,
          },
          { id: 'type', header: t('analysis-results.aurora-design.col-type'), cell: q => q.queryType || '—', width: 100 },
          { id: 'rps', header: t('analysis-results.aurora-design.col-calls-per-second'), cell: q => formatRate(q.callsPerSecond), width: 100 },
          { id: 'reason', header: t('analysis-results.aurora-design.col-reason'), cell: q => (q.reason ? <ProseText text={q.reason} /> : '—'), minWidth: 240 },
        ]}
        items={table.queries}
        wrapLines
        empty={<Box color="text-body-secondary">{t('analysis-results.aurora-design.no-table-queries')}</Box>}
        ariaLabels={{
          tableLabel: t('analysis-results.aurora-design.queries-table-label', { engine: engineName, table: table.tableName }),
        }}
      />
      <SpaceBetween size="xs">
        <Header variant="h3">{t('analysis-results.aurora-design.ddl-header')}</Header>
        {table.ddlStatements ? (
          <CodeBlock capped label={t('analysis-results.aurora-design.table-ddl-label', { table: table.tableName })}>
            {table.ddlStatements.join('\n\n')}
          </CodeBlock>
        ) : (
          <>
            <Box color="text-body-secondary">{t('analysis-results.aurora-design.no-table-ddl')}</Box>
            {table.fallbackDdl && (
              <CodeBlock capped label={t('analysis-results.aurora-design.full-ddl-label')}>
                {table.fallbackDdl}
              </CodeBlock>
            )}
          </>
        )}
      </SpaceBetween>
    </SpaceBetween>
  );
}

export default function AuroraTableDetailPanel({ table, engineName, t }) {
  return (
    <SplitPanel
      header={t('analysis-results.aurora-design.detail-title', { table: table.tableName })}
      closeBehavior="hide"
      i18nStrings={{
        closeButtonAriaLabel: t('analysis-results.aurora-design.detail-close'),
        openButtonAriaLabel: t('analysis-results.aurora-design.detail-open'),
        preferencesTitle: t('analysis-results.query-detail.preferences-title'),
        preferencesPositionLabel: t('analysis-results.query-detail.position-label'),
        preferencesPositionDescription: t('analysis-results.query-detail.position-description'),
        preferencesPositionSide: t('analysis-results.query-detail.position-side'),
        preferencesPositionBottom: t('analysis-results.query-detail.position-bottom'),
        preferencesConfirm: t('analysis-results.query-detail.preferences-confirm'),
        preferencesCancel: t('common.actions.cancel'),
        preferencesCloseAriaLabel: t('analysis-results.query-detail.preferences-close'),
        resizeHandleAriaLabel: t('analysis-results.query-detail.resize'),
      }}
    >
      <AuroraTableDetailBody table={table} engineName={engineName} t={t} />
    </SplitPanel>
  );
}
