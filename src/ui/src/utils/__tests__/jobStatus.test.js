import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import { KNOWN_STATUSES, getStatusType, getStatusText, getStatusLabelKey } from '../jobStatus';

// Every backend-emitted job/stage status (job-level upper-case: RUNNING,
// SUCCEEDED, FAILED, CANCELLED, WAITING, PENDING, COMPLETED; per-stage
// lower-case variants) must resolve to a real Cloudscape status type and a
// translated label, never the generic "info" fallback or a raw/untranslated
// key (#383, #518 round 1).
describe('jobStatus (shared by Dashboard.js and JobMonitoring.js)', () => {
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

  it('covers every known backend status', () => {
    expect(KNOWN_STATUSES).toEqual(
      expect.arrayContaining([
        'RUNNING',
        'SUCCEEDED',
        'FAILED',
        'CANCELLED',
        'WAITING',
        'PENDING',
        'COMPLETED',
      ]),
    );
  });

  it.each(KNOWN_STATUSES)('%s maps to a non-default StatusIndicator type', (status) => {
    expect(getStatusType(status)).not.toBe('info');
  });

  it.each(KNOWN_STATUSES)('%s maps to a translated, non-key label', (status) => {
    const label = getStatusText(i18n.t.bind(i18n), status);
    expect(label).not.toBe(status);
    expect(label.startsWith('job-status.')).toBe(false);
  });

  it.each(KNOWN_STATUSES)('%s has a label key present in en.json, equal to its label', (status) => {
    // en.json is flat with literal dotted keys (keySeparator: false in i18n.js),
    // so this checks the key directly rather than via toHaveProperty's
    // dot-path traversal, which would look for a nested `en["job-status"]`.
    const key = getStatusLabelKey(status);
    expect(key).toBeDefined();
    expect(Object.prototype.hasOwnProperty.call(en, key)).toBe(true);
    expect(getStatusText(i18n.t.bind(i18n), status)).toBe(en[key]);
  });

  it('an unknown status still gets a type and a readable label', () => {
    expect(getStatusType('totally-unknown')).toBe('info');
    expect(getStatusText(i18n.t.bind(i18n), 'totally-unknown')).toBe('Totally-unknown');
  });

  it('a missing status falls back to the translated "Unknown" label', () => {
    expect(getStatusText(i18n.t.bind(i18n), null)).toBe('Unknown');
  });
});
