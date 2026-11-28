// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo, useMemo, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Badge from '@cloudscape-design/components/badge';
import Box from '@cloudscape-design/components/box';
import ExpandableSection from '@cloudscape-design/components/expandable-section';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Header from '@cloudscape-design/components/header';
import Link from '@cloudscape-design/components/link';
import Pagination from '@cloudscape-design/components/pagination';
import Select from '@cloudscape-design/components/select';
import Table from '@cloudscape-design/components/table';
import TextFilter from '@cloudscape-design/components/text-filter';
import { CHANGE_FILTER_ALL, CHANGE_FILTER_ANY, CHANGE_FILTER_SAME, filterAuroraTables } from '../utils/auroraDesign';
import { formatCount } from '../utils/format';
import ProseText from './ProseText';

/**
 * The tables of one Aurora design as one Cloudscape table (#2325): name, columns,
 * primary key, indexes, foreign keys and queries, with a text filter on the name and
 * a pager. Every column sorts. When facts.design_changes compared the design with the
 * source (#2289), a Change column shows each table's kinds of change as badges, and a
 * select filters the rows by them. Picking a row (its name link or its radio) hands the table to the page,
 * which shows it in the split panel (AuroraTableDetailPanel), so the list stays in
 * view. Every name and key renders as text. Figures come from the design as built by
 * buildAuroraDesign; nothing here counts anything but the rows it is given.
 * Callers pass `t` (the page's own `useTranslation` function).
 */

const PAGE_SIZE = 25;

// #2289: each table-level change kind facts.design_changes writes, in its order
// (CHANGE_KINDS in src/shared/design_changes.py, then translated), and its badge colour.
export const TABLE_CHANGE_KINDS = [
  'table_added', 'column_added', 'column_skipped', 'type_changed',
  'index_added', 'index_renamed', 'index_changed', 'index_skipped', 'translated', 'index_step1',
];
const KIND_COLORS = {
  table_added: 'green', column_added: 'green', index_added: 'green',
  column_skipped: 'red', index_skipped: 'red', translated: 'grey', index_step1: 'grey',
};
export const changeColor = (kind) => KIND_COLORS[kind] || 'blue';

/** A design table's change kinds as badges; "Same" with none, a dash when not compared. */
export function TableChangeBadges({ changes, t }) {
  if (!changes) return '—';
  if (changes.uncompared) return <Badge color="grey">{t('analysis-results.aurora-design.change.uncompared')}</Badge>;
  const kinds = changes.change_kinds || [];
  if (kinds.length === 0) return <Badge color="grey">{t('analysis-results.aurora-design.change.same')}</Badge>;
  return (
    <SpaceBetween direction="horizontal" size="xxs">
      {kinds.map(kind => (
        <Badge key={kind} color={changeColor(kind)}>{t(`analysis-results.aurora-design.change.${kind}`)}</Badge>
      ))}
    </SpaceBetween>
  );
}

const compareText = (a, b) => String(a).localeCompare(String(b), undefined, { sensitivity: 'base', numeric: true });
const compareNumber = (a, b) => a - b;
// Not compared sorts first, then the tables the facts mark unchanged, then the changed ones
// by how many kinds of change they have (#2289: the facts' changed flag decides).
const changeRank = (row) => {
  const changes = row.changes;
  if (!changes || changes.uncompared) return -1;
  return changes.changed ? 1 + (changes.change_kinds || []).length : 0;
};

