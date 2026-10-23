/**
 * #1252: the Results page (AnalysisResults.js) renders the one cache change sentence,
 * `cacheChangeSentence(cacheLayerFactsOf(synthesis))`, as a plain JSX text child, the same pattern
 * as every other string this page shows. This does not mount the full page (there is no precedent
 * in this repo and no `@testing-library/react`); it isolates that rendering pattern and proves the
 * sentence comes from the facts, once, and not at all when the design covers every candidate.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { cacheChangeSentence } from '../../utils/cacheLayer';
import { cacheLayerFactsOf } from '../../utils/reportFacts';

global.IS_REACT_ACT_ENVIRONMENT = true;

function CacheChange({ synthesis }) {
  const sentence = cacheChangeSentence(cacheLayerFactsOf(synthesis));
  return <div>{sentence && <div className="cache-change">{sentence}</div>}</div>;
}

const synthesisWith = (cacheLayer) => ({ facts: { cache_layer: cacheLayer } });
const layer = (candidates, designed) => ({
  candidate_read_count: candidates,
  candidate_call_share_percent: 83.4,
  designed_read_count: designed,
  designed_call_share_percent: 76.1,
  owner_only_read_count: candidates - designed,
});

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

const render = (synthesis) => act(() => root.render(<CacheChange synthesis={synthesis} />));

test('states the change once, from the facts', () => {
  render(synthesisWith(layer(20, 14)));
  const notes = container.querySelectorAll('.cache-change');
  expect(notes.length).toBe(1);
  expect(notes[0].textContent).toBe('20 cache candidates, 14 designed; 6 stay on their owner engine.');
});

test('says nothing when the design covers every candidate', () => {
  render(synthesisWith(layer(20, 20)));
  expect(container.querySelectorAll('.cache-change').length).toBe(0);
});

test('a full drop says none designed (#459 round 2: no engine on the overlay)', () => {
  render(synthesisWith({ ...layer(3, 0), engine: null }));
  expect(container.querySelector('.cache-change').textContent)
    .toBe('3 cache candidates, none designed; all 3 stay on their owner engine.');
});

test('renders nothing for a report with no cache layer fact', () => {
  render({ facts: {} });
  expect(container.querySelectorAll('.cache-change').length).toBe(0);
});
