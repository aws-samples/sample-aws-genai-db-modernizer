/**
 * #957: the results page's migration roadmap. It shows report.json's waves (the
 * same ones the executive report and the engineering report show) with each
 * wave's headline, rationale, gate and tables, and computes nothing itself
 * (ADR-032). Cloudscape components are plain DOM stand-ins (see
 * QueryRoutingTable.test.js); the component and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import fixture from '../../utils/__tests__/fixtures/waveHeadlines.json';
import MigrationWavePlan from '../MigrationWavePlan';

jest.mock('@cloudscape-design/components/container', () => ({
  __esModule: true,
  default: ({ header, children }) => <section>{header}{children}</section>,
}));
jest.mock('@cloudscape-design/components/header', () => ({
  __esModule: true,
  default: ({ children, description, counter }) => (
    <header><h2>{children}{counter ? ` ${counter}` : ''}</h2><p className="mock-description">{description}</p></header>
  ),
}));
jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/badge', () => ({
  __esModule: true,
  default: ({ children }) => <span className="mock-badge">{children}</span>,
}));
jest.mock('@cloudscape-design/components/expandable-section', () => ({
  __esModule: true,
  default: ({ headerText, children }) => <details><summary>{headerText}</summary>{children}</details>,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

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

function render(synthesis) {
  act(() => {
    root.render(<MigrationWavePlan synthesis={synthesis} t={t} />);
  });
}

test('renders one card per wave, in report order, with the reports\' headline', () => {
  render(fixture.synthesis);
  const cards = [...container.querySelectorAll('[data-wave]')];
  expect(cards.map(c => c.getAttribute('data-wave'))).toEqual(['1', '2', '3', '4', '5']);
  cards.forEach((card) => {
    const n = card.getAttribute('data-wave');
    expect(card.querySelector('[data-figure="wave-stats"]').textContent).toBe(fixture.expected_headlines[n]);
  });
});

test('the heading states the facts layer\'s wave count and the one-suggested-path note', () => {
  render(fixture.synthesis);
  expect(container.querySelector('h2').textContent).toBe('Migration roadmap (5 waves)');
  expect(container.querySelector('.mock-description').textContent).toBe(
    'One suggested adoption path, not the only one — to modernize in one step instead, '
    + 'adopt the target architecture in Executive summary above directly.',
  );
});

test('each card shows the wave title, its engines, rationale, gate and tables', () => {
  render(fixture.synthesis);
  const first = container.querySelector('[data-wave="1"]');
  expect(first.textContent).toContain('Wave 1: Move to Aurora MySQL');
  expect([...first.querySelectorAll('[data-engine]')].map(e => e.getAttribute('data-engine'))).toEqual(['aurora_mysql']);
  expect(first.querySelector('[data-engine]').textContent).toBe('Aurora MySQL');
  expect(first.textContent).toContain('Whole schema first.');
  expect(first.querySelector('[data-figure="wave-gate"]').textContent).toBe('Gate before the next wave: Parity validated.');
  expect(first.querySelector('[data-figure="wave-tables"]').textContent).toBe('wp.a, wp.b, wp.v1');
});

test('a wave with no gate or no tables leaves that line out', () => {
  render(fixture.synthesis);
  const cache = container.querySelector('[data-wave="2"]');
  expect(cache.querySelector('[data-figure="wave-tables"]')).toBeNull();
  const search = container.querySelector('[data-wave="4"]');
  expect(search.querySelector('[data-figure="wave-gate"]')).toBeNull();
});

test('a wave with no title is named by its engines, like the reports', () => {
  const wave = { wave: 1, engines: ['dynamodb', 'documentdb'], query_count: 2, workload_share_percent: 5, table_count: 0 };
  render({ migration_waves: [wave], facts: { waves: [], migration_wave_count: 1 } });
  expect(container.querySelector('[data-wave="1"]').textContent).toContain('Wave 1: DynamoDB, DocumentDB');
  expect(container.querySelector('h2').textContent).toBe('Migration roadmap (1 wave)');
});

test('renders nothing when the report has no waves', () => {
  render({ migration_waves: [], facts: { waves: [], migration_wave_count: 0 } });
  expect(container.innerHTML).toBe('');
  render({});
  expect(container.innerHTML).toBe('');
});

test('report text is rendered as text, never as markup', () => {
  const hostile = '<img src=x onerror=alert(1)><script>alert(1)</script>';
  const wave = {
    wave: 1, title: hostile, engines: ['dynamodb'], query_count: 1, workload_share_percent: 1,
    table_count: 1, tables: [hostile], rationale: hostile, gate: hostile,
  };
  render({ migration_waves: [wave], facts: { waves: [], migration_wave_count: 1 } });
  expect(container.querySelector('img')).toBeNull();
  expect(container.querySelector('script')).toBeNull();
  expect(container.textContent).toContain(hostile);
});
