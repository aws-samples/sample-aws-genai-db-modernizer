/**
 * #1389: the results page says once that a report predates the pin rule; the text
 * comes from the /results payload, the component only shows it when it is set.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import PinRuleNote from '../PinRuleNote';

jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <p className="mock-box">{children}</p>,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

const NOTE = 'This report was written before utility-pinned queries were left out of an engine\'s mean fit.';

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

const render = (props) => act(() => root.render(<PinRuleNote {...props} />));

test('shows the payload sentence', () => {
  render({ note: NOTE });
  expect(container.textContent).toBe(NOTE);
  expect(container.querySelector('#pin-rule-note')).not.toBeNull();
});

test.each([[null], [undefined], ['']])('is silent without a sentence (%p)', (note) => {
  render({ note });
  expect(container.innerHTML).toBe('');
});
