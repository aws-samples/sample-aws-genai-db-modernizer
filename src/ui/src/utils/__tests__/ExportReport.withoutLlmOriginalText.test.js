/**
 * #269 round 2: the standalone HTML export's embedded `DATA` blob must never
 * carry the rejected AI-written text a `*_llm` field keeps for audit
 * (`summary_llm`, `executive_summary_llm`, ...) once a grounding check has
 * replaced it with the deterministic fallback. The notice already names the
 * *reason* it was rejected (`fallbackWithheldReason`); the stale narrative
 * itself must not be recoverable from the exported file at all, not just
 * left out of the rendered notice.
 */
import { generateHTMLReport, withoutLlmOriginalText } from '../ExportReport';

const MARKER = 'MARKER-STALE-AI-NARRATIVE-DO-NOT-SHIP';

describe('withoutLlmOriginalText', () => {
  it('drops a top-level *_llm field', () => {
    expect(withoutLlmOriginalText({ summary: 'ok', summary_llm: 'stale' })).toEqual({
      summary: 'ok',
    });
  });

  it('drops a nested *_llm field at any depth', () => {
    expect(withoutLlmOriginalText({
      synthesis: {
        summary: 'ok',
        summary_llm: 'stale',
        reality_check: { executive_summary: 'ok', executive_summary_llm: 'stale' },
      },
    })).toEqual({
      synthesis: {
        summary: 'ok',
        reality_check: { executive_summary: 'ok' },
      },
    });
  });

  it('strips inside arrays too', () => {
    expect(withoutLlmOriginalText([{ a_llm: 'x', b: 1 }])).toEqual([{ b: 1 }]);
  });

  it('leaves a field that merely contains "_llm" mid-string alone', () => {
    expect(withoutLlmOriginalText({ llm_mode: 'external', status: 'awaiting_llm' })).toEqual({
      llm_mode: 'external',
      status: 'awaiting_llm',
    });
  });

  it('passes through primitives, null and undefined unchanged', () => {
    expect(withoutLlmOriginalText('text')).toBe('text');
    expect(withoutLlmOriginalText(42)).toBe(42);
    expect(withoutLlmOriginalText(null)).toBeNull();
    expect(withoutLlmOriginalText(undefined)).toBeUndefined();
  });
});

describe('generateHTMLReport never embeds *_llm original text', () => {
  const baseData = (synthesisOverrides = {}) => ({
    jobId: 'job-1',
    exportDate: '2026-10-03T12:00:00Z',
    collector: {},
    schemaDesigns: [],
    queryJourneys: { items: [] },
    results: {
      synthesis: {
        database_name: 'wordpress',
        summary: 'This summary is built from the final records.',
        reality_check: { after_distribution: { dynamodb: 100 } },
        tco_analysis: { projected_monthly_cost: 100 },
        ...synthesisOverrides,
      },
    },
  });

  it('drops summary_llm from the whole exported file, not just the rendered notice', () => {
    const markup = generateHTMLReport(baseData({
      summary_source: 'deterministic_fallback',
      summary_llm: MARKER,
    }));
    expect(markup).not.toContain(MARKER);
  });

  it('drops a reality_check.executive_summary_llm from the whole exported file too', () => {
    const markup = generateHTMLReport(baseData({
      reality_check: {
        after_distribution: { dynamodb: 100 },
        executive_summary_llm: MARKER,
      },
    }));
    expect(markup).not.toContain(MARKER);
  });

  it('keeps the rest of the synthesis data embedded (not an over-broad strip)', () => {
    const markup = generateHTMLReport(baseData({ summary_llm: MARKER }));
    expect(markup).toContain('wordpress');
    expect(markup).toContain('This summary is built from the final records.');
  });
});
