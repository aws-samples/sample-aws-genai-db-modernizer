// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2324: an engine tab with no trade-off left says so, by the engine's catalog name,
 * and lists no placeholder. Which list a tab shows is tested on tradeOffsByTab
 * (reportFacts.test.js): an engine the report covers keeps its tab with an empty list.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import i18n from '../../i18n';
import TradeOffsEmptyNote from '../TradeOffsEmptyNote';
import { tradeOffsByTab, tradeOffTabsShown } from '../../utils/reportFacts';

jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <p className="mock-box">{children}</p>,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

const t = (key, opts) => i18n.t(key, opts);
const labels = { aurora_mysql: 'Aurora MySQL' };
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

test("an engine the report covers with no trade-off left renders the empty state by name", () => {
  const tabs = tradeOffsByTab(
    { aurora_mysql: [{ description: 'A robust merge.' }] },
    { schema_designs: { aurora_mysql: {} }, trade_offs: [] },
  );
  act(() => root.render(
    <TradeOffsEmptyNote engine="aurora_mysql" tradeOffs={tabs.aurora_mysql} engineLabels={labels} t={t} />,
  ));
  expect(container.textContent).toBe('No design trade-offs recorded for Aurora MySQL');
  expect(container.textContent).not.toContain('A robust merge.');
});

test('a tab with trade-offs renders no empty state', () => {
  act(() => root.render(
    <TradeOffsEmptyNote engine="aurora_mysql" tradeOffs={[{ description: 'Reads go to the replica.' }]} engineLabels={labels} t={t} />,
  ));
  expect(container.textContent).toBe('');
});

test('the section keeps a tab for an engine with no trade-off left', () => {
  const tabs = tradeOffsByTab(
    { aurora_mysql: [{ description: 'Raw' }], dynamodb: [{ description: 'Raw' }] },
    { schema_designs: { aurora_mysql: {}, dynamodb: {} }, trade_offs: [{ description: 'Single table.', engine: 'dynamodb' }] },
  );
  expect(tradeOffTabsShown(tabs).map(([engine, list]) => [engine, list.length])).toEqual([['aurora_mysql', 0], ['dynamodb', 1]]);
});
