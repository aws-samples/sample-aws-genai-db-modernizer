// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2314 (after #957, #1028): the results page header has one Download menu, a real
 * Cloudscape ButtonDropdown. It lists each deliverable the job's deliverables/ folder
 * holds, as GET /assessments/{job_id}/deliverables lists it (the folder's own
 * definition, FOLDER_ENTRIES in src/report/deliverables.py): the entry's title, which
 * names the file type, with the file name and size under it, in the listing's order.
 * The folder index stays in the folder, without a menu item.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import en from '../../locales/en.json';
// RequestError reads the app's i18n instance through useTranslation.
import '../../i18n';
import DeliverablesPanel, { formatBytes, menuFiles } from '../DeliverablesPanel';

global.IS_REACT_ACT_ENVIRONMENT = true;

// The listing as the API returns it for a job rendered today: FOLDER_ENTRIES' order,
// then the folder index.
const FILES = [
  { filename: 'executive-report.pdf', title: 'Executive report (PDF)', audience: 'Executives', size_bytes: 1258291, path: 'assessments/job-1/deliverables/executive-report.pdf' },
  { filename: 'engineering-report.md', title: 'Engineering report (Markdown)', audience: 'The team', size_bytes: 20480, path: 'assessments/job-1/deliverables/engineering-report.md' },
  { filename: 'assessment-data.json', title: 'Assessment Data (JSON)', audience: 'Tools', size_bytes: 512, path: 'assessments/job-1/deliverables/assessment-data.json' },
  { filename: 'engineering-report-detailed.md', title: 'Detailed engineering report (Markdown)', audience: 'Engineers', size_bytes: 409600, path: 'assessments/job-1/deliverables/engineering-report-detailed.md' },
  { filename: 'index.md', title: 'Folder index (Markdown)', audience: 'Everyone', size_bytes: 900, path: 'assessments/job-1/deliverables/index.md' },
];
const BASE = 'http://localhost:8000/api/v1/';

let container;
let root;
let t;
let warn;
let error;

beforeAll(async () => {
  const i18n = createInstance();
  await i18n.init({
    resources: { en: { translation: en } },
    lng: 'en',
    fallbackLng: 'en',
    keySeparator: false,
    interpolation: { escapeValue: false },
  });
  t = i18n.t.bind(i18n);
});

beforeEach(() => {
  warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
  error = jest.spyOn(console, 'error').mockImplementation(() => {});
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  // Cloudscape warns about a prop it does not support; React about a bad child.
  expect(warn).not.toHaveBeenCalled();
  expect(error).not.toHaveBeenCalled();
  warn.mockRestore();
  error.mockRestore();
});

function render(props) {
  act(() => {
    root.render(<DeliverablesPanel baseUrl={BASE} t={t} {...props} />);
  });
  return createWrapper(container).findButtonDropdown();
}

function openItems(dropdown) {
  act(() => dropdown.openDropdown());
  return [...container.querySelectorAll('[data-deliverable]')];
}

test('one Download button, the only download control in the header', () => {
  const dropdown = render({ files: FILES, loading: false, error: null });
  expect(dropdown).not.toBeNull();
  expect(createWrapper(container).findAllButtonDropdowns()).toHaveLength(1);
  expect(dropdown.findNativeButton().getElement().textContent).toBe('Download');
  // No separate download buttons next to it.
  expect(createWrapper(container).findAllButtons()).toHaveLength(1);
});

test('the menu lists every deliverable the folder holds, in the listing order, by its title', () => {
  const items = openItems(render({ files: FILES, loading: false, error: null }));
  expect(items.map(i => i.getAttribute('data-deliverable'))).toEqual([
    'executive-report.pdf',
    'engineering-report.md',
    'assessment-data.json',
    'engineering-report-detailed.md',
  ]);
  // Each title names the file type; the file name and size are under it.
  const text = items.map(i => i.textContent);
  expect(text[0]).toBe('Executive report (PDF)executive-report.pdf · 1.2 MB');
  expect(text[3]).toBe('Detailed engineering report (Markdown)engineering-report-detailed.md · 400 KB');
  // The folder index stays in the folder.
  expect(container.textContent).not.toContain('Folder index');
});

test('each item downloads its file from the API and says which file in its name', () => {
  const items = openItems(render({ files: FILES, loading: false, error: null }));
  const link = items[0].querySelector('a');
  expect(link.getAttribute('href')).toBe(`${BASE}assessments/job-1/deliverables/executive-report.pdf`);
  expect(link.getAttribute('download')).toBe('executive-report.pdf');
  expect(link.getAttribute('aria-label')).toBe('Download executive-report.pdf, Executive report (PDF)');
  // A download stays in this tab.
  expect(link.getAttribute('target')).toBeNull();
});

test('an old job whose folder lacks a file lists only what it holds', () => {
  const old = FILES.filter(f => f.filename !== 'engineering-report-detailed.md' && f.filename !== 'engineering-report.md');
  const items = openItems(render({ files: old, loading: false, error: null }));
  expect(items.map(i => i.getAttribute('data-deliverable'))).toEqual(['executive-report.pdf', 'assessment-data.json']);
});

test('menuFiles drops the folder index and anything without a file name or path', () => {
  expect(menuFiles(FILES).map(f => f.filename)).toEqual([
    'executive-report.pdf', 'engineering-report.md', 'assessment-data.json', 'engineering-report-detailed.md',
  ]);
  expect(menuFiles([{ filename: 'x.md' }, { path: 'p' }, null])).toEqual([]);
  expect(menuFiles(undefined)).toEqual([]);
});

test('with nothing in the folder yet the button is disabled and says why', () => {
  for (const files of [[], [FILES[4]]]) {
    const dropdown = render({ files, loading: false, error: null });
    expect(dropdown.findNativeButton().getElement().disabled || dropdown.findNativeButton().getElement().getAttribute('aria-disabled') === 'true').toBe(true);
    expect(container.textContent).toContain('No deliverables yet');
  }
});

test('shows the loading state, then the error state with a retry', () => {
  let dropdown = render({ files: [], loading: true, error: null });
  expect(dropdown.findNativeButton().getElement().getAttribute('aria-disabled')).toBe('true');
  expect(container.textContent).toContain('Loading deliverables');
  const onRetry = jest.fn();
  render({ files: [], loading: false, error: 'server', onRetry });
  expect(container.textContent).toContain('Deliverables unavailable. The service returned an error.');
  // #2346: an unknown error value is never printed.
  render({ files: [], loading: false, error: 'GET http://10.0.0.5:8000/api/v1/x returned 500: <b>boom</b>', onRetry });
  expect(container.textContent).toContain('Deliverables unavailable. The request failed.');
  expect(container.textContent).not.toContain('10.0.0.5');
  expect(container.querySelector('b')).toBeNull();
});

test('a hostile file title is rendered as text, never as markup', () => {
  const hostile = '<img src=x onerror=alert(1)>';
  const items = openItems(render({ files: [{ ...FILES[0], title: hostile, audience: hostile }], loading: false, error: null }));
  expect(container.querySelector('img')).toBeNull();
  expect(items[0].textContent).toContain(hostile);
});

test.each([
  [0, '0 B'],
  [512, '512 B'],
  [20480, '20 KB'],
  [1258291, '1.2 MB'],
  [undefined, ''],
])('formatBytes(%p) is %p', (n, expected) => {
  expect(formatBytes(n)).toBe(expected);
});
