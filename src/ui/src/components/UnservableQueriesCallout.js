// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Link from "@cloudscape-design/components/link";
import { displayEngine } from "../utils/engineNames";
import { unservableOf } from "../utils/reportFacts";
import { rerunLines, scopeOnly } from "../utils/unservable";
import HelpInfoLink from "./HelpInfoLink";

/**
 * "Queries that cannot be served as designed" (#729, #888): each query no analyzed engine could
 * take, where it landed and why (and each flagged query no kept engine can
 * take, #888, which stays where it is, and each query no engine in the customer's
 * engine scope can serve, #1329, which has no engine), from report.json's facts.unservable -- the
 * same list the decision and engineering reports state -- with the gate that
 * ruled out each engine, a redesign hint for those engines and the link to the
 * assignment review. The re-run steps are in the alert's help panel (#2521). Each
 * query opens its "Why this engine" decision trace (`onOpen`). It only formats the
 * facts (ADR-032).
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
        // #2521: the re-run steps are in its help panel, behind the header's Info link.
        header={(
          <span>
            {t('analysis-results.unservable.header', { count: u.unservable_query_count })}
            {' '}
            <HelpInfoLink topic="unservable" t={t} />
          </span>
        )}
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
          {/* #2521: a list of only no-engine-in-scope queries has no assignment review link,
              so it keeps its one short action here; the Info link has the full steps. */}
          {scopeOnly(u) && rerunLines(u, t).map(line => <Box key={line}>{line}</Box>)}
          {!scopeOnly(u) && (
            <Link href={`/analysis/assignments/${jobId}`}>{t('analysis-results.unservable.assignment-link')}</Link>
          )}
        </SpaceBetween>
      </Alert>
    </div>
  );
});

export default UnservableQueriesCallout;
