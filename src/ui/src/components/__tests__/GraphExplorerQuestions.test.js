// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The explorer's Questions tab (#308): the graph query catalog (#1789) with its
 * parameters filled from the graph's choices, run through the API, its rows
 * as a table and its projection handed to the graph. The API is mocked;
 * Cloudscape components are plain DOM stand-ins.
 */

import { act } from 'react';
import { createRoot } from 'react-dom/client';

const mockExecute = jest.fn();
jest.mock('../../classes/ApiManager', () => ({
  __esModule: true,
  default: function MockApiManager() {
    this.execute = mockExecute;
  },
}));
function mockPlain() {
  return {
    __esModule: true,
    default: ({ children, header, description, actions, label, constraintText }) => (
      <div>
        {label}
        {header}
        {description}
        {constraintText}
        {actions}
        {children}
      </div>
    ),
  };
}
jest.mock('@cloudscape-design/components/box', () => mockPlain());
jest.mock('@cloudscape-design/components/alert', () => ({
  __esModule: true,
  default: ({ children, type, action }) => (
    <div data-alert={type}>
      {children}
      {action}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/form-field', () => mockPlain());
jest.mock('@cloudscape-design/components/header', () => mockPlain());
jest.mock('@cloudscape-design/components/space-between', () => mockPlain());
jest.mock('@cloudscape-design/components/spinner', () => mockPlain());
jest.mock('@cloudscape-design/components/button', () => ({
  __esModule: true,
  default: ({ children, onClick, disabled, ariaLabel }) => (
    <button type="button" onClick={onClick} disabled={disabled} aria-label={ariaLabel}>
      {children}
    </button>
  ),
}));
jest.mock('@cloudscape-design/components/input', () => ({
  __esModule: true,
  default: ({ value, onChange, ariaLabel }) => (
    <input aria-label={ariaLabel} value={value} onChange={(e) => onChange({ detail: { value: e.target.value } })} />
  ),
}));
jest.mock('@cloudscape-design/components/select', () => ({
  __esModule: true,
  default: ({ options, onChange, ariaLabel }) => (
    <ul data-testid="select" aria-label={ariaLabel}>
      {(options || []).map((o) => (
        // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions
        <li key={o.value} data-option={o.value} onClick={() => onChange({ detail: { selectedOption: o } })}>
          {o.label}
        </li>
      ))}
    </ul>
  ),
}));
jest.mock('@cloudscape-design/components/autosuggest', () => ({
  __esModule: true,
  default: ({ value, options, onChange, ariaLabel }) => (
    <div data-testid="autosuggest" data-options={(options || []).map((o) => o.value).join('|')}>
      <input aria-label={ariaLabel} value={value} onChange={(e) => onChange({ detail: { value: e.target.value } })} />
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/table', () => ({
  __esModule: true,
  default: ({ items, columnDefinitions, header, empty }) => (
    <div data-testid="table">
      {header}
      <table>
        <thead>
          <tr>{columnDefinitions.map((c) => <th key={c.id}>{c.header}</th>)}</tr>
        </thead>
        <tbody>
          {items.map((item, i) => (
            // eslint-disable-next-line react/no-array-index-key
            <tr key={i}>{columnDefinitions.map((c) => <td key={c.id}>{c.cell(item)}</td>)}</tr>
          ))}
        </tbody>
      </table>
      {items.length === 0 && empty}
    </div>
  ),
}));

/* eslint-disable import/first */
import GraphExplorerQuestions from '../GraphExplorerQuestions';
/* eslint-enable import/first */

global.IS_REACT_ACT_ENVIRONMENT = true;

// t returns the key, with its options when there are any, so tests can read both.
const t = (key, opts) => (opts ? `${key} ${JSON.stringify(opts)}` : key);

const CATALOG = {
  success: true,
  status: 200,
  job_id: 'job-1',
  max_choices: 1000,
  entries: [
    {
      id: 'engine_distribution', ref: 'S2', section: 'summary', title: 'Engine distribution',
      question: 'How many queries does each engine take?', note: '', max_rows: 50, params: [], needs: [],
    },
    {
      id: 'table_blast_radius', ref: 'I1', section: 'impact', title: 'Table blast radius',
      question: 'What would changing this table touch?', note: 'Reads and writes alike.', max_rows: 3,
      params: [{
        name: 'table_id', kind: 'table', description: 'A source table', example: 'wordpress.wp_posts',
        choices: ['wordpress.wp_posts', 'wordpress.wp_users'], choices_truncated: false,
      }],
      needs: [],
    },
    {
      id: 'query_provenance', ref: 'I3', section: 'impact', title: 'Query provenance',
      question: 'Where did this query come from and where does it go?', note: '', max_rows: 50,
      params: [{
        name: 'query_id', kind: 'query', description: 'A query id', example: 'q1',
        choices: ['q1', 'q2'], choices_truncated: true,
      }],
      needs: [],
    },
  ],
};

const ROWS = {
  success: true,
  status: 200,
  job_id: 'job-1',
  entry: 'table_blast_radius',
  params: { table_id: 'wordpress.wp_posts' },
  columns: ['query_id', 'engine'],
  rows: [
    { query_id: 'q1', engine: 'dynamodb' },
    { query_id: 'q2', engine: ['aurora_mysql', 'dynamodb'] },
    { query_id: '<b>q3</b>', engine: 'dynamodb' },
  ],
  row_count: 3,
  truncated: true,
  max_rows: 3,
  projection: {
    nodes: [
      { id: 'q1', label: 'Query' },
      { id: 'wordpress.wp_posts', label: 'SourceTable' },
      { id: 'dynamodb', label: 'Engine' },
      { id: 'R-1', label: 'Risk' },
    ],
    edges: [],
  },
  unmet_needs: [],
  explanation: null,
};

const EMPTY = {
  ...ROWS,
  columns: [],
  rows: [],
  row_count: 0,
  truncated: false,
  projection: { nodes: [], edges: [] },
  unmet_needs: [{ id: 'load_test', stage: 'Load test', produces: 'load-test results' }],
  explanation: 'No rows: this job\'s graph has no data from: Load test (load-test results).',
};

let container;
let root;

beforeEach(() => {
  mockExecute.mockReset();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function render(props = {}) {
  await act(async () => {
    root.render(<GraphExplorerQuestions t={t} jobId="job-1" active onShowOnGraph={() => ({})} {...props} />);
  });
  await act(async () => {});
}

async function click(el) {
  await act(async () => {
    el.click();
  });
  await act(async () => {});
}

const byText = (text) => [...container.querySelectorAll('button')].find((b) => b.textContent.includes(text));

function answer(run) {
  return async (calls) => {
    const [call] = calls;
    if (call.id === 'catalog') return { catalog: CATALOG };
    if (call.id === 'catalog-run') return { 'catalog-run': typeof run === 'function' ? run(call) : run };
    return {};
  };
}

test('it lists the catalog questions in plain words, grouped by section', async () => {
  mockExecute.mockImplementation(answer(ROWS));
  await render();
  expect(mockExecute.mock.calls[0][0][0]).toMatchObject({ id: 'catalog', path: 'assessments/job-1/graph/catalog', method: 'GET' });
  const text = container.textContent;
  expect(text).toMatch('How many queries does each engine take?');
  expect(text).toMatch('What would changing this table touch?');
  expect(text.indexOf('graph-explorer.questions.section.summary')).toBeLessThan(text.indexOf('graph-explorer.questions.section.impact'));
});

test('the catalog is read only once the tab is opened', async () => {
  mockExecute.mockImplementation(answer(ROWS));
  await render({ active: false });
  expect(mockExecute).not.toHaveBeenCalled();
  await render({ active: true });
  expect(mockExecute).toHaveBeenCalledTimes(1);
  await render({ active: false });
  await render({ active: true });
  expect(mockExecute).toHaveBeenCalledTimes(1);
});

test('the filter box narrows the list', async () => {
  mockExecute.mockImplementation(answer(ROWS));
  await render();
  const box = container.querySelector('input[aria-label="graph-explorer.questions.filter"]');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(box, 'blast');
    box.dispatchEvent(new Event('input', { bubbles: true }));
  });
  expect(container.textContent).toMatch('What would changing this table touch?');
  expect(container.textContent).not.toMatch('How many queries does each engine take?');
});

test('a question with parameters shows a select of the graph choices; Run posts them', async () => {
  mockExecute.mockImplementation(answer(ROWS));
  await render();
  await click(byText('What would changing this table touch?'));
  expect(container.textContent).toMatch('Reads and writes alike.');
  const run = byText('graph-explorer.questions.run');
  expect(run.disabled).toBe(true);
  const options = [...container.querySelectorAll('[data-testid=select] li')].map((li) => li.textContent);
  expect(options).toEqual(['wordpress.wp_posts', 'wordpress.wp_users']);
  await click(container.querySelector('[data-option="wordpress.wp_posts"]'));
  expect(run.disabled).toBe(false);
  await click(run);
  const runCall = mockExecute.mock.calls.find(([calls]) => calls[0].id === 'catalog-run')[0][0];
  expect(runCall).toEqual({
    id: 'catalog-run',
    path: 'assessments/job-1/graph/catalog/table_blast_radius',
    method: 'POST',
    params: { params: { table_id: 'wordpress.wp_posts' } },
  });
});

test('a question with no parameters runs at once with an empty body', async () => {
  mockExecute.mockImplementation(answer({ ...ROWS, entry: 'engine_distribution', truncated: false }));
  await render();
  await click(byText('How many queries does each engine take?'));
  await click(byText('graph-explorer.questions.run'));
  const runCall = mockExecute.mock.calls.find(([calls]) => calls[0].id === 'catalog-run')[0][0];
  expect(runCall.params).toEqual({ params: {} });
});

test('truncated choices say so and take a typed value, which the API validates', async () => {
  mockExecute.mockImplementation(answer({ ...ROWS, entry: 'query_provenance' }));
  await render();
  await click(byText('Where did this query come from'));
  expect(container.querySelector('[data-testid=select]')).toBe(null);
  const suggest = container.querySelector('[data-testid=autosuggest]');
  expect(suggest.getAttribute('data-options')).toBe('q1|q2');
  // The API's bound, not a count of the listed values.
  expect(container.textContent).toMatch('graph-explorer.questions.choices-truncated {"count":1000}');
  const box = suggest.querySelector('input');
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    setter.call(box, 'q999');
    box.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await click(byText('graph-explorer.questions.run'));
  const runCall = mockExecute.mock.calls.find(([calls]) => calls[0].id === 'catalog-run')[0][0];
  expect(runCall.params).toEqual({ params: { query_id: 'q999' } });
});

test('the result is a table of its columns and rows, its customer text as text, its bound honest', async () => {
  mockExecute.mockImplementation(answer(ROWS));
  await render();
  await click(byText('What would changing this table touch?'));
  await click(container.querySelector('[data-option="wordpress.wp_posts"]'));
  await click(byText('graph-explorer.questions.run'));
  const table = container.querySelector('[data-testid=table]');
  expect([...table.querySelectorAll('th')].map((th) => th.textContent)).toEqual(['query_id', 'engine']);
  const cells = [...table.querySelectorAll('tbody tr')].map((tr) => [...tr.querySelectorAll('td')].map((td) => td.textContent));
  expect(cells).toEqual([['q1', 'dynamodb'], ['q2', 'aurora_mysql, dynamodb'], ['<b>q3</b>', 'dynamodb']]);
  expect(table.querySelector('b')).toBe(null);
  // The API's own figures: row count, and that more matched than max_rows.
  expect(container.textContent).toMatch('graph-explorer.questions.rows {"count":3}');
  expect(container.textContent).toMatch('graph-explorer.questions.truncated {"max":3}');
});

test('an untruncated result does not claim more rows', async () => {
  mockExecute.mockImplementation(answer({ ...ROWS, truncated: false }));
  await render();
  await click(byText('What would changing this table touch?'));
  await click(container.querySelector('[data-option="wordpress.wp_posts"]'));
  await click(byText('graph-explorer.questions.run'));
  expect(container.textContent).not.toMatch('graph-explorer.questions.truncated');
});

test("an empty result shows the API's explanation and the stages it lacks", async () => {
  mockExecute.mockImplementation(answer(EMPTY));
  await render();
  await click(byText('What would changing this table touch?'));
  await click(container.querySelector('[data-option="wordpress.wp_posts"]'));
  await click(byText('graph-explorer.questions.run'));
  expect(container.textContent).toMatch("No rows: this job's graph has no data from: Load test (load-test results).");
  // Each missing stage is named once.
  expect(container.textContent.split('Load test').length - 1).toBe(1);
  expect(byText('graph-explorer.questions.show-on-graph').disabled).toBe(true);
});

test.each([413, 415, 422, 429, 503, 504])('a %i refusal shows its friendly message', async (status) => {
  mockExecute.mockImplementation(answer({ success: false, status, error: new Error('raw detail') }));
  await render();
  await click(byText('What would changing this table touch?'));
  await click(container.querySelector('[data-option="wordpress.wp_posts"]'));
  await click(byText('graph-explorer.questions.run'));
  expect(container.querySelector('[data-alert=error]').textContent).toMatch(`graph-explorer.questions.errors.${status}`);
  expect(container.querySelector('[data-testid=table]')).toBe(null);
});

test('a catalog that cannot be read says so, and Retry reads it again', async () => {
  mockExecute.mockResolvedValueOnce({ catalog: { success: false, status: 429 } });
  await render();
  expect(container.querySelector('[data-alert=error]').textContent).toMatch('graph-explorer.questions.errors.429');
  mockExecute.mockImplementation(answer(ROWS));
  await click(byText('graph-explorer.questions.retry'));
  expect(mockExecute).toHaveBeenCalledTimes(2);
  expect(container.textContent).toMatch('What would changing this table touch?');
});

test('Show on graph hands the result to the graph and names what it cannot draw', async () => {
  mockExecute.mockImplementation(answer(ROWS));
  const onShowOnGraph = jest.fn(async () => ({ drawn: true, more: true, partial: true }));
  await render({ onShowOnGraph });
  await click(byText('What would changing this table touch?'));
  await click(container.querySelector('[data-option="wordpress.wp_posts"]'));
  await click(byText('graph-explorer.questions.run'));
  await click(byText('graph-explorer.questions.show-on-graph'));
  expect(onShowOnGraph).toHaveBeenCalledTimes(1);
  expect(onShowOnGraph.mock.calls[0][0]).toBe(ROWS);
  // Kinds it cannot draw get their friendly names, not the graph's labels.
  expect(container.textContent).toMatch('graph-explorer.questions.undrawn');
  expect(container.textContent).toMatch('graph-explorer.questions.kind.Risk');
  // The bound is the constant, not a count of what was read.
  expect(container.textContent).toMatch('graph-explorer.questions.more-starts {"limit":12}');
  expect(container.textContent).toMatch('graph-explorer.questions.show-partial');
});

test('when the graph reads fail, Show on graph says so', async () => {
  mockExecute.mockImplementation(answer(ROWS));
  await render({ onShowOnGraph: async () => ({ failed: true }) });
  await click(byText('What would changing this table touch?'));
  await click(container.querySelector('[data-option="wordpress.wp_posts"]'));
  await click(byText('graph-explorer.questions.run'));
  await click(byText('graph-explorer.questions.show-on-graph'));
  expect(container.querySelector('[data-alert=error]').textContent).toMatch('graph-explorer.questions.show-failed');
});

function deferred() {
  let resolve;
  const promise = new Promise((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

test('an answer for an older question or value is dropped', async () => {
  const pending = deferred();
  mockExecute.mockImplementation(async (calls) => {
    if (calls[0].id === 'catalog') return { catalog: CATALOG };
    await pending.promise;
    return { 'catalog-run': ROWS };
  });
  await render();
  await click(byText('What would changing this table touch?'));
  await click(container.querySelector('[data-option="wordpress.wp_posts"]'));
  await click(byText('graph-explorer.questions.run'));
  // The value changes while the run is out.
  await click(container.querySelector('[data-option="wordpress.wp_users"]'));
  await act(async () => {
    pending.resolve();
  });
  await act(async () => {});
  expect(container.querySelector('[data-testid=table]')).toBe(null);
  expect(container.textContent).not.toMatch('graph-explorer.questions.rows');
});

test('an answer for another question is dropped too', async () => {
  const pending = deferred();
  mockExecute.mockImplementation(async (calls) => {
    if (calls[0].id === 'catalog') return { catalog: CATALOG };
    await pending.promise;
    return { 'catalog-run': ROWS };
  });
  await render();
  await click(byText('What would changing this table touch?'));
  await click(container.querySelector('[data-option="wordpress.wp_posts"]'));
  await click(byText('graph-explorer.questions.run'));
  await click(byText('graph-explorer.questions.back'));
  await click(byText('How many queries does each engine take?'));
  await act(async () => {
    pending.resolve();
  });
  await act(async () => {});
  expect(container.querySelector('[data-testid=table]')).toBe(null);
});

test("a Show on graph that ends after the value changed leaves no note", async () => {
  mockExecute.mockImplementation(answer(ROWS));
  const pending = deferred();
  await render({ onShowOnGraph: () => pending.promise.then(() => ({ failed: true })) });
  await click(byText('What would changing this table touch?'));
  await click(container.querySelector('[data-option="wordpress.wp_posts"]'));
  await click(byText('graph-explorer.questions.run'));
  await click(byText('graph-explorer.questions.show-on-graph'));
  await click(container.querySelector('[data-option="wordpress.wp_users"]'));
  await act(async () => {
    pending.resolve();
  });
  await act(async () => {});
  expect(container.querySelector('[data-alert=error]')).toBe(null);
});

test('there is no way to type Cypher', async () => {
  mockExecute.mockImplementation(answer(ROWS));
  await render();
  expect(container.querySelector('textarea')).toBe(null);
  expect(container.textContent.toLowerCase()).not.toMatch('cypher');
  expect(mockExecute.mock.calls.flatMap(([calls]) => calls.map((c) => c.path)).join(' ')).not.toMatch(/graph\/query/);
});
