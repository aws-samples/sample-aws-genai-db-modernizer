import { memo, useEffect, useId, useRef, useState } from 'react';
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import EngineBadge from "./EngineBadge";
import {
  heterogeneousNote, migrationWaves, waveCount, waveGaps, waveHeadline, waveMovesNothing, waveTitle,
} from "../utils/migrationWaves";

/**
 * "Migration roadmap" (#957): report.json's migration waves, the same ones the
 * executive report and the engineering report show, one card per wave
 * with its headline, rationale, gate and tables. It only formats report.json's
 * waves and facts (ADR-032); the headline is the reports' own phrasing
 * (utils/migrationWaves.js).
 *
 * `data-wave` and `data-figure` are stable hooks for the e2e check that this
 * page's waves read the same as the engineering report's (tests/e2e/test_ui.py).
 *
 * The gate shows each of its long lists (table names) as an inline "(…)" that
 * expands in place (#1028). The lists come from the wave's `gate_parts`, which
 * the producer splits out of the gate; a report written before them shows the
 * gate string as it is. Nothing here parses the gate's prose.
 *
 * A cross-engine wave 1 also shows the facts layer's note that it is not yet
 * optimized for heterogeneous migrations, and the relational gaps the check found
 * (#1120), as both reports do.
 *
 * Callers pass `t` (the page's own `useTranslation` function), like
 * QueryRoutingTable.
 */

const INLINE_TOGGLE_STYLE = {
  background: 'none',
  border: 'none',
  padding: 0,
  margin: 0,
  font: 'inherit',
  color: 'inherit',
  cursor: 'pointer',
  textDecoration: 'underline dotted',
};

const HIDE_TOGGLE_STYLE = { ...INLINE_TOGGLE_STYLE, textDecoration: 'none', marginLeft: '0.2em' };

// One list of a gate, collapsed to "(…)" until it is clicked. Open, the list is
// plain text outside the button, so a screen reader reads it, and a small
// "hide" control follows it; focus moves to whichever control is shown.
function InlineList({ items, t }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const showRef = useRef(null);
  const hideRef = useRef(null);
  const toggled = useRef(false);
  useEffect(() => {
    if (!toggled.current) return;
    const target = open ? hideRef.current : showRef.current;
    if (target) target.focus();
  }, [open]);
  const toggle = (next) => { toggled.current = true; setOpen(next); };
  if (!open) {
    return (
      <button
        ref={showRef}
        type="button"
        style={INLINE_TOGGLE_STYLE}
        aria-expanded={false}
        aria-label={t('analysis-results.waves.list-show', { count: items.length })}
        onClick={() => toggle(true)}
      >
        (…)
      </button>
    );
  }
  return (
    <>
      <span id={listId} data-figure="gate-list">{`(${items.join(', ')})`}</span>
      <button
        ref={hideRef}
        type="button"
        data-gate-hide=""
        style={HIDE_TOGGLE_STYLE}
        aria-expanded
        aria-controls={listId}
        aria-label={t('analysis-results.waves.list-hide', { count: items.length })}
        onClick={() => toggle(false)}
      >
        ×
      </button>
    </>
  );
}

function GateText({ wave, t }) {
  const parts = Array.isArray(wave.gate_parts) ? wave.gate_parts : null;
  if (!parts || parts.length === 0) {
    return t('analysis-results.waves.gate', { gate: wave.gate });
  }
  return (
    <>
      {t('analysis-results.waves.gate', { gate: '' })}
      {/* Parts have no id; their order is fixed by the report. */}
      {parts.map((part, i) => (
        <span key={i}>
          {part.text}
          {(part.items || []).length > 0 && <>{' '}<InlineList items={part.items} t={t} /></>}
        </span>
      ))}
    </>
  );
}

const MigrationWavePlan = memo(({ synthesis, t }) => {
  const waves = migrationWaves(synthesis);
  if (waves.length === 0) return null;
  const count = waveCount(synthesis);
  return (
    <div id="migration-roadmap-section">
      <Container
        header={(
          <Header
            variant="h2"
            counter={count === null ? undefined : t('analysis-results.waves.counter', { count })}
            description={t('analysis-results.waves.suggested-path', {
              section: t('analysis-results.executive-summary.title'),
            })}
          >
            {t('analysis-results.waves.title')}
          </Header>
        )}
      >
        <SpaceBetween size="l">
          {waves.map((wave) => (
            <div key={wave.wave} data-wave={wave.wave}>
              <SpaceBetween size="xs">
                <Box variant="h3">
                  {t('analysis-results.waves.wave-title', { wave: wave.wave, title: waveTitle(wave) })}
                </Box>
                <SpaceBetween direction="horizontal" size="xs">
                  {(wave.engines || []).map(engine => (
                    <span key={engine} data-engine={engine}><EngineBadge engine={engine} /></span>
                  ))}
                </SpaceBetween>
                {!waveMovesNothing(synthesis, wave) && (
                  <div data-figure="wave-stats">{waveHeadline(t, synthesis, wave)}</div>
                )}
                {wave.rationale && <Box color="text-body-secondary">{wave.rationale}</Box>}
                {wave.gate && (
                  <div data-figure="wave-gate"><GateText wave={wave} t={t} /></div>
                )}
                {heterogeneousNote(synthesis, wave) && (
                  <div data-figure="wave-heterogeneous-note">{heterogeneousNote(synthesis, wave)}</div>
                )}
                {waveGaps(synthesis, wave).length > 0 && (
                  <div>
                    <Box variant="h4">{t('analysis-results.waves.gaps')}</Box>
                    <ul>
                      {waveGaps(synthesis, wave).map(gap => (
                        <li key={gap.rule_id} data-figure="wave-gap">
                          {t('analysis-results.waves.gap', {
                            feature: gap.feature || gap.rule_id,
                            impact: t(`analysis-results.waves.gap-impact.${gap.impact}`),
                            reason: gap.reason || '',
                          })}
                        </li>
                      ))}
                    </ul>
                  </div>
                )}
                {!waveMovesNothing(synthesis, wave) && (wave.tables || []).length > 0 && (
                  <ExpandableSection headerText={t('analysis-results.waves.tables')} variant="footer">
                    <Box fontSize="body-s"><span data-figure="wave-tables">{wave.tables.join(', ')}</span></Box>
                  </ExpandableSection>
                )}
              </SpaceBetween>
            </div>
          ))}
        </SpaceBetween>
      </Container>
    </div>
  );
});

export default MigrationWavePlan;
