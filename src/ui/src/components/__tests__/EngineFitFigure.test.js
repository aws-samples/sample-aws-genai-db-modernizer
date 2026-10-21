/**
 * #1291: the pattern analysis page labels the cache layer's average "Avg cache fit"
 * and states the shared cache fit definition; an owning engine keeps "Avg workload
 * fit" and no cache definition. Cloudscape Box is a plain DOM stand-in.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import EngineFitFigure from '../EngineFitFigure';
import { CACHE_FIT_DEFINITION } from '../../utils/workloadFitLabels';

jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
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

const renderFigure = (target, props = {}) => act(() => root.render(<EngineFitFigure target={target} value={41} t={t} {...props} />));

test('the cache layer reads as a cache fit with its definition', () => {
  renderFigure('elasticache');
  expect(container.textContent).toContain('Avg cache fit');
  expect(container.textContent).not.toContain('Avg workload fit');
  expect(container.textContent).toContain('41%');
  expect(container.textContent).toContain(CACHE_FIT_DEFINITION);
});

test('an owning engine keeps the workload fit label and no cache definition', () => {
  renderFigure('dynamodb');
  expect(container.textContent).toContain('Avg workload fit');
  expect(container.textContent).not.toContain('cache fit');
  expect(container.textContent).not.toContain(CACHE_FIT_DEFINITION);
});

test('a cache layer with no routed reads reads as not applicable, not as a percentage (#1325)', () => {
  renderFigure('elasticache', { applicable: false });
  expect(container.textContent).toContain('Avg cache fit');
  expect(container.textContent).toContain('Not applicable');
  expect(container.textContent).not.toContain('41%');
  expect(container.textContent).toContain(CACHE_FIT_DEFINITION);
});

test('an owning engine ignores the applicable flag (#1325)', () => {
  renderFigure('dynamodb', { applicable: false });
  expect(container.textContent).toContain('41%');
});
