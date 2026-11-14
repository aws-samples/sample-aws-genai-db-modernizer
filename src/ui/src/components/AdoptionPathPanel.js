// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import ToggleButton from "@cloudscape-design/components/toggle-button";
import Button from "@cloudscape-design/components/button";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Link from "@cloudscape-design/components/link";
import { displayEngine } from "../utils/engineNames";
import {
  costNote, costStated, defaultPathId, differenceStated, differenceText, formatCount, formatUsd,
  introText, notOfferedLines, oneStepChecks, oneStepOf, oneStepRuleText, pathLabel, TARGET_ENGINES_ID, relationalEngine, selectorIsCapped,
  selectorPaths, stepsText,
} from "../utils/adoptionPaths";

/**
 * The adoption paths above the roadmap's wave cards (#1132, design section 12.3): a
 * selector with one chip per path, the selected path's four tiles (engines, queries
 * moved, monthly cost and its difference, open critical and high risks), and the
 * one-step option, collapsed. Every figure is a facts.adoption field (ADR-032); the
 * words are the engineering report's (src/report/adoption.py).
 *
 * The staged full target is selected when the page opens, so the default view is
 * today's. Choosing a path calls `onSelect` and nothing else: it changes what the page
 * shows, never the assignment. The capped selector shows the paths summary_path_ids
 * names (#2172); "Show all" lists every offered path. Nothing ranks the paths.
 *
 * `data-figure` / `data-path` are hooks for the e2e check that each tile reads the
 * same as report.json (tests/e2e/test_ui.py).
 */

const CHIPS_STYLE = { display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'center' };
const CHECKS_TABLE_STYLE = { borderCollapse: 'collapse' };
const CELL_STYLE = { textAlign: 'left', padding: '2px 16px 2px 0' };

function Tile({ label, figure, children }) {
  return (
    <div>
      <Box variant="awsui-key-label">{label}</Box>
      <div data-figure={figure}>{children}</div>
    </div>
  );
}

function PathTiles({ adoption, path, plan, t }) {
  const engine = relationalEngine(plan);
  const cost = costStated(adoption, path);
  return (
    <ColumnLayout columns={4} variant="text-grid">
      <Tile label={t('analysis-results.adoption.tile.engines')} figure="path-engines">
        {(path.engines || []).map(displayEngine).join(', ')}
      </Tile>
      <Tile
        label={engine
          ? t('analysis-results.adoption.tile.queries-off', { engine: displayEngine(engine) })
          : t('analysis-results.adoption.tile.queries-moved')}
        figure="path-queries-moved"
      >
        {formatCount(path.queries_moved)}
      </Tile>
      <Tile label={t('analysis-results.adoption.tile.cost')} figure="path-cost">
        {cost ? formatUsd(path.monthly_cost_usd) : t('analysis-results.adoption.no-cost')}
        {differenceStated(adoption, path) && (
          <Box fontSize="body-s" color="text-body-secondary">
            <span data-figure="path-cost-difference">{differenceText(t, path)}</span>
          </Box>
        )}
      </Tile>
      <Tile label={t('analysis-results.adoption.tile.risks')} figure="path-risks">
        {formatCount(path.critical_high_risks)}
      </Tile>
    </ColumnLayout>
  );
}

