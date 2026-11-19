// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo, useMemo, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Header from '@cloudscape-design/components/header';
import Link from '@cloudscape-design/components/link';
import Pagination from '@cloudscape-design/components/pagination';
import Table from '@cloudscape-design/components/table';
import TextFilter from '@cloudscape-design/components/text-filter';
import { filterAuroraTables } from '../utils/auroraDesign';
import { formatCount } from '../utils/format';

/**
 * The tables of one Aurora design as one Cloudscape table (#2325): name, columns,
 * primary key, indexes, foreign keys and queries, with a text filter on the name and
 * a pager. Picking a row (its name link or its radio) hands the table to the page,
 * which shows it in the split panel (AuroraTableDetailPanel), so the list stays in
 * view. Every name and key renders as text. Figures come from the design as built by
 * buildAuroraDesign; nothing here counts anything but the rows it is given.
 * Callers pass `t` (the page's own `useTranslation` function).
 */

const PAGE_SIZE = 25;

const AuroraDesignTables = memo(({ design, count, selectedKey, onSelect, t }) => {
  const [text, setText] = useState('');
  const [page, setPage] = useState(1);

  const filtered = useMemo(() => filterAuroraTables(design.tables, text), [design.tables, text]);
  const pagesCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pagesCount);
  const pageRows = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const selectedItems = pageRows.filter(r => r.key === selectedKey);
  // The heading states the report's count (the tab reads the same one); a filter adds how many rows it shows.
  const counter = text.trim()
    ? t('analysis-results.aurora-design.counter-filtered', { shown: formatCount(filtered.length), total: formatCount(design.tables.length) })
    : undefined;

  const columnDefinitions = [
    {
      id: 'table',
      header: t('analysis-results.aurora-design.col-table'),
      cell: item => (
        <Link
          ariaLabel={t('analysis-results.aurora-design.open-label', { table: item.tableName })}
          onFollow={(e) => { e.preventDefault(); onSelect(item.key); }}
        >
          <span style={{ overflowWrap: 'anywhere' }}>{item.tableName}</span>
        </Link>
      ),
      width: 220,
      minWidth: 140,
    },
    {
      id: 'columns',
      header: t('analysis-results.aurora-design.col-columns'),
      cell: item => formatCount(item.columns.length),
      width: 90,
    },
    {
      id: 'primary-key',
      header: t('analysis-results.aurora-design.primary-key'),
      cell: item => (item.primaryKey.length > 0 ? item.primaryKey.join(', ') : '—'),
      width: 180,
    },
    {
      id: 'indexes',
      header: t('analysis-results.aurora-design.col-indexes'),
      cell: item => formatCount(item.indexes.length),
      width: 90,
    },
    {
      id: 'foreign-keys',
      header: t('analysis-results.aurora-design.col-foreign-keys'),
      cell: item => formatCount(item.foreignKeys.length),
      width: 110,
    },
    {
      id: 'queries',
      header: t('analysis-results.aurora-design.col-queries'),
      cell: item => formatCount(item.queryCount),
      width: 90,
    },
  ];

  return (
    <SpaceBetween size="m">
      <Header variant="h3" counter={counter}>{t('analysis-results.aurora-design.tables-header', { count })}</Header>
      <Table
        id="aurora-design-tables"
        variant="embedded"
        filter={(
          <TextFilter
            filteringText={text}
            filteringPlaceholder={t('analysis-results.aurora-design.filter-placeholder')}
            filteringAriaLabel={t('analysis-results.aurora-design.filter-label')}
            filteringClearAriaLabel={t('common.actions.clear-filter')}
            countText={t('analysis-results.aurora-design.count-text', { count: filtered.length, shown: formatCount(filtered.length) })}
            onChange={({ detail }) => { setText(detail.filteringText); setPage(1); }}
          />
        )}
        ariaLabels={{
          tableLabel: t('analysis-results.aurora-design.tables-table-label', { engine: design.displayName }),
          selectionGroupLabel: t('analysis-results.aurora-design.selection-group'),
          itemSelectionLabel: (data, item) => t('analysis-results.aurora-design.select-item', { table: item.tableName }),
          resizerRoleDescription: t('analysis-results.query-routing.resizer'),
        }}
        resizableColumns
        wrapLines
        trackBy="key"
        columnDefinitions={columnDefinitions}
        selectionType="single"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) => {
          const picked = detail.selectedItems[0];
          if (picked) onSelect(picked.key);
        }}
        items={pageRows}
        empty={<Box textAlign="center" padding="l">{t('analysis-results.aurora-design.empty')}</Box>}
      />
      {pagesCount > 1 && (
        <Box float="right">
          <Pagination
            ariaLabels={{
              nextPageLabel: t('analysis-results.query-routing.next-page'),
              previousPageLabel: t('analysis-results.query-routing.previous-page'),
              pageLabel: n => t('analysis-results.query-routing.page', { page: n }),
              paginationLabel: t('analysis-results.aurora-design.pagination'),
            }}
            currentPageIndex={currentPage}
            pagesCount={pagesCount}
            onChange={({ detail }) => setPage(detail.currentPageIndex)}
          />
        </Box>
      )}
    </SpaceBetween>
  );
});

/**
 * #2484: the objects the design leaves out, from facts.design_left_out (`leftOut`, one
 * engine's row): a short alert above the table naming each one, its kind of note and the
 * reason, so a table the design does not create is never just missing from the list.
 * The count is the fact's; every name renders as text.
 */
export const AuroraLeftOutAlert = memo(({ leftOut, t }) => {
  if (!leftOut || !(leftOut.objects || []).length) return null;
  return (
    <Alert type="warning" header={t('analysis-results.aurora-design.left-out-title', { count: leftOut.object_count })}>
      <SpaceBetween size="xxs">
        <Box fontSize="body-s">{t(leftOut.ddl_names_each ? 'analysis-results.aurora-design.left-out-body-ddl' : 'analysis-results.aurora-design.left-out-body')}</Box>
        <ul id="aurora-design-left-out" style={{ margin: 0, paddingLeft: '1.25rem' }}>
          {leftOut.objects.map((o, i) => (
            <li key={i}>
              <Box fontSize="body-s">
                {t('analysis-results.aurora-design.left-out-item', { object: o.object, label: o.label })}{' '}
                <Box variant="code" display="inline">{o.name}</Box>
                {': '}
                {o.reason}
              </Box>
            </li>
          ))}
        </ul>
      </SpaceBetween>
    </Alert>
  );
});

export default AuroraDesignTables;
