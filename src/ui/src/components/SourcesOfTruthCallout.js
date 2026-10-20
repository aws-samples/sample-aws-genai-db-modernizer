import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { displayEngine } from "../utils/engineNames";
import { tableSourcesOf } from "../utils/reportFacts";

/**
 * "Sources of truth and replicas" (#992, ADR-031 Amendment 9): each written
 * table whose reads run on another engine, the written tables with no replica
 * and the tables whose writes sit on more than one engine, from report.json's
 * facts.table_sources -- the same rows the engineering report states: the
 * source of truth its writes land on, the CDC direction to its replicas and the
 * reads a replica serves (most replica reads first; the facts name the rest's
 * count). It only formats the facts (ADR-032).
 *
 * #1232: collapsed when the page opens, like Query routing, and the
 * eventual-consistency risk is stated once for every table whose facts say
 * `shares_replica_risk` (the engineering report's rule since #1170, decided in
 * the facts layer), naming their sources of truth. Each table shows its CDC
 * direction and its replica read count; a row that does not share it (an
 * older report's wording) keeps its own stored risk.
 *
 * Callers pass `t` (the page's own `useTranslation` function), like
 * UnservableQueriesCallout.
 */

function cdcText(row) {
  const replicas = (row.replica_engines || []).map(displayEngine).join(', ');
  return `${displayEngine(row.source_of_truth)} → ${replicas}`;
}

function hasReadCount(row) {
  return typeof row.replica_read_count === 'number';
}

function sharesRisk(row) {
  return row.shares_replica_risk === true;
}

// The risk once for every table that shares it, naming their sources of
// truth in the order the rows list them (the engineering report's
// shared_replica_risk_text).
function sharedRiskText(rows, t) {
  const names = [...new Set(rows.map(row => row.source_of_truth))].map(displayEngine);
  if (names.length === 1) {
    return t('analysis-results.sources-of-truth.risk-one-source', { source: names[0] });
  }
  return t('analysis-results.sources-of-truth.risk-many-sources', {
    sources: names.slice(0, -1).join(', '),
    last: names.slice(-1)[0],
  });
}

const SourcesOfTruthCallout = memo(({ synthesis, t }) => {
  const ts = tableSourcesOf(synthesis);
  if (!ts) return null;
  const rows = ts.replicated_tables || [];
  const shared = rows.filter(sharesRisk);
  const allShared = rows.every(sharesRisk);
  return (
    <div id="sources-of-truth-section">
      <ExpandableSection
        variant="container"
        defaultExpanded={false}
        headerText={t('analysis-results.sources-of-truth.header', { count: ts.written_table_count })}
      >
        <SpaceBetween size="s">
          <Box>{t('analysis-results.sources-of-truth.intro')}</Box>
          {rows.map(row => (
            <Box key={row.table}>
              <div data-table={row.table}>
                <span style={{ fontFamily: 'monospace' }}>{row.table}</span>
                {' '}
                {t('analysis-results.sources-of-truth.cdc', { cdc: cdcText(row) })}
              </div>
              {hasReadCount(row) && (
                <div data-figure="replica-reads">
                  {t('analysis-results.sources-of-truth.replica-reads', { count: row.replica_read_count })}
                </div>
              )}
              {!sharesRisk(row) && <div data-figure="own-risk">{row.risk}</div>}
            </Box>
          ))}
          {shared.length > 0 && (
            <Box data-figure="shared-risk">
              {t(allShared
                ? 'analysis-results.sources-of-truth.risk-lead'
                : 'analysis-results.sources-of-truth.risk-lead-other')}
              {' '}
              {sharedRiskText(shared, t)}
            </Box>
          )}
          {ts.more_replicated_count > 0 && (
            <Box>{t('analysis-results.sources-of-truth.more', { count: ts.more_replicated_count })}</Box>
          )}
          {ts.single_engine_table_count > 0 && (
            <Box data-figure="single-engine-tables">
              {t('analysis-results.sources-of-truth.single', {
                count: ts.single_engine_table_count,
                tables: (ts.single_engine_tables || []).join(', '),
              })}
            </Box>
          )}
          {ts.split_write_table_count > 0 && (
            <Box data-figure="split-write-tables">
              {t('analysis-results.sources-of-truth.split', { count: ts.split_write_table_count })}
            </Box>
          )}
        </SpaceBetween>
      </ExpandableSection>
    </div>
  );
});

export default SourcesOfTruthCallout;
