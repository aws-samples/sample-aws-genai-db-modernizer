/**
 * #724: the target-database checkboxes come from the engine catalog's status. Active
 * engines (both Aurora engines among them) can be ticked; Neptune and Keyspaces are
 * future placeholders (ADR-031 Decision 9), shown disabled as "Coming soon", and
 * "Select all" never turns them on, so the request never names them. The legacy
 * generic "aurora" id is not offered. The page and the English strings are real;
 * Cloudscape components are plain DOM stand-ins, as in CreateAnalysis.credentials.test.js.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import en from '../locales/en.json';
import ApiManager from '../classes/ApiManager';
import CreateAnalysis from '../pages/CreateAnalysis';
import { ENGINE_LABELS, ENGINE_STATUS } from '../utils/engineCatalog.generated';

jest.mock('react-i18next', () => {
  const strings = jest.requireActual('../locales/en.json');
  const t = (key, vars = {}) => (strings[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_, name) => vars[name]);
  return { useTranslation: () => ({ t }) };
});
jest.mock('react-router-dom', () => ({ useNavigate: () => jest.fn() }));
jest.mock('../classes/ApiManager', () => ({ __esModule: true, default: jest.fn() }));
jest.mock('../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('../config/GlobalConfigurations', () => ({
  SideNavigationConfigurations: {},
  ApiConfigurations: { baseUrl: 'http://localhost:8000/api/v1/' },
}));

const mockDiv = ({ children }) => <div>{children}</div>;
jest.mock('@cloudscape-design/components/app-layout', () => ({
  __esModule: true,
  default: ({ breadcrumbs, content }) => <div>{breadcrumbs}{content}</div>,
}));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/wizard', () => ({
  __esModule: true,
  default: ({ steps, onSubmit }) => (
    <div>
      {steps.map((step) => <section key={step.title}>{step.content}</section>)}
      <button type="button" data-testid="submit" onClick={onSubmit}>submit</button>
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/space-between', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
jest.mock('@cloudscape-design/components/button', () => ({
  __esModule: true,
  default: ({ children, onClick }) => <button type="button" onClick={onClick}>{children}</button>,
}));
jest.mock('@cloudscape-design/components/form-field', () => ({
  __esModule: true,
  default: ({ label, description, constraintText, errorText, children }) => (
    <div data-field={label}>
      <span>{label}</span>
      <p>{description}</p>
      <p>{constraintText}</p>
      <p className="error">{errorText}</p>
      {children}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/select', () => ({ __esModule: true, default: () => <select /> }));
jest.mock('@cloudscape-design/components/input', () => ({
  __esModule: true,
  default: ({ value, onChange, type, placeholder, disabled }) => (
    <input
      type={type || 'text'}
      placeholder={placeholder}
      value={value ?? ''}
      disabled={disabled}
      onChange={(e) => onChange && onChange({ detail: { value: e.target.value } })}
    />
  ),
}));
jest.mock('@cloudscape-design/components/radio-group', () => ({
  __esModule: true,
  default: ({ items, value, onChange }) => (
    <div role="radiogroup">
      {items.map((item) => (
        <label key={item.value}>
          <input
            type="radio"
            value={item.value}
            checked={value === item.value}
            onChange={() => onChange({ detail: { value: item.value } })}
          />
          {item.label}
          <span>{item.description}</span>
        </label>
      ))}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/toggle', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/slider', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/checkbox', () => ({
  __esModule: true,
  default: ({ checked, disabled, onChange, description, children }) => (
    <label data-checkbox={typeof children === 'string' ? children : ''}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange && onChange({ detail: { checked: e.target.checked } })}
      />
      {children}
      <span className="description">{description}</span>
    </label>
  ),
}));
jest.mock('@cloudscape-design/components/expandable-section', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
jest.mock('@cloudscape-design/components/textarea', () => ({ __esModule: true, default: () => <textarea /> }));
jest.mock('@cloudscape-design/components/side-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/flashbar', () => ({
  __esModule: true,
  default: ({ items }) => (
    <div data-testid="flashbar">
      {items.map((item) => <p key={item.id}>{item.header}: {item.content}</p>)}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/tiles', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/box', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
jest.mock('@cloudscape-design/components/file-upload', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/container', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
jest.mock('@cloudscape-design/components/header', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
jest.mock('@cloudscape-design/components/progress-bar', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/status-indicator', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
jest.mock('@cloudscape-design/components/alert', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
jest.mock('@cloudscape-design/components/column-layout', () => ({ __esModule: true, default: (p) => mockDiv(p) }));

global.IS_REACT_ACT_ENVIRONMENT = true;

const SECRET_ARN = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:app-AbCdEf'; // pragma: allowlist secret

let container;
let root;
let execute;

beforeEach(() => {
  execute = jest.fn().mockResolvedValue({ 'create-assessment': { success: true, job_id: 'job-1' } });
  ApiManager.mockImplementation(() => ({ execute }));
  jest.spyOn(console, 'log').mockImplementation(() => {});
  jest.spyOn(console, 'error').mockImplementation(() => {});
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(<CreateAnalysis />));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  jest.restoreAllMocks();
});

const targetField = () => container.querySelector(`[data-field="${en['create-analysis.target-databases.label']}"]`);
const checkbox = (label) => targetField().querySelector(`[data-checkbox="${label}"]`);
const box = (label) => checkbox(label).querySelector('input');
const button = (text) => [...container.querySelectorAll('button')].find((b) => b.textContent === text);

function type(input, value) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function fillSource() {
  const field = (label) => container.querySelector(`[data-field="${label}"] input`);
  type(field(en['create-analysis.source.host.label']), 'db.example.internal');
  type(field(en['create-analysis.source.port.label']), '3306');
  type(field(en['create-analysis.source.database-name.label']), 'app');
  type(field(en['create-analysis.source.secret-arn.label']), SECRET_ARN);
}

async function submit() {
  await act(async () => {
    container.querySelector('[data-testid="submit"]').click();
  });
}

async function submitValidForm() {
  fillSource();
  await submit();
  return execute.mock.calls[0][0][0].params.target_databases;
}

const ids = (status) => Object.keys(ENGINE_LABELS).filter((id) => ENGINE_STATUS[id] === status);
const comingSoon = (id) => `${ENGINE_LABELS[id]} (Coming soon)`;

test('every active catalog engine is offered and enabled, both Aurora engines included', () => {
  expect(ids('active')).toEqual(expect.arrayContaining(['aurora_mysql', 'aurora_postgresql']));
  ids('active').forEach((id) => {
    expect(box(ENGINE_LABELS[id]).disabled).toBe(false);
  });
});

test('Neptune and Keyspaces are disabled, unticked and labelled Coming soon', () => {
  expect(ids('future').sort()).toEqual(['keyspaces', 'neptune']);
  ids('future').forEach((id) => {
    expect(checkbox(ENGINE_LABELS[id])).toBeNull();
    expect(box(comingSoon(id)).disabled).toBe(true);
    expect(box(comingSoon(id)).checked).toBe(false);
  });
});

test('Aurora is no longer described as Coming soon, and the legacy generic id is not offered', () => {
  ['aurora_mysql', 'aurora_postgresql'].forEach((id) => {
    expect(checkbox(ENGINE_LABELS[id]).textContent).not.toMatch(/coming soon/i);
  });
  expect(checkbox('Aurora')).toBeNull();
  expect(checkbox(comingSoon('aurora'))).toBeNull();
});

test('Select all ticks every active engine and never a placeholder', async () => {
  act(() => button(en['create-analysis.target-databases.deselect-all']).click());
  act(() => button(en['create-analysis.target-databases.select-all']).click());

  ids('active').forEach((id) => expect(box(ENGINE_LABELS[id]).checked).toBe(true));
  ids('future').forEach((id) => expect(box(comingSoon(id)).checked).toBe(false));
  expect(await submitValidForm()).toEqual(ids('active'));
});

test('the request names only the engines the user left ticked', async () => {
  act(() => button(en['create-analysis.target-databases.deselect-all']).click());
  act(() => box('DynamoDB').click());
  act(() => box('Aurora MySQL').click());

  expect(await submitValidForm()).toEqual(['dynamodb', 'aurora_mysql']);
});

test('with every engine deselected the form refuses to submit', async () => {
  fillSource();
  act(() => button(en['create-analysis.target-databases.deselect-all']).click());
  await submit();
  expect(execute).not.toHaveBeenCalled();
  expect(targetField().querySelector('.error').textContent).toMatch(/at least one target database/);
});

// The same behaviour with literal ids, independent of the generated catalog module.
test('after Select all the request names both Aurora engines and no placeholder or legacy id', async () => {
  act(() => button(en['create-analysis.target-databases.select-all']).click());
  const sent = await submitValidForm();
  expect(sent).toEqual(expect.arrayContaining(['aurora_mysql', 'aurora_postgresql']));
  expect(sent).not.toContain('neptune');
  expect(sent).not.toContain('keyspaces');
  expect(sent).not.toContain('aurora');
});

test('no Aurora checkbox says Coming soon', () => {
  const aurora = [...targetField().querySelectorAll('[data-checkbox]')]
    .filter((node) => /aurora/i.test(node.textContent));
  expect(aurora.length).toBeGreaterThan(0);
  aurora.forEach((node) => expect(node.textContent).not.toMatch(/coming soon/i));
});
