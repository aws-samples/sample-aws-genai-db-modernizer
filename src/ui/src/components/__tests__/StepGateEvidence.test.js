// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #1801: the results page's "Step 1 gate evidence" section. It shows report.json's
 * facts.step_gate -- the block the engineering report's section of that name and the
 * executive report's wave plan print -- as stored: the result and the cutover message
 * verbatim, the full load, lag and validation lines, the findings, each table and the
 * named exceptions. Nothing when the step's gate never ran. The fixture is the facts
 * layer's own output for a synthetic failed gate (tests/unit/shared/test_step_gate_facts.py
 * keeps it equal). Cloudscape components are plain DOM stand-ins; the component and the
 * English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import StepGateEvidence from '../StepGateEvidence';
import failed from './fixtures/stepGateFacts.json';

jest.mock('@cloudscape-design/components/container', () => ({
  __esModule: true,
  default: ({ header, children }) => <section data-container="true">{header}{children}</section>,
}));
jest.mock('@cloudscape-design/components/header', () => ({
  __esModule: true,
  default: ({ children, description }) => (
    <header><h2>{children}</h2><p data-figure="description">{description}</p></header>
  ),
}));
jest.mock('@cloudscape-design/components/expandable-section', () => ({
  __esModule: true,
  default: ({ headerText, children, defaultExpanded }) => (
    <section data-expandable="true" data-default-expanded={String(defaultExpanded)}>
      <h3>{headerText}</h3>
      {children}
    </section>
  ),
}));
jest.mock('@cloudscape-design/components/status-indicator', () => ({
  __esModule: true,
  default: ({ type, children }) => <span data-status={type}>{children}</span>,
}));
jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children, variant, ...props }) => (
    <div
      data-box-variant={variant}
      {...Object.fromEntries(Object.entries(props).filter(([k]) => k.startsWith('data-')))}
    >
      {children}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/table', () => ({
  __esModule: true,
  default: ({ items, columnDefinitions, trackBy }) => (
    <table>
      <thead>
        <tr>{columnDefinitions.map(c => <th key={c.id}>{c.header}</th>)}</tr>
      </thead>
      <tbody>
        {items.map(item => (
          <tr key={trackBy(item)} data-row={item.table}>
            {columnDefinitions.map(c => <td key={c.id}>{c.cell(item)}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  ),
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

// src/shared/cutover_message.py: CUTOVER_DISCLAIMER and REPLICATING_MESSAGE, word for word.
const DISCLAIMER = 'The cutover is your responsibility. The modernizer does not run, schedule or advise on it.';
const REPLICATING = 'Full load complete. Replication is in progress.';

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

function render(stepGate) {
  const synthesis = stepGate === undefined ? { facts: {} } : { facts: { step_gate: stepGate } };
  act(() => {
    root.render(<StepGateEvidence synthesis={synthesis} t={t} />);
  });
}

function figure(name) {
  return container.querySelector(`[data-figure="${name}"]`).textContent;
}

describe('StepGateEvidence', () => {
  it('renders nothing when the step never ran its gate', () => {
    render(undefined);
    expect(container.innerHTML).toBe('');
    render(null);
    expect(container.innerHTML).toBe('');
    act(() => root.render(<StepGateEvidence synthesis={null} t={t} />));
    expect(container.innerHTML).toBe('');
  });

  it('states the result, the time of the read and the cutover message as the facts word them', () => {
    render(failed);
    expect(container.querySelector('#step-gate-section h2').textContent).toBe('Step 1 gate evidence');
    expect(figure('description')).toBe(
      "What step 1's gate read from DMS and CloudWatch, read-only, on 2026-11-14 09:30 UTC. "
      + 'The figures are as DMS reported them at that time.',
    );
    expect(container.querySelector('[data-status]').getAttribute('data-status')).toBe('error');
    expect(figure('step-gate-result')).toBe(`Failed ${failed.result_line}`);
    expect(figure('step-gate-cutover-message')).toBe(DISCLAIMER);
    expect(figure('step-gate-load-line')).toBe(failed.load_line);
    expect(figure('step-gate-lag-line')).toBe(failed.lag_line);
    expect(figure('step-gate-validation-line')).toBe(failed.validation_line);
    expect(container.textContent).not.toContain(REPLICATING);
  });

  it('lists every finding with its check', () => {
    render(failed);
    const items = [...container.querySelectorAll('[data-figure="step-gate-findings"] li')].map(li => li.textContent);
    expect(items).toEqual(failed.gate_findings.map(f => `${f.check_text}: ${f.text}`));
    render({ ...failed, gate_findings_not_shown: 7 });
    expect(container.querySelector('[data-figure="step-gate-findings"]').textContent).toContain('And 7 more findings.');
  });

  it('lists each table and named exception in sections collapsed when the page opens', () => {
    render(failed);
    const sections = [...container.querySelectorAll('[data-expandable="true"]')];
    expect(sections.map(s => s.getAttribute('data-default-expanded'))).toEqual(['false', 'false']);
    expect(sections.map(s => s.querySelector('h3').textContent)).toEqual([
      'Each table (3 tables)',
      'Named exceptions (2)',
    ]);
    const orders = sections[0].querySelector('[data-row="shop.orders"]');
    expect([...orders.querySelectorAll('td')].map(td => td.textContent)).toEqual([
      'shop.orders', '1,233,321', 'Mismatched records', '3', '1', 'Validation failed',
    ]);
    const view = sections[1].querySelector('[data-row="shop.order_totals"]');
    expect([...view.querySelectorAll('td')].map(td => td.textContent)).toEqual([
      'shop.order_totals', '', 'View, not validated', 'not validated: no primary or unique key', 'not reported',
    ]);
  });

  it('a passed gate says the replicating message and the cutover message', () => {
    render({ ...failed, status: 'pass', status_text: 'Passed', result_line: REPLICATING, gate_findings: [] });
    expect(container.querySelector('[data-status]').getAttribute('data-status')).toBe('success');
    expect(figure('step-gate-result-line')).toBe(REPLICATING);
    expect(figure('step-gate-cutover-message')).toBe(DISCLAIMER);
    expect(container.querySelector('[data-figure="step-gate-findings"]')).toBeNull();
  });

  it('renders collection and DMS text as plain text, never markup', () => {
    const name = 'shop.x<script>alert(1)</script>';
    render({
      ...failed,
      gate_tables: [{ ...failed.gate_tables[0], table: name, validation_state: '<img src=x onerror=alert(1)>' }],
      named_exceptions: [{ ...failed.named_exceptions[0], table: name, reason: '<b>bold</b>' }],
    });
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.querySelector('td b')).toBeNull();
    expect(container.textContent).toContain(name);
    expect(container.textContent).toContain('<b>bold</b>');
  });
});

describe('the results page mounts it', () => {
  it('right after the migration roadmap, with the page synthesis', () => {
    const fs = require('fs');
    const path = require('path');
    const page = fs.readFileSync(path.join(__dirname, '../../pages/AnalysisResults.js'), 'utf8');
    expect(page).toContain('import StepGateEvidence from "../components/StepGateEvidence";');
    const plan = page.indexOf('<MigrationWavePlan');
    const mount = page.indexOf('<StepGateEvidence synthesis={synthesis} t={t} />');
    expect(plan).toBeGreaterThan(-1);
    expect(mount).toBeGreaterThan(plan);
    // Nothing but the wave plan's own props between the two.
    expect(page.slice(plan, mount).split('/>').length).toBe(2);
  });
});
