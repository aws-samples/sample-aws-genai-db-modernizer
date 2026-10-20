import { memo, useMemo, useState } from 'react';
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Badge from "@cloudscape-design/components/badge";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Select from "@cloudscape-design/components/select";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import TextFilter from "@cloudscape-design/components/text-filter";
import SegmentedControl from "@cloudscape-design/components/segmented-control";
import Pagination from "@cloudscape-design/components/pagination";
import Link from "@cloudscape-design/components/link";
import Table from "@cloudscape-design/components/table";
import { filterQueryRoutingRows, queryRoutingFilterOptions } from "../utils/queryRouting";

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
 * The section opens collapsed (#1028), below the query flow. Inside it, the
 * table is the query explorer every run has, with or without schema design:
 * text search (id, statement, table, engine), plus a query type filter and an
 * engine filter whose options are the values the rows hold.
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

const QueryRoutingTable = memo(({ rows, loading, error, hasLedger, truncated, onOpen, t }) => {
  const [filterText, setFilterText] = useState('');
  const [mode, setMode] = useState('all');
  const [page, setPage] = useState(1);
  const [queryType, setQueryType] = useState(null);
  const [engine, setEngine] = useState(null);

  const options = useMemo(() => queryRoutingFilterOptions(rows || []), [rows]);
  const filtered = useMemo(
    () => filterQueryRoutingRows(rows || [], filterText, mode, { queryType, engine }),
    [rows, filterText, mode, queryType, engine],
  );
  const pagesCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pagesCount);
  const pageRows = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const isFiltered = Boolean(filterText.trim()) || mode !== 'all' || Boolean(queryType) || Boolean(engine);
  const anyType = { value: '', label: t('analysis-results.query-routing.type-any') };
  const typeOptions = [anyType, ...options.queryTypes.map(q => ({ value: q, label: q }))];
  const anyEngine = { value: '', label: t('analysis-results.query-routing.engine-any') };
  const engineOptions = [anyEngine, ...options.engines.map(e => ({ value: e.engine, label: e.engineName }))];
  const counter = isFiltered ? `(${filtered.length} of ${(rows || []).length})` : `(${(rows || []).length})`;

  // #1244: Engine and Routing, the columns a reader opens this table for, come right
  // after the query id, and long text wraps (the Table's wrapLines) instead of widening
  // the table past its card: at 1440 px the whole table fits with no horizontal scroll.
  // The id column stays pinned for narrower windows.
  const columnDefinitions = [
    {
      id: 'query',
      header: t('analysis-results.query-routing.col-query'),
      cell: item => (
        <Link
          ariaLabel={t('analysis-results.query-routing.open-label', { id: item.queryId })}
          onFollow={(e) => { e.preventDefault(); onOpen(item.queryId); }}
        >
          <span style={{ fontFamily: 'monospace' }} title={item.queryId}>{shortId(item.queryId)}</span>
        </Link>
      ),
      width: 150,
    },
    {
      id: 'engine',
      header: t('analysis-results.query-routing.col-engine'),
      cell: item => item.engineName || '—',
      width: 150,
    },
    {
      id: 'routing',
      header: t('analysis-results.query-routing.col-routing'),
      cell: item => <RoutingBadges kinds={item.kinds} hasLedger={hasLedger} t={t} />,
      width: 190,
    },
    {
      id: 'type',
      header: t('analysis-results.query-routing.col-type'),
      cell: item => item.queryType || '—',
      width: 100,
    },
    {
      id: 'tables',
      header: t('analysis-results.query-routing.col-tables'),
      cell: item => (item.tables.length > 0 ? item.tables.join(', ') : '—'),
      width: 230,
    },
    {
      id: 'statement',
      header: t('analysis-results.query-routing.col-statement'),
      cell: item => (
        <Box fontSize="body-s">
          <span style={{ fontFamily: 'monospace', overflowWrap: 'anywhere' }}>{item.excerpt || '—'}</span>
        </Box>
      ),
    },
  ];

  return (
    <div id="query-routing-section">
      <ExpandableSection
        variant="container"
        defaultExpanded={false}
        headerText={t('analysis-results.query-routing.title')}
        headerCounter={counter}
        headerDescription={t('analysis-results.query-routing.description')}
      >
        {error ? (
          <StatusIndicator type="error">{t('analysis-results.query-routing.error', { message: error })}</StatusIndicator>
        ) : (
          <SpaceBetween size="m">
            <SpaceBetween direction="horizontal" size="s">
              <TextFilter
                filteringText={filterText}
                filteringPlaceholder={t('analysis-results.query-routing.filter-placeholder')}
                filteringAriaLabel={t('analysis-results.query-routing.filter-label')}
                onChange={({ detail }) => { setFilterText(detail.filteringText); setPage(1); }}
              />
              <Select
                ariaLabel={t('analysis-results.query-routing.type-label')}
                selectedOption={typeOptions.find(o => o.value === (queryType || '')) || anyType}
                options={typeOptions}
                onChange={({ detail }) => { setQueryType(detail.selectedOption.value || null); setPage(1); }}
              />
              <Select
                ariaLabel={t('analysis-results.query-routing.engine-label')}
                selectedOption={engineOptions.find(o => o.value === (engine || '')) || anyEngine}
                options={engineOptions}
                onChange={({ detail }) => { setEngine(detail.selectedOption.value || null); setPage(1); }}
              />
              <SegmentedControl
                label={t('analysis-results.query-routing.mode-label')}
                selectedId={mode}
                onChange={({ detail }) => { setMode(detail.selectedId); setPage(1); }}
                options={[
                  { id: 'all', text: t('analysis-results.query-routing.mode-all') },
                  { id: 'listed', text: t('analysis-results.query-routing.mode-listed') },
                ]}
              />
            </SpaceBetween>
            {!hasLedger && !loading && (
              <Box color="text-body-secondary">{t('analysis-results.query-trace.no-ledger')}</Box>
            )}
            {truncated && (
              <StatusIndicator type="info">{t('analysis-results.query-routing.truncated', { count: (rows || []).length })}</StatusIndicator>
            )}
            <Table
              variant="embedded"
              ariaLabels={{ tableLabel: t('analysis-results.query-routing.title') }}
              trackBy="queryId"
              loading={loading}
              loadingText={t('analysis-results.query-routing.loading')}
              columnDefinitions={columnDefinitions}
              wrapLines
              stickyColumns={{ first: 1 }}
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
      </ExpandableSection>
    </div>
  );
});

export default QueryRoutingTable;
