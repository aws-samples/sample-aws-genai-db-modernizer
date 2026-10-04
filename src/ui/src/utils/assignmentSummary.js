/**
 * Hero text for the Assignment review page (#250).
 *
 * The opening sentence is always built from the data the page itself shows
 * (database name, access-pattern count from after_distribution, table count from
 * the collector), so the approver can see which workload they are signing off on.
 * An LLM-written reality-check summary, when present, follows that sentence; it
 * does not replace it. Without one, the distribution and the top integration
 * pattern are described client-side.
 */
export function buildAssignmentSummary({
  llmSummary,
  databaseName,
  afterDist = {},
  tableCount = 0,
  patterns = [],
  engineLabel = (engine) => engine,
}) {
  const narrative = typeof llmSummary === 'string' ? llmSummary.trim() : '';
  const engines = Object.keys(afterDist);
  if (engines.length === 0) return narrative;

  const totalQueries = Object.values(afterDist).reduce((a, b) => a + b, 0);
  const size = tableCount > 0
    ? `${totalQueries} access patterns across ${tableCount} tables`
    : `${totalQueries} access patterns`;
  const parts = [`Your ${databaseName || 'database'} workload has ${size}.`];

  if (narrative) {
    parts.push(narrative);
  } else if (engines.length === 1) {
    parts.push(`All access patterns map to ${engineLabel(engines[0])}.`);
  } else {
    const distribution = Object.entries(afterDist)
      .filter(([, count]) => count > 0)
      .sort((a, b) => b[1] - a[1])
      .map(([engine, count]) => `${count} to ${engineLabel(engine)}`);
    parts.push(`We map ${distribution.join(', ')}.`);
    if (patterns.length > 0) {
      parts.push(`Recommended integration pattern: ${patterns[0].name}.`);
    }
  }

  return parts.join(' ');
}
