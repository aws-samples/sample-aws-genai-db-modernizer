// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2346: a failed request maps to one short kind, whatever shape it arrives in
 * (an ApiManager result, an axios error, a fetch error or a bare status). The kind
 * picks a fixed message; the technical detail goes to the console only.
 */
import { failureError, failureFlash, failureKind, failureMessageKey, failureReason, isRetryable, reportFailure, upsertFlash } from '../requestFailure';
import { errorFromResponse } from '../apiErrors';

const en = require('../../locales/en.json');
const t = (key, o) => (en[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_, k) => (o && o[k]) ?? '');

describe('failureKind', () => {
  test.each([
    [404, 'not-found'],
    [400, 'rejected'],
    [403, 'rejected'],
    [409, 'rejected'],
    [422, 'rejected'],
    [408, 'timeout'],
    [429, 'server'],
    [500, 'server'],
    [503, 'server'],
    [200, 'other'],
  ])('status %s is %s', (status, kind) => {
    expect(failureKind(status)).toBe(kind);
    expect(failureKind({ success: false, status, error: new Error('x') })).toBe(kind);
    // axios: the server answered, the status is under response.
    expect(failureKind({ isAxiosError: true, message: 'Request failed', response: { status } })).toBe(kind);
    // fetch: a Response-like object.
    expect(failureKind({ ok: false, status })).toBe(kind);
  });

  test('no status and no answer is unreachable, however the client reports it', () => {
    expect(failureKind(undefined)).toBe('unreachable');
    expect(failureKind(null)).toBe('unreachable');
    expect(failureKind({ success: false, error: new Error('Network error') })).toBe('unreachable');
    // axios: the request left and nothing came back.
    expect(failureKind({ isAxiosError: true, request: {}, message: 'Network Error' })).toBe('unreachable');
    expect(failureKind({ code: 'ERR_NETWORK' })).toBe('unreachable');
    expect(failureKind({ code: 'ECONNREFUSED' })).toBe('unreachable');
    // fetch rejects with a TypeError when it cannot reach the server.
    expect(failureKind(new TypeError('Failed to fetch'))).toBe('unreachable');
  });

  test('a request that ran out of time is a timeout', () => {
    expect(failureKind({ code: 'ECONNABORTED', message: 'timeout of 30000ms exceeded' })).toBe('timeout');
    expect(failureKind({ name: 'AbortError' })).toBe('timeout');
    expect(failureKind({ success: false, kind: 'timeout', error: new Error('Request timeout') })).toBe('timeout');
  });

  test('a kind already stored is kept, and an unknown string is never echoed', () => {
    expect(failureKind('server')).toBe('server');
    jest.spyOn(console, 'warn').mockImplementation(() => {});
    expect(failureKind(failureError({ success: false, status: 404 }, 'x'))).toBe('not-found');
    expect(failureKind('Query not found at http://10.0.0.5:8000/api')).toBe('other');
  });

  test('a script error is not reported as an unreachable API', () => {
    expect(failureKind(new Error('boom'))).toBe('other');
    expect(failureKind(new RangeError('bad'))).toBe('other');
    expect(failureKind(new TypeError('items.concat is not a function'))).toBe('other');
  });
});

describe('isRetryable', () => {
  test('retrying helps for a server error, a timeout and an unreachable API, not for a missing or refused request', () => {
    expect(['server', 'timeout', 'unreachable', 'other'].map(isRetryable)).toEqual([true, true, true, true]);
    expect(isRetryable('not-found')).toBe(false);
    expect(isRetryable('rejected')).toBe(false);
  });
});

