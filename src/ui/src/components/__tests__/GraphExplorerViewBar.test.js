// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #308 round 3: the explorer's view switcher, filter row and legend.
 * Cloudscape ships ESM this repo's Jest config cannot transform, so its
 * components are mocked as plain DOM elements that expose their props.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';

jest.mock('@cloudscape-design/components/segmented-control', () => ({
  __esModule: true,
  default: ({ options, selectedId, onChange }) => (
    <div data-testid="views">
      {options.map((o) => (
        <button key={o.id} data-selected={o.id === selectedId} onClick={() => onChange({ detail: { selectedId: o.id } })}>
          {o.text}
        </button>
      ))}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/toggle', () => ({
  __esModule: true,
  default: ({ checked, onChange, children }) => (
    <button type="button" data-testid="toggle" data-checked={String(checked)} onClick={() => onChange({ detail: { checked: !checked } })}>
      {children}
    </button>
  ),
}));
jest.mock('@cloudscape-design/components/slider', () => ({
  __esModule: true,
  default: ({ value, onChange, disabled }) => (
    <input
      data-testid="slider"
      type="range"
      value={value}
      disabled={disabled}
      onChange={(e) => onChange({ detail: { value: Number(e.target.value) } })}
    />
  ),
}));
jest.mock('@cloudscape-design/components/select', () => ({
  __esModule: true,
  default: ({ options, disabled }) => (
    <select data-testid="select" disabled={disabled}>
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  ),
}));
function mockPlain() {
  return {
    __esModule: true,
    default: ({ children, label, description, constraintText }) => (
      <div>
        <span className="label">{label}</span>
        <span className="description">{description}</span>
        <span className="constraint">{constraintText}</span>
        {children}
      </div>
    ),
  };
}
jest.mock('@cloudscape-design/components/form-field', () => mockPlain());
jest.mock('@cloudscape-design/components/space-between', () => mockPlain());
jest.mock('@cloudscape-design/components/box', () => mockPlain());

/* eslint-disable import/first */
import GraphExplorerViewBar from '../GraphExplorerViewBar';
/* eslint-enable import/first */

global.IS_REACT_ACT_ENVIRONMENT = true;

const t = (key, opts) => (opts ? `${key} ${JSON.stringify(opts)}` : key);
const traffic = { min: 0.01, max: 100, samples: [0.01, 1, 100] };
const waves = [{ wave: 3, engines: ['dynamodb'], engine_labels: ['DynamoDB'], migrating: true }];

let container;
let root;
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
  const all = {
    t, views: ['traffic', 'who-writes'], mode: 'traffic', onMode: jest.fn(), engines: [], engineFilter: null,
    onEngine: jest.fn(), waves, wave: 3, onWave: jest.fn(), traffic, cost: null, slider: 0,
    onSlider: jest.fn(), threshold: 0, below: null, ...props,
  };
  act(() => root.render(<GraphExplorerViewBar {...all} />));
  return all;
}

test('the switcher offers only the views the API lists and reports a pick', () => {
  const props = render({});
  const buttons = [...container.querySelectorAll('[data-testid=views] button')];
  expect(buttons.map((b) => b.textContent)).toEqual(['graph-explorer.views.traffic', 'graph-explorer.views.who-writes']);
  expect(buttons[0].getAttribute('data-selected')).toBe('true');
  act(() => buttons[1].click());
  expect(props.onMode).toHaveBeenCalledWith('who-writes');
});

test('the slider reports its position, and the hidden count is the API figure', () => {
  const props = render({ slider: 40, threshold: 0.4, below: { min_calls_per_second: 0.4, hidden_count: 7, hidden_query_ids: [] } });
  const slider = container.querySelector('[data-testid=slider]');
  act(() => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(slider, '60');
    slider.dispatchEvent(new Event('input', { bubbles: true }));
  });
  expect(props.onSlider).toHaveBeenCalledWith(60);
  expect(container.textContent).toContain('graph-explorer.filters.hidden {"count":7,"value":"0.4"}');
});

test('without a threshold nothing is reported hidden', () => {
  render({});
  expect(container.textContent).not.toContain('graph-explorer.filters.hidden');
  expect(container.textContent).toContain('graph-explorer.filters.min-traffic-off');
});

test('the slider is off when the job has no traffic range', () => {
  render({ traffic: null });
  expect(container.querySelector('[data-testid=slider]').disabled).toBe(true);
});

test('the legend shows thin, medium and thick samples with the API values', () => {
  render({});
  ['0.01', '1', '100'].forEach((v) => {
    expect(container.textContent).toContain(`graph-explorer.legend.traffic {"value":"${v}"}`);
  });
});

test('the wave filter is shown only when the job has waves, and offers every wave', () => {
  const props = render({});
  const selects = container.querySelectorAll('[data-testid=select]');
  expect(selects).toHaveLength(2);
  expect([...selects[1].options].map((o) => o.textContent)).toEqual([
    'graph-explorer.filters.all-waves',
    'graph-explorer.filters.wave-option {"wave":3,"engines":"DynamoDB"}',
  ]);
  expect(props.onWave).not.toHaveBeenCalled();
  render({ waves: [] });
  expect(container.querySelectorAll('[data-testid=select]')).toHaveLength(1);
});

test('the cost legend shows the report engine costs', () => {
  render({ mode: 'cost', cost: { min: 12.29, max: 192.12, samples: [12.29, 192.12] } });
  expect(container.textContent).toContain('graph-explorer.legend.cost {"value":"12.29"}');
  expect(container.textContent).toContain('graph-explorer.legend.cost {"value":"192.12"}');
});

describe('the icon legend', () => {
  const { IconLegend } = jest.requireActual('../GraphExplorerViewBar');
  const { ENGINE_ICONS, NODE_TYPE_ICONS } = jest.requireActual('../../utils/nodeIcons');

  test('lists the table icon, the placeholder when drawn, and each engine drawn, each with its name', () => {
    act(() => root.render(<IconLegend t={t} engines={['aurora_mysql', 'dynamodb']} placeholder />));
    const legend = container.querySelector('[data-testid=graph-explorer-icon-legend]');
    const srcs = [...legend.querySelectorAll('img')].map((img) => img.getAttribute('src'));
    expect(srcs).toEqual([NODE_TYPE_ICONS.table.icon, NODE_TYPE_ICONS.placeholder_table.icon, ENGINE_ICONS.aurora_mysql, ENGINE_ICONS.dynamodb]);
    // Never the icon alone: every one has its name, and the image itself is decoration.
    expect(legend.textContent).toContain('graph-explorer.icons.table');
    expect(legend.textContent).toContain('Aurora MySQL');
    expect(legend.textContent).toContain('DynamoDB');
    [...legend.querySelectorAll('img')].forEach((img) => expect(img.getAttribute('alt')).toBe(''));
  });

  test('without a placeholder drawn, it is not listed', () => {
    act(() => root.render(<IconLegend t={t} engines={[]} />));
    expect(container.querySelectorAll('[data-testid=graph-explorer-icon-legend] img')).toHaveLength(1);
  });
});

test('the link legend explains bundles and fanned-out queries', () => {
  const { LinkLegend } = jest.requireActual('../GraphExplorerViewBar');
  act(() => root.render(<LinkLegend t={t} />));
  const legend = container.querySelector('[data-testid=graph-explorer-link-legend]');
  expect(legend.textContent).toContain('graph-explorer.legend.bundle');
  expect(legend.textContent).toContain('graph-explorer.legend.query');
});
