import { memo } from 'react';
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Link from "@cloudscape-design/components/link";
import { displayEngine } from "../utils/engineNames";
import { unservableOf } from "../utils/reportFacts";

/**
 * "Queries that cannot be served as designed" (#729, #888): each query no analyzed engine could
 * take, where it landed and why (and each flagged query no kept engine can
 * take, #888, which stays where it is), from report.json's facts.unservable -- the
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

// The sentence for each kind listed (#888): "These" when every row is of that
// kind, "Some" when both are; the same sentences as src/report/unservable.py.
function kindKeys(u) {
  const fallback = u.no_fit_query_count || 0;
  const flagged = u.flagged_query_count || 0;
  const held = u.held_query_count || 0;
  const several = [fallback, flagged, held].filter(n => n > 0).length > 1;
  const which = several ? 'some' : 'all';
  const keys = [];
  if (fallback > 0) keys.push(`analysis-results.unservable.fallback_${which}`);
  if (flagged > 0) keys.push(`analysis-results.unservable.flagged_${which}`);
  if (held > 0) keys.push(`analysis-results.unservable.held_${which}`);
  return keys;
}

const UnservableQueriesCallout = memo(({ synthesis, jobId, onOpen, t }) => {
  const u = unservableOf(synthesis);
  if (!u) return null;
  return (
    <div id="unservable-section">
      <Alert
        type="warning"
        header={t('analysis-results.unservable.header', { count: u.unservable_query_count })}
      >
        <SpaceBetween size="s">
          <Box>{t('analysis-results.unservable.intro')}</Box>
          {kindKeys(u).map(key => <Box key={key}>{t(key)}</Box>)}
          {u.unservable_queries.map(q => (
            <Box key={q.query_id}>
              <SpaceBetween direction="horizontal" size="xs">
                <Link
                  ariaLabel={t('analysis-results.unservable.open-label', { id: q.query_id })}
                  onFollow={(e) => { e.preventDefault(); onOpen(q.query_id); }}
                >
                  <span style={{ fontFamily: 'monospace' }}>{q.query_id}</span>
                </Link>
                <span>{t('analysis-results.unservable.on-engine', { engine: displayEngine(q.engine) })}</span>
              </SpaceBetween>
              <div>{q.explanation}</div>
              {(q.blocking_gates || []).length > 0 && (
                <div>{t('analysis-results.unservable.ruled-out', { gates: gatesText(q) })}</div>
              )}
              <div>
                {q.kind === 'held'
                  ? t('analysis-results.unservable.redesign_held', { hints: (q.redesign_hints || []).join(' ') })
                  : t('analysis-results.unservable.redesign', { engine: displayEngine(q.engine), hints: (q.redesign_hints || []).join(' ') })}
              </div>
            </Box>
          ))}
          {u.more_unservable_count > 0 && (
            <Box>{t('analysis-results.unservable.more', { count: u.more_unservable_count })}</Box>
          )}
          <Box>{t('analysis-results.unservable.rerun')}</Box>
          <Link href={`/analysis/assignments/${jobId}`}>{t('analysis-results.unservable.assignment-link')}</Link>
        </SpaceBetween>
      </Alert>
    </div>
  );
});

export default UnservableQueriesCallout;
