/**
 * #1246: EngineBadge fills a Cloudscape Badge with the engine's catalog colour and names
 * the engine by its catalog display name. Cloudscape ships ESM this repo's Jest config
 * cannot transform, so the Badge is a stand-in that records the props it gets.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import EngineBadge from '../EngineBadge';
import { ENGINE_HEX, ENGINE_LABELS } from '../../utils/engineCatalog.generated';
import { DEFAULT_ENGINE_HEX } from '../../utils/engineColors';

jest.mock('@cloudscape-design/components/badge', () => ({
  __esModule: true,
  default: ({ color, style, children }) => (
    <span
      className="mock-badge"
      data-color={color || ''}
      data-background={style?.root?.background || ''}
      data-text={style?.root?.color || ''}
    >
      {children}
    </span>
  ),
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

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

const badge = () => container.querySelector('.mock-badge');

test.each(Object.keys(ENGINE_LABELS))('%s is drawn in its catalog colour, by display name', (engine) => {
  act(() => root.render(<EngineBadge engine={engine} />));
  expect(badge().dataset.background).toBe(ENGINE_HEX[engine]);
  expect(badge().dataset.color).toBe('');
  expect(badge().textContent).toBe(ENGINE_LABELS[engine]);
  expect(container.querySelector('[data-engine-badge]').dataset.engineBadge).toBe(engine);
});

test('Aurora MySQL is no longer grey or DynamoDB blue', () => {
  act(() => root.render(<EngineBadge engine="aurora_mysql" />));
  expect(badge().dataset.background).toBe('#9c5700');
  expect(badge().dataset.background).not.toBe(ENGINE_HEX.dynamodb);
});

test('children replace the label, the colour stays the engine one', () => {
  act(() => root.render(<EngineBadge engine="elasticache">ElastiCache (cache layer)</EngineBadge>));
  expect(badge().textContent).toBe('ElastiCache (cache layer)');
  expect(badge().dataset.background).toBe(ENGINE_HEX.elasticache);
});

test('an unknown engine id shows as written, in the neutral grey', () => {
  act(() => root.render(<EngineBadge engine="timestream" />));
  expect(badge().textContent).toBe('timestream');
  expect(badge().dataset.background).toBe(DEFAULT_ENGINE_HEX);
});
