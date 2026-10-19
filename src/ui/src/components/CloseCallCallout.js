import { memo, useState } from 'react';
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
 * A short banner (#1028): each pair names its first SHOWN_IDS closest queries,
 * then "+N more" (the rest of the ids the facts list) expands to the rest. A
 * link shows the first SHORT_ID_CHARS characters of the id; its tooltip, label
 * and target keep the full id.
 *
 * Callers pass `t` (the page's own `useTranslation` function), like
 * QueryRoutingTable.
 */

const SHOWN_IDS = 3;
const SHORT_ID_CHARS = 6;

const INLINE_TOGGLE_STYLE = {
  background: 'none',
  border: 'none',
  padding: 0,
  margin: 0,
  font: 'inherit',
  color: 'inherit',
  cursor: 'pointer',
  textDecoration: 'underline',
};

function gapText(pair, t) {
  if (pair.min_gap === pair.max_gap) {
    return t('analysis-results.close-calls.gap', { count: pair.max_gap, gap: pair.max_gap });
  }
  return t('analysis-results.close-calls.gap-range', { low: pair.min_gap, high: pair.max_gap });
}

function pairText(pair, margin, t) {
  const engine = displayEngine(pair.engine);
  const alternative = displayEngine(pair.alternative);
  const n = pair.pair_query_count || 0;
  const ties = pair.exact_tie_count || 0;
  const parts = [];
  if (n) {
    parts.push(t('analysis-results.close-calls.pair', { count: n, engine, alternative, margin, gap: gapText(pair, t) }));
    if (ties) parts.push(t('analysis-results.close-calls.ties', { count: ties, engine, alternative }));
  } else {
    parts.push(t('analysis-results.close-calls.ties-only', { count: ties, engine, alternative }));
  }
  if (pair.group_mean_count) {
    parts.push(t('analysis-results.close-calls.groups', { count: pair.group_mean_count }));
  }
  // Both kinds listed: always "every one of them"; otherwise agree with the one count.
  const serve = t('analysis-results.close-calls.can-serve', { count: n && ties ? 2 : (n || ties), alternative });
  return `${parts.join('; ')}. ${serve}`;
}

function headerText(cc, t) {
  const head = t('analysis-results.close-calls.header', { count: cc.close_call_query_count });
  return cc.settled_tie_count
    ? `${head}${t('analysis-results.close-calls.header-ties', { count: cc.settled_tie_count })}`
    : head;
}

function ClosestQueries({ pair, onOpen, t }) {
  const [open, setOpen] = useState(false);
  const ids = pair.query_ids || [];
  const shown = open ? ids : ids.slice(0, SHOWN_IDS);
  const rest = ids.slice(SHOWN_IDS);
  // The toggle stays mounted in the same place, so focus stays on it.
  return (
    <SpaceBetween direction="horizontal" size="xs">
      <span>{t('analysis-results.close-calls.closest')}</span>
      {shown.map(qid => (
        <Link
          key={qid}
          ariaLabel={t('analysis-results.close-calls.open-label', { id: qid })}
          onFollow={(e) => { e.preventDefault(); onOpen(qid); }}
        >
          <span style={{ fontFamily: 'monospace' }} title={qid}>{qid.slice(0, SHORT_ID_CHARS)}</span>
        </Link>
      ))}
      {(open || rest.length === 0) && pair.more_query_count > 0 && (
        <span>{t('analysis-results.close-calls.more', { count: pair.more_query_count })}</span>
      )}
      {rest.length > 0 && (
        <button
          type="button"
          style={INLINE_TOGGLE_STYLE}
          aria-expanded={open}
          aria-label={open ? undefined : t('analysis-results.close-calls.show-more-label', { count: rest.length })}
          onClick={() => setOpen(v => !v)}
        >
          {open
            ? t('analysis-results.close-calls.show-fewer')
            : t('analysis-results.close-calls.show-more', { count: rest.length })}
        </button>
      )}
    </SpaceBetween>
  );
}

const CloseCallCallout = memo(({ synthesis, jobId, onOpen, t }) => {
  const cc = closeCallsOf(synthesis);
  if (!cc) return null;
  return (
    <div id="close-calls-section">
      <Alert
        type="info"
        header={headerText(cc, t)}
      >
        <SpaceBetween size="s">
          <Box>{t('analysis-results.close-calls.intro', { margin: cc.margin_points })}</Box>
          {cc.engine_pairs.map(pair => (
            <Box key={`${pair.engine}-${pair.alternative}`}>
              <div>{pairText(pair, cc.margin_points, t)}</div>
              <ClosestQueries pair={pair} onOpen={onOpen} t={t} />
            </Box>
          ))}
          <Box>{t('analysis-results.close-calls.override')}</Box>
          <Link href={`/analysis/assignments/${jobId}`}>{t('analysis-results.close-calls.assignment-link')}</Link>
        </SpaceBetween>
      </Alert>
    </div>
  );
});

export default CloseCallCallout;
