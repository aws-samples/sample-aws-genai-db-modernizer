import { memo } from 'react';
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Link from "@cloudscape-design/components/link";
import { displayEngine } from "../utils/engineNames";
import { closeCallsOf } from "../utils/reportFacts";

/**
 * "Close calls" (#730): the queries an engine won by less than the close-call
 * margin over another engine the plan keeps, from report.json's
 * facts.close_calls -- the same pairs the decision and engineering reports
 * list. Each named query opens its "Why this engine" decision trace
 * (`onOpen`, the page's query-journey opener), and the assignment review link
 * is where the customer moves them. It only formats the facts (ADR-032).
 *
 * Callers pass `t` (the page's own `useTranslation` function), like
 * QueryRoutingTable.
 */

function gapText(pair, t) {
  if (pair.min_gap === pair.max_gap) {
    return t('analysis-results-v2.close-calls.gap', { count: pair.max_gap, gap: pair.max_gap });
  }
  return t('analysis-results-v2.close-calls.gap-range', { low: pair.min_gap, high: pair.max_gap });
}

function pairText(pair, margin, t) {
  const engine = displayEngine(pair.engine);
  const alternative = displayEngine(pair.alternative);
  const n = pair.pair_query_count || 0;
  const ties = pair.exact_tie_count || 0;
  const parts = [];
  if (n) {
    parts.push(t('analysis-results-v2.close-calls.pair', { count: n, engine, alternative, margin, gap: gapText(pair, t) }));
    if (ties) parts.push(t('analysis-results-v2.close-calls.ties', { count: ties, engine, alternative }));
  } else {
    parts.push(t('analysis-results-v2.close-calls.ties-only', { count: ties, engine, alternative }));
  }
  if (pair.group_mean_count) {
    parts.push(t('analysis-results-v2.close-calls.groups', { count: pair.group_mean_count }));
  }
  // Both kinds listed: always "every one of them"; otherwise agree with the one count.
  const serve = t('analysis-results-v2.close-calls.can-serve', { count: n && ties ? 2 : (n || ties), alternative });
  return `${parts.join('; ')}. ${serve}`;
}

function headerText(cc, t) {
  const head = t('analysis-results-v2.close-calls.header', { count: cc.close_call_query_count });
  return cc.settled_tie_count
    ? `${head}${t('analysis-results-v2.close-calls.header-ties', { count: cc.settled_tie_count })}`
    : head;
}

const CloseCallCallout = memo(({ synthesis, jobId, onOpen, t }) => {
  const cc = closeCallsOf(synthesis);
  if (!cc) return null;
  return (
    <Alert
      type="info"
      header={headerText(cc, t)}
    >
      <SpaceBetween size="s">
        <Box>{t('analysis-results-v2.close-calls.intro', { margin: cc.margin_points })}</Box>
        {cc.engine_pairs.map(pair => (
          <Box key={`${pair.engine}-${pair.alternative}`}>
            <div>{pairText(pair, cc.margin_points, t)}</div>
            <SpaceBetween direction="horizontal" size="xs">
              <span>{t('analysis-results-v2.close-calls.closest')}</span>
              {(pair.query_ids || []).map(qid => (
                <Link
                  key={qid}
                  ariaLabel={t('analysis-results-v2.close-calls.open-label', { id: qid })}
                  onFollow={(e) => { e.preventDefault(); onOpen(qid); }}
                >
                  <span style={{ fontFamily: 'monospace' }}>{qid}</span>
                </Link>
              ))}
              {pair.more_query_count > 0 && (
                <span>{t('analysis-results-v2.close-calls.more', { count: pair.more_query_count })}</span>
              )}
            </SpaceBetween>
          </Box>
        ))}
        <Box>{t('analysis-results-v2.close-calls.override')}</Box>
        <Link href={`/analysis/assignments/${jobId}`}>{t('analysis-results-v2.close-calls.assignment-link')}</Link>
      </SpaceBetween>
    </Alert>
  );
});

export default CloseCallCallout;
