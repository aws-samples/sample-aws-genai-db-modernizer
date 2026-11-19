// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo, useEffect, useMemo, useState } from 'react';
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Badge from "@cloudscape-design/components/badge";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Alert from "@cloudscape-design/components/alert";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import PropertyFilter from "@cloudscape-design/components/property-filter";
import Pagination from "@cloudscape-design/components/pagination";
import Link from "@cloudscape-design/components/link";
import Table from "@cloudscape-design/components/table";
import { failureMessageKey } from '../utils/requestFailure';
import { filterQueryRows, queryFilterOptions } from "../utils/queryRouting";
import { OPERATION_TYPES } from '../utils/accessPatterns';
import { propertyFilterStrings } from '../utils/propertyFilterStrings';
import { formatCount, formatShortId } from '../utils/format';
import EngineBadge from './EngineBadge';
import DestinationTables from './DestinationTables';

/**
 * The Queries tab's one query table (#2319, ADR-031: the query is the unit).
 * Every query of the assessment, open when the tab opens, with a property filter
 * over its columns (engine, operation, table, destination, cached, routing
 * changed) and free text over the id and the statement. A row's id button or
 * its radio selects the query; the page shows the selected query in a split
 * panel (QueryDetailPanel) so this table stays visible. Unlike the access
 * pattern explorer, the table needs no schema design: a deterministic run (or
 * one whose schema design was skipped) lists every query, with a dash where the
 * destination or the cache would be.
 *
 * Rows come from `buildQueryRoutingRows` (utils/queryRouting.js) and only
 * format what the API, the facts layer and the schema design hold. The page
 * shows 25 rows at a time, so a job with 1,654 queries renders 25 rows, and the
 * filter compares one lower-cased string per row.
 *
 * #2446: the table is the tab's one list of queries. The access pattern explorer's
 * pattern and cache tables are folded in: a query's access patterns and cache key
 * designs open in its detail panel, and the explorer's operation type filter is the
 * "Operation type" property here. #2483: its pattern group, Uses GSI, target operation and
 * source table (through merged tables) filters are properties too, grouped under the
 * target design. A link that names an engine (`linkedFilter`, a
 * property filter token) opens the table filtered to it.
 *
 * Callers pass `t` (the page's own `useTranslation` function), like
 * QueryDecisionTrace. `queryPatterns` is facts.query_pattern_counts as
 * `{ queries, patterns }` (queryPatternCountsOf), or null for a report written before it
 * (#1146): "1,654 queries in 1,045 distinct query patterns", the line the executive and
 * engineering reports print, stated above the filter (#2315: moved here from the summary).
 */

const PAGE_SIZE = 25;
// #2483: the group the target design's properties are listed under (customGroupsText).
const TARGET_DESIGN_GROUP = 'target-design';

// A table name that can wrap at its underscores, never in the middle of a word.
function WrappableName({ name }) {
  const parts = String(name).split('_');
  return (
    <>
      {parts.map((part, i) => (
        <span key={`${i}-${part}`}>{i > 0 && <wbr />}{i > 0 ? `_${part}` : part}</span>
      ))}
    </>
  );
}

function RoutingBadges({ kinds, hasLedger, t }) {
  // Without a move ledger the facts list nothing, which says nothing about
  // whether a query kept its first assignment (the trace view's no-ledger note
  // above the table says why).
  if (!hasLedger) return '—';
  if (!kinds) {
    return <Box color="text-body-secondary" fontSize="body-s">{t('analysis-results.query-routing.kept-first')}</Box>;
  }
  const labels = [];
  if (kinds.moved) labels.push(t('analysis-results.query-routing.kind-moved'));
  if (kinds.openTie) labels.push(t('analysis-results.query-routing.kind-open-tie'));
  else if (kinds.tied) labels.push(t('analysis-results.query-routing.kind-tie-settled'));
  if (kinds.overridden) labels.push(t('analysis-results.query-routing.kind-overridden'));
  if (kinds.pinned) labels.push(t('analysis-results.query-routing.kind-pinned'));
  return (
    <SpaceBetween direction="horizontal" size="xxs">
      {labels.map(label => <Badge key={label} color="blue">{label}</Badge>)}
    </SpaceBetween>
  );
}

