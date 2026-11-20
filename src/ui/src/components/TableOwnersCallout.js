// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo, useMemo, useState } from 'react';
import Box from "@cloudscape-design/components/box";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Link from "@cloudscape-design/components/link";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import TextFilter from "@cloudscape-design/components/text-filter";
import { displayEngine } from "../utils/engineNames";
import { tableOwnershipOf, tableSourcesOf } from "../utils/reportFacts";
import { tableOwnerRows } from "../utils/tableOwnerRows";
import EngineBadge from "./EngineBadge";

/**
 * "Table owners" (#212, ADR-031 Amendment 9; #2321 folded in "Sources of truth and
 * replicas", #992): one row per mapped source table from report.json's
 * facts.table_owner_lines and facts.table_sources, the same rows the engineering
 * report's Migration map and Sources of truth section state. A row has the table's
 * owner (the engine that takes its writes, its source of truth; a table no query
 * writes, the plan's relational engine (#2166); never a cache), the engines that read
 * a CDC copy of it, the reads a replica serves and the cache in front of its reads.
 * The columns sort and a text filter narrows the rows. The eventual-consistency risk
 * is stated once behind an Info link, in the sentence the engineering report prints
 * (facts.table_sources.shared_risk_sentence; #1170, #1232, #2321). The counts are the
 * facts layer's (ADR-032), each table's replica reads its own owner line's; this only
 * formats them.
 * Collapsed when the page opens.
 *
 * Callers pass `t` (the page's own `useTranslation` function).
 */

const DASH = '—';
const CONSISTENCY_ID = 'table-owners-consistency';

function engineList(engines) {
  const shown = (engines || []).map(displayEngine);
  return shown.length > 0 ? shown.join(', ') : DASH;
}

function badges(engines) {
  if (!engines || engines.length === 0) return DASH;
  // The badges wrap inside the cell: on a phone a second badge on the same line ran
  // under the next column, clipped from view (axe read the next cell's text on it).
  return (
    <span data-badge-list style={{ display: 'inline-flex', flexWrap: 'wrap', gap: 4 }}>
      {engines.map((engine) => (
        <span key={engine}><EngineBadge engine={engine} /></span>
      ))}
    </span>
  );
}

// The owner's name, and "no single writer" for a table whose writes still sit on
// several engines (the engineering report's owner_cell).
function ownerSplitText(row, t) {
  return row.owner ? `(${t('analysis-results.table-owners.split-writes')})` : t('analysis-results.table-owners.split-writes');
}

// The text a row sorts and filters on, one lower-cased string per column.
function searchable(row) {
  return [row.table, row.owner && displayEngine(row.owner), engineList(row.cdcTo), row.cachedBy && displayEngine(row.cachedBy)]
    .filter(Boolean).join(' ').toLowerCase();
}

function compareText(a, b) {
  return String(a).localeCompare(String(b));
}

// A table with no replica read count sorts below every count when the most reads
// come first.
function compareReads(a, b) {
  const x = a.replicaReads === null ? -1 : a.replicaReads;
  const y = b.replicaReads === null ? -1 : b.replicaReads;
  return x - y;
}

// #2166 review: the rule the report's own lines follow (facts.table_owner_lines.
// unwritten_owner_rule): a report saved before #2166 gave a table nothing writes to
// the engine serving most of its queries, so it states that rule above its rows.
export function ruleKey(ownership) {
  return ownership.unwritten_owner_rule === 'most_queries'
    ? 'analysis-results.table-owners.rule-most-queries'
    : 'analysis-results.table-owners.rule';
}

