/**
 * #459: the Results page (AnalysisResults-02.js) renders each
 * `cacheOverlayNotes(cacheOverlay)` entry as a plain JSX text child
 * (`{note}`), the same pattern as every other server-generated string this
 * page shows. This test does not mount the full page -- there is no
 * precedent for that in this repo (AppRoutes.test.js stubs it out entirely,
 * and no `@testing-library/react` is installed) -- it isolates exactly that
 * rendering pattern and proves a hostile note (`<script>`/`<b>&`) comes out
 * as inert text, never live markup, matching the same guarantee already
 * covered server-side (decision/analysis reports) and in the standalone
 * HTML export (ExportReport.cacheLayer.test.js).
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { cacheOverlayNotes } from '../../utils/cacheLayer';

global.IS_REACT_ACT_ENVIRONMENT = true;

// Mirrors the Results page's cache-overlay-notes block exactly (JSX text
// child, no dangerouslySetInnerHTML): `{cacheOverlayNotes(cacheOverlay).map(...)}`.
function CacheOverlayNotes({ cacheOverlay }) {
  return (
    <div>
      {cacheOverlayNotes(cacheOverlay).map((note, idx) => (
        <div key={idx} className="cache-overlay-note">{note}</div>
      ))}
    </div>
  );
}

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

test('renders a hostile safety-net note as inert text, never live markup', () => {
  const cacheOverlay = {
    engine: 'elasticache',
    query_count: 10,
    safety_net_notes: ['<script>alert(1)</script><b>bold</b> & escaped'],
  };
  act(() => {
    root.render(<CacheOverlayNotes cacheOverlay={cacheOverlay} />);
  });

  expect(container.querySelectorAll('script').length).toBe(0);
  expect(container.querySelector('b')).toBeNull();
  const note = container.querySelector('.cache-overlay-note');
  expect(note.textContent).toBe('<script>alert(1)</script><b>bold</b> & escaped');
  expect(note.innerHTML).not.toContain('<script>alert(1)</script>');
});

test('renders nothing when there are no safety-net notes', () => {
  act(() => {
    root.render(<CacheOverlayNotes cacheOverlay={{ engine: 'elasticache', query_count: 10 }} />);
  });
  expect(container.querySelectorAll('.cache-overlay-note').length).toBe(0);
});

test('still renders the note after a full drop, with no engine key on the overlay (#459 round 2)', () => {
  const cacheOverlay = {
    dropped_query_ids: ['q9'],
    safety_net_notes: [
      '1 hot read (10.0% of calls) was assigned at the assignment gate. The '
      + 'ElastiCache schema design covers none of them; it is no longer '
      + 'cached and stays served by its owner engine. None remain.',
    ],
  };
  act(() => {
    root.render(<CacheOverlayNotes cacheOverlay={cacheOverlay} />);
  });
  const note = container.querySelector('.cache-overlay-note');
  expect(note.textContent).toContain('None remain.');
});
