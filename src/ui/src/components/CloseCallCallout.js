// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo, useMemo, useState } from 'react';
import Box from "@cloudscape-design/components/box";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Link from "@cloudscape-design/components/link";
import Pagination from "@cloudscape-design/components/pagination";
import PropertyFilter from "@cloudscape-design/components/property-filter";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import EngineBadge from "./EngineBadge";
import HelpInfoLink from "./HelpInfoLink";
import { closeCallFilterOptions, closeCallRows, hasListedCloseCalls, unlistedCloseCalls } from "../utils/closeCalls";
import { formatCount, formatFigure, formatShortId } from "../utils/format";
import { propertyFilterStrings } from "../utils/propertyFilterStrings";
import { filterQueryRows } from "../utils/queryRouting";
import { closeCallsOf } from "../utils/reportFacts";
import { displayEngine } from "../utils/engineNames";

/**
 * "Close calls" (#730, #2318): the queries an engine won by less than the close-call
 * margin over another engine the plan keeps, from report.json's facts.close_calls
 * (the figures) and query_traces.close_calls (one row per query), the same pairs the
 * decision and engineering reports list. One table: the query, its statement, the
 * assigned engine, the runner-up, the gap in points and what settled it. A query's id
 * opens its "Why this engine" decision trace (`onOpen`), and the assignment review link
 * is where the customer moves them. It only formats the report (ADR-032).
 *
 * Ids read as in the query routing table (the first eight characters, the whole id in
 * the tooltip and the label). The filter and pager keep a job with hundreds of close
 * calls readable. Callers pass `t` (the page's own `useTranslation` function) and
 * `queries`, the query routing table's rows, for the statements.
 */

const PAGE_SIZE = 25;

