import { act } from 'react';
import { createRoot } from 'react-dom/client';
import ChartSankey from '../ChartSankey-01';

global.IS_REACT_ACT_ENVIRONMENT = true;

// The palette ChartSankey-01.js used to fall back to for any node id it did not
// know. Aurora's node ids are aurora_mysql / aurora_postgresql (never a bare
// "aurora"), so both used to land here -- and DynamoDB's #3184e8 is close enough
// that the two read as the same colour on the Results page, the Assignment gate
// and the Job summary.
const DEFAULT_NODE_COLOR = '#0972D3';

// The colour AssignmentGate.js already uses for both Aurora engines
// (ENGINE_COLORS aurora_mysql / aurora_postgresql bg). This is the palette the
// issue thread settled on: the export's indigo shades belong to its single-colour
// report design, while the live pages share #9c5700.
const LIVE_UI_AURORA = '#9c5700';

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

  expect(nodeFill('Aurora MySQL (49)')).toBe(LIVE_UI_AURORA);
  expect(nodeFill('Aurora PostgreSQL (12)')).toBe(LIVE_UI_AURORA);
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
  expect(links[0].getAttribute('stroke')).toBe(LIVE_UI_AURORA);
  expect(links[1].getAttribute('stroke')).toBe(LIVE_UI_AURORA);
});
