import { memo } from 'react';
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Link from "@cloudscape-design/components/link";
import { displayEngine } from "../utils/engineNames";
import { unservableOf } from "../utils/reportFacts";

/**
 * "Queries on a fallback engine" (#729): each query no analyzed engine could
 * take, where it landed and why, from report.json's facts.unservable -- the
 * same list the decision and engineering reports state -- with the gate that
 * ruled out each engine, a redesign hint for those engines and the way to
 * re-run (the
 * assignment review). Each query opens its "Why this engine" decision trace
 * (`onOpen`). It only formats the facts (ADR-032).
 *
 * Callers pass `t` (the page's own `useTranslation` function), like
 * QueryRoutingTable.
 */

function gatesText(q) {
  return (q.blocking_gates || [])
    .map(g => `${displayEngine(g.engine)} (${g.gate_text || g.gate})`)
    .join('; ');
}

const UnservableQueriesCallout = memo(({ synthesis, jobId, onOpen, t }) => {
  const u = unservableOf(synthesis);
  if (!u) return null;
  return (
    <Alert
      type="warning"
      header={t('analysis-results-v2.unservable.header', { count: u.unservable_query_count })}
    >
      <SpaceBetween size="s">
        <Box>{t('analysis-results-v2.unservable.intro')}</Box>
        {u.unservable_queries.map(q => (
          <Box key={q.query_id}>
            <SpaceBetween direction="horizontal" size="xs">
              <Link
                ariaLabel={t('analysis-results-v2.unservable.open-label', { id: q.query_id })}
                onFollow={(e) => { e.preventDefault(); onOpen(q.query_id); }}
              >
                <span style={{ fontFamily: 'monospace' }}>{q.query_id}</span>
              </Link>
              <span>{t('analysis-results-v2.unservable.on-engine', { engine: displayEngine(q.engine) })}</span>
            </SpaceBetween>
            <div>{q.explanation}</div>
            {(q.blocking_gates || []).length > 0 && (
              <div>{t('analysis-results-v2.unservable.ruled-out', { gates: gatesText(q) })}</div>
            )}
            <div>{t('analysis-results-v2.unservable.redesign', { engine: displayEngine(q.engine), hints: (q.redesign_hints || []).join(' ') })}</div>
          </Box>
        ))}
        {u.more_unservable_count > 0 && (
          <Box>{t('analysis-results-v2.unservable.more', { count: u.more_unservable_count })}</Box>
        )}
        <Box>{t('analysis-results-v2.unservable.rerun')}</Box>
        <Link href={`/analysis/assignments/${jobId}`}>{t('analysis-results-v2.unservable.assignment-link')}</Link>
      </SpaceBetween>
    </Alert>
  );
});

export default UnservableQueriesCallout;
