// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #1221: the results page's trade-offs tab lists each engine's design trade-offs
 * by topic, from report.json's facts.trade_off_topics -- the groups, labels and
 * counts the engineering report prints from the same fixture
 * (tests/unit/report/test_trade_off_topics.py). Cloudscape components are plain
 * DOM stand-ins (see CloseCallCallout.test.js); the component, the helper and the
 * English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import fixture from './fixtures/tradeOffTopics.json';
import TradeOffTopicGroups from '../TradeOffTopicGroups';
import { tradeOffTopicGroupsOf } from '../../utils/reportFacts';

jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children, variant }) => (variant === 'h3' ? <h3>{children}</h3> : <div>{children}</div>),
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
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

const renderTradeOff = (tradeOff, key) => <p key={key}>{tradeOff.description}</p>;

function renderGroups(engine) {
  const groups = tradeOffTopicGroupsOf(fixture, engine);
  act(() => {
    root.render(<TradeOffTopicGroups groups={groups.topics} renderTradeOff={renderTradeOff} t={t} />);
  });
  return groups;
}

const headings = () => [...container.querySelectorAll('h3')].map(h => h.textContent);

describe('tradeOffTopicGroupsOf', () => {
  test('reads each engine\'s groups, labels and counts from the facts, review notes apart', () => {
    const groups = tradeOffTopicGroupsOf(fixture, 'dynamodb');
    expect(groups.topics.map(g => [g.topic, g.label, g.count])).toEqual([
      ['item_size', 'Item and value size', 2],
      ['indexing', 'Indexes', 1],
      // The schema agents' warnings stay with the design decisions, as their own group.
      ['warning', 'Design warnings', 1],
    ]);
    expect(groups.topics[0].tradeOffs).toEqual([fixture.trade_offs[0], fixture.trade_offs[2]]);
    expect(groups.reviewNotes).toEqual([fixture.trade_offs[3]]);
    expect(groups.reviewNoteCount).toBe(1);
    // The engineering report's label for the same group.
    expect(groups.reviewNoteLabel).toBe('Design review notes');
  });

  test('is null for the reality-check tab and for a report without the groups', () => {
    expect(tradeOffTopicGroupsOf(fixture, 'reality-check')).toBeNull();
    expect(tradeOffTopicGroupsOf({ trade_offs: fixture.trade_offs, facts: {} }, 'dynamodb')).toBeNull();
    expect(tradeOffTopicGroupsOf(null, 'dynamodb')).toBeNull();
  });
});

describe('TradeOffTopicGroups', () => {
  test('lists each topic under its label and count, then its trade-offs', () => {
    renderGroups('dynamodb');
    expect(headings()).toEqual(['Item and value size (2)', 'Indexes (1)', 'Design warnings (1)']);
    const size = container.querySelector('[data-trade-off-topic="item_size"]');
    expect([...size.querySelectorAll('p')].map(p => p.textContent)).toEqual([
      fixture.trade_offs[0].description,
      fixture.trade_offs[2].description,
    ]);
  });

  test('an engine whose trade-offs are all other lists them with no topic heading', () => {
    const groups = renderGroups('elasticache');
    expect(headings()).toEqual([]);
    expect(container.textContent).toContain(fixture.trade_offs[5].description);
    expect(groups.reviewNoteCount).toBe(1);
  });
});