// The risk once for every replicated table that shares it, as
// facts.table_sources.shared_risk_sentence words it (the engineering report prints the
// same sentence, #2321), then any older report's own per-table risk. The Info link
// discloses it in place, so it says whether it is open and what it controls.
function ConsistencyNote({ sources, t }) {
  const [open, setOpen] = useState(false);
  const all = (sources && sources.replicated_tables) || [];
  const shared = sources && sources.shared_risk_sentence;
  // The sentence also covers a table nothing writes that has a CDC copy (#2459), so it
  // decides whether there is a note, with or without a listed written replicated table.
  if (all.length === 0 && !shared) return null;
  const own = all.filter(row => row.shares_replica_risk !== true);
  return (
    <SpaceBetween size="xs">
      <span data-figure="consistency-info">
        <Link
          variant="info"
          ariaLabel={t('analysis-results.table-owners.info-label')}
          nativeAttributes={{ 'aria-expanded': open ? 'true' : 'false', 'aria-controls': CONSISTENCY_ID }}
          onFollow={(e) => { e.preventDefault(); setOpen(value => !value); }}
        >
          {t('analysis-results.table-owners.info')}
        </Link>
      </span>
      <div id={CONSISTENCY_ID}>
        {open && (
          <Box data-figure="consistency">
            <SpaceBetween size="xs">
              <div>{t('analysis-results.sources-of-truth.intro')}</div>
              {shared && <div data-figure="shared-risk">{shared}</div>}
              {own.map(row => (
                <div key={row.table} data-own-risk={row.table}>
                  <span style={{ fontFamily: 'monospace' }}>{row.table}</span>
                  {' '}
                  {row.risk}
                </div>
              ))}
            </SpaceBetween>
          </Box>
        )}
      </div>
    </SpaceBetween>
  );
}

