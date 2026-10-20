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
  targetEngineSelection,
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

test('a stale saved key (placeholder or legacy id) never reaches the request', () => {
  const stale = { dynamodb: true, neptune: true, keyspaces: true, aurora: true, aurora_mysql: false };
  expect(selectedTargetEngines(stale)).toEqual(['dynamodb']);
  expect(selectedTargetEngines({})).toEqual([]);
  expect(selectedTargetEngines(undefined)).toEqual([]);
});

test.each([
  'create-analysis.target-databases',
  'settings.analysis-options.target-databases',
])('every offered engine has a %s description', (prefix) => {
  [...SELECTABLE_TARGET_ENGINES, ...COMING_SOON_TARGET_ENGINES].forEach((id) => {
    const text = en[`${prefix}.${id}-description`];
    expect(text).toEqual(expect.any(String));
    // "Coming soon" is the disabled checkbox's label, never part of a description.
    expect(text).not.toMatch(/coming soon/i);
  });
});

const PAGE_SOURCES = {
  'CreateAnalysis.js': fs.readFileSync(path.resolve(__dirname, '../../pages/CreateAnalysis.js'), 'utf8'),
  'Settings.js': fs.readFileSync(path.resolve(__dirname, '../../pages/Settings.js'), 'utf8'),
};

test.each(Object.keys(PAGE_SOURCES))('%s keeps no hand-written target-engine list', (page) => {
  const source = PAGE_SOURCES[page];
  expect(source).toMatch(/<TargetEngineCheckboxes/);
  expect(source).not.toMatch(/\bneptune\s*:/);
  expect(source).not.toMatch(/\bkeyspaces\s*:/);
  expect(source).not.toMatch(/\baurora\s*:/);
});
