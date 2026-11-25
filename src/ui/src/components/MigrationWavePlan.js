// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo, useState } from 'react';
import Container from "@cloudscape-design/components/container";
import Header from "@cloudscape-design/components/header";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Badge from "@cloudscape-design/components/badge";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import EngineBadge from "./EngineBadge";
import AdoptionPathPanel from "./AdoptionPathPanel";
import WaveStages from "./WaveStages";
import GateChecklist, { TableNameList } from "./GateChecklist";
import RoutineMigrationSection from "./RoutineMigrationSection";
import EngineChoicesNote from "./EngineChoicesNote";
import HelpInfoLink from "./HelpInfoLink";
import {
  heterogeneousNote, migrationWaves, readinessText, readinessType, waveCount, waveGaps, waveHeadline,
  waveGateConditions, waveMovesNothing, waveReadiness, waveTitle,
} from "../utils/migrationWaves";
import { cutoverResponsibilityOf } from "../utils/reportFacts";
import {
  adoptionOf, defaultPathId, findPath, oneStepPointerOf, pathLabel, pathLeavesWaveOut, pathStopsInWave,
  selectorPaths, slicePlanOf, wavePhase,
} from "../utils/adoptionPaths";

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
 * #2287: the gate is a checklist (GateChecklist), one item per condition the facts cut it
 * into (facts.waves[].gate_conditions), each table list behind "Show N tables". The
 * wave's tables are a list with a filter on the name. Nothing here parses the gate's prose.
 *
 * A cross-engine wave 1 also shows the facts layer's note that it is not yet
 * optimized for heterogeneous migrations, and the relational gaps the check found
 * (#1120), as both reports do.
 *
 * Each card also states whether the plan suggests the wave, with what holds it and the
 * next step (facts.waves[].readiness_line, #1992), and the rule is stated once.
 *
 * The plan states once that the cutover is the customer's (facts.cutover_responsibility,
 * #2107), as both reports do, in the roadmap header's description (#2318).
 *
 * #2318: a wave's readiness is a status ("Suggested, 5 warnings to accept") and the
 * facts' readiness line, shown whole as the reports print it. The status is in the wave's
 * header and the line in its body, so the status is stated once (#2287). Wave 1 also holds what the plan has to do before it, in the plan itself: the
 * routines (facts.routines) and the engine choices made automatically.
 *
 * #1132: above the wave cards, the adoption paths (AdoptionPathPanel): a selector, the
 * selected path's tiles and the one-step option; each card states its steps and stages
 * (WaveStages). The full target is selected when the page opens, so the default view is
 * today's; an earlier path greys out the waves and stages it leaves out. The selection is
 * this component's own state: it changes what is shown and is never sent anywhere. A
 * report without facts.adoption shows no selector and greys nothing.
 *
 * #2286: each wave is a collapsed container section ("Wave 3: ..."). Its header holds the
 * engines, the headline with the query and table counts, and the readiness status, so
 * the shape of the plan reads with every wave closed; open, it holds the rest, and its
 * steps and stages nest inside as collapsed sections of their own (WaveStages).
 *
 * Callers pass `t` (the page's own `useTranslation` function), like
 * QueryRoutingTable, and may pass `schemaSectionId`, the target schemas' section the
 * one-step option links to.
 */

// The wave's readiness line, whole, as every report prints it. Its status is in the
// wave's header (WaveHeaderFacts), stated once (#2287).
function WaveReadiness({ view, t }) {
  return (
    <div data-figure="wave-readiness" data-readiness={view.readiness || ''}>
      <SpaceBetween size="xxs">
        <Box variant="awsui-key-label">{t('analysis-results.waves.readiness-label')}</Box>
        <Box><span data-figure="wave-readiness-line">{view.line}</span></Box>
      </SpaceBetween>
    </div>
  );
}

// The wave's gate as a checklist of the facts' conditions (#2287), under its heading.
function WaveGate({ synthesis, wave, t }) {
  const conditions = waveGateConditions(synthesis, wave);
  if (conditions.length === 0) return null;
  const heading = t('analysis-results.waves.gate-heading');
  return (
    <div data-figure="wave-gate-block">
      <SpaceBetween size="xxs">
        <Box variant="h4">{heading}</Box>
        <GateChecklist conditions={conditions} t={t} label={heading} figure="wave-gate" />
      </SpaceBetween>
    </div>
  );
}

// What a collapsed wave shows in its header (#2286), so the plan's shape reads without
// opening it: the wave's engines, the reports' headline (its query and table counts) and
// the readiness status, the wave's gate state. A wave the selected path leaves out says
// so here too. Every figure is report.json's, as the body and the reports print it.
function WaveHeaderFacts({ synthesis, wave, readiness, leftOut, t }) {
  const status = readiness ? readinessText(readiness, t) : null;
  return (
    <SpaceBetween size="xxs">
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        {(wave.engines || []).map(engine => (
          <span key={engine} data-engine={engine}><EngineBadge engine={engine} /></span>
        ))}
        {status && (
          <span data-figure="wave-header-readiness" data-readiness={readiness.readiness || ''}>
            <Box variant="span" color="text-body-secondary">{t('analysis-results.waves.header-readiness')}</Box>
            {' '}
            <StatusIndicator type={readinessType(readiness)}>{status}</StatusIndicator>
          </span>
        )}
        {leftOut && (
          <span data-figure="wave-left-out-badge">
            <Badge color="grey">{t('analysis-results.adoption.stage-left-out')}</Badge>
          </span>
        )}
      </SpaceBetween>
      {!waveMovesNothing(synthesis, wave) && (
        <Box variant="span"><span data-figure="wave-stats">{waveHeadline(t, synthesis, wave)}</span></Box>
      )}
    </SpaceBetween>
  );
}

// A wave the selected path leaves out: marked by a dashed rule and a full-contrast
// badge, never faded (WCAG AA contrast, review of #2249).
const LEFT_OUT_STYLE = { borderLeft: '3px dashed currentColor', paddingLeft: '12px' };

const MigrationWavePlan = memo(({ synthesis, t, schemaSectionId }) => {
  const [chosenPathId, setChosenPathId] = useState(null);
  const [showAllPaths, setShowAllPaths] = useState(false);
  const waves = migrationWaves(synthesis);
  // No waves: the routines and the pending engine choices still show, on their own.
  if (waves.length === 0) {
    return (
      <>
        <RoutineMigrationSection synthesis={synthesis} t={t} />
        <EngineChoicesNote synthesis={synthesis} t={t} />
      </>
    );
  }
  const count = waveCount(synthesis);
  const adoption = adoptionOf(synthesis);
  const plan = slicePlanOf(synthesis);
  const selected = adoption ? findPath(adoption, chosenPathId || defaultPathId(adoption)) : null;
  // Nothing is greyed for the default path (the full target, today's view).
  const greying = selected && selected.path_id !== defaultPathId(adoption) ? selected : null;
  const selectedLabel = selected ? pathLabel(t, selected, defaultPathId(adoption)) : '';
  return (
    <div id="migration-roadmap-section">
      <Container
        header={(
          <Header
            variant="h2"
            counter={count === null ? undefined : t('analysis-results.waves.counter', { count })}
            // #2512: the step and unit definitions and the readiness rule are in its panel.
            info={<HelpInfoLink topic="migration-roadmap" t={t} />}
            description={(
              <>
                {adoption && adoption.one_step
                  ? t('analysis-results.waves.suggested-path-adoption')
                  : t('analysis-results.waves.suggested-path', {
                    section: t('analysis-results.executive-summary.title'),
                  })}
                {' '}
                <span data-figure="cutover-responsibility">{cutoverResponsibilityOf(synthesis)}</span>
              </>
            )}
          >
            {t('analysis-results.waves.title')}
          </Header>
        )}
      >
        <SpaceBetween size="l">
          {adoption && selected && (
            <AdoptionPathPanel
              adoption={adoption}
              plan={plan}
              selected={selected}
              onSelect={setChosenPathId}
              showAll={showAllPaths}
              onShowAll={(next) => {
                setShowAllPaths(next);
                // "Show fewer" hides a path picked under "Show all": back to the default.
                if (!next && selected && !selectorPaths(adoption, false).some(p => p.path_id === selected.path_id)) {
                  setChosenPathId(null);
                }
              }}
              pointer={oneStepPointerOf(synthesis)}
              schemaSectionId={schemaSectionId}
              t={t}
            />
          )}
          {waves.map((wave, index) => {
            const phase = wavePhase(plan, wave.wave);
            const leftOut = pathLeavesWaveOut(greying, phase);
            const readiness = waveReadiness(synthesis, wave);
            return (
            <div
              key={wave.wave}
              data-wave={wave.wave}
              data-left-out={leftOut ? 'true' : undefined}
              style={leftOut ? LEFT_OUT_STYLE : undefined}
            >
              <ExpandableSection
                variant="container"
                headingTagOverride="h3"
                headerText={t('analysis-results.waves.wave-title', { wave: wave.wave, title: waveTitle(wave) })}
                headerSecondaryContent={(
                  <WaveHeaderFacts
                    synthesis={synthesis}
                    wave={wave}
                    readiness={readiness}
                    leftOut={leftOut}
                    t={t}
                  />
                )}
              >
                <SpaceBetween size="s">
                  {leftOut && (
                    <Box>
                      <span data-figure="wave-left-out">
                        {t('analysis-results.adoption.wave-left-out', { path: selectedLabel })}
                      </span>
                    </Box>
                  )}
                  {pathStopsInWave(greying, phase) && (
                    <Box>
                      <span data-figure="wave-path-stops">
                        {t('analysis-results.adoption.wave-stops-inside', {
                          path: selectedLabel, step: greying.stops_after_step,
                        })}
                      </span>
                    </Box>
                  )}
                  {wave.rationale && <Box color="text-body-secondary">{wave.rationale}</Box>}
                  <WaveGate synthesis={synthesis} wave={wave} t={t} />
                  {readiness && <WaveReadiness view={readiness} t={t} />}
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
                  {index === 0 && <RoutineMigrationSection synthesis={synthesis} t={t} />}
                  {index === 0 && <EngineChoicesNote synthesis={synthesis} t={t} />}
                  {phase && <WaveStages plan={plan} wave={wave.wave} path={greying} t={t} />}
                  {!waveMovesNothing(synthesis, wave) && (wave.tables || []).length > 0 && (
                    <ExpandableSection
                      headerText={t('analysis-results.waves.tables')}
                      variant="footer"
                    >
                      <TableNameList
                        names={wave.tables}
                        t={t}
                        label={t('analysis-results.waves.tables-label', { wave: wave.wave })}
                        figure="wave-tables"
                        filter
                      />
                    </ExpandableSection>
                  )}
                </SpaceBetween>
              </ExpandableSection>
            </div>
            );
          })}
        </SpaceBetween>
      </Container>
    </div>
  );
});

export default MigrationWavePlan;
