/**
 * #1195 (ADR-032): the results page's cost cards state what each engine's figure
 * was sized for, as the facts layer states it (facts.engines[].cost_sizing), in the
 * reports' wording. A report written before has no sizing and states none.
 */
import i18next from 'i18next';
import { costAssumptionsOf, costSizingOf, costSizingText } from '../reportFacts';
import en from '../../locales/en.json';

const i18n = i18next.createInstance();

beforeAll(() => i18n.init({
  lng: 'en',
  fallbackLng: 'en',
  resources: { en: { translation: en } },
  interpolation: { escapeValue: false },
  keySeparator: false,
}));

const t = (key, options) => i18n.t(key, options);

const AURORA = { basis: 'assigned_queries', sized_query_count: 67, calls_per_second: 96.51 };
const CACHE = { basis: 'cache_overlay', sized_query_count: 20, calls_per_second: 185.07 };
const synthesis = {
  facts: {
    engines: [
      { engine: 'aurora_mysql', cost_sizing: AURORA },
      { engine: 'elasticache', cost_sizing: CACHE },
      { engine: 'dynamodb', cost_sizing: null },
    ],
  },
};

test('each engine\'s sizing is the stored fact', () => {
  expect(costSizingOf(synthesis, 'aurora_mysql')).toBe(AURORA);
  expect(costSizingOf(synthesis, 'dynamodb')).toBeNull();
  expect(costSizingOf(synthesis, 'opensearch')).toBeNull();
  expect(costSizingOf({}, 'aurora_mysql')).toBeNull();
});

test('it reads as the reports word it', () => {
  expect(costSizingText(t, AURORA)).toBe('Sized for 67 queries, 96.51 calls/s');
  expect(costSizingText(t, CACHE)).toBe('Sized for 20 cached reads, 185.07 calls/s, also billed on their owners');
  expect(costSizingText(t, { basis: 'no_in_scope_queries', sized_query_count: 0, calls_per_second: 0 }))
    .toBe('Sized for no in-scope queries');
  expect(costSizingText(t, { basis: 'assigned_queries', sized_query_count: 1, calls_per_second: 1 }))
    .toBe('Sized for 1 query, 1 call/s');
  expect(costSizingText(t, { basis: 'assigned_queries', sized_query_count: 1654, calls_per_second: 1234.5 }))
    .toBe('Sized for 1,654 queries, 1,234.5 calls/s');
});

test('a report written before states no sizing', () => {
  [undefined, null, {}].forEach((sizing) => expect(costSizingText(t, sizing)).toBe(''));
});

test('the TCO assumptions are the stored fact', () => {
  expect(costAssumptionsOf({ facts: { cost_assumptions: ['A', 'B'] }, tco_analysis: { assumptions: ['old'] } }))
    .toEqual(['A', 'B']);
  expect(costAssumptionsOf({ tco_analysis: { assumptions: ['old'] } })).toEqual(['old']);
  expect(costAssumptionsOf({})).toEqual([]);
});
