import fs from 'fs';
import path from 'path';
import { createInstance } from 'i18next';
import en from '../locales/en.json';

test('every literal Results-page translation resolves to an English label', async () => {
  const i18n = createInstance();
  await i18n.init({
    resources: { en: { translation: en } },
    lng: 'en',
    fallbackLng: 'en',
    keySeparator: false,
    interpolation: { escapeValue: false },
  });
  const source = fs.readFileSync(
    path.join(__dirname, '../pages/AnalysisResults-02.js'), 'utf8',
  );
  const keys = [...source.matchAll(/\bt\(['"]([^'"]+)['"]/g)].map((match) => match[1]);
  expect(keys).toContain('analysis-results-v2.explorer.col-description');
  for (const key of new Set(keys)) {
    expect(i18n.exists(key, { count: 2 })).toBe(true);
    expect(i18n.t(key, { count: 2 })).not.toBe(key);
  }
  expect(i18n.t('analysis-results-v2.explorer.col-description')).toBe('Description');
});
