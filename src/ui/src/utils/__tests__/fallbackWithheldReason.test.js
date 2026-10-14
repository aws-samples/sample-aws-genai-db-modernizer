/**
 * #393 / #269: ports ``src/report/formatting.py``'s ``fallback_withheld_reason``
 * (and its ``_FALLBACK_REASONS`` table) so the Results page and the standalone
 * HTML export name the same specific reason the Decision Report does, instead
 * of a hard-coded one that could show even when a different check actually
 * rejected the summary. Test cases mirror
 * ``tests/unit/report/test_fallback_withheld_reason.py``.
 */
import { fallbackWithheldReason } from '../fallbackWithheldReason';

describe('fallbackWithheldReason', () => {
  test('table attribution reason', () => {
    const warnings = [
      '[high confidence] Summary attributes wp_posts ("wp_posts") to DynamoDB, but no '
      + 'in-scope query of that engine touches it (served by Aurora MySQL): some sentence.',
    ];
    expect(fallbackWithheldReason(warnings)).toBe(
      'named a table under an engine that does not serve it',
    );
  });

  test('internal field name reason', () => {
    const warnings = [
      '[high confidence] Summary names an internal field, "tco_analysis", instead of '
      + 'just stating the figure.',
    ];
    expect(fallbackWithheldReason(warnings)).toBe(
      "named one of this codebase's internal field names",
    );
  });

  test('wave order reason', () => {
    const warnings = [
      '[high confidence] Summary states ElastiCache before Aurora MySQL, but the '
      + 'migration waves move Aurora MySQL first (wave 1) and ElastiCache later '
      + '(wave 2): some sentence.',
    ];
    expect(fallbackWithheldReason(warnings)).toBe(
      'stated a migration order that contradicts the roadmap',
    );
  });

  test('risk count reason', () => {
    const warnings = [
      '[high confidence] Summary says "two HIGH-severity risks", but '
      + 'the risk register has 1 HIGH risk: some sentence.',
    ];
    expect(fallbackWithheldReason(warnings)).toBe(
      "stated a risk count or severity that doesn't match the risk assessment",
    );
  });

  test('multiple reasons are joined', () => {
    const warnings = [
      '[high confidence] Summary names an internal field, "tco_analysis", instead of '
      + 'just stating the figure.',
      '[high confidence] Summary states ElastiCache before Aurora MySQL, but the '
      + 'migration waves move Aurora MySQL first (wave 1) and ElastiCache later '
      + '(wave 2): some sentence.',
    ];
    const reason = fallbackWithheldReason(warnings);
    expect(reason).toContain("named one of this codebase's internal field names");
    expect(reason).toContain('stated a migration order that contradicts the roadmap');
    expect(reason).toContain(' and ');
  });

  test('a low-confidence-only warning falls back to the default', () => {
    const warnings = ['[low confidence] Summary mentions users loosely: some sentence.'];
    expect(fallbackWithheldReason(warnings)).toBe(
      'named a table under an engine that does not serve it',
    );
  });

  test('no warnings falls back to the default', () => {
    expect(fallbackWithheldReason([])).toBe(
      'named a table under an engine that does not serve it',
    );
    expect(fallbackWithheldReason(undefined)).toBe(
      'named a table under an engine that does not serve it',
    );
    expect(fallbackWithheldReason(null)).toBe(
      'named a table under an engine that does not serve it',
    );
  });

  test('non-string entries are ignored rather than thrown on', () => {
    expect(fallbackWithheldReason([42, { msg: 'x' }])).toBe(
      'named a table under an engine that does not serve it',
    );
  });
});
