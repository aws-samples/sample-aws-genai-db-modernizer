/**
 * #526 round 1 review of #519: JobMonitoring.js's own Cancel-job modal
 * needed the same "already finished" distinction as the Dashboard's, and
 * its hard-coded flashbar strings moved to i18n in the same pass. This
 * mirrors AnalysisResults.translations.test.js's and
 * Dashboard.translations.test.js's pattern: scan the page source for every
 * literal `t('...')` call and check each key resolves to real English text.
 * It also checks the Cancel modal's warning no longer claims "all progress
 * will be lost" -- cancelling doesn't remove the job's artifacts.
 */
import fs from 'fs';
import path from 'path';
import { createInstance } from 'i18next';
import en from '../locales/en.json';

const jobMonitoringSource = fs.readFileSync(
  path.join(__dirname, '../pages/JobMonitoring.js'), 'utf8',
);

test('every literal JobMonitoring-page translation resolves to an English label', async () => {
  const i18n = createInstance();
  await i18n.init({
    resources: { en: { translation: en } },
    lng: 'en',
    fallbackLng: 'en',
    keySeparator: false,
    interpolation: { escapeValue: false },
  });
  const keys = [...jobMonitoringSource.matchAll(/\bt\(['"]([^'"]+)['"]/g)].map((match) => match[1]);
  expect(keys).toContain('job-monitoring.cancel-modal.already-finished-header');
  for (const key of new Set(keys)) {
    expect(i18n.exists(key, { count: 2 })).toBe(true);
    expect(i18n.t(key, { count: 2 })).not.toBe(key);
  }
});

test('the cancel-job warning no longer claims progress is lost (#526 round 1 review)', () => {
  const warning = en['job-monitoring.cancel-modal.warning'].toLowerCase();
  expect(warning).not.toContain('all progress will be lost');
  expect(warning).not.toContain('terminated');
});
