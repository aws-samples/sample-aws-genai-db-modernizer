/**
 * #957: the results page's download panel lists the job's deliverables/ folder
 * (GET /assessments/{job_id}/deliverables) and downloads each file from the
 * API's download route. Cloudscape components are plain DOM stand-ins (see
 * QueryRoutingTable.test.js); the component and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import DeliverablesPanel, { formatBytes } from '../DeliverablesPanel';

jest.mock('@cloudscape-design/components/container', () => ({
  __esModule: true,
  default: ({ header, children }) => <section>{header}{children}</section>,
}));
jest.mock('@cloudscape-design/components/header', () => ({
  __esModule: true,
  default: ({ children, description }) => <header><h2>{children}</h2><p className="mock-description">{description}</p></header>,
}));
jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/column-layout', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/status-indicator', () => ({
  __esModule: true,
  default: ({ children, type }) => <div data-status={type}>{children}</div>,
}));
jest.mock('@cloudscape-design/components/button', () => ({
  __esModule: true,
  default: ({ children, href, download, ariaLabel }) => (
    <a href={href} download={download} aria-label={ariaLabel}>{children}</a>
  ),
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

const FILES = [
  { filename: 'executive-report.pdf', title: 'Executive report (PDF)', audience: 'Executives', size_bytes: 1258291, path: 'assessments/job-1/deliverables/executive-report.pdf' },
  { filename: 'engineering-report.md', title: 'Engineering report (Markdown)', audience: 'The team', size_bytes: 20480, path: 'assessments/job-1/deliverables/engineering-report.md' },
  { filename: 'assessment-data.json', title: 'Assessment Data (JSON)', audience: 'Tools', size_bytes: 512, path: 'assessments/job-1/deliverables/assessment-data.json' },
  { filename: 'index.md', title: 'Folder index (Markdown)', audience: 'Everyone', size_bytes: 900, path: 'assessments/job-1/deliverables/index.md' },
];

let container;
let root;
let t;

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
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function render(props) {
  act(() => {
    root.render(<DeliverablesPanel baseUrl="http://localhost:8000/api/v1/" t={t} {...props} />);
  });
}

test('lists every file the API returns, in its order, each with a download link', () => {
  render({ files: FILES, loading: false, error: null });
  const rows = [...container.querySelectorAll('[data-deliverable]')];
  expect(rows.map(r => r.getAttribute('data-deliverable'))).toEqual(
    ['executive-report.pdf', 'engineering-report.md', 'assessment-data.json', 'index.md'],
  );
  const pdf = rows[0];
  expect(pdf.textContent).toContain('Executive report (PDF)');
  expect(pdf.textContent).toContain('Executives');
  expect(pdf.textContent).toContain('1.2 MB');
  const link = pdf.querySelector('a');
  expect(link.getAttribute('href')).toBe('http://localhost:8000/api/v1/assessments/job-1/deliverables/executive-report.pdf');
  expect(link.getAttribute('download')).toBe('executive-report.pdf');
  expect(link.getAttribute('aria-label')).toBe('Download executive-report.pdf');
});

test('says so when the folder has no files yet', () => {
  render({ files: [], loading: false, error: null });
  expect(container.querySelectorAll('[data-deliverable]').length).toBe(0);
  expect(container.textContent).toContain('No deliverables yet');
});

test('shows the loading state, then the error state', () => {
  render({ files: [], loading: true, error: null });
  expect(container.querySelector('[data-status="loading"]').textContent).toBe('Loading deliverables');
  render({ files: [], loading: false, error: 'boom' });
  expect(container.querySelector('[data-status="error"]').textContent).toBe('Deliverables unavailable: boom');
});

test('a hostile file title is rendered as text, never as markup', () => {
  const hostile = '<img src=x onerror=alert(1)>';
  render({ files: [{ ...FILES[0], title: hostile, audience: hostile }], loading: false, error: null });
  expect(container.querySelector('img')).toBeNull();
  expect(container.textContent).toContain(hostile);
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
