// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #724: the target databases a form offers come from the engine catalog's status, so a
 * placeholder engine (ADR-031 Decision 9) can never be ticked and a new active engine
 * appears with no edit to the forms.
 */
import fs from 'fs';
import path from 'path';
import en from '../../locales/en.json';
import { ENGINE_LABELS, ENGINE_STATUS } from '../engineCatalog.generated';
import {
  COMING_SOON_TARGET_ENGINES,
  SELECTABLE_TARGET_ENGINES,
  selectedTargetEngines,
  targetDatabasesScope,
  targetEngineSelection,
  defaultTargetSelection,
} from '../targetEngines';

test('the selectable engines are exactly the active catalog engines, in catalog order', () => {
  expect(SELECTABLE_TARGET_ENGINES).toEqual(
    Object.keys(ENGINE_LABELS).filter((id) => ENGINE_STATUS[id] === 'active'),
  );
  expect(SELECTABLE_TARGET_ENGINES).toEqual(
    expect.arrayContaining(['dynamodb', 'documentdb', 'opensearch', 'elasticache', 'aurora_mysql', 'aurora_postgresql']),
  );
});

test('placeholders are coming soon, and the legacy generic aurora id is offered nowhere', () => {
  expect(COMING_SOON_TARGET_ENGINES.sort()).toEqual(['keyspaces', 'neptune']);
  expect(SELECTABLE_TARGET_ENGINES).not.toContain('aurora');
  expect(COMING_SOON_TARGET_ENGINES).not.toContain('aurora');
  COMING_SOON_TARGET_ENGINES.forEach((id) => expect(SELECTABLE_TARGET_ENGINES).not.toContain(id));
});

test('select all turns on every selectable engine and no placeholder', () => {
  const all = targetEngineSelection(true);
  expect(Object.keys(all)).toEqual(SELECTABLE_TARGET_ENGINES);
  expect(Object.values(all).every((v) => v === true)).toBe(true);
  expect(all.neptune).toBeUndefined();
  expect(all.keyspaces).toBeUndefined();
  expect(Object.values(targetEngineSelection(false)).some(Boolean)).toBe(false);
});

// #2327: the forms start with every engine but the cache, which the user opts in to.
test('the default selection is every selectable engine except ElastiCache', () => {
  const start = defaultTargetSelection();
  expect(Object.keys(start)).toEqual(SELECTABLE_TARGET_ENGINES);
  expect(start.elasticache).toBe(false);
  expect(selectedTargetEngines(start)).toEqual(SELECTABLE_TARGET_ENGINES.filter((id) => id !== 'elasticache'));
  expect(en['create-analysis.target-databases.elasticache-description']).toMatch(/off by default/);
});

test('a stale saved key (placeholder or legacy id) never reaches the request', () => {
  const stale = { dynamodb: true, neptune: true, keyspaces: true, aurora: true, aurora_mysql: false };
  expect(selectedTargetEngines(stale)).toEqual(['dynamodb']);
  expect(selectedTargetEngines({})).toEqual([]);
  expect(selectedTargetEngines(undefined)).toEqual([]);
});

// The Settings page and its settings.* keys were removed in #437.
test.each([
  'create-analysis.target-databases',
])('every offered engine has a %s description', (prefix) => {
  SELECTABLE_TARGET_ENGINES.forEach((id) => {
    const text = en[`${prefix}.${id}-description`];
    expect(text).toEqual(expect.any(String));
    expect(text).not.toMatch(/coming soon/i);
  });
  // #2327: a future placeholder is not offered, so it has no description and no label.
  COMING_SOON_TARGET_ENGINES.forEach((id) => {
    expect(en[`${prefix}.${id}-description`]).toBeUndefined();
  });
  expect(en['create-analysis.target-databases.coming-soon']).toBeUndefined();
});

const PAGE_SOURCES = {
  'CreateAnalysis.js': fs.readFileSync(path.resolve(__dirname, '../../pages/CreateAnalysis.js'), 'utf8'),
};

test.each(Object.keys(PAGE_SOURCES))('%s keeps no hand-written target-engine list', (page) => {
  const source = PAGE_SOURCES[page];
  expect(source).toMatch(/<TargetEngineCheckboxes/);
  expect(source).not.toMatch(/\bneptune\s*:/);
  expect(source).not.toMatch(/\bkeyspaces\s*:/);
  expect(source).not.toMatch(/\baurora\s*:/);
});

// #1328: the request's target_databases is the engine scope the analysis applies.
describe('targetDatabasesScope', () => {
  test('every engine ticked is no scope (null): every engine, as with no setting', () => {
    expect(targetDatabasesScope(targetEngineSelection(true))).toBeNull();
    // A stale placeholder key does not turn "every engine" into a scope.
    expect(targetDatabasesScope({ ...targetEngineSelection(true), neptune: true, aurora: true })).toBeNull();
  });

  test('a subset is the scope, in catalog order', () => {
    const selection = { ...targetEngineSelection(false), aurora_postgresql: true, dynamodb: true };
    expect(targetDatabasesScope(selection)).toEqual(
      SELECTABLE_TARGET_ENGINES.filter((id) => id === 'dynamodb' || id === 'aurora_postgresql'),
    );
  });

  test('one engine short of all is still a scope', () => {
    const selection = { ...targetEngineSelection(true), elasticache: false };
    expect(targetDatabasesScope(selection)).toEqual(SELECTABLE_TARGET_ENGINES.filter((id) => id !== 'elasticache'));
  });

  test('nothing ticked is an empty list, which the forms refuse before sending', () => {
    expect(targetDatabasesScope(targetEngineSelection(false))).toEqual([]);
  });
});
