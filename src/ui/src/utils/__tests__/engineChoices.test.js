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

// #2147: a statement that reads no table is its own choice, shown as "No source table".
test('a choice for a statement that reads no table is shown as No source table', () => {
  const t = (key, opts) => `${key}${opts && opts.context ? `_${opts.context}` : ''}|${(opts && opts.query) || ''}`;
  const noTable = { choice_id: 'query:0xAB', table: '', query_ids: ['0xAB'] };
  expect(choiceTableName(noTable)).toBe('No source table (0xAB)');
  expect(choiceTableName(noTable, t)).toBe('assignment-gate.choices.no-source-table_query|0xAB');
  // A job saved before #2147 named the collector's placeholder as the table.
  expect(choiceTableName({ table: 'unknown', query_ids: ['q1', 'q2'] })).toBe('No source table');
  expect(choiceTableName({ table: 'app.Unknown' })).toBe('Unknown');
});

test('reads the stored choice counts, 0 for an older report', () => {
  // An older report has no split (#2147): every choice was a table's.
  expect(engineChoiceCountsOf({ facts: { pending_choice_count: 9, pending_choice_query_count: 32 } })).toEqual({
    pendingTables: 9, pendingQueries: 32, pendingTableChoices: 9, pendingNoTable: 0,
    decidedTables: 0, decidedQueries: 0, decidedTableChoices: 0, decidedNoTable: 0,
  });
  expect(engineChoiceCountsOf({})).toEqual({
    pendingTables: 0, pendingQueries: 0, pendingTableChoices: 0, pendingNoTable: 0,
    decidedTables: 0, decidedQueries: 0, decidedTableChoices: 0, decidedNoTable: 0,
  });
});

// #2147, the DNN shape: 2 tables and 1 statement that reads no table.
test('reads the facts split of tables and statements with no source table', () => {
  const facts = {
    auto_decided_choice_count: 3, auto_decided_table_count: 2, auto_decided_no_table_count: 1,
    auto_decided_query_count: 3,
  };
  expect(engineChoiceCountsOf({ facts })).toMatchObject({
    decidedTables: 3, decidedTableChoices: 2, decidedNoTable: 1, decidedQueries: 3,
  });
});