const TableOwnersCallout = memo(({ synthesis, t }) => {
  const ownership = tableOwnershipOf(synthesis);
  const sources = tableSourcesOf(synthesis);
  const rows = useMemo(() => tableOwnerRows(synthesis), [synthesis]);
  const [filteringText, setFilteringText] = useState('');
  const [sorting, setSorting] = useState(null);
  if (!ownership && !sources) return null;

  const owned = Object.entries((ownership && ownership.owned_table_counts) || {});
  const withCache = Boolean(ownership);
  const hasReplicas = Boolean(
    sources && ((sources.replicated_tables || []).length > 0 || sources.shared_risk_sentence)
  );
  const columns = [
    {
      id: 'table',
      header: t('analysis-results.table-owners.col-table'),
      cell: row => <span data-table={row.table} style={{ fontFamily: 'monospace' }}>{row.table}</span>,
      sortingComparator: (a, b) => compareText(a.table, b.table),
    },
    {
      id: 'owner',
      header: t('analysis-results.table-owners.col-owner'),
      cell: row => (
        <span data-cell="owner">
          {row.owner ? <EngineBadge engine={row.owner} /> : null}
          {row.split ? <>{row.owner ? ' ' : ''}{ownerSplitText(row, t)}</> : null}
          {!row.owner && !row.split ? DASH : null}
        </span>
      ),
      sortingComparator: (a, b) => compareText(a.owner ? displayEngine(a.owner) : '', b.owner ? displayEngine(b.owner) : ''),
    },
    {
      id: 'cdc-to',
      header: t('analysis-results.table-owners.col-cdc-to'),
      cell: row => <span data-cell="cdc-to">{badges(row.cdcTo)}</span>,
      sortingComparator: (a, b) => compareText(engineList(a.cdcTo), engineList(b.cdcTo)),
    },
    {
      id: 'replica-reads',
      header: t('analysis-results.table-owners.col-replica-reads'),
      cell: row => <span data-cell="replica-reads">{row.replicaReads === null ? DASH : String(row.replicaReads)}</span>,
      sortingComparator: compareReads,
    },
    ...(withCache ? [{
      id: 'cached-by',
      header: t('analysis-results.table-owners.col-cached-by'),
      cell: row => <span data-cell="cached-by">{row.cachedBy ? <EngineBadge engine={row.cachedBy} /> : DASH}</span>,
      sortingComparator: (a, b) => compareText(a.cachedBy ? displayEngine(a.cachedBy) : '', b.cachedBy ? displayEngine(b.cachedBy) : ''),
    }] : []),
  ];

  const needle = filteringText.trim().toLowerCase();
  const filtered = needle ? rows.filter(row => searchable(row).includes(needle)) : rows;
  let shown = filtered;
  if (sorting) {
    const column = columns.find(c => c.id === sorting.id);
    const sign = sorting.descending ? -1 : 1;
    shown = [...filtered].sort((a, b) => sign * column.sortingComparator(a, b));
  }
  const headerText = ownership
    ? t('analysis-results.table-owners.header', { count: ownership.table_count })
    : t('analysis-results.table-owners.header-written', { count: sources.written_table_count });

  return (
    <div id="table-owners-section">
      {/* #2321: where an old link to the removed Sources of truth section lands. */}
      <div id="sources-of-truth-section">
      <ExpandableSection variant="container" defaultExpanded={false} headerText={headerText}>
        <SpaceBetween size="s">
          {ownership && <Box data-figure="owner-rule">{t(ruleKey(ownership))}</Box>}
          {ownership && (
            <Box data-figure="owned-tables">
              {owned.map(([engine, count]) => (
                <div key={engine} data-engine={engine}>
                  {t('analysis-results.table-owners.owns', { engine: displayEngine(engine), count })}
                </div>
              ))}
              {ownership.no_owner_table_count > 0 && (
                <div data-figure="no-owner-tables">
                  {t('analysis-results.table-owners.no-owner', { count: ownership.no_owner_table_count })}
                </div>
              )}
            </Box>
          )}
          {ownership && (
            <Box data-figure="also-read-tables">
              {t('analysis-results.table-owners.also-read', { count: ownership.also_read_table_count })}
            </Box>
          )}
          {ownership && ownership.cached_table_count > 0 && (
            <Box data-figure="cached-tables">
              {t('analysis-results.table-owners.cached', {
                count: ownership.cached_table_count,
                engines: engineList(ownership.cache_engines),
              })}
            </Box>
          )}
          <TextFilter
            filteringText={filteringText}
            filteringPlaceholder={t('analysis-results.table-owners.filter-placeholder')}
            filteringAriaLabel={t('analysis-results.table-owners.filter-label')}
            countText={needle ? t('analysis-results.table-owners.matches', { count: filtered.length }) : undefined}
            onChange={({ detail }) => setFilteringText(detail.filteringText)}
          />
          <Table
            columnDefinitions={columns}
            items={shown}
            trackBy="table"
            variant="embedded"
            sortingColumn={sorting ? columns.find(c => c.id === sorting.id) : undefined}
            sortingDescending={sorting ? sorting.descending : false}
            onSortingChange={({ detail }) => setSorting({ id: detail.sortingColumn.id, descending: detail.isDescending })}
            empty={<Box textAlign="center" padding="s">{t('analysis-results.table-owners.no-match')}</Box>}
            ariaLabels={{ tableLabel: t('analysis-results.table-owners.table-label') }}
            footer={hasReplicas ? <ConsistencyNote sources={sources} t={t} /> : undefined}
          />
          {sources && !ownership && sources.more_replicated_count > 0 && (
            <Box>{t('analysis-results.sources-of-truth.more', { count: sources.more_replicated_count })}</Box>
          )}
          {sources && !ownership && sources.single_engine_table_count > 0 && (
            <Box data-figure="single-engine-tables">
              {t('analysis-results.sources-of-truth.single', {
                count: sources.single_engine_table_count,
                tables: (sources.single_engine_tables || []).join(', '),
              })}
            </Box>
          )}
          {sources && sources.source_database_table_count > 0 && (
            <Box data-figure="source-database-tables">
              {t('analysis-results.sources-of-truth.source-database', { count: sources.source_database_table_count })}
            </Box>
          )}
          {sources && sources.split_write_table_count > 0 && (
            <Box data-figure="split-write-tables">
              {t('analysis-results.sources-of-truth.split', { count: sources.split_write_table_count })}
            </Box>
          )}
        </SpaceBetween>
      </ExpandableSection>
      </div>
    </div>
  );
});

export default TableOwnersCallout;
