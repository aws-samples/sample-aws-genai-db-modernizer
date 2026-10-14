/**
 * Risk assessment as the Python deliverables show it (#373).
 *
 * `synthesis.risk_assessment` carries `overall_risk_level`, `risks` and
 * `resolved_risks`. Severity is always one of `CRITICAL`, `HIGH`, `MEDIUM`,
 * `LOW` (`src/contracts/synthesis_output.py::Risk.severity`). A risk's
 * description carries its engine as a leading `[engine]` tag (there is no
 * separate `engine` field on an open risk) -- `splitRiskDescription` mirrors
 * `src/report/risks.py::_risk_engine_and_body` so the UI shows the same
 * engine/body split the decision and engineering reports do.
 *
 * `filterRisksWithContent` mirrors `risks.py::filtered_risks` /
 * `_risk_has_content`: a defensive guard against a risk whose description is
 * empty (or only `"unknown:"`) after its `[engine]` prefix is removed, so this
 * view's risk count can never disagree with the decision/engineering reports'
 * (#201).
 *
 * Severity styling mirrors `decision_report.py::_risk_tile_class`: CRITICAL is at
 * least as severe as HIGH (both map to `error`/red), never downgraded to a
 * plain warning (#270: the deleted `ReportResults.js` page's bug, where
 * CRITICAL fell back to the same styling as a warning and had no tab of its
 * own).
 *
 * A severity outside the four known ones (`UNRATED`) is its own bucket and
 * status, matching the deck's `UNRATED` handling (`pptx_report.py`) -- it
 * used to fold into `LOW`, which told a reader an unrated risk was known to
 * be low severity rather than simply unrated (#270 round-1 review).
 */

/** Severities in the order every severity-based view should show them. */
export const RISK_SEVERITIES = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];

/** A risk whose severity is none of `RISK_SEVERITIES`, mirroring `pptx_report.UNRATED`. */
export const UNRATED = 'UNRATED';

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

/**
 * Cloudscape status/alert type for a severity: CRITICAL and HIGH both read as
 * `error`. An unrated severity reads as `stopped` (a neutral grey, matching
 * the deck's muted `UNRATED` accent) -- not `success`, which would read an
 * unrated risk as known to be low severity rather than simply unrated.
 */
export function riskSeverityStatus(severity) {
  const sev = String(severity || '').toUpperCase();
  if (sev === 'CRITICAL' || sev === 'HIGH') return 'error';
  if (sev === 'MEDIUM') return 'warning';
  if (sev === 'LOW') return 'success';
  return 'stopped';
}

/**
 * Groups the (already content-filtered) open risks by severity, one bucket
 * per `RISK_SEVERITIES` entry present even when empty, so a severity with no
 * open risk still gets its own (empty) tab rather than disappearing (#373
 * suggested fix). A risk with a severity outside the four known ones gets its
 * own `UNRATED` bucket -- added only when at least one such risk exists, the
 * same "only when present" rule the deck's `UNRATED` bar follows -- instead
 * of silently counting as `LOW`.
 */
export function groupRisksBySeverity(risks) {
  const groups = { CRITICAL: [], HIGH: [], MEDIUM: [], LOW: [] };
  (risks || []).forEach((r) => {
    const sev = String(r?.severity || '').toUpperCase();
    if (groups[sev]) {
      groups[sev].push(r);
    } else {
      (groups[UNRATED] || (groups[UNRATED] = [])).push(r);
    }
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
