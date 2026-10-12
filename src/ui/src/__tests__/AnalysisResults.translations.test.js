import fs from 'fs';
import path from 'path';
import { createInstance } from 'i18next';
import en from '../locales/en.json';

const PAGES = [
  {
    name: 'AnalysisResults-02.js',
    file: '../pages/AnalysisResults-02.js',
    canaryKey: 'analysis-results-v2.explorer.col-description',
    canaryValue: 'Description',
  },
  {
    // #269 round-1 review: extended to also cover the Assignment review page,
    // which gained its own `t('assignment-gate.summary.fallback-*')` calls.
    name: 'AssignmentGate.js',
    file: '../pages/AssignmentGate.js',
    canaryKey: 'assignment-gate.summary.fallback-notice',
    canaryValue: 'The AI summary was replaced with one built from the final assignment.',
  },
];

describe.each(PAGES)('$name literal translations', ({ file, canaryKey, canaryValue }) => {
  let i18n;

  beforeAll(async () => {
    i18n = createInstance();
    await i18n.init({
      resources: { en: { translation: en } },
      lng: 'en',
      fallbackLng: 'en',
      keySeparator: false,
      interpolation: { escapeValue: false },
    });
  });

  test('every literal t(...) key resolves to an English label', () => {
    // `file` comes from the fixed PAGES table above, defined in this test file
    // itself -- not user input, so this is not a path-traversal vector despite
    // matching the generic semgrep pattern.
    // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal
    const source = fs.readFileSync(path.join(__dirname, file), 'utf8');
    const keys = [...source.matchAll(/\bt\(['"]([^'"]+)['"]/g)].map((match) => match[1]);
    expect(keys).toContain(canaryKey);
    for (const key of new Set(keys)) {
      expect(i18n.exists(key, { count: 2 })).toBe(true);
      expect(i18n.t(key, { count: 2 })).not.toBe(key);
    }
    expect(i18n.t(canaryKey)).toBe(canaryValue);
  });
});
