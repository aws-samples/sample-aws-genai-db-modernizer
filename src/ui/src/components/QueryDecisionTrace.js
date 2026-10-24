import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Badge from "@cloudscape-design/components/badge";
import { pinnedFitLabel } from "../utils/queryRouting";

/**
 * "Why this engine" (#339): one query's decision trace, from
 * `GET /assessments/{job_id}/graph/queries/{query_id}/trace`
 * (`QueryTraceResponse`, built by `src/shared/query_trace.py`, the same
 * builder that writes report.json's `query_traces` for the engineering
 * report's appendix, so the two never disagree).
 *
 * It shows the first assignment with every engine's score or gate, each later
 * Move with its version and rendered reason, the final engine, the runner-up
 * (an engine of the final plan) with its gap or gate, the engines that scored
 * higher with the step that decided against each, an open tie, and the keys
 * the redesign considered for the query, accepted or refused (#1361). Every
 * sentence is the server's (`src/shared/move_reasons.py`); this component only
 * lays them out, and React escapes every string it renders.
 *
 * A job written before the move ledger has no steps (`has_ledger: false`):
 * only the final engine is shown, with a note saying why.
 *
 * Callers pass `t` (the page's own `useTranslation` function), like
 * SummaryFallbackNotice takes already-translated text.
 */

const SCORE_PLACES = 1;

// A score as the trace prints it: 82, 82.5, or an em dash when there is none.
export function formatTraceScore(score) {
  if (typeof score !== 'number' || Number.isNaN(score)) return '—';
  return String(Number(score.toFixed(SCORE_PLACES)));
}

function stepKey(step) {
  return `${step.version}-${step.seq}`;
}

