// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// #1003: the assignment review's per-table engine choices. The API sends the open
// ties grouped per table (`pending_choices`, built once on the server by
// src/agents/referee/pending_choices.py); the page only collects the customer's
// pick per table and sends it back. A model never picks one.

// The PUT body for the picks made so far: `{ choice_id: engine }` -> `table_choices`
// (only engines the choice ties between), plus "decide for me" when asked.
export function buildChoiceRequest(selections = {}, choices = [], decideForMe = false) {
  const byId = new Map((choices || []).map((c) => [c.choice_id, c]));
  const tableChoices = Object.entries(selections || {})
    .filter(([id, engine]) => byId.has(id) && (byId.get(id).tied_engines || []).includes(engine))
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([choice_id, engine]) => ({ choice_id, engine }));
  return decideForMe ? { table_choices: tableChoices, decide_for_me: true } : { table_choices: tableChoices };
}

// Switching an --auto decision to its alternative: one override per query it
// placed (the decision is no longer a pending choice, so it is a plain re-route).
export function switchDecisionOverrides(decision, engine) {
  if (!decision || !engine || engine === decision.engine) return [];
  return (decision.query_ids || []).map((query_id) => ({ query_id, assigned_engine: engine }));
}

// The table's short name (`discourse.theme_fields` -> `theme_fields`).
export function choiceTableName(choice) {
  const table = String((choice && choice.table) || '');
  return table.includes('.') ? table.slice(table.lastIndexOf('.') + 1) : table;
}

// "75 (75.30 before rounding)" for one engine of a choice, in the page's language.
export function formatChoiceScore(engine, t) {
  const whole = engine && engine.score;
  const exact = engine && engine.unrounded_score;
  if (typeof whole !== 'number' || typeof exact !== 'number') return t('assignment-gate.choices.no-score');
  return t('assignment-gate.choices.score', { score: whole, unrounded: exact.toFixed(2) });
}