const AuroraDesignTables = memo(({ design, count, selectedKey, onSelect, t }) => {
  const [text, setText] = useState('');
  const [change, setChange] = useState(CHANGE_FILTER_ALL);
  const [page, setPage] = useState(1);
  const [sorting, setSorting] = useState(null);
  const compared = Boolean(design.changes);

  const filtered = useMemo(() => filterAuroraTables(design.tables, text, change), [design.tables, text, change]);
  const filtering = Boolean(text.trim()) || change !== CHANGE_FILTER_ALL;
  // The heading states the report's count (the tab reads the same one); a filter adds how many rows it shows.
  const counter = filtering
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
      sortingComparator: (a, b) => compareText(a.tableName, b.tableName),
    },
    {
      id: 'columns',
      header: t('analysis-results.aurora-design.col-columns'),
      cell: item => formatCount(item.columns.length),
      width: 90,
      sortingComparator: (a, b) => compareNumber(a.columns.length, b.columns.length),
    },
    {
      id: 'primary-key',
      header: t('analysis-results.aurora-design.primary-key'),
      cell: item => (item.primaryKey.length > 0 ? item.primaryKey.join(', ') : '—'),
      width: 180,
      sortingComparator: (a, b) => compareText(a.primaryKey.join(', '), b.primaryKey.join(', ')),
    },
    {
      id: 'indexes',
      header: t('analysis-results.aurora-design.col-indexes'),
      cell: item => formatCount(item.indexes.length),
      width: 90,
      sortingComparator: (a, b) => compareNumber(a.indexes.length, b.indexes.length),
    },
    {
      id: 'foreign-keys',
      header: t('analysis-results.aurora-design.col-foreign-keys'),
      cell: item => formatCount(item.foreignKeys.length),
      width: 110,
      sortingComparator: (a, b) => compareNumber(a.foreignKeys.length, b.foreignKeys.length),
    },
    {
      id: 'queries',
      header: t('analysis-results.aurora-design.col-queries'),
      cell: item => formatCount(item.queryCount),
      width: 90,
      sortingComparator: (a, b) => compareNumber(a.queryCount, b.queryCount),
    },
    ...(compared ? [{
      id: 'change',
      header: t('analysis-results.aurora-design.col-change'),
      cell: item => <TableChangeBadges changes={item.changes} t={t} />,
      width: 220,
      sortingComparator: (a, b) => compareNumber(changeRank(a), changeRank(b)),
    }] : []),
  ];

  let sorted = filtered;
  const sortingColumn = sorting ? columnDefinitions.find(c => c.id === sorting.id) : undefined;
  if (sortingColumn) {
    const sign = sorting.descending ? -1 : 1;
    sorted = [...filtered].sort((a, b) => sign * sortingColumn.sortingComparator(a, b));
  }
  const pagesCount = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const currentPage = Math.min(page, pagesCount);
  const pageRows = sorted.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const selectedItems = pageRows.filter(r => r.key === selectedKey);

  // #2289: the change filter lists every kind the design has, besides all, any and none.
  const presentKinds = TABLE_CHANGE_KINDS.filter(k => (design.changes?.change_counts || {})[k]);
  const changeOptions = [
    { value: CHANGE_FILTER_ALL, label: t('analysis-results.aurora-design.change-filter.all') },
    { value: CHANGE_FILTER_ANY, label: t('analysis-results.aurora-design.change-filter.any') },
    { value: CHANGE_FILTER_SAME, label: t('analysis-results.aurora-design.change-filter.same') },
    ...presentKinds.map(k => ({ value: k, label: t(`analysis-results.aurora-design.change.${k}`) })),
  ];
  const textFilter = (
    <TextFilter
      filteringText={text}
      filteringPlaceholder={t('analysis-results.aurora-design.filter-placeholder')}
      filteringAriaLabel={t('analysis-results.aurora-design.filter-label')}
      filteringClearAriaLabel={t('common.actions.clear-filter')}
      countText={t('analysis-results.aurora-design.count-text', { count: filtered.length, shown: formatCount(filtered.length) })}
      onChange={({ detail }) => { setText(detail.filteringText); setPage(1); }}
    />
  );

  return (
    <SpaceBetween size="m">
      <Header variant="h3" counter={counter}>{t('analysis-results.aurora-design.tables-header', { count })}</Header>
      <Table
        id="aurora-design-tables"
        variant="embedded"
        filter={compared ? (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'flex-start' }}>
            <div style={{ flex: '1 1 260px', maxWidth: '480px' }}>{textFilter}</div>
            <div style={{ flex: '0 1 240px' }}>
              <Select
                ariaLabel={t('analysis-results.aurora-design.change-filter.label')}
                selectedOption={changeOptions.find(o => o.value === change) || changeOptions[0]}
                options={changeOptions}
                onChange={({ detail }) => { setChange(detail.selectedOption.value); setPage(1); }}
              />
            </div>
          </div>
        ) : textFilter}
        sortingColumn={sortingColumn}
        sortingDescending={sorting ? sorting.descending : false}
        onSortingChange={({ detail }) => { setSorting({ id: detail.sortingColumn.id, descending: detail.isDescending }); setPage(1); }}
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
 * #2289: what the design changes from the source, as facts.design_changes counts it: how
 * many of the compared tables change (the facts' changed flag), how many column types a
 * heterogeneous design translates, how many expression indexes step 1 recreates and how
 * many tables are not compared. Nothing for a design that was not compared.
 */
export const AuroraChangeSummary = memo(({ changes, t }) => {
  if (!changes) return null;
  const counts = changes.change_counts || {};
  const translated = counts.translated || 0;
  const step1 = counts.index_step1 || 0;
  const uncompared = changes.uncompared_table_count || 0;
  const lines = [];
  if (changes.changed_table_count > 0) {
    lines.push(t('analysis-results.aurora-design.changes-summary', { changed: formatCount(changes.changed_table_count), total: formatCount(changes.table_count) }));
  } else if (changes.table_count > 0) {
    lines.push(t('analysis-results.aurora-design.changes-none', { count: changes.table_count, total: formatCount(changes.table_count) }));
  }
  if (translated > 0) lines.push(t('analysis-results.aurora-design.changes-translated', { count: translated, shown: formatCount(translated) }));
  if (step1 > 0) lines.push(t('analysis-results.aurora-design.changes-step1', { count: step1, shown: formatCount(step1) }));
  if (uncompared > 0) lines.push(t('analysis-results.aurora-design.changes-uncompared', { count: uncompared, shown: formatCount(uncompared) }));
  if (lines.length === 0) return null;
  return (
    <Box fontSize="body-s" color="text-body-secondary" data-figure="design-changes">
      {lines.join(' ')}
    </Box>
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

/**
 * #2545: the design's app-layer notes (`design.appLayerNotes`, built by appLayerNotesOf):
 * a collapsed section listing each note's kind, its object and its text, an index the
 * design renames included, so a name in the DDL the source does not have is explained
 * here too. It shows no count. Every name and text renders as text.
 */
export const AuroraAppLayerNotes = memo(({ notes, t }) => {
  if (!notes || notes.length === 0) return null;
  return (
    <ExpandableSection headerText={t('analysis-results.aurora-design.app-layer-notes-header')} defaultExpanded={false}>
      <SpaceBetween size="xxs">
        <Box fontSize="body-s" color="text-body-secondary">{t('analysis-results.aurora-design.app-layer-notes-body')}</Box>
        <ul id="aurora-design-app-layer-notes" style={{ margin: 0, paddingLeft: '1.25rem' }}>
          {notes.map((n, i) => (
            <li key={i}>
              <Box fontSize="body-s">
                {t('analysis-results.aurora-design.app-layer-notes-item', { label: n.label })}{' '}
                <Box variant="code" display="inline">{n.object}</Box>
                {n.text ? ': ' : ''}
                <ProseText text={n.text} />
              </Box>
            </li>
          ))}
        </ul>
      </SpaceBetween>
    </ExpandableSection>
  );
});

export default AuroraDesignTables;