const CloseCallCallout = memo(({ synthesis, jobId, onOpen, queries, t }) => {
  const cc = closeCallsOf(synthesis);
  const [query, setQuery] = useState({ tokens: [], operation: 'and' });
  const [page, setPage] = useState(1);
  const rows = useMemo(() => closeCallRows(synthesis, queries), [synthesis, queries]);
  const settledLabel = (kind) => t(`analysis-results.close-calls.settled-by.${kind}`);
  const filterOptions = useMemo(() => closeCallFilterOptions(rows, settledLabel), [rows, t]); // eslint-disable-line react-hooks/exhaustive-deps
  const filteringProperties = useMemo(() => [
    { key: 'engine', propertyLabel: t('analysis-results.close-calls.col-engine'), groupValuesLabel: t('analysis-results.close-calls.group-engine'), operators: ['=', '!='] },
    { key: 'runnerUp', propertyLabel: t('analysis-results.close-calls.col-runner-up'), groupValuesLabel: t('analysis-results.close-calls.group-runner-up'), operators: ['=', '!='] },
    { key: 'settledBy', propertyLabel: t('analysis-results.close-calls.col-settled-by'), groupValuesLabel: t('analysis-results.close-calls.group-settled-by'), operators: ['=', '!='] },
  ], [t]);
  const i18nStrings = useMemo(() => propertyFilterStrings(t, {
    ariaLabel: t('analysis-results.close-calls.filter-label'),
    placeholder: t('analysis-results.close-calls.filter-placeholder'),
  }), [t]);
  if (!cc) return null;

  const filtered = filterQueryRows(rows, query);
  const pagesCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pagesCount);
  const pageRows = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const isFiltered = query.tokens.length > 0;

  // One count, of the table's rows (the ties it lists included). An older report that
  // lists only some ids has no total for the rows, so no counter.
  const listed = hasListedCloseCalls(synthesis);
  const total = formatCount(rows.length);
  let counter;
  if (listed) {
    counter = isFiltered
      ? t('analysis-results.close-calls.counter-filtered', { shown: formatCount(filtered.length), total })
      : `(${total})`;
  }
  const unlisted = unlistedCloseCalls(synthesis);

  const columnDefinitions = [
    {
      id: 'query',
      header: t('analysis-results.close-calls.col-query'),
      cell: item => (
        <Link
          ariaLabel={t('analysis-results.close-calls.open-label', { id: item.queryId })}
          onFollow={(e) => { e.preventDefault(); onOpen(item.queryId); }}
        >
          <span style={{ fontFamily: 'monospace' }} title={item.queryId}>{formatShortId(item.queryId)}</span>
        </Link>
      ),
      width: 110,
    },
    {
      id: 'statement',
      header: t('analysis-results.close-calls.col-statement'),
      cell: item => (
        <Box fontSize="body-s">
          <span style={{ fontFamily: 'monospace', overflowWrap: 'break-word' }}>{item.statement || '—'}</span>
        </Box>
      ),
      minWidth: 160,
    },
    {
      id: 'engine',
      header: t('analysis-results.close-calls.col-engine'),
      cell: item => <span style={{ whiteSpace: 'nowrap' }}><EngineBadge engine={item.engine} /></span>,
      width: 170,
    },
    {
      id: 'runner-up',
      header: t('analysis-results.close-calls.col-runner-up'),
      cell: item => <span style={{ whiteSpace: 'nowrap' }}><EngineBadge engine={item.runnerUp} /></span>,
      width: 170,
    },
    {
      id: 'gap',
      header: t('analysis-results.close-calls.col-gap'),
      cell: item => (typeof item.gap === 'number' ? formatFigure(item.gap, 1) : '—'),
      width: 90,
    },
    {
      id: 'settled-by',
      header: t('analysis-results.close-calls.col-settled-by'),
      cell: item => (item.settledBy ? settledLabel(item.settledBy) : '—'),
      width: 130,
    },
  ];

  return (
    <div id="close-calls-section">
      <Container
        header={(
          <Header
            variant="h2"
            counter={counter}
            info={<HelpInfoLink topic="close-calls" t={t} />}
            description={(
              <>
                {t('analysis-results.close-calls.description', { margin: cc.margin_points })}
                {cc.settled_tie_count > 0 && ` ${t('analysis-results.close-calls.ties-listed', { count: cc.settled_tie_count })}`}
                {' '}
                <Link href={`/analysis/assignments/${jobId}`}>{t('analysis-results.close-calls.assignment-link')}</Link>
              </>
            )}
          >
            {t('analysis-results.close-calls.title')}
          </Header>
        )}
      >
        <SpaceBetween size="m">
          <PropertyFilter
            query={query}
            onChange={({ detail }) => { setQuery(detail); setPage(1); }}
            filteringProperties={filteringProperties}
            filteringOptions={filterOptions}
            i18nStrings={i18nStrings}
            filteringPlaceholder={t('analysis-results.close-calls.filter-placeholder')}
            countText={t('analysis-results.close-calls.count-text', { count: filtered.length, shown: formatCount(filtered.length) })}
          />
          <Table
            variant="embedded"
            ariaLabels={{
              tableLabel: t('analysis-results.close-calls.title'),
              resizerRoleDescription: t('analysis-results.query-routing.resizer'),
            }}
            resizableColumns
            trackBy="queryId"
            columnDefinitions={columnDefinitions}
            wrapLines
            items={pageRows}
            empty={<Box textAlign="center" padding="l">{t('analysis-results.close-calls.empty')}</Box>}
          />
          {unlisted.map(pair => (
            <Box key={`${pair.engine}-${pair.alternative}`} color="text-body-secondary" fontSize="body-s">
              {t('analysis-results.close-calls.unlisted', {
                count: pair.more_query_count,
                engine: displayEngine(pair.engine),
                alternative: displayEngine(pair.alternative),
              })}
            </Box>
          ))}
          {pagesCount > 1 && (
            <Box float="right">
              <Pagination
                ariaLabels={{
                  nextPageLabel: t('analysis-results.query-routing.next-page'),
                  previousPageLabel: t('analysis-results.query-routing.previous-page'),
                  pageLabel: n => t('analysis-results.query-routing.page', { page: n }),
                  paginationLabel: t('analysis-results.close-calls.pagination'),
                }}
                currentPageIndex={currentPage}
                pagesCount={pagesCount}
                onChange={({ detail }) => setPage(detail.currentPageIndex)}
              />
            </Box>
          )}
        </SpaceBetween>
      </Container>
    </div>
  );
});

export default CloseCallCallout;
