// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo, useMemo, useState } from 'react';
import Alert from '@cloudscape-design/components/alert';
import Box from '@cloudscape-design/components/box';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import KeyValuePairs from '@cloudscape-design/components/key-value-pairs';
import Link from '@cloudscape-design/components/link';
import Modal from '@cloudscape-design/components/modal';
import Pagination from '@cloudscape-design/components/pagination';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Table from '@cloudscape-design/components/table';
import Tabs from '@cloudscape-design/components/tabs';
import TextFilter from '@cloudscape-design/components/text-filter';
import EngineBadge from './EngineBadge';
import DestinationTables from './DestinationTables';
import ProseText from './ProseText';
import { displayEngine } from '../utils/engineNames';
import { formatCount, formatShortId } from '../utils/format';

/**
 * The Data model tab's source tables (#2446): the access pattern explorer's "By
 * source table" view, kept when its pattern and cache tables folded into the Queries
 * tab's query table. One row per source table an access pattern reads, with its
 * engines, its destination tables, the tables it merges with into one destination and
 * its operations; a text filter on the name and a pager. A row opens its destination
 * designs in a dialog: each destination's keys and indexes and the patterns it serves,
 * whose queries open in the page's query detail panel, with focus moved to it (#2480 review).
 *
 * Rows come from `sourceTableRows` (utils/accessPatterns.js) and only format what the
 * schema designs hold. Every name renders as text. Callers pass `t`.
 */

const PAGE_SIZE = 25;

function keyValue(key) {
  return key ? <><code>{key.attribute_name}</code> ({key.attribute_type})</> : null;
}

function engineLabel(engine, cacheEngine, t) {
  return `${displayEngine(engine)}${engine === cacheEngine ? t('cache-layer.engine-badge-suffix') : ''}`;
}

function DestinationDesign({ group, engine, destTable, patterns, destTableLookup, onSelectQuery, t }) {
  const info = destTableLookup[`${engine}::${destTable}`];
  const def = info && info.def;
  const others = ((info && info.allSourceTables) || []).filter(name => name !== group.table);
  const gsis = (def && (def.gsis || def.gsi_indexes)) || [];
  const shards = def && def.settings ? def.settings.number_of_shards : null;
  const items = [{ label: t('analysis-results.source-table.table'), value: destTable }];
  if (def && def.partition_key) items.push({ label: t('analysis-results.pattern-detail.partition-key'), value: keyValue(def.partition_key) });
  if (def && def.sort_key) items.push({ label: t('analysis-results.pattern-detail.sort-key'), value: keyValue(def.sort_key) });
  if (gsis.length > 0) {
    items.push({ label: t('analysis-results.source-table.gsis'), value: gsis.map(g => g.gsi_name || g.index_name).join(', ') });
  }
  if (shards != null) items.push({ label: t('analysis-results.source-table.shards'), value: formatCount(shards) });
  if (others.length > 0) items.push({ label: t('analysis-results.source-table.consolidates-with'), value: others.join(', ') });

  return (
    <Container>
      <SpaceBetween size="m">
        <KeyValuePairs columns={4} items={items} />
        <Table
          variant="embedded"
          wrapLines
          ariaLabels={{ tableLabel: t('analysis-results.source-table.patterns-label', { table: destTable }) }}
          columnDefinitions={[
            {
              id: 'pattern',
              header: t('analysis-results.explorer.col-pattern'),
              cell: item => <span style={{ fontFamily: 'monospace' }} title={item.id}>{formatShortId(item.id)}</span>,
              width: 110,
            },
            { id: 'operation', header: t('analysis-results.explorer.col-operation'), cell: item => item.operation, width: 130 },
            ...(engine === 'dynamodb' ? [{
              id: 'gsi',
              header: t('analysis-results.pattern-detail.gsi'),
              cell: item => item.gsiName || '—',
              width: 140,
            }] : []),
            {
              id: 'queries',
              header: t('analysis-results.source-table.col-queries'),
              cell: item => (item.queryIds.length > 0 ? (
                <SpaceBetween direction="horizontal" size="xs">
                  {item.queryIds.map(qid => (
                    <Link
                      key={qid}
                      ariaLabel={t('analysis-results.query-routing.open-label', { id: qid })}
                      onFollow={(e) => { e.preventDefault(); onSelectQuery(qid); }}
                    >
                      <span style={{ fontFamily: 'monospace' }}>{formatShortId(qid)}</span>
                    </Link>
                  ))}
                </SpaceBetween>
              ) : '—'),
              width: 200,
            },
            {
              id: 'description',
              header: t('analysis-results.explorer.col-description'),
              cell: item => (item.description ? <ProseText text={item.description} /> : '—'),
            },
          ]}
          items={patterns}
          empty={<Box textAlign="center">{t('analysis-results.source-table.no-patterns')}</Box>}
        />
      </SpaceBetween>
    </Container>
  );
}

export function SourceTableDetail({ group, cacheEngine, destTableLookup, onSelectQuery, t }) {
  const engines = [];
  group.patterns.forEach((ap) => {
    let entry = engines.find(e => e.engine === ap.engine);
    if (!entry) {
      entry = { engine: ap.engine, destinations: [] };
      engines.push(entry);
    }
    let dest = entry.destinations.find(d => d.destTable === ap.destTable);
    if (!dest) {
      dest = { destTable: ap.destTable, patterns: [] };
      entry.destinations.push(dest);
    }
    dest.patterns.push(ap);
  });
  return (
    <Tabs
      ariaLabel={t('analysis-results.source-table.engines-label', { table: group.table })}
      i18nStrings={{
        scrollLeftAriaLabel: t('analysis-results.tabs.scroll-left'),
        scrollRightAriaLabel: t('analysis-results.tabs.scroll-right'),
      }}
      tabs={engines.map(({ engine, destinations }) => ({
        id: engine,
        label: engineLabel(engine, cacheEngine, t),
        content: (
          <SpaceBetween size="l">
            {destinations.map(d => (
              <DestinationDesign
                key={d.destTable}
                group={group}
                engine={engine}
                destTable={d.destTable}
                patterns={d.patterns}
                destTableLookup={destTableLookup}
                onSelectQuery={onSelectQuery}
                t={t}
              />
            ))}
          </SpaceBetween>
        ),
      }))}
    />
  );
}

