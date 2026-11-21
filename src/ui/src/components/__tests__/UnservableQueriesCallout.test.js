// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #729: the results page's callout for queries on a fallback engine. It
 * lists each query from report.json's facts.unservable (the same list the
 * decision and engineering reports flag) with the gates, the redesign hint and
 * the re-run path, opens a query's "Why this engine" decision trace, and links
 * to the assignment review. Cloudscape components are plain DOM stand-ins (see
 * QueryRoutingTable.test.js); the component and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import UnservableQueriesCallout from '../UnservableQueriesCallout';
import { ResultsHelp } from '../../hooks/useResultsDrawers';
import { helpPanelContent, resultsHelpContext } from '../../utils/resultsHelp';

jest.mock('@cloudscape-design/components/alert', () => ({
  __esModule: true,
  default: ({ header, children, type }) => <div data-alert={type}><h3>{header}</h3>{children}</div>,
}));
jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/link', () => ({
  __esModule: true,
  default: ({ children, onFollow, ariaLabel, href, nativeAttributes }) => (href
    ? <a className="mock-href" href={href}>{children}</a>
    : <a role="button" tabIndex={0} aria-label={ariaLabel} {...nativeAttributes} onClick={() => onFollow({ preventDefault: () => {}, detail: {} })}>{children}</a>),
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
  helpOpened = [];
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const facts = {
  unservable_query_count: 2,
  fallback_query_count: 2,
  gated_everywhere_query_count: 0,
  no_fit_query_count: 2,
  unservable_queries: [
    {
      query_id: 'q-count',
      engine: 'aurora_mysql',
      kind: 'fallback',
      rule: 'fallback.no_score',
      explanation: 'No engine could score this query, so it falls back to Aurora MySQL.',
      blocking_gates: [{ engine: 'dynamodb', gate: 'capability', gate_text: 'missing a capability the query needs' }],
      required_capabilities: ['aggregation'],
      capability_labels: ['aggregation'],
      redesign_hints: ['Maintain the aggregate as the data changes.'],
    },
    {
      query_id: 'q-<i>',
      engine: 'aurora_mysql',
      kind: 'fallback',
      rule: 'fallback.no_owner',
      explanation: 'No analyzed engine can own this query, so it falls back to Aurora MySQL.',
      blocking_gates: [],
      required_capabilities: [],
      capability_labels: [],
      redesign_hints: ['Rewrite the query so a kept engine can serve it.'],
    },
  ],
  more_unservable_count: 3,
};
const synthesis = { facts: { unservable: facts } };

let helpOpened;
const render = (props) => {
  act(() => root.render(
    <ResultsHelp.Provider value={topic => helpOpened.push(topic)}>
      <UnservableQueriesCallout t={t} synthesis={synthesis} jobId="job-1" onOpen={() => {}} {...props} />
    </ResultsHelp.Provider>,
  ));
};
// The re-run steps (#2521): what the alert's help panel holds for these facts.
const panelText = syn => helpPanelContent('unservable', t, resultsHelpContext(syn || synthesis)).paragraphs.join('\n');

test('flags each query with its engine, why, the gates and a redesign', () => {
  render();
  const text = container.textContent;
  expect(container.querySelector('[data-alert]').dataset.alert).toBe('warning');
  expect(text).toContain('2 queries that cannot be served as designed');
  expect(text).toContain(en['analysis-results.unservable.fallback_all']);
  expect(text).not.toContain(en['analysis-results.unservable.flagged_all']);
  expect(text).not.toContain('no kept engine');
  expect(text).toContain('does not bring back an engine it removed');
  expect(text).toContain('on Aurora MySQL');
  expect(text).toContain('No engine could score this query, so it falls back to Aurora MySQL.');
  expect(text).toContain('Ruled out: DynamoDB (missing a capability the query needs).');
  expect(text).toContain('Keep it on Aurora MySQL, or redesign it. Maintain the aggregate as the data changes.');
  expect(text).toContain('3 more queries are listed in the engineering report.');
});

test('a query opens its Why this engine trace', () => {
  const opened = [];
  render({ onOpen: id => opened.push(id) });
  act(() => container.querySelector('[role="button"][aria-label="Open the decision trace for query q-count"]').click());
  expect(opened).toEqual(['q-count']);
});

test('links to the assignment review and leaves the re-run steps to its help panel', () => {
  render();
  const link = container.querySelector('a.mock-href');
  expect(link.getAttribute('href')).toBe('/analysis/assignments/job-1');
  expect(container.textContent).not.toContain('To re-run, open the assignment review');
  expect(panelText()).toContain('To re-run, open the assignment review');
});

