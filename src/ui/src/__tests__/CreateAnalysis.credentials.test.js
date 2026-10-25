/**
 * #723 (ADR-004 Decision 5): the Create Analysis page offers only secret
 * references (an AWS Secrets Manager ARN or name), tells the user to keep the
 * credentials in a secrets manager, renders no password field, and the request
 * it sends never carries a username or password. The page and the English
 * strings are real; Cloudscape components are plain DOM stand-ins (as in
 * DeliverablesPanel.test.js), and ApiManager is replaced by a recorder so the
 * test reads the exact request the page would send.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import en from '../locales/en.json';
import ApiManager from '../classes/ApiManager';
import CreateAnalysis from '../pages/CreateAnalysis';

jest.mock('react-i18next', () => {
  const strings = jest.requireActual('../locales/en.json');
  return { useTranslation: () => ({ t: (key) => strings[key] ?? key }) };
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
jest.mock('@cloudscape-design/components/multiselect', () => ({ __esModule: true, default: () => null }));
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
jest.mock('@cloudscape-design/components/checkbox', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
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

const SECRET_ARN = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/db/creds-AbCdEf'; // pragma: allowlist secret
const SECRET_NAME = 'prod/db/credentials'; // pragma: allowlist secret

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

const field = (label) => container.querySelector(`[data-field="${label}"] input`);

function type(input, value) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function choose(kind) {
  act(() => {
    container.querySelector(`input[type="radio"][value="${kind}"]`).click();
  });
}

function fillSource() {
  type(field(en['create-analysis.source.host.label']), 'db.example.internal');
  type(field(en['create-analysis.source.port.label']), '3306');
  type(field(en['create-analysis.source.database-name.label']), 'app');
}

async function submit() {
  await act(async () => {
    container.querySelector('[data-testid="submit"]').click();
  });
}

const sentRequest = () => execute.mock.calls[0][0][0];

test('the form renders no password or username field', () => {
  expect(container.querySelectorAll('input[type="password"]')).toHaveLength(0);
  const labels = [...container.querySelectorAll('[data-field]')].map((f) => f.dataset.field);
  expect(labels.filter((l) => /pass|user/i.test(l))).toEqual([]);
});

test('only secret references are offered, with secrets-manager guidance', () => {
  const kinds = [...container.querySelectorAll('input[type="radio"]')].map((r) => r.value);
  expect(kinds).toEqual(['secrets-arn', 'secrets-name']);
  const guidance = en['create-analysis.source.credentials.description'];
  expect(guidance).toMatch(/AWS Secrets Manager/);
  expect(container.textContent).toContain(guidance);
  expect(container.textContent).not.toMatch(/direct credentials|enter username and password/i);
});

test('choosing a secret name shows the name field and no other credential field', () => {
  choose('secrets-name');
  expect(field(en['create-analysis.source.secret-name.label'])).not.toBeNull();
  expect(field(en['create-analysis.source.secret-arn.label'])).toBeNull();
  expect(container.querySelectorAll('input[type="password"]')).toHaveLength(0);
});

test('the request sends the secret ARN by reference and no password', async () => {
  fillSource();
  type(field(en['create-analysis.source.secret-arn.label']), SECRET_ARN);
  await submit();

  expect(execute).toHaveBeenCalledTimes(1);
  const request = sentRequest();
  expect(request.method).toBe('POST');
  expect(request.path).toBe('assessments');
  expect(request.params.connection).toEqual({
    host: 'db.example.internal',
    port: 3306,
    database: 'app',
    credentials_type: 'secrets-arn',
    secret_arn: SECRET_ARN,
  });
  expect(JSON.stringify(request.params)).not.toMatch(/pass|pwd|username/i);
});

test('the request sends the secret name by reference and no password', async () => {
  fillSource();
  choose('secrets-name');
  type(field(en['create-analysis.source.secret-name.label']), SECRET_NAME);
  await submit();

  const { connection } = sentRequest().params;
  expect(connection).toEqual({
    host: 'db.example.internal',
    port: 3306,
    database: 'app',
    credentials_type: 'secrets-name',
    secret_name: SECRET_NAME,
  });
});

test("the API's refusal reaches the user", async () => {
  const refusal = 'This request includes a password or secret (connection.password). Store the '
    + 'database credentials in a secrets manager such as AWS Secrets Manager.';
  execute.mockResolvedValue({ 'create-assessment': { error: { message: refusal }, status: 400 } });
  fillSource();
  type(field(en['create-analysis.source.secret-arn.label']), SECRET_ARN);
  await submit();

  expect(container.querySelector('[data-testid="flashbar"]').textContent).toContain(refusal);
});

test('no credentials choice in the form sends a password, whatever is typed', async () => {
  const typed = 'Zq7-not-a-real-value-4Kx9'; // pragma: allowlist secret
  fillSource();
  const kinds = [...container.querySelectorAll('input[type="radio"]')].map((r) => r.value);
  for (const kind of kinds) {
    choose(kind);
    container.querySelectorAll('input[type="password"]').forEach((input) => type(input, typed));
    const username = field('Username');
    if (username) type(username, 'app');
    const arn = field(en['create-analysis.source.secret-arn.label']);
    if (arn) type(arn, SECRET_ARN);
    const name = field(en['create-analysis.source.secret-name.label']);
    if (name) type(name, SECRET_NAME);
    await submit();
  }

  expect(execute).toHaveBeenCalledTimes(kinds.length);
  for (const [calls] of execute.mock.calls) {
    const sent = JSON.stringify(calls[0].params);
    expect(sent).not.toContain(typed);
    expect(sent).not.toMatch(/pass|pwd|username/i);
  }
});
