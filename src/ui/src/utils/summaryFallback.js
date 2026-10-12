/**
 * Whether a generated executive summary was replaced by its deterministic
 * fallback, and the warnings to show when it was (#269).
 *
 * Two pipeline summaries can be replaced this way, and both record it with the
 * same shape, so one function reads either:
 *   - the reality-check summary: `executive_summary_source` /
 *     `executive_summary_validation_warnings` (#236)
 *   - the synthesis summary: `summary_source` / `summary_validation_warnings`
 *     (#205)
 *
 * `source` is `"llm"` when the generated text passed its grounding checks,
 * `"deterministic_fallback"` when a check rejected it (the text on screen was
 * then built from the final records instead), or `"deterministic"` when no
 * model ran at all. A missing `source` is treated as `"llm"`: older artifacts
 * predate the field, and had nothing to hide.
 */
export function summaryFallbackInfo(source, warnings) {
  const isFallback = source === 'deterministic_fallback';
  return {
    isFallback,
    warnings: isFallback && Array.isArray(warnings)
      ? warnings.filter((w) => typeof w === 'string' && w.length > 0)
      : [],
  };
}
