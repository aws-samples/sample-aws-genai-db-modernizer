import { act } from 'react';
import { createRoot } from 'react-dom/client';
import ChartSankey from '../ChartSankey-01';
import { ENGINE_LABELS } from '../../utils/engineNames';

global.IS_REACT_ACT_ENVIRONMENT = true;

let container, root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

// helper to mock
const queryFlow = (dist) => ({
  nodes: [{ id: 'queries' }, ...Object.keys(dist).map(id => ({ id }))],
  links: Object.entries(dist).map(([target, value]) => ({ source: 'queries', target, value }))
});

const renderSankey = (data, onNodeClick) => {
  act(() => root.render(<ChartSankey width={900} height={400} data={data} onNodeClick={onNodeClick} />));
  return Array.from(container.querySelectorAll('text')).map(n => n.textContent);
};

const nodeGroup = (label) =>
  Array.from(container.querySelectorAll('text')).find(n => n.textContent === label)?.parentNode;

test('uses display names instead of raw engine keys', () => {
  const labels = renderSankey(queryFlow({ dynamodb: 87, aurora_mysql: 40, opensearch: 12 }));
  expect(labels).toEqual(expect.arrayContaining(['DynamoDB (87)', 'Aurora MySQL (40)', 'OpenSearch (12)']));
  // make sure raw keys don't show up
  expect(labels.some(l => /^(dynamodb|aurora_mysql|opensearch) /.test(l))).toBe(false);
});

test.each(Object.entries(ENGINE_LABELS))('labels %s as %s', (engine, displayName) => {
  expect(renderSankey(queryFlow({ [engine]: 5 }))).toContain(`${displayName} (5)`);
});

test('keeps unknown node ids unchanged', () => {
  const labels = renderSankey(queryFlow({ dynamodb: 3, unmapped_engine: 2 }));
  expect(labels).toEqual(expect.arrayContaining(['queries (5)', 'unmapped_engine (2)']));
});

test('passes raw engine key to onNodeClick', () => {
  const onNodeClick = jest.fn();
  renderSankey(queryFlow({ aurora_mysql: 40, dynamodb: 87 }), onNodeClick);

  act(() => {
    nodeGroup('Aurora MySQL (40)')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });

  expect(onNodeClick).toHaveBeenCalledWith('aurora_mysql');
});