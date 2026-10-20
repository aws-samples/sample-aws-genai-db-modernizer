import { act } from 'react';
import { createRoot } from 'react-dom/client';
import ChartSankey from '../ChartSankey';

global.IS_REACT_ACT_ENVIRONMENT = true;

// The palette ChartSankey.js used to fall back to for any node id it did not
// know. Aurora's node ids are aurora_mysql / aurora_postgresql (never a bare
// "aurora"), so both used to land here -- and DynamoDB's #3184e8 is close enough
// that the two read as the same colour on the Results page, the Assignment gate
// and the Job summary.
const DEFAULT_NODE_COLOR = '#0972D3';

// Each Aurora engine's catalog colour (colors.ui). #476 settled on #9c5700 for both;
// #1246 gave Aurora PostgreSQL its own, so the two read apart in every view.
const LIVE_UI_AURORA_MYSQL = '#9c5700';
const LIVE_UI_AURORA_POSTGRESQL = '#c33d69';

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

const queryFlow = (dist) => ({
  nodes: [{ id: 'queries' }, ...Object.keys(dist).map(id => ({ id }))],
  links: Object.entries(dist).map(([target, value]) => ({ source: 'queries', target, value }))
});

const renderSankey = (data) => {
  act(() => root.render(<ChartSankey width={900} height={400} data={data} />));
};

// A node's <rect> is the first child of the <g> that holds its label <text>.
const nodeFill = (label) =>
  Array.from(container.querySelectorAll('text'))
    .find(n => n.textContent === label)?.parentNode.querySelector('rect')
    ?.getAttribute('fill');

test('colours both Aurora engines with the live UI palette instead of the default blue', () => {
  renderSankey(queryFlow({ dynamodb: 87, aurora_mysql: 49, aurora_postgresql: 12 }));

  expect(nodeFill('Aurora MySQL (49)')).toBe(LIVE_UI_AURORA_MYSQL);
  expect(nodeFill('Aurora PostgreSQL (12)')).toBe(LIVE_UI_AURORA_POSTGRESQL);
  // #1246: the two Aurora engines no longer share a colour.
  expect(nodeFill('Aurora MySQL (49)')).not.toBe(nodeFill('Aurora PostgreSQL (12)'));
  expect(nodeFill('Aurora MySQL (49)')).not.toBe(DEFAULT_NODE_COLOR);
  expect(nodeFill('Aurora PostgreSQL (12)')).not.toBe(DEFAULT_NODE_COLOR);
  // ...and neither reads as DynamoDB, the colour the bug report mistook Aurora for.
  expect(nodeFill('Aurora MySQL (49)')).not.toBe(nodeFill('DynamoDB (87)'));
  expect(nodeFill('Aurora PostgreSQL (12)')).not.toBe(nodeFill('DynamoDB (87)'));
});

test('colours the links into each Aurora engine with that engine colour', () => {
  renderSankey(queryFlow({ aurora_mysql: 49, aurora_postgresql: 12 }));

  // Links are drawn before the nodes; one link per queryFlow entry, in order.
  const links = Array.from(container.querySelectorAll('path'));
  expect(links[0].getAttribute('stroke')).toBe(LIVE_UI_AURORA_MYSQL);
  expect(links[1].getAttribute('stroke')).toBe(LIVE_UI_AURORA_POSTGRESQL);
});