test('the alert header has an Info link that opens the unservable help panel', () => {
  render();
  const link = container.querySelector('[data-help-topic="unservable"]');
  expect(link.getAttribute('aria-label')).toBe('Info about the Queries that cannot be served section');
  expect(container.querySelector('h3').contains(link)).toBe(true);
  act(() => link.click());
  expect(helpOpened).toEqual(['unservable']);
  // The alert's own message is unchanged.
  expect(container.textContent).toContain(en['analysis-results.unservable.intro']);
});

test('a query id is text, never markup, and a query without gates states none', () => {
  render();
  expect(container.querySelector('i')).toBeNull();
  expect(container.textContent).toContain('q-<i>');
  expect(container.textContent.match(/Ruled out:/g)).toHaveLength(1);
});

test('renders nothing without any, or for a report written before them', () => {
  render({ synthesis: { facts: { unservable: { ...facts, unservable_query_count: 0, unservable_queries: [] } } } });
  expect(container.innerHTML).toBe('');
  render({ synthesis: { facts: {} } });
  expect(container.innerHTML).toBe('');
});

test('one query reads in the singular', () => {
  render({ synthesis: { facts: { unservable: { ...facts, unservable_query_count: 1, unservable_queries: [facts.unservable_queries[0]], more_unservable_count: 0 } } } });
  expect(container.textContent).toContain('1 query that cannot be served as designed');
  expect(container.textContent).not.toContain('more quer');
});

const flaggedRow = {
  query_id: 'q-scan',
  engine: 'dynamodb',
  kind: 'flagged',
  rule: '',
  explanation: "The reality check's review found DynamoDB cannot serve this query (it would need a full scan), and no other engine the plan keeps can take it, so it stays on DynamoDB.",
  blocking_gates: [{ engine: 'dynamodb', gate: 'model_flag', gate_text: 'a design review found it cannot support this query' }],
  required_capabilities: [],
  capability_labels: [],
  redesign_hints: ['For DynamoDB: give the query a key or an index it can look up.'],
};

test('#888: both kinds listed: each gets its "Some" sentence', () => {
  render({ synthesis: { facts: { unservable: { ...facts, unservable_query_count: 3, flagged_query_count: 1, unservable_queries: [...facts.unservable_queries, flaggedRow] } } } });
  const text = container.textContent;
  expect(text).toContain(en['analysis-results.unservable.fallback_some']);
  expect(text).toContain(en['analysis-results.unservable.flagged_some']);
  expect(text).toContain('on DynamoDB');
  expect(text).toContain('Ruled out: DynamoDB (a design review found it cannot support this query).');
  expect(text).toContain('Keep it on DynamoDB, or redesign it. For DynamoDB: give the query a key');
});

test('#888: only flagged queries never read as a fallback', () => {
  render({ synthesis: { facts: { unservable: { unservable_query_count: 1, fallback_query_count: 0, gated_everywhere_query_count: 0, no_fit_query_count: 0, flagged_query_count: 1, unservable_queries: [flaggedRow], more_unservable_count: 0 } } } });
  const text = container.textContent;
  expect(text).toContain('1 query that cannot be served as designed');
  expect(text).toContain(en['analysis-results.unservable.flagged_all']);
  expect(text).not.toContain('fallback engine');
  expect(text).not.toContain('no analysis scored it');
});

const heldRow = {
  query_id: 'w2',
  engine: 'dynamodb',
  kind: 'held',
  rule: '',
  explanation: 'A design review found DynamoDB cannot serve this query (it needs a feature the engine lacks). A write on its table is pinned to DynamoDB in the assignment review, and a table has one writer, so it stays on DynamoDB until the pin changes.',
  blocking_gates: [{ engine: 'dynamodb', gate: 'model_flag', gate_text: 'a design review found it cannot support this query' }],
  required_capabilities: [],
  capability_labels: [],
  redesign_hints: ['For DynamoDB: rewrite the query so it needs only what the engine supports.'],
};
const heldOnly = { unservable_query_count: 1, fallback_query_count: 0, gated_everywhere_query_count: 0, no_fit_query_count: 0, flagged_query_count: 0, held_query_count: 1, unservable_queries: [heldRow], more_unservable_count: 0 };

test('#1033: a held query names the pin and never reads as a fallback', () => {
  render({ synthesis: { facts: { unservable: heldOnly } } });
  const text = container.textContent;
  expect(text).toContain('1 query that cannot be served as designed');
  expect(text).toContain(en['analysis-results.unservable.intro']);
  expect(text).toContain(en['analysis-results.unservable.held_all']);
  expect(text).toContain('To move it, change the pin in the assignment review, or redesign it. For DynamoDB: rewrite the query');
  expect(text).not.toContain('Keep it on DynamoDB');
  expect(text).not.toContain('fallback engine');
  expect(text).not.toContain('no other engine the plan keeps');
});

