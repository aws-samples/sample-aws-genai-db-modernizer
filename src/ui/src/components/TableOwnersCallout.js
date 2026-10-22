import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Table from "@cloudscape-design/components/table";
import { displayEngine } from "../utils/engineNames";
import { tableOwnershipOf } from "../utils/reportFacts";

/**
 * "Table owners" (#212, ADR-031 Amendment 9): one line per mapped source table
 * from report.json's facts.table_owner_lines -- the same lines the engineering
 * report's Migration map states: its owner (the engine that takes its writes;
 * a table no query writes, the engine serving most of its queries; never a
 * cache), the other engines that also read it through CDC replicas, and the
 * cache in front of its reads. The counts are the facts layer's (ADR-032); this
 * only formats them. Collapsed when the page opens, like Sources of truth.
 *
 * Callers pass `t` (the page's own `useTranslation` function).
 */

const DASH = '—';

function names(engines) {
  const shown = (engines || []).map(displayEngine);
  return shown.length > 0 ? shown.join(', ') : DASH;
}

// The owner, and "no single writer" for a table whose writes still sit on
// several engines (the engineering report's owner_cell).
function ownerText(row, t) {
  const name = row.owner ? displayEngine(row.owner) : DASH;
  if (row.owner_basis !== 'split_writes') return name;
  const split = t('analysis-results.table-owners.split-writes');
  return row.owner ? `${name} (${split})` : split;
}

const TableOwnersCallout = memo(({ synthesis, t }) => {
  const ownership = tableOwnershipOf(synthesis);
  if (!ownership) return null;
  const owned = Object.entries(ownership.owned_table_counts || {});
  const columns = [
    {
      id: 'table',
      header: t('analysis-results.table-owners.col-table'),
      cell: row => <span data-table={row.table} style={{ fontFamily: 'monospace' }}>{row.table}</span>,
    },
    {
      id: 'owner',
      header: t('analysis-results.table-owners.col-owner'),
      cell: row => <span data-cell="owner">{ownerText(row, t)}</span>,
    },
    {
      id: 'also-read-by',
      header: t('analysis-results.table-owners.col-also-read-by'),
      cell: row => <span data-cell="also-read-by">{names(row.also_read_by)}</span>,
    },
    {
      id: 'cached-by',
      header: t('analysis-results.table-owners.col-cached-by'),
      cell: row => <span data-cell="cached-by">{row.cached_by ? displayEngine(row.cached_by) : DASH}</span>,
    },
  ];
  return (
    <div id="table-owners-section">
      <ExpandableSection
        variant="container"
        defaultExpanded={false}
        headerText={t('analysis-results.table-owners.header', { count: ownership.table_count })}
      >
        <SpaceBetween size="s">
          <Box>{t('analysis-results.table-owners.rule')}</Box>
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
          <Box data-figure="also-read-tables">
            {t('analysis-results.table-owners.also-read', { count: ownership.also_read_table_count })}
          </Box>
          {ownership.cached_table_count > 0 && (
            <Box data-figure="cached-tables">
              {t('analysis-results.table-owners.cached', {
                count: ownership.cached_table_count,
                engines: names(ownership.cache_engines),
              })}
            </Box>
          )}
          <Table
            columnDefinitions={columns}
            items={ownership.table_lines || []}
            trackBy="table"
            variant="embedded"
            ariaLabels={{ tableLabel: t('analysis-results.table-owners.table-label') }}
          />
        </SpaceBetween>
      </ExpandableSection>
    </div>
  );
});

export default TableOwnersCallout;