const SourceTablesSection = memo(({ rows, availability, expectedCount, shownCount, cacheEngine, destTableLookup, onSelectQuery, t }) => {
  const [text, setText] = useState('');
  const [page, setPage] = useState(1);
  const [openTable, setOpenTable] = useState(null);

  const filtered = useMemo(() => {
    const wanted = text.trim().toLowerCase();
    return wanted ? rows.filter(r => r.table.toLowerCase().includes(wanted)) : rows;
  }, [rows, text]);
  const pagesCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const currentPage = Math.min(page, pagesCount);
  const pageRows = filtered.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE);
  const counter = text.trim()
    ? t('analysis-results.source-tables.counter-filtered', { shown: formatCount(filtered.length), total: formatCount(rows.length) })
    : `(${formatCount(rows.length)})`;
  const open = openTable ? rows.find(r => r.table === openTable) : null;

  const columnDefinitions = [
    {
      id: 'table',
      header: t('analysis-results.explorer.col-source-table'),
      cell: item => (
        <Link
          ariaLabel={t('analysis-results.source-tables.open-label', { table: item.table })}
          onFollow={(e) => { e.preventDefault(); setOpenTable(item.table); }}
        >
          <span style={{ overflowWrap: 'anywhere' }}>{item.table}</span>
        </Link>
      ),
      width: 200,
    },
    {
      id: 'engines',
      header: t('analysis-results.explorer.col-engines'),
      cell: item => (
        <SpaceBetween direction="horizontal" size="xxs">
          {item.engines.map(e => <EngineBadge key={e} engine={e}>{engineLabel(e, cacheEngine, t)}</EngineBadge>)}
        </SpaceBetween>
      ),
      width: 200,
    },
    {
      id: 'dest_tables',
      header: t('analysis-results.explorer.col-destination-tables'),
      cell: item => <DestinationTables destinations={item.destinations} />,
      width: 250,
    },
    {
      id: 'merged_with',
      header: t('analysis-results.source-tables.col-merged-with'),
      cell: item => (item.mergedWith.length > 0 ? item.mergedWith.join(', ') : '—'),
      width: 200,
    },
    {
      id: 'operations',
      header: t('analysis-results.explorer.col-operations'),
      cell: item => item.operations.join(', ') || '—',
      width: 200,
    },
  ];

  return (
    <div id="source-tables-section">
      <Container
        header={(
          <Header variant="h2" counter={availability === 'unavailable' ? undefined : counter} description={t('analysis-results.source-tables.description')}>
            {t('analysis-results.source-tables.title')}
          </Header>
        )}
      >
        {availability === 'unavailable' ? (
          <Alert type="warning" header={t('analysis-results.explorer.unavailable-title')}>
            {t('analysis-results.explorer.unavailable-body', { count: expectedCount })}
          </Alert>
        ) : (
          <SpaceBetween size="m">
            {availability === 'partial' && (
              <Alert type="info" header={t('analysis-results.explorer.partial-title')}>
                {t('analysis-results.explorer.partial-body', { count: expectedCount, shown: shownCount })}
              </Alert>
            )}
            <TextFilter
              filteringText={text}
              filteringPlaceholder={t('analysis-results.source-tables.filter-placeholder')}
              filteringAriaLabel={t('analysis-results.source-tables.filter-label')}
              countText={t('analysis-results.query-routing.count-text', { count: filtered.length, shown: formatCount(filtered.length) })}
              onChange={({ detail }) => { setText(detail.filteringText); setPage(1); }}
            />
            <Table
              variant="embedded"
              wrapLines
              ariaLabels={{ tableLabel: t('analysis-results.source-tables.title') }}
              columnDefinitions={columnDefinitions}
              items={pageRows}
              trackBy="table"
              empty={<Box textAlign="center" padding="l">{t('analysis-results.explorer.no-matching-source-tables')}</Box>}
            />
            {pagesCount > 1 && (
              <Box float="right">
                <Pagination
                  ariaLabels={{
                    nextPageLabel: t('analysis-results.query-routing.next-page'),
                    previousPageLabel: t('analysis-results.query-routing.previous-page'),
                    pageLabel: n => t('analysis-results.query-routing.page', { page: n }),
                    paginationLabel: t('analysis-results.source-tables.pagination'),
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
      <Modal
        visible={!!open}
        onDismiss={() => setOpenTable(null)}
        header={open ? t('analysis-results.source-tables.dialog-title', { table: open.table }) : ''}
        closeAriaLabel={t('analysis-results.source-tables.dialog-close')}
        size="max"
      >
        {open && (
          <SourceTableDetail
            group={open}
            cacheEngine={cacheEngine}
            destTableLookup={destTableLookup}
            onSelectQuery={(qid) => { setOpenTable(null); onSelectQuery(qid, { focusPanel: true }); }}
            t={t}
          />
        )}
      </Modal>
    </div>
  );
});

export default SourceTablesSection;
