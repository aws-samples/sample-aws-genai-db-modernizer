// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2346: the shared error component and the flash items, on real Cloudscape components.
 * A failure prints one fixed message per kind, a Retry only where retrying can help, and
 * never the request URL, host, port, status, stack or the server's text (HTML and script
 * text included).
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import Flashbar from '@cloudscape-design/components/flashbar';
import RequestError from '../RequestError';
import { failureFlash } from '../../utils/requestFailure';

const en = require('../../locales/en.json');
const t = (key) => en[key] ?? key;
jest.mock('react-i18next', () => ({ useTranslation: () => ({ t: (key) => jest.requireActual('../../locales/en.json')[key] ?? key }) }));

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

const SERVER_TEXT = '<script>window.__pwned = 1</script><img src=x onerror="window.__pwned = 1"> GET http://10.0.0.5:8000/api/v1/assessments/x failed with 502 at Object.<anonymous> (server.py:12)';
const result = (status) => ({ success: false, status, error: new Error(SERVER_TEXT) });
// Port 8000 is matched as a whole number, so a millisecond timestamp attribute such as 1795046638000 is not a leak.
const LEAKS = /10\.0\.0\.5|\b8000\b|\b502\b|\b500\b|script|server\.py|api\/v1|onerror|Object\.</;

let container;
let root;
function render(node) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => { root.render(node); });
}
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  jest.restoreAllMocks();
});
const button = () => container.querySelector('button');

describe('the inline error', () => {
  test.each([
    [404, 'The service has no data for this request.', false],
    [422, 'The service did not accept the request.', false],
    [500, 'The service returned an error.', true],
    [408, 'The service took too long to answer.', true],
    [undefined, 'The service could not be reached.', true],
  ])('status %s reads "%s", retry offered: %s', (status, text, retry) => {
    const onRetry = jest.fn();
    render(<RequestError failure={result(status)} lead="The list did not load." onRetry={onRetry} t={t} />);
    expect(container.textContent).toContain(`The list did not load. ${text}`);
    expect(container.textContent).not.toMatch(LEAKS);
    if (retry) {
      expect(button().textContent).toBe('Retry');
      act(() => { button().click(); });
      expect(onRetry).toHaveBeenCalledTimes(1);
    } else {
      expect(button()).toBeNull();
    }
  });

  test('server text with HTML and script is never rendered, as text or as markup', () => {
    render(<RequestError failure={result(500)} onRetry={() => {}} t={t} />);
    expect(container.querySelector('script, img')).toBeNull();
    expect(container.innerHTML).not.toMatch(LEAKS);
    expect(window.__pwned).toBeUndefined();
  });

  test('the line sits in a live region, so it is announced', () => {
    render(<RequestError failure="server" t={t} />);
    const region = container.querySelector('[role="status"]');
    expect(region).not.toBeNull();
    expect(region.textContent).toContain('The service returned an error.');
  });

  test('no Retry without a handler, and an unknown value is never echoed', () => {
    render(<RequestError failure="GET http://10.0.0.5:8000/x returned 500: <b>boom</b>" t={t} />);
    expect(button()).toBeNull();
    expect(container.textContent).toBe('The request failed.');
    expect(container.querySelector('b')).toBeNull();
  });
});

describe('a flashbar item', () => {
  test('a failed request in a real Flashbar shows the header, the fixed message and Retry, and nothing else', () => {
    const retry = jest.fn();
    jest.spyOn(console, 'warn').mockImplementation(() => {});
    const item = failureFlash(t, result(500), { header: 'The assessment was not created', onRetry: retry });
    render(<Flashbar items={[item]} />);
    expect(container.textContent).toContain('The assessment was not created');
    expect(container.textContent).toContain('The service returned an error.');
    expect(container.innerHTML).not.toMatch(LEAKS);
    const retryButton = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Retry');
    act(() => { retryButton.click(); });
    expect(retry).toHaveBeenCalledTimes(1);
  });
});

describe('the leak pattern', () => {
  test.each([
    'localhost:8000',
    'http://127.0.0.1:8000/api',
    'GET http://10.0.0.5:8000/api/v1/x',
    'port 8000',
  ])('still catches %s', (text) => {
    expect(text).toMatch(LEAKS);
  });

  test.each(['1795046638000', 'data-ts="1795046638000"', 'id-80001', '18000'])('ignores the unrelated number in %s', (text) => {
    expect(text).not.toMatch(LEAKS);
  });

  test('a rendered message with localhost:8000 would fail the check', () => {
    render(<div data-ts="1795046638000">The service returned an error. http://127.0.0.1:8000/api</div>);
    expect(container.innerHTML).toMatch(LEAKS);
  });

  test('a timestamp attribute that contains 8000 does not trip the check', () => {
    jest.spyOn(Date, 'now').mockReturnValue(1795046638000);
    render(<RequestError failure={result(500)} onRetry={() => {}} t={t} />);
    container.setAttribute('data-ts', String(Date.now()));
    expect(container.outerHTML).toContain('1795046638000');
    expect(container.innerHTML).not.toMatch(LEAKS);
  });
});