const QueryRoutingTable = memo(({
  rows, loading, error, hasLedger, truncated, selectedId, onSelect, linkedFilter, t, queryPatterns,
}) => {
  const [query, setQuery] = useState({ tokens: [], operation: 'and' });
  const [page, setPage] = useState(1);
  // A link's token replaces the filter once, when it arrives; the reader's own
  // changes stand after that.
  const linkedKey = linkedFilter ? `${linkedFilter.propertyKey}=${linkedFilter.value}` : null;
  useEffect(() => {
    if (!linkedFilter) return;
    setQuery({ tokens: [{ propertyKey: linkedFilter.propertyKey, operator: '=', value: linkedFilter.value }], operation: 'and' });
    setPage(1);
  }, [linkedKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const filterOptions = useMemo(() => {
    const yes = t('analysis-results.query-routing.yes');
    const no = t('analysis-results.query-routing.no');
    return [
      ...queryFilterOptions(rows || []),
      ...OPERATION_TYPES.filter(type => (rows || []).some(r => (r.opTypes || []).includes(type))).map(type => ({ propertyKey: 'opType', value: type, label: t(`analysis-results.query-routing.op-type-${type}`) })),
      // #2483: only the Uses GSI values a row holds, so a run with no design offers none.
      ...['yes', 'no'].filter(value => (rows || []).some(r => (r.usesGsi || []).includes(value))).map(value => ({ propertyKey: 'usesGsi', value, label: value === 'yes' ? yes : no })),
      { propertyKey: 'cached', value: 'yes', label: yes },
      { propertyKey: 'cached', value: 'no', label: no },
      ...(hasLedger ? [
        { propertyKey: 'routing', value: 'yes', label: yes },
        { propertyKey: 'routing', value: 'no', label: no },
      ] : []),
    ];
  }, [rows, hasLedger, t]);
  // #2446: an engine or operation type token reads as its name ("Engine = DynamoDB"),
  // as the option the reader picked does, also when a link set it. #2480 review: so do
  // the yes/no tokens ("Cached = Yes", the cache layer's Sankey link).
  const filteringProperties = useMemo(() => {
    const labelOf = key => value => (filterOptions.find(o => o.propertyKey === key && o.value === value) || {}).label || value;
    const labelled = key => ['=', '!='].map(operator => ({ operator, format: labelOf(key) }));
    return [
    { key: 'engine', propertyLabel: t('analysis-results.query-routing.col-engine'), groupValuesLabel: t('analysis-results.query-routing.group-engine'), operators: labelled('engine') },
    { key: 'operation', propertyLabel: t('analysis-results.query-routing.col-operation'), groupValuesLabel: t('analysis-results.query-routing.group-operation'), operators: ['=', '!='] },
    { key: 'opType', propertyLabel: t('analysis-results.query-routing.prop-op-type'), groupValuesLabel: t('analysis-results.query-routing.group-op-type'), operators: labelled('opType') },
    { key: 'table', propertyLabel: t('analysis-results.query-routing.col-tables'), groupValuesLabel: t('analysis-results.query-routing.group-table'), operators: ['=', '!=', ':'] },
    { key: 'destination', propertyLabel: t('analysis-results.query-routing.col-destination'), groupValuesLabel: t('analysis-results.query-routing.group-destination'), operators: ['=', '!=', ':'] },
    { key: 'cached', propertyLabel: t('analysis-results.query-routing.col-cached'), groupValuesLabel: t('analysis-results.query-routing.group-cached'), operators: [{ operator: '=', format: labelOf('cached') }] },
    ...(hasLedger ? [{ key: 'routing', propertyLabel: t('analysis-results.query-routing.prop-routing'), groupValuesLabel: t('analysis-results.query-routing.group-routing'), operators: [{ operator: '=', format: labelOf('routing') }] }] : []),
    // #2483: the access pattern explorer's design filters, read from the patterns that
    // serve each query. Their group heading is their description: a query matches when
    // any access pattern that serves it matches.
    ...[
      { key: 'patternGroup', propertyLabel: t('analysis-results.query-routing.prop-pattern-group'), groupValuesLabel: t('analysis-results.query-routing.group-pattern-group'), operators: ['=', '!=', ':'] },
      { key: 'usesGsi', propertyLabel: t('analysis-results.query-routing.prop-uses-gsi'), groupValuesLabel: t('analysis-results.query-routing.group-uses-gsi'), operators: [{ operator: '=', format: labelOf('usesGsi') }] },
      { key: 'targetOperation', propertyLabel: t('analysis-results.query-routing.prop-target-operation'), groupValuesLabel: t('analysis-results.query-routing.group-target-operation'), operators: ['=', '!='] },
      { key: 'sourceTable', propertyLabel: t('analysis-results.query-routing.prop-source-table'), groupValuesLabel: t('analysis-results.query-routing.group-source-table'), operators: ['=', '!=', ':'] },
    ].map(property => ({ ...property, group: TARGET_DESIGN_GROUP })),
    ];
  }, [filterOptions, hasLedger, t]);
  const i18nStrings = useMemo(() => propertyFilterStrings(t, {
    ariaLabel: t('analysis-results.query-routing.filter-label'),
    placeholder: t('analysis-results.query-routing.filter-placeholder'),
  }), [t]);

  const customGroupsText = useMemo(() => [{
    group: TARGET_DESIGN_GROUP,
    properties: t('analysis-results.query-routing.group-target-design'),
    values: t('analysis-results.query-routing.group-target-design-values'),
  }], [t]);

  const filtered = useMemo(() => filterQueryRows(rows || [], query), [rows, query]);
  const pagesCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pagesCount);
  const pageRows = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const isFiltered = query.tokens.length > 0;
  // No count while the list loads or when it could not be read: a "(0)" there reads as
  // "no queries" (#2308).
  const counter = (loading || error) ? undefined : isFiltered ? t('analysis-results.query-routing.counter-filtered', { shown: formatCount(filtered.length), total: formatCount((rows || []).length) }) : `(${formatCount((rows || []).length)})`;
  const selectedItems = pageRows.filter(r => r.queryId === selectedId);

  const columnDefinitions = [
    {
      id: 'query',
      header: t('analysis-results.query-routing.col-query'),
      cell: item => (
        <Link
          ariaLabel={t('analysis-results.query-routing.open-label', { id: item.queryId })}
          onFollow={(e) => { e.preventDefault(); onSelect(item.queryId); }}
        >
          <span style={{ fontFamily: 'monospace' }} title={item.queryId}>{formatShortId(item.queryId)}</span>
        </Link>
      ),
      width: 90,
    },
    {
      id: 'operation',
      header: t('analysis-results.query-routing.col-operation'),
      cell: item => item.queryType || '—',
      width: 90,
    },
    {
      id: 'engine',
      header: t('analysis-results.query-routing.col-engine'),
      // An engine's name stays on one line: the badge never wraps to several.
      cell: item => (item.engine ? <span style={{ whiteSpace: 'nowrap' }}><EngineBadge engine={item.engine} /></span> : '—'),
      width: 170,
    },
    {
      id: 'destination',
      header: t('analysis-results.query-routing.col-destination'),
      cell: item => <DestinationTables destinations={item.destinations} />,
      width: 130,
    },
    {
      id: 'cached',
      header: t('analysis-results.query-routing.col-cached'),
      cell: item => (item.cacheEngine ? <span style={{ whiteSpace: 'nowrap' }}><EngineBadge engine={item.cacheEngine} /></span> : '—'),
      width: 130,
    },
    {
      id: 'routing',
      header: t('analysis-results.query-routing.col-routing'),
      cell: item => <RoutingBadges kinds={item.kinds} hasLedger={hasLedger} t={t} />,
      width: 120,
    },
    {
      id: 'tables',
      header: t('analysis-results.query-routing.col-tables'),
      cell: item => (item.tables.length > 0
        ? item.tables.map((name, i) => (
          <span key={name}>{i > 0 && ', '}<WrappableName name={name} /></span>
        ))
        : '—'),
      width: 130,
    },
    {
      id: 'statement',
      header: t('analysis-results.query-routing.col-statement'),
      cell: item => (
        <Box fontSize="body-s">
          <span style={{ fontFamily: 'monospace', overflowWrap: 'break-word' }}>{item.excerpt || '—'}</span>
        </Box>
      ),
      minWidth: 120,
    },
  ];

  return (
    <div id="query-routing-section">
      <Container
        header={(
          <Header
            variant="h2"
            counter={counter}
            description={t('analysis-results.query-routing.description')}
          >
            {t('analysis-results.query-routing.title')}
          </Header>
        )}
      >
        {error ? (
          <Alert type="warning" header={t('analysis-results.query-routing.unavailable-title')}>
            {t('analysis-results.query-routing.unavailable-body')}{' '}
            {t(failureMessageKey(error))}
          </Alert>
        ) : (
          <SpaceBetween size="m">
            {queryPatterns && (
              <Box color="text-body-secondary">
                <span data-figure="query-patterns">
                  {t('analysis-results.executive-summary.query-patterns', {
                    count: queryPatterns.patterns,
                    patterns: queryPatterns.patterns.toLocaleString('en-US'),
                    queries: t('analysis-results.executive-summary.query-patterns-queries', {
                      count: queryPatterns.queries,
                      queries: queryPatterns.queries.toLocaleString('en-US'),
                    }),
                  })}
                </span>
              </Box>
            )}
            <PropertyFilter
              query={query}
              onChange={({ detail }) => { setQuery(detail); setPage(1); }}
              filteringProperties={filteringProperties}
              filteringOptions={filterOptions}
              i18nStrings={i18nStrings}
              customGroupsText={customGroupsText}
              filteringPlaceholder={t('analysis-results.query-routing.filter-placeholder')}
              countText={t('analysis-results.query-routing.count-text', { count: filtered.length, shown: formatCount(filtered.length) })}
              expandToViewport
            />
            {!hasLedger && !loading && (
              <Box color="text-body-secondary">{t('analysis-results.query-trace.no-ledger')}</Box>
            )}
            {truncated && (
              <StatusIndicator type="info">{t('analysis-results.query-routing.truncated', { count: (rows || []).length })}</StatusIndicator>
            )}
            <Table
              variant="embedded"
              ariaLabels={{
                tableLabel: t('analysis-results.query-routing.title'),
                selectionGroupLabel: t('analysis-results.query-routing.selection-group'),
                itemSelectionLabel: (data, item) => t('analysis-results.query-routing.select-item', { id: formatShortId(item.queryId) }),
                resizerRoleDescription: t('analysis-results.query-routing.resizer'),
              }}
              // Column widths apply only to a resizable table. They keep the engine badge
              // on one line, and a reader can widen a column that cuts a statement.
              resizableColumns
              trackBy="queryId"
              loading={loading}
              loadingText={t('analysis-results.query-routing.loading')}
              columnDefinitions={columnDefinitions}
              wrapLines
              selectionType="single"
              selectedItems={selectedItems}
              onSelectionChange={({ detail }) => {
                const picked = detail.selectedItems[0];
                if (picked) onSelect(picked.queryId);
              }}
              items={pageRows}
              empty={<Box textAlign="center" padding="l">{t('analysis-results.query-routing.empty')}</Box>}
            />
            {pagesCount > 1 && (
              <Box float="right">
                <Pagination
                  ariaLabels={{
                    nextPageLabel: t('analysis-results.query-routing.next-page'),
                    previousPageLabel: t('analysis-results.query-routing.previous-page'),
                    pageLabel: n => t('analysis-results.query-routing.page', { page: n }),
                    paginationLabel: t('analysis-results.query-routing.pagination'),
                  }}
                  currentPageIndex={currentPage}
                  pagesCount={pagesCount}
                  onChange={({ detail }) => setPage(detail.currentPageIndex)}
                />
              </Box>
            )}
          </SpaceBetween>
        )}
      </Container>
    </div>
  );
});

export default QueryRoutingTable;
