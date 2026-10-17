import { memo } from 'react';
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import Badge from "@cloudscape-design/components/badge";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import { displayEngine } from "../utils/engineNames";
import { migrationWaves, waveCount, waveHeadline, waveTitle } from "../utils/migrationWaves";

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
 * Callers pass `t` (the page's own `useTranslation` function), like
 * QueryRoutingTable.
 */
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
                    <span key={engine} data-engine={engine}><Badge color="blue">{displayEngine(engine)}</Badge></span>
                  ))}
                </SpaceBetween>
                <div data-figure="wave-stats">{waveHeadline(t, synthesis, wave)}</div>
                {wave.rationale && <Box color="text-body-secondary">{wave.rationale}</Box>}
                {wave.gate && (
                  <div data-figure="wave-gate">{t('analysis-results.waves.gate', { gate: wave.gate })}</div>
                )}
                {(wave.tables || []).length > 0 && (
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
