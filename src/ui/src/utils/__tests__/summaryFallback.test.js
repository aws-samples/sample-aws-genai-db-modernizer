/**
 * #269: when a generated executive summary is replaced by its deterministic
 * fallback, the UI must say so -- `summaryFallbackInfo` is the one place that
 * decides it, shared by the reality-check summary (AssignmentGate.js), the
 * synthesis summary (AnalysisResults-02.js), and the standalone HTML export
 * (ExportReport.js).
 */
import { summaryFallbackInfo } from '../summaryFallback';

describe('summaryFallbackInfo', () => {
  test('a missing source is treated as llm: no notice, nothing hidden (older artifacts)', () => {
    expect(summaryFallbackInfo(undefined, ['some warning'])).toEqual({
      isFallback: false,
      warnings: [],
    });
  });

  test('source "llm" is not a fallback', () => {
    expect(summaryFallbackInfo('llm', [])).toEqual({
      isFallback: false,
      warnings: [],
    });
  });

  test('source "deterministic" (no model ran) is not a fallback', () => {
    expect(summaryFallbackInfo('deterministic', [])).toEqual({
      isFallback: false,
      warnings: [],
    });
  });

  test('source "deterministic_fallback" carries its warnings', () => {
    expect(summaryFallbackInfo(
      'deterministic_fallback',
      ['[high confidence] names an internal field'],
    )).toEqual({
      isFallback: true,
      warnings: ['[high confidence] names an internal field'],
    });
  });

  test('a fallback with no warnings still reports isFallback', () => {
    expect(summaryFallbackInfo('deterministic_fallback', [])).toEqual({
      isFallback: true,
      warnings: [],
    });
  });

  test('a non-array warnings value is dropped rather than thrown on', () => {
    expect(summaryFallbackInfo('deterministic_fallback', null).warnings).toEqual([]);
  });

  test('falsy and empty-string entries in the warnings array are filtered out', () => {
    expect(summaryFallbackInfo('deterministic_fallback', ['a', '', null, 'b']).warnings)
      .toEqual(['a', 'b']);
  });

  test('non-string entries in the warnings array are filtered out', () => {
    expect(summaryFallbackInfo('deterministic_fallback', ['a', 42, { msg: 'b' }, undefined]).warnings)
      .toEqual(['a']);
  });
});
