import { memo, useMemo, useState } from 'react';
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Badge from "@cloudscape-design/components/badge";
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import TextFilter from "@cloudscape-design/components/text-filter";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import Pagination from "@cloudscape-design/components/pagination";
import Link from "@cloudscape-design/components/link";
import Table from "@cloudscape-design/components/table";
import { filterQueryRoutingRows } from "../utils/queryRouting";

/**
 * "Query routing" (#822): every query of the assessment with the engine it is
 * assigned to, and a control per query that opens its journey and "Why this
 * engine" decision trace. Unlike the access-pattern explorer and the
 * trade-off chips, it needs no schema design, so a deterministic run
 * (`--llm-mode none`) or a run whose schema design was skipped reaches every
 * query's trace too.
 *
 * The open control is a Cloudscape Link without an href: a focusable
 * `role="button"` that Enter and Space activate. Rows come from
 * `buildQueryRoutingRows` (utils/queryRouting.js); the routing column only
 * names the facts layer's own reasons a query is listed.
 *
 * Callers pass `t` (the page's own `useTranslation` function), like
 * QueryDecisionTrace.
 */

const PAGE_SIZE = 25;
const SHORT_ID_CHARS = 12;

function shortId(id) {
  return id.length > SHORT_ID_CHARS ? `${id.slice(0, SHORT_ID_CHARS)}…` : id;
}

function RoutingBadges({ kinds, hasLedger, t }) {
  // Without a move ledger the facts list nothing, which says nothing about
  // whether a query kept its first assignment (the trace view's no-ledger note
  // above the table says why).
  if (!hasLedger) return '—';
  if (!kinds) {
    return <Box color="text-body-secondary" fontSize="body-s">{t('analysis-results-v2.query-routing.kept-first')}</Box>;
  }
  const labels = [];
  if (kinds.moved) labels.push(t('analysis-results-v2.query-routing.kind-moved'));
  if (kinds.openTie) labels.push(t('analysis-results-v2.query-routing.kind-open-tie'));
  else if (kinds.tied) labels.push(t('analysis-results-v2.query-routing.kind-tie-settled'));
  if (kinds.overridden) labels.push(t('analysis-results-v2.query-routing.kind-overridden'));
  if (kinds.pinned) labels.push(t('analysis-results-v2.query-routing.kind-pinned'));
  return (
    <SpaceBetween direction="horizontal" size="xxs">
      {labels.map(label => <Badge key={label} color="blue">{label}</Badge>)}
    </SpaceBetween>
  );
}

const QueryRoutingTable = memo(({ rows, loading, error, hasLedger, truncated, onOpen, t }) => {
  const [filterText, setFilterText] = useState('');
  const [mode, setMode] = useState('all');
  const [page, setPage] = useState(1);

  const filtered = useMemo(() => filterQueryRoutingRows(rows || [], filterText, mode), [rows, filterText, mode]);
  const pagesCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pagesCount);
  const pageRows = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const isFiltered = Boolean(filterText.trim()) || mode !== 'all';
  const counter = isFiltered ? `(${filtered.length} of ${(rows || []).length})` : `(${(rows || []).length})`;

  const columnDefinitions = [
    {
      id: 'query',
      header: t('analysis-results-v2.query-routing.col-query'),
      cell: item => (
        <Link
          ariaLabel={t('analysis-results-v2.query-routing.open-label', { id: item.queryId })}
          onFollow={(e) => { e.preventDefault(); onOpen(item.queryId); }}
        >
          <span style={{ fontFamily: 'monospace' }} title={item.queryId}>{shortId(item.queryId)}</span>
        </Link>
      ),
      width: 170,
    },
    {
      id: 'statement',
      header: t('analysis-results-v2.query-routing.col-statement'),
      cell: item => <Box fontSize="body-s"><span style={{ fontFamily: 'monospace' }}>{item.excerpt || '—'}</span></Box>,
    },
    {
      id: 'tables',
      header: t('analysis-results-v2.query-routing.col-tables'),
      cell: item => (item.tables.length > 0 ? item.tables.join(', ') : '—'),
    },
    {
      id: 'engine',
      header: t('analysis-results-v2.query-routing.col-engine'),
      cell: item => item.engineName || '—',
      width: 160,
    },
    {
      id: 'routing',
      header: t('analysis-results-v2.query-routing.col-routing'),
      cell: item => <RoutingBadges kinds={item.kinds} hasLedger={hasLedger} t={t} />,
      width: 220,
    },
  ];

  return (
    <Container
      id="query-routing-section"
      header={
        <Header
          variant="h2"
          counter={counter}
          description={t('analysis-results-v2.query-routing.description')}
        >
          {t('analysis-results-v2.query-routing.title')}
        </Header>
      }
    >
      {error ? (
        <StatusIndicator type="error">{t('analysis-results-v2.query-routing.error', { message: error })}</StatusIndicator>
      ) : (
        <SpaceBetween size="m">
          <SpaceBetween direction="horizontal" size="s">
            <TextFilter
              filteringText={filterText}
              filteringPlaceholder={t('analysis-results-v2.query-routing.filter-placeholder')}
              filteringAriaLabel={t('analysis-results-v2.query-routing.filter-label')}
              onChange={({ detail }) => { setFilterText(detail.filteringText); setPage(1); }}
            />
            <SegmentedControl
              label={t('analysis-results-v2.query-routing.mode-label')}
              selectedId={mode}
              onChange={({ detail }) => { setMode(detail.selectedId); setPage(1); }}
              options={[
                { id: 'all', text: t('analysis-results-v2.query-routing.mode-all') },
                { id: 'listed', text: t('analysis-results-v2.query-routing.mode-listed') },
              ]}
            />
          </SpaceBetween>
          {!hasLedger && !loading && (
            <Box color="text-body-secondary">{t('analysis-results-v2.query-trace.no-ledger')}</Box>
          )}
          {truncated && (
            <StatusIndicator type="info">{t('analysis-results-v2.query-routing.truncated', { count: (rows || []).length })}</StatusIndicator>
          )}
          <Table
            variant="embedded"
            ariaLabels={{ tableLabel: t('analysis-results-v2.query-routing.title') }}
            trackBy="queryId"
            loading={loading}
            loadingText={t('analysis-results-v2.query-routing.loading')}
            columnDefinitions={columnDefinitions}
            items={pageRows}
            empty={<Box textAlign="center" padding="l">{t('analysis-results-v2.query-routing.empty')}</Box>}
          />
          {pagesCount > 1 && (
            <Box float="right">
              <Pagination
                ariaLabels={{
                  nextPageLabel: t('analysis-results-v2.query-routing.next-page'),
                  previousPageLabel: t('analysis-results-v2.query-routing.previous-page'),
                  pageLabel: n => t('analysis-results-v2.query-routing.page', { page: n }),
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
  );
});

export default QueryRoutingTable;
