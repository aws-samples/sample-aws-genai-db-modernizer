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
 * take, #888, which stays where it is, and each query no engine in the customer's
 * engine scope can serve, #1329, which has no engine), from report.json's facts.unservable -- the
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
  // #1329: no engine in the customer's engine scope can serve them.
  const scoped = u.no_engine_in_scope_query_count || 0;
  const several = [fallback, flagged, held, scoped].filter(n => n > 0).length > 1;
  const which = several ? 'some' : 'all';
  const keys = [];
  // A list of only no-engine-in-scope queries says it in its intro (scopeOnly).
  if (scopeOnly(u)) return keys;
  if (fallback > 0) keys.push(`analysis-results.unservable.fallback_${which}`);
  if (flagged > 0) keys.push(`analysis-results.unservable.flagged_${which}`);
  if (held > 0) keys.push(`analysis-results.unservable.held_${which}`);
  if (scoped > 0) keys.push('analysis-results.unservable.scope_some');
  return keys;
}

// #1329: every listed query has no engine in the customer's engine scope. The callout
// then opens with their own intro and never points at the assignment review, which
// refuses to edit them; the same rule as src/report/unservable.py.
function scopeOnly(u) {
  return (u.no_engine_in_scope_query_count || 0) > 0
    && !(u.no_fit_query_count || u.flagged_query_count || u.held_query_count);
}

function engineList(engines) {
  const names = engines.map(displayEngine);
  if (names.length < 2) return names.join('');
  return `${names.slice(0, -1).join(', ')} and ${names.slice(-1)[0]}`;
}

function rerunKeys(u, t) {
  const lines = [];
  if (!scopeOnly(u)) lines.push(t('analysis-results.unservable.rerun'));
  if ((u.no_engine_in_scope_query_count || 0) > 0) {
    const engines = u.no_engine_in_scope_closest_engines || [];
    lines.push(engines.length > 0
      ? t('analysis-results.unservable.rerun_scope', { engines: engineList(engines) })
      : t('analysis-results.unservable.rerun_scope_wider'));
  }
  return lines;
}

// Where a query is (#1329: a query with no engine in the scope is on none, and names
// the engine it would fall back to, which the scope leaves out).
function engineText(q, t) {
  if (q.kind === 'no_engine_in_scope') {
    return q.closest_engine
      ? t('analysis-results.unservable.no-engine', { engine: displayEngine(q.closest_engine) })
      : t('analysis-results.unservable.no-engine-in-scope');
  }
  return t('analysis-results.unservable.on-engine', { engine: displayEngine(q.engine) });
}

function redesignText(q, t) {
  const hints = (q.redesign_hints || []).join(' ');
  if (q.kind === 'held') return t('analysis-results.unservable.redesign_held', { hints });
  if (q.kind === 'no_engine_in_scope') {
    return q.closest_engine
      ? t('analysis-results.unservable.redesign_scope', { engine: displayEngine(q.closest_engine), hints })
      : t('analysis-results.unservable.redesign_scope_wider', { hints });
  }
  return t('analysis-results.unservable.redesign', { engine: displayEngine(q.engine), hints });
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
          <Box>{t(scopeOnly(u) ? 'analysis-results.unservable.scope_intro' : 'analysis-results.unservable.intro')}</Box>
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
                <span data-figure="unservable-engine">{engineText(q, t)}</span>
              </SpaceBetween>
              <div>{q.explanation}</div>
              {(q.blocking_gates || []).length > 0 && (
                <div>{t('analysis-results.unservable.ruled-out', { gates: gatesText(q) })}</div>
              )}
              <div>{redesignText(q, t)}</div>
            </Box>
          ))}
          {u.more_unservable_count > 0 && (
            <Box>{t('analysis-results.unservable.more', { count: u.more_unservable_count })}</Box>
          )}
          {rerunKeys(u, t).map(line => <Box key={line}>{line}</Box>)}
          {!scopeOnly(u) && (
            <Link href={`/analysis/assignments/${jobId}`}>{t('analysis-results.unservable.assignment-link')}</Link>
          )}
        </SpaceBetween>
      </Alert>
    </div>
  );
});

export default UnservableQueriesCallout;
