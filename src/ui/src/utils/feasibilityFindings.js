// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The feasibility review's findings on a saved assignment edit (ADR-029 Decision C,
 * #2519), as a Flashbar item for the assignment review, or null when there are none.
 *
 * The API reviews every edit before it writes it: a blocking finding the edit introduces
 * refuses it (a 422, shown like every other refused edit), and the PUT response's
 * `feasibility_findings` lists the rest: each advisory finding the edit introduced, and
 * each blocking one the routing already had. This only formats them: each finding's own
 * `message`, in the API's order, as text. `lines` is for the page to render as a list.
 *
 * @param {Function} t - i18next t
 * @param {object} response - the PUT's ApiManager result
 * @returns {{type: string, header: string, lines: string[], dismissible: boolean, id: string} | null}
 */
export function feasibilityFlash(t, response) {
    const findings = Array.isArray(response?.feasibility_findings) ? response.feasibility_findings : [];
    const messages = findings
        .filter((f) => f && typeof f.message === 'string' && f.message)
        .map((f) => f.message);
    if (messages.length === 0) {
        return null;
    }
    return {
        type: 'warning',
        header: t('assignment-gate.flash.feasibility-findings'),
        lines: messages,
        dismissible: true,
        id: 'feasibility-findings',
    };
}
