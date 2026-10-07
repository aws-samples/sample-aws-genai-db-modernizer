/**
 * Risk assessment as the Python deliverables show it (#373).
 *
 * `synthesis.risk_assessment` carries `overall_risk_level`, `risks` and
 * `resolved_risks`. Severity is always one of `CRITICAL`, `HIGH`, `MEDIUM`,
 * `LOW` (`src/contracts/synthesis_output.py::Risk.severity`). A risk's
 * description carries its engine as a leading `[engine]` tag (there is no
 * separate `engine` field on an open risk) -- `splitRiskDescription` mirrors
 * `src/report/renderers.py::_risk_engine_and_body` so the UI shows the same
 * engine/body split the decision and engineering reports do.
 *
 * `filterRisksWithContent` mirrors `renderers.py::filtered_risks` /
 * `_risk_has_content`: a defensive guard against a risk whose description is
 * empty (or only `"unknown:"`) after its `[engine]` prefix is removed, so this
 * view's risk count can never disagree with the decision/engineering reports'
 * (#201).
 *
 * Severity styling mirrors `renderers.py::_risk_tile_class`: CRITICAL is at
 * least as severe as HIGH (both map to `error`/red), never downgraded to a
 * plain warning (#270: the deleted `ReportResults.js` page's bug, where
 * CRITICAL fell back to the same styling as a warning and had no tab of its
 * own).
 */

/** Severities in the order every severity-based view should show them. */
export const RISK_SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];

/** `"[dynamodb] some text"` -> `{ engine: 'dynamodb', body: 'some text' }`. */
export function splitRiskDescription(description) {
  const text = String(description ?? '').trim();
  if (text.startsWith('[')) {
    const close = text.indexOf(']');
    if (close !== -1) {
      const engine = text.slice(1, close).trim() || '(general)';
      return { engine, body: text.slice(close + 1).trim() };
    }
  }
  return { engine: '(general)', body: text };
}

/** True when a risk has real text after its `[engine]` prefix is removed. */
export function riskHasContent(description) {
  let { body } = splitRiskDescription(description);
  if (body.toLowerCase().startsWith('unknown:')) {
    body = body.slice('unknown:'.length).trim();
  }
  return body.length > 0;
}

/** The one risk list this view renders and counts from -- mirrors `filtered_risks`. */
export function filterRisksWithContent(risks) {
  return (risks || []).filter((r) => r && typeof r === 'object' && riskHasContent(r.description));
}

/** Cloudscape status/alert type for a severity: CRITICAL and HIGH both read as `error`. */
export function riskSeverityStatus(severity) {
  const sev = String(severity || '').toUpperCase();
  if (sev === 'CRITICAL' || sev === 'HIGH') return 'error';
  if (sev === 'MEDIUM') return 'warning';
  return 'success';
}

/**
 * Groups the (already content-filtered) open risks by severity, one bucket
 * per `RISK_SEVERITIES` entry present even when empty, so a severity with no
 * open risk still gets its own (empty) tab rather than disappearing (#373
 * suggested fix). A risk with a severity outside the four known ones is kept
 * -- defensively -- in `LOW`, so it is never silently dropped from the total.
 */
export function groupRisksBySeverity(risks) {
  const groups = { CRITICAL: [], HIGH: [], MEDIUM: [], LOW: [] };
  (risks || []).forEach((r) => {
    const sev = String(r?.severity || '').toUpperCase();
    (groups[sev] || groups.LOW).push(r);
  });
  return groups;
}

/** True when `mitigation` repeats text already in `description` (mirrors `_repeats`). */
export function mitigationRepeatsDescription(mitigation, description) {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const m = norm(mitigation);
  const d = norm(description);
  return Boolean(m) && d.includes(m);
}
