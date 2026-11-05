// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #1876: the results page's "Queries on Aurora" list states how many statements are
 * procedure-internal, from facts.query_group_counts.procedure_internal_queries, the one
 * figure the engineering report prints too. The page cannot render under this repo's
 * Jest config (see AnalysisResultsAuroraOptimizationLabels.test.js), so this checks the
 * page's source and the facts reader it calls.
 */
import fs from 'fs';
import path from 'path';
import {
  procedureInternalQueryCountOf,
  procedureInternalRoutineCountOn,
  procedureInternalRoutineCountsOf,
} from '../../utils/reportFacts';
import {
  PROCEDURE_INTERNAL_GROUP,
  PROCEDURE_INTERNAL_ROUTINE_GROUP,
  AURORA_BLAME_LABELS,
} from '../../utils/auroraDesign';

const PAGE = fs.readFileSync(path.join(__dirname, '..', 'AnalysisResults.js'), 'utf8');
const EN = JSON.parse(
  fs.readFileSync(path.join(__dirname, '..', '..', 'locales', 'en.json'), 'utf8'),
);

test('the sentence reads the stored fact and formats it', () => {
  const start = PAGE.indexOf("t('analysis-results.aurora-design.procedure-internal'");
  expect(start).toBeGreaterThan(-1);
  const block = PAGE.slice(PAGE.lastIndexOf('{procedureInternalQueryCountOf(', start), start + 200);
  expect(block).toContain('count: procedureInternalQueryCountOf(synthesis)');
  expect(block).toContain('g.sourceTable === PROCEDURE_INTERNAL_GROUP');
});

test('the reader returns the fact, and 0 without one', () => {
  const synthesis = { facts: { query_group_counts: { procedure_internal_queries: 9 } } };
  expect(procedureInternalQueryCountOf(synthesis)).toBe(9);
  expect(procedureInternalQueryCountOf({ facts: {} })).toBe(0);
  expect(procedureInternalQueryCountOf(null)).toBe(0);
});

test('the group is a blame group and both plural forms exist', () => {
  expect(AURORA_BLAME_LABELS).toContain(PROCEDURE_INTERNAL_GROUP);
  expect(EN['analysis-results.aurora-design.procedure-internal_one']).toMatch(/^\{\{count\}\} statement is /);
  expect(EN['analysis-results.aurora-design.procedure-internal_other']).toMatch(/^\{\{count\}\} statements are /);
});

// #1891: the statements that follow their routine, per engine, from
// facts.query_group_counts.procedure_internal_routine_counts: an Aurora engine's on its
// tab, any other engine's under the target engines, as the engineering report places them.
test('each Aurora tab states its own engine\'s followers', () => {
  const start = PAGE.indexOf("t('analysis-results.aurora-design.procedure-internal-routine'");
  expect(start).toBeGreaterThan(-1);
  const block = PAGE.slice(PAGE.lastIndexOf('{procedureInternalRoutineCountOn(', start), start + 200);
  expect(block).toContain('procedureInternalRoutineCountOn(synthesis, design.engine) > 0');
  expect(block).toContain('count: procedureInternalRoutineCountOn(synthesis, design.engine)');
});

test('another engine states its followers under the target engines', () => {
  const start = PAGE.indexOf("t('analysis-results.procedure-internal-routine-on'");
  expect(start).toBeGreaterThan(-1);
  const block = PAGE.slice(PAGE.lastIndexOf('procedureInternalRoutineCountsOf(synthesis)', start), start + 200);
  expect(block).toContain('count > 0 && !isAuroraEngine(engine)');
  expect(block).toContain('engine: ENGINE_LABELS[engine] || engine');
});

test('the per-engine readers return the facts, and nothing without them', () => {
  const synthesis = {
    facts: { query_group_counts: { procedure_internal_routine_counts: { dynamodb: 1, aurora_postgresql: 2 } } },
  };
  expect(procedureInternalRoutineCountsOf(synthesis)).toEqual({ dynamodb: 1, aurora_postgresql: 2 });
  expect(procedureInternalRoutineCountOn(synthesis, 'dynamodb')).toBe(1);
  expect(procedureInternalRoutineCountOn(synthesis, 'documentdb')).toBe(0);
  expect(procedureInternalRoutineCountsOf({ facts: {} })).toEqual({});
  expect(procedureInternalRoutineCountOn(null, 'dynamodb')).toBe(0);
});

test('the routine group is a blame group and every plural form exists', () => {
  expect(PROCEDURE_INTERNAL_ROUTINE_GROUP).toBe('Procedure-internal statements, following their routine');
  expect(AURORA_BLAME_LABELS).toContain(PROCEDURE_INTERNAL_ROUTINE_GROUP);
  expect(EN['analysis-results.aurora-design.procedure-internal-routine_one']).toMatch(/^\{\{count\}\} statement on /);
  expect(EN['analysis-results.aurora-design.procedure-internal-routine_other']).toMatch(/^\{\{count\}\} statements on /);
  expect(EN['analysis-results.procedure-internal-routine-on_one']).toMatch(/^On \{\{engine\}\}, \{\{count\}\} statement on /);
  expect(EN['analysis-results.procedure-internal-routine-on_other']).toMatch(/^On \{\{engine\}\}, \{\{count\}\} statements on /);
});
