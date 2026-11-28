// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
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
 * the routing reason, and its own generated DDL in an expandable section. The DDL is
 * read-only text in a code block, never HTML, and every name renders through React's
 * escaping. The panel adds nothing the design and the facts do not hold.
 *
 * #2289: when facts.design_changes compared the table with its collected table
 * (`table.changes`), the columns table adds each column's source type, nullable and
 * default (as the stored CREATE TABLE writes them) and its change from the source, and
 * the indexes are a table of name, columns, unique and change, a source index the design
 * does not create included. A report written before the comparison shows the stored
 * index statements as before.
 */

const DASH = '—';

const ROW_COLORS = { same: 'grey', translated: 'grey', step1: 'grey', added: 'green', skipped: 'red' };

const RowChange = ({ change, t }) => (
  <Badge color={ROW_COLORS[change] || 'blue'}>{t(`analysis-results.aurora-design.row-change.${change}`)}</Badge>
);

const yesNo = (value, t) => (value === null || value === undefined
  ? DASH
  : t(value ? 'analysis-results.aurora-design.yes' : 'analysis-results.aurora-design.no'));

const compared = (table) => Boolean(table.changes) && !table.changes.uncompared;

function columnRows(table) {
  if (!compared(table)) {
    return table.columns.map(c => ({ name: c.name, aurora_type: c.auroraType, change: null }));
  }
  return table.changes.columns || [];
}

function columnDefinitions(table, t) {
  const changed = compared(table);
  const code = v => (v === null || v === undefined || v === '' ? DASH : <Box fontFamily="monospace" fontSize="body-s">{v}</Box>);
  return [
    { id: 'name', header: t('analysis-results.aurora-design.col-column'), cell: c => c.name, width: 200 },
    { id: 'type', header: t('analysis-results.aurora-design.col-aurora-type'), cell: c => code(c.aurora_type), width: 180 },
    ...(changed ? [
      { id: 'source-type', header: t('analysis-results.aurora-design.col-source-type'), cell: c => code(c.source_type), width: 180 },
      { id: 'nullable', header: t('analysis-results.aurora-design.col-nullable'), cell: c => yesNo(c.nullable, t), width: 100 },
      {
        id: 'default',
        header: t('analysis-results.aurora-design.col-default'),
        cell: c => (c.identity ? t('analysis-results.aurora-design.identity') : code(c.default)),
        width: 160,
      },
      { id: 'change', header: t('analysis-results.aurora-design.col-change'), cell: c => <RowChange change={c.change} t={t} />, width: 140 },
    ] : []),
  ];
}

// The lines under an index's change badge (#2289): its source name, what differs from the
// source with the source's key, and the reason the draft or the design's notes give.
function indexDetails(index, t) {
  const lines = [];
  if (index.change === 'renamed' && index.source_name) {
    lines.push(t('analysis-results.aurora-design.renamed-from', { name: index.source_name }));
  }
  const parts = index.differences || [];
  if (parts.length > 0) {
    lines.push(t('analysis-results.aurora-design.differs-in', {
      parts: parts.map(p => t(`analysis-results.aurora-design.index-part.${p}`)).join(', '),
    }));
    if (index.source_columns) {
      lines.push(t(index.source_unique ? 'analysis-results.aurora-design.source-key-unique' : 'analysis-results.aurora-design.source-key', { columns: index.source_columns.join(', ') }));
    }
  }
  if (index.change === 'step1') lines.push(t('analysis-results.aurora-design.step1-body'));
  if (index.reason) lines.push(index.reason);
  else if (index.change === 'skipped') lines.push(t('analysis-results.aurora-design.skipped-no-note'));
  return lines;
}

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
          { label: t('analysis-results.aurora-design.primary-key'), value: table.primaryKey.join(', ') || DASH },
          ...(compared(table) ? [] : [{
            label: t('analysis-results.aurora-design.indexes', { count: table.indexes.length }),
            value: table.indexes.length > 0 ? list(table.indexes) : DASH,
          }]),
          {
            label: t('analysis-results.aurora-design.foreign-keys', { count: table.foreignKeys.length }),
            value: table.foreignKeys.length > 0 ? list(table.foreignKeys) : DASH,
          },
        ]}
      />
      <Table
        variant="embedded"
        header={<Header variant="h3">{t('analysis-results.aurora-design.columns-title')}</Header>}
        columnDefinitions={columnDefinitions(table, t)}
        items={columnRows(table)}
        wrapLines
        empty={<Box color="text-body-secondary">{t('analysis-results.aurora-design.no-columns')}</Box>}
        ariaLabels={{
          tableLabel: t('analysis-results.aurora-design.columns-table-label', { engine: engineName, table: table.tableName }),
        }}
      />
      {table.changes && table.changes.uncompared && (
        <Box color="text-body-secondary" data-figure="table-uncompared">
          {t('analysis-results.aurora-design.uncompared-body', { reason: table.changes.reason })}
        </Box>
      )}
      {compared(table) && (
        <Table
          variant="embedded"
          header={<Header variant="h3">{t('analysis-results.aurora-design.indexes-title')}</Header>}
          columnDefinitions={[
            { id: 'name', header: t('analysis-results.aurora-design.col-index'), cell: i => <Box fontFamily="monospace" fontSize="body-s">{i.name}</Box>, width: 220 },
            { id: 'columns', header: t('analysis-results.aurora-design.col-index-columns'), cell: i => (i.columns || []).join(', ') || DASH, width: 220 },
            { id: 'unique', header: t('analysis-results.aurora-design.col-unique'), cell: i => yesNo(i.unique, t), width: 90 },
            {
              id: 'change',
              header: t('analysis-results.aurora-design.col-change'),
              cell: i => (
                <SpaceBetween size="xxs">
                  <RowChange change={i.change} t={t} />
                  {indexDetails(i, t).map((line, n) => (
                    <Box key={n} fontSize="body-s" color="text-body-secondary"><ProseText text={line} /></Box>
                  ))}
                </SpaceBetween>
              ),
              width: 260,
            },
          ]}
          items={table.changes.indexes || []}
          wrapLines
          empty={<Box color="text-body-secondary">{t('analysis-results.aurora-design.no-indexes')}</Box>}
          ariaLabels={{
            tableLabel: t('analysis-results.aurora-design.indexes-table-label', { engine: engineName, table: table.tableName }),
          }}
        />
      )}
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
      <ExpandableSection headerText={t('analysis-results.aurora-design.ddl-header')} defaultExpanded={false}>
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
      </ExpandableSection>
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
