/**
 * #1071: the offline upload form asks which other AWS databases the application runs
 * today, with a multi-select of the catalog's active engines, and the request carries the
 * chosen ids as `engines_in_use`. Same stand-ins as CreateAnalysis.credentials.test.js.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import en from '../locales/en.json';
import ApiManager from '../classes/ApiManager';
import CreateAnalysis from '../pages/CreateAnalysis';
import { enginesInUseOptions, enginesInUseParam, SELECTABLE_TARGET_ENGINES } from '../utils/targetEngines';

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
jest.mock('@cloudscape-design/components/multiselect', () => ({
  __esModule: true,
  default: ({ options, selectedOptions }) => (
    <ul data-multiselect data-selected={(selectedOptions || []).map((o) => o.value).join(',')}>
      {options.map((o) => <li key={o.value} data-option={o.value}>{o.label}</li>)}
    </ul>
  ),
}));
jest.mock('@cloudscape-design/components/select', () => ({
  __esModule: true,
  default: ({ selectedOption, options }) => (
    <select data-selected={selectedOption ? selectedOption.value : ''} data-options={(options || []).map((o) => o.value).join(',')} />
  ),
}));
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
jest.mock('@cloudscape-design/components/tiles', () => ({
  __esModule: true,
  default: ({ items, onChange }) => (
    <div>
      {items.map((item) => (
        <button type="button" key={item.value} data-tile={item.value} onClick={() => onChange({ detail: { value: item.value } })}>
          {item.label}
        </button>
      ))}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/box', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
jest.mock('@cloudscape-design/components/file-upload', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/container', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
jest.mock('@cloudscape-design/components/header', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
jest.mock('@cloudscape-design/components/progress-bar', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/status-indicator', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
jest.mock('@cloudscape-design/components/alert', () => ({ __esModule: true, default: (p) => mockDiv(p) }));
jest.mock('@cloudscape-design/components/column-layout', () => ({ __esModule: true, default: (p) => mockDiv(p) }));

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;

beforeEach(() => {
  ApiManager.mockImplementation(() => ({ execute: jest.fn() }));
  jest.spyOn(console, 'log').mockImplementation(() => {});
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

const label = en['create-analysis.offline.database-info.engines-in-use-label'];

test('the offline form offers every active engine and starts with none chosen', () => {
  act(() => {
    container.querySelector('[data-tile="offline"]').click();
  });
  const field = container.querySelector(`[data-field="${label}"]`);
  expect(field).not.toBeNull();
  expect(field.querySelector('[data-multiselect]').dataset.selected).toBe('');
  const offered = [...field.querySelectorAll('[data-option]')].map((o) => o.dataset.option);
  expect(offered).toEqual(SELECTABLE_TARGET_ENGINES);
  expect(offered).toEqual(expect.arrayContaining(['opensearch', 'elasticache', 'documentdb']));
});

test('the online form does not ask for it', () => {
  expect(container.querySelector(`[data-field="${label}"]`)).toBeNull();
});

test('the request param holds the chosen selectable ids in catalog order, each once', () => {
  const opt = (value) => ({ value });
  expect(enginesInUseParam([])).toEqual([]);
  expect(enginesInUseParam(undefined)).toEqual([]);
  expect(enginesInUseParam([opt('opensearch'), opt('documentdb'), opt('opensearch')])).toEqual(
    SELECTABLE_TARGET_ENGINES.filter((id) => id === 'documentdb' || id === 'opensearch'),
  );
  // A placeholder, a legacy id or a source engine never reaches the request.
  expect(enginesInUseParam([opt('neptune'), opt('aurora'), opt('mysql')])).toEqual([]);
  expect(enginesInUseOptions().map((o) => o.value)).toEqual(SELECTABLE_TARGET_ENGINES);
});
