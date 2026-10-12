/**
 * #519: the Dashboard's old "Delete" action only ever called
 * `DELETE /assessments/{job_id}`, which cancels a job -- it never removed
 * artifacts. The UI called it "Delete" and told the user the analysis was
 * "permanently deleted", which was false. That action, its modal, and its
 * i18n keys were removed outright (checked below, under their own
 * identifiers/keys so a reintroduction is caught even if it's reworded).
 *
 * #529 added a real "Delete artifacts" action -- a different, genuine
 * `DELETE /assessments/{job_id}/artifacts` call that actually removes
 * files from disk -- under deliberately distinct identifiers/keys
 * (`delete-artifacts`, `jobToDeleteArtifacts`, `dashboard.delete-artifacts-
 * modal.*`) so it can never collide with the banned #519 ones.
 *
 * This test, mirroring AnalysisResults.translations.test.js's pattern,
 * scans Dashboard.js's (and its action-item helper's) source for every
 * literal `t('...')` call and checks each key resolves to real English
 * text (never falls back to the raw key), then checks: the cancel-specific
 * strings are honest (no "cancel" wording claims permanent deletion); the
 * old #519 "delete" wording/keys are gone; and the #529 delete-artifacts
 * strings are honest the other way (they *do* claim removal from disk, and
 * never reuse the cancel modal's "not removed" claim). The action-
 * visibility logic itself (hide Cancel for a terminal job, offer Delete
 * artifacts instead) is unit-tested directly in dashboardActions.test.js,
 * not here.
 */
import fs from 'fs';
import path from 'path';
import { createInstance } from 'i18next';
import en from '../locales/en.json';

const dashboardSource = fs.readFileSync(
  path.join(__dirname, '../pages/Dashboard.js'), 'utf8',
);
const dashboardActionsSource = fs.readFileSync(
  path.join(__dirname, '../utils/dashboardActions.js'), 'utf8',
);

test('every literal Dashboard-page translation resolves to an English label', async () => {
  const i18n = createInstance();
  await i18n.init({
    resources: { en: { translation: en } },
    lng: 'en',
    fallbackLng: 'en',
    keySeparator: false,
    interpolation: { escapeValue: false },
  });
  const combinedSource = `${dashboardSource}\n${dashboardActionsSource}`;
  const keys = [...combinedSource.matchAll(/\bt\(['"]([^'"]+)['"]/g)].map((match) => match[1]);
  expect(keys).toContain('dashboard.cancel-modal.header');
  expect(keys).toContain('dashboard.actions.cancel-analysis');
  expect(keys).toContain('dashboard.actions.delete-artifacts');
  expect(keys).toContain('dashboard.delete-artifacts-modal.header');
  for (const key of new Set(keys)) {
    expect(i18n.exists(key, { count: 2 })).toBe(true);
    expect(i18n.t(key, { count: 2 })).not.toBe(key);
  }
});

// #529: DELETE /assessments/{job_id}/artifacts actually removes files from
// disk (unlike the retired "Delete" action #519 removed, which only
// cancelled). The modal must say so plainly, and must stay a visibly
// different action from Cancel -- not reuse any of its removed identifiers
// or keys (checked above by the still-passing "Delete" removal tests).
test('the delete-artifacts action and modal text are honest about removing files from disk (#529)', () => {
  const deleteArtifactsKeys = Object.keys(en).filter((key) => key.startsWith('dashboard.delete-artifacts-modal.')
    || key === 'dashboard.actions.delete-artifacts');
  expect(deleteArtifactsKeys.length).toBeGreaterThan(0);

  const warningText = en['dashboard.delete-artifacts-modal.warning'].toLowerCase();
  expect(warningText).toContain('disk');
  expect(warningText).toContain('cannot be undone');

  // Distinct from the cancel modal's own (true, for cancel) claim that
  // artifacts are not removed.
  for (const key of deleteArtifactsKeys) {
    expect(en[key].toLowerCase()).not.toContain('are not removed');
  }
});

test('Dashboard.js no longer calls the action "Delete" (#519)', () => {
  expect(dashboardSource).not.toMatch(/dashboard\.actions\.delete-analysis/);
  expect(dashboardSource).not.toMatch(/dashboard\.delete-modal/);
  expect(dashboardSource).not.toMatch(/\bhandleDeleteConfirm\b/);
  expect(dashboardSource).not.toMatch(/\bshowDeleteModal\b/);
  expect(dashboardSource).not.toMatch(/\bjobToDelete\b/);
});

test('the placeholder "Stop analysis" action is gone, not just unused (#526 round 1 review)', () => {
  expect(dashboardSource).not.toMatch(/dashboard\.actions\.stop-analysis/);
  expect(dashboardSource).not.toMatch(/detail\.id === 'stop'/);
  expect(Object.prototype.hasOwnProperty.call(en, 'dashboard.actions.stop-analysis')).toBe(false);
});

test('the cancel action and modal text are honest about only cancelling, not deleting (#519)', () => {
  const cancelKeys = Object.keys(en).filter((key) => key.startsWith('dashboard.cancel-modal.')
    || key === 'dashboard.actions.cancel-analysis');
  expect(cancelKeys.length).toBeGreaterThan(0);
  for (const key of cancelKeys) {
    const text = en[key].toLowerCase();
    expect(text).not.toContain('permanently deleted');
    expect(text).not.toContain('delete');
  }
  // The old, inaccurate wording must not survive under a new key either.
  expect(Object.values(en)).not.toContain('This action cannot be undone. All analysis data and results will be permanently deleted.');
});

test('the old delete-analysis i18n keys are removed, not just unused', () => {
  // en.json is flat with literal dotted keys (keySeparator: false in i18n.js
  // -- see jobStatus.test.js), so this checks the key directly rather than
  // via toHaveProperty's dot-path traversal, which would look for a nested
  // `en.dashboard.actions['delete-analysis']` that was never there.
  expect(Object.prototype.hasOwnProperty.call(en, 'dashboard.actions.delete-analysis')).toBe(false);
  expect(Object.keys(en).some((key) => key.startsWith('dashboard.delete-modal.'))).toBe(false);
});

test('the unused common.actions.delete key is removed (#526 round 1 review)', () => {
  expect(Object.prototype.hasOwnProperty.call(en, 'common.actions.delete')).toBe(false);
});