function OneStepOption({ adoption, pointer, schemaSectionId, t }) {
  const one = oneStepOf(adoption);
  if (!one) return null;
  const checks = oneStepChecks(one);
  const streams = one.staged_temporary_stream_count || 0;
  // The report's pointer (oneStepPointerOf); the schemas' section is only where it lands.
  const toSchemas = pointer === 'schemas';
  const targetId = toSchemas ? (schemaSectionId || 'access-patterns-section') : TARGET_ENGINES_ID;
  const follow = (event) => {
    if (event && event.preventDefault) event.preventDefault();
    const el = document.getElementById(targetId);
    if (el && el.scrollIntoView) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  return (
    <div data-figure="one-step-option">
      <ExpandableSection headerText={t('analysis-results.adoption.one-step.show')} variant="container">
        <SpaceBetween size="s">
          <ul>
            <li data-figure="one-step-in-place">
              {t('analysis-results.adoption.one-step.in-place', {
                steps: t('analysis-results.adoption.step-count', { count: one.staged_step_count || 0 }),
                stages: t('analysis-results.adoption.stage-count', { count: one.staged_stage_count || 0 }),
              })}
            </li>
            <li data-figure="one-step-gate">
              {t('analysis-results.adoption.one-step.gate', { count: one.gate_count || 0 })}
            </li>
            {streams > 0 && (
              <li data-figure="one-step-streams">
                {t('analysis-results.adoption.one-step.streams', { count: streams })}
              </li>
            )}
          </ul>
          {checks.length > 0 && (
            <>
              <Box>
                <span data-figure="one-step-verdict">
                  {one.suits_one_step
                    ? t('analysis-results.adoption.one-step.suits')
                    : t('analysis-results.adoption.one-step.does-not-suit')}
                </span>
              </Box>
              <table style={CHECKS_TABLE_STYLE}>
                <thead>
                  <tr>
                    {['check', 'observed', 'limit', 'holds'].map(c => (
                      <th key={c} scope="col" style={CELL_STYLE}>{t(`analysis-results.adoption.one-step.column.${c}`)}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {checks.map(check => (
                    <tr key={check.rule} data-figure="one-step-check" data-rule={check.rule}>
                      <td style={CELL_STYLE}>{oneStepRuleText(t, check.rule)}</td>
                      <td style={CELL_STYLE}>{formatCount(check.observed)}</td>
                      <td style={CELL_STYLE}>{formatCount(check.limit)}</td>
                      <td style={CELL_STYLE}>
                        {check.passed
                          ? t('analysis-results.adoption.one-step.holds-yes')
                          : t('analysis-results.adoption.one-step.holds-no')}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          <div data-figure="one-step-adopt">
            {t('analysis-results.adoption.one-step.adopt-prefix')}{' '}
            <Link href={`#${targetId}`} onFollow={follow}>
              {toSchemas
                ? t('analysis-results.adoption.one-step.link-schemas')
                : t('analysis-results.adoption.one-step.link-architecture')}
            </Link>{' '}
            {t('analysis-results.adoption.one-step.adopt-suffix')}
          </div>
        </SpaceBetween>
      </ExpandableSection>
    </div>
  );
}

export default function AdoptionPathPanel({
  adoption, plan, selected, onSelect, showAll, onShowAll, pointer, schemaSectionId, t,
}) {
  const defaultId = defaultPathId(adoption);
  const chips = selectorPaths(adoption, showAll);
  const capped = selectorIsCapped(adoption);
  return (
    <div id="adoption-paths" data-figure="adoption-paths">
      <SpaceBetween size="m">
        <Box variant="h3">
          {t('analysis-results.adoption.title')}{' '}
          <span data-figure="adoption-path-count">
            {t('analysis-results.adoption.counter', { count: adoption.path_count || 0 })}
          </span>
        </Box>
        <Box fontSize="body-s" color="text-body-secondary">
          <SpaceBetween size="xxs">
            <span>{t('analysis-results.adoption.definitions')}</span>
            <span>{introText(t, plan)}</span>
          </SpaceBetween>
        </Box>
        <div role="group" aria-label={t('analysis-results.adoption.selector-label')} style={CHIPS_STYLE}>
          {chips.map(path => (
            <span key={path.path_id} data-path={path.path_id}>
              <ToggleButton
                pressed={path.path_id === selected.path_id}
                onChange={() => onSelect(path.path_id)}
              >
                {pathLabel(t, path, defaultId)}
              </ToggleButton>
            </span>
          ))}
          {capped && (
            <span data-figure="adoption-show-all">
              <Button variant="inline-link" onClick={() => onShowAll(!showAll)}>
                {showAll
                  ? t('analysis-results.adoption.show-fewer')
                  : t('analysis-results.adoption.show-all', { count: adoption.path_count || 0 })}
              </Button>
            </span>
          )}
        </div>
        {capped && !showAll && (
          <Box fontSize="body-s" color="text-body-secondary">{t('analysis-results.adoption.capped')}</Box>
        )}
        <div data-figure="selected-path" data-path={selected.path_id}>
          <SpaceBetween size="s">
            <Box variant="h4">{pathLabel(t, selected, defaultId)}</Box>
            <Box color="text-body-secondary"><span data-figure="path-steps">{stepsText(t, selected, plan)}</span></Box>
            <PathTiles adoption={adoption} path={selected} plan={plan} t={t} />
            <Box fontSize="body-s" color="text-body-secondary">
              <span data-figure="path-cost-note">{costNote(t, adoption)}</span>
            </Box>
            <Box fontSize="body-s" color="text-body-secondary">
              {t('analysis-results.adoption.selection-note')}
            </Box>
          </SpaceBetween>
        </div>
        {notOfferedLines(t, adoption).map(line => (
          <Box key={line.pathId} fontSize="body-s" color="text-body-secondary">
            <span data-figure="path-not-offered">{line.text}</span>
          </Box>
        ))}
        <OneStepOption adoption={adoption} pointer={pointer} schemaSectionId={schemaSectionId} t={t} />
      </SpaceBetween>
    </div>
  );
}
