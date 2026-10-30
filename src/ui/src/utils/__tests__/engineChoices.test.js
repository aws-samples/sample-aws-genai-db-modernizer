// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import {
  buildChoiceRequest,
  choiceTableName,
  formatChoiceScore,
  switchDecisionOverrides,
} from '../engineChoices';
import { engineChoiceCountsOf } from '../reportFacts';

const CHOICES = [
  { choice_id: 'discourse.theme_fields', table: 'discourse.theme_fields', tied_engines: ['documentdb', 'dynamodb'] },
  { choice_id: 'discourse.incoming_emails', table: 'discourse.incoming_emails', tied_engines: ['documentdb', 'dynamodb'] },
];

test('sends one pick per table, sorted, and only tied engines', () => {
  const body = buildChoiceRequest(
    { 'discourse.theme_fields': 'dynamodb', 'discourse.incoming_emails': 'aurora_postgresql', unknown: 'dynamodb' },
    CHOICES,
  );
  expect(body).toEqual({ table_choices: [{ choice_id: 'discourse.theme_fields', engine: 'dynamodb' }] });
});

test('decide for me is sent only when asked', () => {
  expect(buildChoiceRequest({}, CHOICES, true)).toEqual({ table_choices: [], decide_for_me: true });
  expect(buildChoiceRequest({}, CHOICES)).toEqual({ table_choices: [] });
});

test('switching a decided choice re-routes every query it placed, and nothing when unchanged', () => {
  const decision = { engine: 'documentdb', alternatives: ['dynamodb'], query_ids: ['q1', 'q2'] };
  expect(switchDecisionOverrides(decision, 'dynamodb')).toEqual([
    { query_id: 'q1', assigned_engine: 'dynamodb' },
    { query_id: 'q2', assigned_engine: 'dynamodb' },
  ]);
  expect(switchDecisionOverrides(decision, 'documentdb')).toEqual([]);
  expect(switchDecisionOverrides(null, 'dynamodb')).toEqual([]);
});

test('formats the table name and both scores through the strings', () => {
  const t = (key, vars) => `${key}:${JSON.stringify(vars || {})}`;
  expect(choiceTableName(CHOICES[0])).toBe('theme_fields');
  expect(choiceTableName({ table: 'users' })).toBe('users');
  expect(formatChoiceScore({ score: 75, unrounded_score: 75.3 }, t)).toBe(
    'assignment-gate.choices.score:{"score":75,"unrounded":"75.30"}',
  );
  expect(formatChoiceScore({ score: null }, t)).toBe('assignment-gate.choices.no-score:{}');
});

test('reads the stored choice counts, 0 for an older report', () => {
  expect(engineChoiceCountsOf({ facts: { pending_choice_count: 9, pending_choice_query_count: 32 } })).toEqual({
    pendingTables: 9, pendingQueries: 32, decidedTables: 0, decidedQueries: 0,
  });
  expect(engineChoiceCountsOf({})).toEqual({ pendingTables: 0, pendingQueries: 0, decidedTables: 0, decidedQueries: 0 });
});