test('#1033: a held and a fallback query each get their "Some" sentence', () => {
  render({ synthesis: { facts: { unservable: { ...heldOnly, unservable_query_count: 3, no_fit_query_count: 2, fallback_query_count: 2, unservable_queries: [...facts.unservable_queries, heldRow] } } } });
  const text = container.textContent;
  expect(text).toContain(en['analysis-results.unservable.fallback_some']);
  expect(text).toContain(en['analysis-results.unservable.held_some']);
  expect(text).not.toContain(en['analysis-results.unservable.held_all']);
  expect(text).toContain('Keep it on Aurora MySQL, or redesign it.');
});

test('#1033: a facts block written before held queries has no held sentence', () => {
  render();
  expect(container.textContent).not.toContain('pinned to that engine');
});

// #1329: a query no engine in the customer's engine scope can serve has no engine; the
// callout names the engine the scope left out and says to start a new job to add it.
const scopeFacts = {
  unservable_query_count: 1,
  fallback_query_count: 0,
  gated_everywhere_query_count: 0,
  flagged_query_count: 0,
  held_query_count: 0,
  no_fit_query_count: 0,
  no_engine_in_scope_query_count: 1,
  unservable_queries: [
    {
      query_id: 'q9',
      engine: '',
      kind: 'no_engine_in_scope',
      rule: 'fallback.no_score',
      explanation: "No engine in the customer's engine scope (DynamoDB) can serve this query.",
      blocking_gates: [{ engine: 'dynamodb', gate: 'capability', gate_text: 'missing a capability the query needs' }],
      required_capabilities: ['sql_admin'],
      capability_labels: ['database administration statements'],
      redesign_hints: [],
      closest_engine: 'aurora_mysql',
    },
  ],
  more_unservable_count: 0,
};

test('a query with no engine in the scope names the engine the scope left out', () => {
  render({ synthesis: { facts: { unservable: { ...scopeFacts, no_engine_in_scope_closest_engines: ['aurora_mysql'] } } } });
  const text = container.textContent;
  expect(text).toContain('1 query that cannot be served as designed');
  // Only this kind: its own intro and way to serve them, never the assignment review,
  // which refuses to edit these queries.
  expect(text).toContain(en['analysis-results.unservable.scope_intro']);
  expect(text).not.toContain(en['analysis-results.unservable.intro']);
  expect(text).not.toContain(en['analysis-results.unservable.rerun']);
  expect(text).not.toContain(en['analysis-results.unservable.assignment-link']);
  expect(text).not.toContain("change a query's engine");
  // The steps are in the help panel, with no assignment review step.
  const steps = panelText({ facts: { unservable: { ...scopeFacts, no_engine_in_scope_closest_engines: ['aurora_mysql'] } } });
  expect(steps).toContain('To serve them, start a new assessment whose scope adds Aurora MySQL, or keep them on the source database.');
  expect(steps).not.toContain(en['analysis-results.unservable.rerun']);
  expect(container.querySelector('[data-figure="unservable-engine"]').textContent)
    .toBe('no engine (Aurora MySQL is outside the scope)');
  expect(text).toContain('Start a new assessment whose scope adds Aurora MySQL, or keep it on the source database.');
  expect(text).not.toContain('Keep it on');
  expect(text).not.toContain('on undefined');
});

test('a scope-only alert keeps its one action line and the Info link, with no assignment review link', () => {
  render({ synthesis: { facts: { unservable: { ...scopeFacts, no_engine_in_scope_closest_engines: ['aurora_mysql'] } } } });
  const text = container.textContent;
  expect(text).toContain('To serve them, start a new assessment whose scope adds Aurora MySQL, or keep them on the source database.');
  expect(container.querySelector('a.mock-href')).toBeNull();
  expect(container.querySelector('[data-help-topic="unservable"]')).not.toBeNull();
  // The other kinds keep their steps in the panel only.
  render();
  expect(container.textContent).not.toContain(en['analysis-results.unservable.rerun']);
});

test('the scope kind next to another kind uses the "Some" sentences', () => {
  const both = {
    ...scopeFacts,
    unservable_query_count: 3,
    no_fit_query_count: 2,
    fallback_query_count: 2,
    unservable_queries: [...facts.unservable_queries, ...scopeFacts.unservable_queries],
  };
  render({ synthesis: { facts: { unservable: both } } });
  const text = container.textContent;
  expect(text).toContain(en['analysis-results.unservable.scope_some']);
  expect(text).toContain(en['analysis-results.unservable.fallback_some']);
  expect(text).toContain('on Aurora MySQL');
  // Both ways to act: the assignment review for the queries on an engine, a new
  // assessment for the ones with no engine in the scope.
  expect(text).toContain(en['analysis-results.unservable.intro']);
  const steps = panelText({ facts: { unservable: both } });
  expect(steps).toContain(en['analysis-results.unservable.rerun']);
  expect(steps).toContain(en['analysis-results.unservable.rerun_scope_wider']);
  expect(text).not.toContain(en['analysis-results.unservable.rerun']);
});
