/**
 * Build the Error a failed (non-2xx) API request should resolve with (#548
 * round 2 review item 3). The API returns the real, customer-facing reason in
 * the JSON body's `detail` field -- a plain string for most rejections (e.g.
 * `apply_assignment_overrides`'s domain errors, mapped through
 * `AssignmentOverrideError`), or `{message, errors, warnings}` for a 422
 * hard-validation failure (`AssignmentValidationFailed`). Before this, only
 * `HTTP <status>: <statusText>` was ever shown, so a rejection like "DynamoDB
 * can't run this query: ..." never reached the customer -- just "HTTP 400:
 * Bad Request".
 *
 * Falls back to the plain `HTTP <status>: <statusText>` text when there is no
 * body, the body isn't JSON, or `detail` has neither shape -- the same text
 * every caller already saw before this fix, so a response this function
 * cannot parse degrades to the old behavior rather than losing the error
 * entirely.
 *
 * Kept in its own module, separate from `classes/ApiManager.js`: that file
 * also imports `config/GlobalConfigurations.js`, which pulls in
 * `@cloudscape-design/components` for an unrelated empty-state renderer --
 * fine for the app bundle, but Jest has no transform configured for that
 * package's ESM build, so any test that imported this function from
 * `ApiManager.js` directly would fail before it even ran. This module has no
 * such dependency, so it (and the Error it returns, which is always a plain
 * string message -- never markup, so a consumer that renders it as a normal
 * React child, as `AssignmentGate.js`'s Flashbar does, gets it auto-escaped)
 * is safe to unit test directly.
 *
 * @param {number} status - xhr.status
 * @param {string} statusText - xhr.statusText
 * @param {string} responseText - xhr.responseText (may be empty)
 * @returns {Error}
 */
export function errorFromResponse(status, statusText, responseText) {
    const fallback = () => new Error(`HTTP ${status}: ${statusText}`);
    if (!responseText) {
        return fallback();
    }

    let body;
    try {
        body = JSON.parse(responseText);
    } catch {
        return fallback();
    }

    const detail = body && body.detail;
    if (typeof detail === 'string' && detail) {
        return new Error(detail);
    }
    if (detail && typeof detail === 'object') {
        const parts = [];
        if (typeof detail.message === 'string' && detail.message) {
            parts.push(detail.message);
        }
        if (Array.isArray(detail.errors) && detail.errors.length) {
            parts.push(detail.errors.join('; '));
        }
        if (parts.length) {
            return new Error(parts.join(': '));
        }
    }
    return fallback();
}