function CandidateTable({ candidates, t }) {
  if (!candidates || candidates.length === 0) return null;
  return (
    <table
      className="query-trace-candidates"
      aria-label={t('analysis-results.query-trace.candidates-label')}
      style={{ borderCollapse: 'collapse', width: '100%' }}
    >
      <thead>
        <tr>
          <th scope="col" style={{ textAlign: 'left', padding: '4px 8px' }}>{t('analysis-results.query-trace.col-engine')}</th>
          <th scope="col" style={{ textAlign: 'right', padding: '4px 8px' }}>{t('analysis-results.query-trace.col-score')}</th>
          <th scope="col" style={{ textAlign: 'left', padding: '4px 8px' }}>{t('analysis-results.query-trace.col-gate')}</th>
        </tr>
      </thead>
      <tbody>
        {candidates.map((c) => (
          <tr key={c.engine} data-engine={c.engine}>
            <td style={{ padding: '4px 8px' }}>{c.engine_name}</td>
            <td style={{ padding: '4px 8px', textAlign: 'right' }}>{formatTraceScore(c.score)}</td>
            <td style={{ padding: '4px 8px' }}>{c.gate_text || '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function StepLabel({ step, t }) {
  return (
    <Box variant="awsui-key-label">
      {t('analysis-results.query-trace.step-label', { step: step.step, version: step.version, rule: step.rule })}
    </Box>
  );
}

// #1361: each column the redesign considered as a key for the query, accepted
// or refused, with the server's sentence naming every guard that refused it.
function KeyCandidates({ candidates, t }) {
  if (!candidates || candidates.length === 0) return null;
  return (
    <section data-testid="query-trace-key-candidates">
      <Box variant="h3">{t('analysis-results.query-trace.key-candidates')}</Box>
      <Box fontSize="body-s" color="text-body-secondary">
        {t('analysis-results.query-trace.key-candidates-intro')}
      </Box>
      <ul style={{ margin: 0, paddingLeft: '20px' }}>
        {candidates.map((k) => (
          <li key={`${k.table_id}:${k.column}:${k.via}`} data-column={k.column} data-outcome={k.outcome}>
            <code>{k.column}</code>{' '}
            <Badge color={k.outcome === 'accepted' ? 'green' : 'grey'}>
              {t(k.outcome === 'accepted'
                ? 'analysis-results.query-trace.key-accepted'
                : 'analysis-results.query-trace.key-refused')}
            </Badge>
            <Box>{k.explanation}</Box>
          </li>
        ))}
      </ul>
    </section>
  );
}

// The rule of the step that decided against an engine: the query's own step
// number (`decided_by_step`), never the ledger's version-wide seq.
function ruleOfStep(trace, stepNumber) {
  const steps = [trace.first_assignment, ...(trace.moves || [])].filter(Boolean);
  const step = steps.find((s) => s.step === stepNumber);
  return step ? step.rule : '';
}

const QueryDecisionTrace = memo(({ trace, loading, error, t }) => {
  if (loading) {
    return (
      <Box textAlign="center" padding="l">
        <StatusIndicator type="loading">{t('analysis-results.query-trace.loading')}</StatusIndicator>
      </Box>
    );
  }
  if (error) {
    return (
      <Box padding="s" data-testid="query-trace-error">
        <StatusIndicator type="error">{t('analysis-results.query-trace.error', { message: error })}</StatusIndicator>
      </Box>
    );
  }
  if (!trace) {
    return (
      <Box padding="s" color="text-body-secondary">{t('analysis-results.query-trace.empty')}</Box>
    );
  }

  // A utility pin shows its pin, not its score (#1373).
  const pinLabel = pinnedFitLabel(trace.pinned, t);
  const finalLine = !trace.final_engine_name
    ? '—'
    : pinLabel
      ? t('analysis-results.query-trace.final-value-pinned', {
          engine: trace.final_engine_name,
          pin: pinLabel,
        })
      : t('analysis-results.query-trace.final-value', {
          engine: trace.final_engine_name,
          score: formatTraceScore(trace.final_score),
        });

  if (!trace.has_ledger) {
    return (
      <SpaceBetween size="s">
        <Box data-testid="query-trace-no-ledger" color="text-body-secondary">
          {t('analysis-results.query-trace.no-ledger')}
        </Box>
        <div>
          <Box variant="awsui-key-label">{t('analysis-results.query-trace.final')}</Box>
          <Box data-testid="query-trace-final">{trace.final_engine_name || '—'}</Box>
        </div>
        <KeyCandidates candidates={trace.key_candidates} t={t} />
      </SpaceBetween>
    );
  }

  const first = trace.first_assignment;
  const moves = trace.moves || [];
  const runner = trace.runner_up;
  const outscored = trace.outscored_by || [];
  const tied = trace.tied_engine_names || [];

  return (
    <SpaceBetween size="m">
      {trace.summary && <Box data-testid="query-trace-summary">{trace.summary}</Box>}

      {first && (
        <section data-testid="query-trace-first">
          <Box variant="h3">{t('analysis-results.query-trace.first-assignment', { version: first.version })}</Box>
          <StepLabel step={first} t={t} />
          <Box>{first.explanation}</Box>
          <CandidateTable candidates={first.candidates} t={t} />
        </section>
      )}

      <section data-testid="query-trace-moves">
        <Box variant="h3">{t('analysis-results.query-trace.later-steps')}</Box>
        {moves.length > 0 ? (
          <ol style={{ margin: 0, paddingLeft: '20px' }}>
            {moves.map((step) => (
              <li key={stepKey(step)} data-version={step.version}>
                <StepLabel step={step} t={t} />
                <Box>{step.explanation}</Box>
              </li>
            ))}
          </ol>
        ) : (
          <Box color="text-body-secondary">{t('analysis-results.query-trace.no-later-steps')}</Box>
        )}
      </section>

      <section data-testid="query-trace-final">
        <Box variant="h3">{t('analysis-results.query-trace.final')}</Box>
        <Box>
          {finalLine}{' '}
          {trace.final_version != null && (
            <Badge>{t('analysis-results.query-trace.as-of-version', { version: trace.final_version })}</Badge>
          )}
          {trace.effective_version != null && trace.effective_version !== trace.final_version && (
            <>
              {' '}
              <Badge color="grey">
                {t('analysis-results.query-trace.effective-version', { version: trace.effective_version })}
              </Badge>
            </>
          )}
        </Box>
      </section>

      {runner && (
        <section data-testid="query-trace-runner-up">
          <Box variant="h3">{t('analysis-results.query-trace.runner-up')}</Box>
          <Box>
            {runner.explanation}{' '}
            {typeof runner.gap === 'number' && runner.gap > 0 && (
              <Badge color="blue">
                {t('analysis-results.query-trace.gap', { count: runner.gap, gap: formatTraceScore(runner.gap) })}
              </Badge>
            )}
          </Box>
          {runner.basis === 'group_mean' && (
            <Box fontSize="body-s" color="text-body-secondary">
              {t('analysis-results.query-trace.group-basis', {
                final: formatTraceScore(trace.final_group_mean ?? runner.final_score),
              })}
            </Box>
          )}
        </section>
      )}

      {outscored.length > 0 && (
        <section data-testid="query-trace-outscored">
          <Box variant="h3">{t('analysis-results.query-trace.outscored')}</Box>
          <ul style={{ margin: 0, paddingLeft: '20px' }}>
            {outscored.map((o) => (
              <li key={o.engine} data-engine={o.engine}>
                <Box>{o.explanation}</Box>
                <Box fontSize="body-s" color="text-body-secondary">
                  {t('analysis-results.query-trace.decided-by', {
                    version: o.decided_by_version,
                    step: o.decided_by_step,
                    rule: ruleOfStep(trace, o.decided_by_step),
                  })}
                </Box>
              </li>
            ))}
          </ul>
        </section>
      )}

      {tied.length > 0 && (
        <section data-testid="query-trace-tie">
          <Box variant="h3">{t('analysis-results.query-trace.open-tie')}</Box>
          <Box>
            {t('analysis-results.query-trace.open-tie-text', {
              engines: tied.join(', '),
              engine: trace.final_engine_name,
            })}
          </Box>
        </section>
      )}

      <KeyCandidates candidates={trace.key_candidates} t={t} />
    </SpaceBetween>
  );
});

QueryDecisionTrace.displayName = 'QueryDecisionTrace';

export default QueryDecisionTrace;