describe('failureMessageKey', () => {
  test('every kind has a fixed message with no digit, URL or host', () => {
    ['not-found', 'rejected', 'server', 'timeout', 'unreachable', 'other'].forEach((kind) => {
      const text = t(failureMessageKey(kind));
      expect(text).not.toBe(failureMessageKey(kind));
      expect(text).not.toMatch(/\d|http|localhost|:\/\//);
    });
    expect(failureMessageKey('whatever')).toBe('errors.request.other');
  });
});

describe('the technical detail goes to the console only', () => {
  const HOSTILE = {
    success: false,
    status: 502,
    error: new Error('<script>alert(1)</script> GET http://10.0.0.5:8000/api/v1/assessments/x\n  at Object.<anonymous> (server.py:12)'),
  };
  let warn;
  beforeEach(() => { warn = jest.spyOn(console, 'warn').mockImplementation(() => {}); });
  afterEach(() => { warn.mockRestore(); });

  test('reportFailure logs the status and detail and returns the kind', () => {
    expect(reportFailure(HOSTILE, 'deliverables')).toBe('server');
    expect(warn).toHaveBeenCalledTimes(1);
    const [, info] = warn.mock.calls[0];
    expect(info).toMatchObject({ context: 'deliverables', kind: 'server', status: 502 });
    expect(info.detail).toContain('10.0.0.5');
  });

  test('a flash item has a fixed header and message, and nothing of the request', () => {
    const item = failureFlash(t, HOSTILE, { header: 'The artifacts were not deleted', context: 'delete' });
    expect(item).toMatchObject({ type: 'error', header: 'The artifacts were not deleted', content: 'The service returned an error.', dismissible: true });
    expect(JSON.stringify(item)).not.toMatch(/10\.0\.0\.5|8000|502|script|server\.py|api\/v1/);
  });

  test('a flash item offers Retry only where retrying can help', () => {
    const retry = jest.fn();
    const server = failureFlash(t, HOSTILE, { header: 'h', onRetry: retry });
    expect(server.buttonText).toBe('Retry');
    server.onButtonClick();
    expect(retry).toHaveBeenCalledTimes(1);
    expect(failureFlash(t, { success: false, status: 404 }, { header: 'h', onRetry: retry }).buttonText).toBeUndefined();
    expect(failureFlash(t, { success: false, status: 422 }, { header: 'h', onRetry: retry }).buttonText).toBeUndefined();
    expect(failureFlash(t, HOSTILE, { header: 'h' }).buttonText).toBeUndefined();
  });

  test('a stored kind makes a flash without logging again', () => {
    const item = failureFlash(t, 'unreachable', { header: 'h' });
    expect(item.content).toBe('The service could not be reached.');
    expect(warn).not.toHaveBeenCalled();
  });

  test('failureError carries the kind and no request detail in its message', () => {
    const err = failureError(HOSTILE, 'prepare-upload');
    expect(err.kind).toBe('server');
    expect(err.message).toBe('prepare-upload');
  });
});

describe('failureReason: the API own sentence, for the four opt-in call sites', () => {
  const refused = (status, detail) => ({ success: false, status, error: errorFromResponse(status, 'x', JSON.stringify({ detail })) });
  const SENTENCE = 'growth must be between 0 (excluded) and 100, got 500';
  const reasonOf = (detail, status = 422) => failureReason(refused(status, detail), t);

  test.each([400, 409, 422])('a %s with a plain-string detail gives that sentence', (status) => {
    expect(failureReason(refused(status, SENTENCE), t)).toBe(SENTENCE);
  });

  test('the real refusal sentences pass', () => {
    const secrets = 'This request includes a password or secret (connection.password). Store the database credentials in a secrets manager such as AWS Secrets Manager.'; // pragma: allowlist secret
    expect(reasonOf(secrets, 400)).toBe(secrets);
    const design = "This assessment's saved Aurora MySQL design failed its safety check: it has an index definition the tool does not write, so it was not loaded. Rerun the Aurora MySQL schema design.";
    expect(reasonOf(design, 409)).toBe(design);
  });

  test('a customer table name echoed in the detail is shown as text', () => {
    expect(reasonOf('Table wp_posts is not tied to engine DynamoDB')).toBe('Table wp_posts is not tied to engine DynamoDB');
    expect(reasonOf('Unknown choice order-items_v2')).toBe('Unknown choice order-items_v2');
  });

  test('only a 4xx whose detail the API wrote', () => {
    expect(failureReason(refused(500, SENTENCE), t)).toBeNull();
    expect(failureReason(refused(200, SENTENCE), t)).toBeNull();
    expect(failureReason({ success: false, status: 404, error: new Error('HTTP 404: Not Found') }, t)).toBeNull();
    expect(failureReason({ success: false, status: 400, error: new Error(SENTENCE) }, t)).toBeNull(); // not built from a response
    expect(failureReason({ success: false, error: new Error('Network error') }, t)).toBeNull();
    expect(failureReason(undefined, t)).toBeNull();
    expect(failureReason('server', t)).toBeNull();
  });

  test('the raw detail is not a property of the Error', () => {
    const err = errorFromResponse(400, 'x', JSON.stringify({ detail: SENTENCE }));
    expect(Object.getOwnPropertyNames(err).sort()).toEqual(['message', 'stack']);
    expect(JSON.stringify(err)).toBe('{}');
  });

  // The cases the review listed: each must fall back to the kind's fixed message.
  test.each([
    ['https with a zero-width space', 'ht\u200Btps://evil.example/login'],
    ['www with a zero-width space', 'www\u200B.evil.example'],
    ['a bare domain with a path', 'Sign in again at evil.example/login'],
    ['a bare domain', 'Sign in again at evil.example'],
    ['a host with no port', 'artifacts.internal.corp'],
    ['a homoglyph scheme', '\u04BBttps://evil.example'],
    ['a fullwidth colon scheme', 'https\uFF1A//evil.example'],
    ['a file URL', 'file:///etc/passwd'],
    ['an s3 URL', 's3://bucket/acme/job/uploads/x.json'],
    ['an absolute path', '/var/lib/modernizer/artifacts/acme/x'],
    ['a Windows path', 'C:\\Users\\acme\\x'],
    ['a bracketed IPv6 loopback', '[::1]'],
    ['a bracketed IPv6 with a port', '[::1]:8000'],
    ['a bare IPv6', 'fe80::1'],
    ['a long IPv6', '2001:db8:85a3:0:0:8a2e:370:7334'],
    ['a dotted quad', 'Host 10.0.0.5 refused it'],
    ['a host and port', 'Connection to db:5432 was refused'],
    ['a javascript scheme', 'javascript:alert(1)'],
    ['a bidi override', 'abc\u202Edef'],
    ['a bidi isolate', 'abc\u2066def'],
    ['a line separator', 'abc\u2028def'],
    ['a paragraph separator', 'abc\u2029def'],
    ['a C1 control', 'abc\u0085def'],
    ['a DEL', 'abc\u007Fdef'],
    ['a byte order mark', '\uFEFFabc'],
    ['a line break', 'First line\nsecond line'],
    ['a tab', 'abc\tdef'],
    ['HTML', '<script>alert(1)</script> not allowed'],
    ['an entity', 'Use &lt;b&gt; instead'],
    ['a stack line', 'Failed at Object.<anonymous> (server.py:12)'],
    ['a Python trace', 'Traceback (most recent call last): boom'],
    ['a file and line', 'File "/app/x.py", line 12, in run'],
    ['a non-ASCII letter', 'Tabelle f\u00FCr Kunden'],
    ['an emoji', 'Done \u2705'],
    ['a very long text', 'x'.repeat(301)],
    ['a blank text', '   '],
  ])('%s falls back (null)', (_, detail) => {
    expect(reasonOf(detail)).toBeNull();
  });

  test('the length cap is 300 characters', () => {
    expect(reasonOf('a'.repeat(300))).toBe('a'.repeat(300));
    expect(reasonOf('a'.repeat(301))).toBeNull();
  });

  test('NFKC folds compatibility characters before the rules run', () => {
    // A fullwidth letter and a fullwidth digit read as plain ASCII, so the text is accepted as such.
    expect(reasonOf('Value \uFF11 is too small')).toBe('Value 1 is too small');
  });

  test('a host and port with spaces, and decimal or hex IPv4, fall back', () => {
    ['Cannot reach db : 5432', 'Cannot reach db :5432', 'Cannot reach 167772165', 'Cannot reach 0x0a000005'].forEach((text) => {
      expect(reasonOf(text, 400)).toBeNull();
    });
    expect(reasonOf('growth must be between 0 and 100, got 500', 400)).toBe('growth must be between 0 and 100, got 500');
  });

  test.each(['evil.ai', 'evil.sh', 'evil.zip', 'evil.ly', 'evil.page', 'evil.link', 'evil.gg', 'evil.to', 'evil\uFF0Eai'])(
    'the dotted domain %s falls back, also with a fullwidth dot', (host) => {
      expect(reasonOf(`Sign in at ${host} now`, 400)).toBeNull();
    });

  describe('FastAPI 422 shapes', () => {
    test('a list names the fields from each loc, through the same allowlist', () => {
      const list = [
        { loc: ['body', 'overrides', 0, 'engine'], msg: 'Field required', type: 'missing' },
        { loc: ['body', 'base_version'], msg: 'Input should be a valid integer', type: 'int_parsing' },
      ];
      expect(reasonOf(list)).toBe('The service did not accept these fields: overrides, engine, base_version.');
    });

    test('a field name that fails the allowlist is left out, and none left falls back', () => {
      expect(reasonOf([{ loc: ['body', 'evil.example'], msg: 'x' }, { loc: ['body', 'ok_field'], msg: 'x' }])).toBe('The service did not accept these fields: ok_field.');
      expect(reasonOf([{ loc: ['body', 'ht\u200Btps://x'], msg: 'x' }])).toBeNull();
      expect(reasonOf([{ loc: ['body'], msg: 'x' }])).toBeNull();
      expect(reasonOf([])).toBeNull();
    });

    test('the field-name sentence has a total length cap', () => {
      const names = ['a', 'b', 'c', 'd', 'e'].map((c) => c.repeat(80));
      const reason = reasonOf(names.map((n) => ({ loc: ['body', n], msg: 'x' })));
      expect(reason).toBe(`The service did not accept these fields: ${names[0]}.`);
      expect(reasonOf([{ loc: ['body', 'x'.repeat(299)], msg: 'x' }])).toBeNull();
    });

    test('the {message, errors} object adds each safe error to a fixed sentence', () => {
      const detail = { message: 'Assignment validation failed with hard errors', errors: ['Query q7 cannot run on DynamoDB', 'see https://evil.example/x', 'Query q9 has no table'], warnings: [] };
      expect(reasonOf(detail)).toBe('The request did not pass validation: Query q7 cannot run on DynamoDB; Query q9 has no table.');
    });

    test('an object with no safe error falls back', () => {
      expect(reasonOf({ message: 'x', errors: ['<b>bad</b>'] })).toBeNull();
      expect(reasonOf({ message: 'x', errors: [] })).toBeNull();
    });

    test('without a t function the two sentences are not built', () => {
      expect(failureReason(refused(422, [{ loc: ['body', 'a'], msg: 'x' }]))).toBeNull();
    });
  });

  test('a flash shows the reason only when the call site opts in, else the fixed message', () => {
    const src = refused(409, SENTENCE);
    expect(failureFlash(t, src, { header: 'h', allowReason: true }).content).toBe(SENTENCE);
    expect(failureFlash(t, src, { header: 'h' }).content).toBe('The service did not accept the request.');
  });

  test('a flash with an unsafe reason falls back to the kind message, even when opted in', () => {
    const hostile = refused(422, '<img src=x onerror=alert(1)> at https://10.0.0.5:8000/x');
    expect(failureFlash(t, hostile, { header: 'h', allowReason: true, variant: 'form' }).content).toBe(en['errors.request.rejected-form']);
  });
});

describe('the rejected wording and the flash ids', () => {
  test('a rejected answer reads by variant: neutral, an action, a form', () => {
    expect(t(failureMessageKey('rejected'))).toBe('The service did not accept the request.');
    expect(t(failureMessageKey('rejected', 'action'))).toBe('The service refused this action.');
    expect(t(failureMessageKey('rejected', 'form'))).toContain('Check the values you entered');
    expect(failureMessageKey('server', 'action')).toBe('errors.request.server');
  });

  test('every flash gets its own id unless the caller gives one', () => {
    jest.spyOn(console, 'warn').mockImplementation(() => {});
    const a = failureFlash(t, { success: false, status: 500 }, { header: 'h', context: 'x' });
    const b = failureFlash(t, { success: false, status: 500 }, { header: 'h', context: 'x' });
    expect(a.id).not.toBe(b.id);
    expect(failureFlash(t, 'server', { header: 'h', id: 'fixed' }).id).toBe('fixed');
  });

  test('upsertFlash replaces the item with the same id and appends otherwise', () => {
    const one = { id: 'a', content: '1' };
    const two = { id: 'b', content: '2' };
    expect(upsertFlash([one], two)).toEqual([one, two]);
    expect(upsertFlash([one, two], { id: 'a', content: '3' })).toEqual([{ id: 'a', content: '3' }, two]);
  });
});

describe('the pages that retry or poll replace their own flash', () => {
  const fs = require('fs');
  const path = require('path');
  const read = (file) => fs.readFileSync(file, 'utf8');
  const PAGES = {
    'AnalysisResults.js': () => read(path.join(__dirname, '../../pages/AnalysisResults.js')),
    'AssignmentGate.js': () => read(path.join(__dirname, '../../pages/AssignmentGate.js')),
    'JobMonitoringSummary.js': () => read(path.join(__dirname, '../../pages/JobMonitoringSummary.js')),
    'Dashboard.js': () => read(path.join(__dirname, '../../pages/Dashboard.js')),
  };
  const page = (name) => PAGES[name]();

  test('the results, gate and job summary pages upsert by id', () => {
    ['AnalysisResults.js', 'AssignmentGate.js', 'JobMonitoringSummary.js'].forEach((name) => {
      expect(page(name)).toMatch(/upsertFlash\(/);
    });
    expect(page('AnalysisResults.js')).toContain("id: 'results-load-failed'");
    expect(page('AssignmentGate.js')).toContain("id: 'gate-load-failed'");
  });

  test('the job summary gives each failure its own id', () => {
    const ids = [...page('JobMonitoringSummary.js').matchAll(/id: '([a-z-]+-failed)'/g)].map((m) => m[1]);
    expect(ids).toEqual(['job-details-failed', 'agent-details-failed', 'job-summary-failed']);
  });

  test('the gate own load failure reads as a read, not a form', () => {
    const line = page('AssignmentGate.js').split('\n').find((l) => l.includes("id: 'gate-load-failed'"));
    expect(line).toContain('variant: undefined');
    expect(failureFlash(t, { success: false, status: 400 }, { header: 'h', variant: undefined }).content).toBe('The service did not accept the request.');
  });

  test('cancel and delete use the action wording', () => {
    const src = page('Dashboard.js');
    expect((src.match(/variant: 'action'/g) || []).length).toBe(4);
  });
});
