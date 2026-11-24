// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2519: the assignment review shows the feasibility review's findings on a saved edit,
 * each finding's own message as the API wrote it, and a refused edit's finding through the
 * same reason path as every other refused edit.
 */
import { feasibilityFlash } from '../feasibilityFindings';
import { errorFromResponse } from '../apiErrors';
import { failureFlash } from '../requestFailure';

const en = require('../../locales/en.json');
const t = (key, o) => (en[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_, k) => (o && o[k]) ?? '');

const ADVISORY = {
    kind: 'read_write_split',
    severity: 'advisory',
    table: 'shop.users',
    engines: ['aurora_postgresql', 'dynamodb'],
    query_ids: ['q4', 'q5'],
    message: 'Table shop.users has its source of truth on Aurora PostgreSQL and a replica on DynamoDB.',
    recommended_pattern: 'CDC',
};
const STANDING = {
    kind: 'co_dependency_split',
    severity: 'blocking',
    table: null,
    engines: ['aurora_postgresql', 'dynamodb'],
    query_ids: ['q1', 'q2'],
    message: 'Co-dependent queries (q1, q2) share a significant JOIN but are split.',
};

describe('feasibilityFlash', () => {
    test('a saved edit with findings is a warning listing each message in order', () => {
        const flash = feasibilityFlash(t, { success: true, feasibility_findings: [STANDING, ADVISORY] });
        expect(flash).toEqual({
            type: 'warning',
            header: en['assignment-gate.flash.feasibility-findings'],
            lines: [STANDING.message, ADVISORY.message],
            dismissible: true,
            id: 'feasibility-findings',
        });
    });

    test('no findings, an older API without the field, or no response show nothing', () => {
        expect(feasibilityFlash(t, { success: true, feasibility_findings: [] })).toBeNull();
        expect(feasibilityFlash(t, { success: true })).toBeNull();
        expect(feasibilityFlash(t, undefined)).toBeNull();
    });

    test('a finding with no message is left out, never made up', () => {
        const flash = feasibilityFlash(t, { feasibility_findings: [{ ...ADVISORY, message: '' }, null, STANDING] });
        expect(flash.lines).toEqual([STANDING.message]);
    });

    test('the message is shown as the API wrote it', () => {
        const message = 'Table <b>x</b> & y.';
        expect(feasibilityFlash(t, { feasibility_findings: [{ ...ADVISORY, message }] }).lines).toEqual([message]);
    });
});

describe('a refused edit', () => {
    test("shows the blocking finding's message like any other hard gate error", () => {
        jest.spyOn(console, 'warn').mockImplementation(() => {});
        const error = 'ERROR: Co-dependent queries (q1, q2, q3) share a significant JOIN but are split across '
            + 'Aurora PostgreSQL and DynamoDB; DynamoDB cannot serve the JOIN (no complex-join support).';
        const body = JSON.stringify({
            detail: { message: 'Assignment validation failed with hard errors', errors: [error], warnings: [] },
        });
        const source = { success: false, status: 422, error: errorFromResponse(422, 'Unprocessable Entity', body) };
        const flash = failureFlash(t, source, { header: 'Save failed', allowReason: true, variant: 'form' });
        expect(flash.type).toBe('error');
        expect(flash.content).toContain('DynamoDB cannot serve the JOIN');
        console.warn.mockRestore();
    });
});
