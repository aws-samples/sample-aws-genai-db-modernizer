/**
 * #478: Cloudscape's CodeEditor renders a hidden ARIA status
 * description built from i18nStrings.errorsTab/warningsTab/cursorPosition.
 * With none of the three supplied (and no I18nProvider configured for this
 * app), it falls back to literal "undefined", which showed up as
 * "0 undefined, 0 undefined" in the real Results page -- an e2e-llm UI
 * check on a real Bedrock-produced Aurora design caught it (the DDL
 * viewer's hidden ARIA description was still part of
 * `page.inner_text("body")` even collapsed inside an ExpandableSection).
 *
 * `@cloudscape-design/components` ships ESM that this repo's Jest config
 * does not transform (no precedent here for mounting a real Cloudscape
 * component -- see AnalysisResultsCacheOverlayNotes.test.js), so this
 * covers the pure function the Results page's two `CodeEditor` instances
 * both call to build `i18nStrings`, with the exact shape Cloudscape reads
 * (`errorsTab`/`warningsTab`: strings; `cursorPosition`: a `(row, column)
 * => string` function) and the real `en.json` translations.
 */
import i18next from 'i18next';
import en from '../../locales/en.json';
import { codeEditorI18nStrings } from '../codeEditorI18n';

const i18n = i18next.createInstance();

beforeAll(() => i18n.init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
  keySeparator: false,
}));

const t = (key, options) => i18n.t(key, options);

describe('codeEditorI18nStrings', () => {
  it('never leaves errorsTab/warningsTab/cursorPosition for CodeEditor to default to undefined', () => {
    const strings = codeEditorI18nStrings(t);
    expect(strings.errorsTab).toBe('Errors');
    expect(strings.warningsTab).toBe('Warnings');
    expect(typeof strings.cursorPosition).toBe('function');
    expect(strings.cursorPosition(3, 7)).toBe('Ln 3, Col 7');
    // The exact fields CodeEditor's status bar interpolates into
    // "{count} {label}" -- none of the three may be undefined.
    expect(strings.errorsTab).not.toBeUndefined();
    expect(strings.warningsTab).not.toBeUndefined();
    expect(strings.cursorPosition(1, 1)).not.toContain('undefined');
  });

  it('still carries caller-supplied strings (loadingState etc.) alongside the shared ones', () => {
    const strings = codeEditorI18nStrings(t, {
      loadingState: 'Loading...',
      errorState: 'Error loading',
      errorStateRecovery: 'Retry',
    });
    expect(strings.loadingState).toBe('Loading...');
    expect(strings.errorState).toBe('Error loading');
    expect(strings.errorStateRecovery).toBe('Retry');
    expect(strings.errorsTab).toBe('Errors');
    expect(strings.warningsTab).toBe('Warnings');
  });

  it('caller-supplied strings win if they overlap with the shared ones', () => {
    const strings = codeEditorI18nStrings(t, { errorsTab: 'Custom errors' });
    expect(strings.errorsTab).toBe('Custom errors');
  });
});
