// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2315: the executive summary leads with the decision. The Container header is the
 * line the facts word for it (facts.executive_brief.decision_headline), never a model's
 * text, with the decision wave's readiness as a StatusIndicator; five figures follow, then
 * the summary's paragraphs under "Why". A report with no headline keeps the "Executive
 * summary" title. Cloudscape components are plain DOM stand-ins (see
 * QueryRoutingTable.test.js); the components and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import ExecutiveSummary from '../ExecutiveSummary';
import { costBaselineStats } from '../../utils/tcoAnalysis';

jest.mock('@cloudscape-design/components/container', () => ({
  __esModule: true,
  default: ({ id, header, children }) => <section id={id}>{header}{children}</section>,
}));
jest.mock('@cloudscape-design/components/header', () => ({
  __esModule: true,
  default: ({ children, description, actions, variant }) => (
    <header data-variant={variant}>
      <h2>{children}</h2>
      {description && <p className="mock-description">{description}</p>}
      {actions && <div className="mock-actions">{actions}</div>}
    </header>
  ),
}));
jest.mock('@cloudscape-design/components/expandable-section', () => ({
  __esModule: true,
  default: ({ headerText, defaultExpanded, children }) => (
    <details className="mock-why" open={defaultExpanded}><summary>{headerText}</summary>{children}</details>
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
jest.mock('@cloudscape-design/components/key-value-pairs', () => ({
  __esModule: true,
  default: ({ items }) => (
    <dl className="mock-kvp">
      {items.map(item => <div key={item.label}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}
    </dl>
  ),
}));
jest.mock('@cloudscape-design/components/status-indicator', () => ({
  __esModule: true,
  default: ({ type, children }) => <span className="mock-status" data-type={type}>{children}</span>,
}));
jest.mock('@cloudscape-design/components/popover', () => ({
  __esModule: true,
  default: ({ children }) => <span>{children}</span>,
}));
jest.mock('@cloudscape-design/components/link', () => ({
  __esModule: true,
  default: ({ children }) => <a>{children}</a>,
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

const WAVES = [
  { wave: 1, engines: ['aurora_mysql'], kind: 'homogeneous', readiness: 'suggested' },
  { wave: 2, engines: ['elasticache'], kind: 'cache', readiness: 'suggested' },
  { wave: 3, engines: ['dynamodb'], kind: 'migration', readiness: 'suggested' },
];

function synthesisWith({ headline = 'Approve wave 1: move wordpress to Aurora MySQL', decision = 1, readiness = 'suggested', warnings = 0 } = {}) {
  return {
    summary: 'Recommendation: move it.\nRisk: medium.\nDecision: approve wave 1.',
    facts: {
      open_risk_count: 9,
      critical_high_risk_count: 0,
      migration_wave_count: 3,
      query_total_count: 107,
      source_table_count: 50,
      waves: WAVES.map(w => ({
        wave: w.wave,
        readiness: w.wave === 1 ? readiness : 'suggested',
        readiness_line: 'its design was reviewed and no check holds it',
        open_warning_count: w.wave === 1 ? warnings : 0,
      })),
      executive_brief: {
        overall_risk_level: 'MEDIUM',
        decision_wave: decision,
        ...(headline === null ? {} : { decision_headline: headline }),
        waves: WAVES,
      },
    },
  };
}

function render(synthesis) {
  act(() => {
    root.render(
      <ExecutiveSummary
        synthesis={synthesis}
        t={t}
        why={<p className="prose">{synthesis.summary}</p>}
        statsProps={{ projectedCost: 507.39, costBaseline: costBaselineStats({}) }}
      />,
    );
  });
}

const headline = () => container.querySelector('[data-figure="decision-headline"]');
const status = () => container.querySelector('[data-figure="decision-readiness"]');

test('the header states the decision the facts word, with the decision wave as suggested', () => {
  render(synthesisWith());
  const section = container.querySelector('#executive-summary-section');
  expect(section).not.toBeNull();
  expect(section.querySelector('header').getAttribute('data-variant')).toBe('h2');
  expect(headline().textContent).toBe('Approve wave 1: move wordpress to Aurora MySQL');
  expect(section.querySelector('.mock-description').textContent).toBe('Executive summary');
  expect(status().getAttribute('data-readiness')).toBe('suggested');
  expect(status().textContent).toBe('Wave 1: Suggested');
  expect(status().querySelector('.mock-status').getAttribute('data-type')).toBe('success');
});

test('the decision comes first, then the five figures, then the paragraphs under Why', () => {
  render(synthesisWith());
  const order = ['[data-figure="decision-headline"]', '.mock-kvp', '.mock-why']
    .map(sel => container.querySelector(sel));
  order.slice(1).forEach((el, i) => {
    expect(order[i].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
  const why = container.querySelector('.mock-why');
  expect(why.querySelector('summary').textContent).toBe('Why');
  expect(why.open).toBe(true);
  expect(why.querySelector('.prose').textContent).toContain('Recommendation: move it.');
});

test('the header never takes the decision from the prose', () => {
  const synthesis = synthesisWith();
  synthesis.summary = 'Decision: approve wave 3 now and cut over next week.';
  render(synthesis);
  expect(headline().textContent).toBe('Approve wave 1: move wordpress to Aurora MySQL');
});

test('with no wave suggested, the header states the plan and the first wave its readiness', () => {
  render(synthesisWith({
    headline: 'Move trailgear to Aurora PostgreSQL, ElastiCache and DynamoDB in 3 waves',
    decision: null,
    readiness: 'not_designed',
  }));
  expect(headline().textContent).toBe('Move trailgear to Aurora PostgreSQL, ElastiCache and DynamoDB in 3 waves');
  expect(status().textContent).toBe('Wave 1: Not designed');
  expect(status().querySelector('.mock-status').getAttribute('data-type')).toBe('pending');
});

test('a suggested wave with warnings to accept says so', () => {
  render(synthesisWith({ warnings: 2 }));
  expect(status().textContent).toBe('Wave 1: Suggested, 2 warnings to accept');
  expect(status().querySelector('.mock-status').getAttribute('data-type')).toBe('warning');
});

test('a report saved before the headline and the readiness keeps the title and shows no status', () => {
  const synthesis = synthesisWith({ headline: null });
  synthesis.facts.waves = synthesis.facts.waves.map(({ wave }) => ({ wave }));
  render(synthesis);
  expect(headline().textContent).toBe('Executive summary');
  expect(container.querySelector('.mock-description')).toBeNull();
  expect(status()).toBeNull();
  render({ summary: '' });
  expect(headline().textContent).toBe('Executive summary');
  expect(status()).toBeNull();
});
