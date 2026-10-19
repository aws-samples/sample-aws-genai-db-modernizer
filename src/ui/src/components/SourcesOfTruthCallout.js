import { memo } from 'react';
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { displayEngine } from "../utils/engineNames";
import { tableSourcesOf } from "../utils/reportFacts";

/**
 * "Sources of truth and replicas" (#992, ADR-031 Amendment 9): each written
 * table whose reads run on another engine, the written tables with no replica
 * and the tables whose writes sit on more than one engine, from report.json's
 * facts.table_sources -- the same rows the engineering report states: the source of truth its writes land on, the CDC direction to
 * its replicas and the eventual-consistency risk counting the reads a replica
 * serves (most replica reads first; the facts name the rest's count). It only
 * formats the facts (ADR-032).
 *
 * Callers pass `t` (the page's own `useTranslation` function), like
 * UnservableQueriesCallout.
 */

function cdcText(row) {
  const replicas = (row.replica_engines || []).map(displayEngine).join(', ');
  return `${displayEngine(row.source_of_truth)} → ${replicas}`;
}

const SourcesOfTruthCallout = memo(({ synthesis, t }) => {
  const ts = tableSourcesOf(synthesis);
  if (!ts) return null;
  return (
    <div id="sources-of-truth-section">
    <Alert
      type="info"
      header={t('analysis-results.sources-of-truth.header', { count: ts.written_table_count })}
    >
      <SpaceBetween size="s">
        <Box>{t('analysis-results.sources-of-truth.intro')}</Box>
        {(ts.replicated_tables || []).map(row => (
          <Box key={row.table}>
            <div data-table={row.table}>
              <span style={{ fontFamily: 'monospace' }}>{row.table}</span>
              {' '}
              {t('analysis-results.sources-of-truth.cdc', { cdc: cdcText(row) })}
            </div>
            <div>{row.risk}</div>
          </Box>
        ))}
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
    </Alert>
    </div>
  );
});

export default SourcesOfTruthCallout;
