/**
 * #996: the results page's no-open-risks sentence, the one the engineering and
 * executive reports print. The text comes from the /results payload; the
 * component only decides whether to show it, from facts.open_risk_count.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import NoOpenRisksNote from '../NoOpenRisksNote';

jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <p className="mock-box">{children}</p>,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

const CAVEAT = 'No open risks is not the same as no migration risk: this analysis reads schema and query patterns only.';

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

const render = (props) => act(() => root.render(<NoOpenRisksNote {...props} />));

test('states the sentence when the facts count no open risk', () => {
  render({ synthesis: { facts: { open_risk_count: 0 } }, caveat: CAVEAT });
  expect(container.textContent).toBe(CAVEAT);
  expect(container.querySelector('#no-open-risks-note')).not.toBeNull();
});

test('is silent when a risk is open', () => {
  render({ synthesis: { facts: { open_risk_count: 2 } }, shownRisks: [{}, {}], caveat: CAVEAT });
  expect(container.innerHTML).toBe('');
});

test('without facts, the shown list decides', () => {
  render({ synthesis: {}, shownRisks: [{ risk_id: 'R1' }], caveat: CAVEAT });
  expect(container.innerHTML).toBe('');
  render({ synthesis: {}, shownRisks: [], caveat: CAVEAT });
  expect(container.textContent).toBe(CAVEAT);
});

test('renders nothing when the payload carries no sentence (older API)', () => {
  render({ synthesis: { facts: { open_risk_count: 0 } } });
  expect(container.innerHTML).toBe('');
});

jest.mock('@cloudscape-design/components/container', () => ({
  __esModule: true,
  default: ({ header, children }) => <section>{header}{children}</section>,
}));
jest.mock('@cloudscape-design/components/header', () => ({
  __esModule: true,
  default: ({ children }) => <h2>{children}</h2>,
}));

describe('NoOpenRisksSection (nothing else to say about risk)', () => {
  // eslint-disable-next-line global-require
  const NoOpenRisksSection = require('../NoOpenRisksSection').default;
  const renderSection = (props) => act(() => root.render(<NoOpenRisksSection title="Risk assessment" {...props} />));

  test('is a titled section holding just the sentence', () => {
    renderSection({ synthesis: { facts: { open_risk_count: 0 } }, caveat: CAVEAT });
    expect(container.querySelector('h2').textContent).toBe('Risk assessment');
    expect(container.querySelector('#no-open-risks-note').textContent).toBe(CAVEAT);
  });

  test('renders no section at all when a risk is open or the payload has no sentence', () => {
    renderSection({ synthesis: { facts: { open_risk_count: 1 } }, caveat: CAVEAT });
    expect(container.innerHTML).toBe('');
    renderSection({ synthesis: { facts: { open_risk_count: 0 } } });
    expect(container.innerHTML).toBe('');
  });
});
