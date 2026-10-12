/**
 * #269: the Assignment review page's hero fallback notice renders exactly
 * when `summaryFallbackInfo(...)` reports `isFallback` -- never for a normal
 * LLM summary, and never for an older artifact with no
 * `executive_summary_source` field at all.
 *
 * Round-2 review: exercises the real `SummaryFallbackNotice` component
 * (src/ui/src/components/SummaryFallbackNotice.js) that AssignmentGate.js
 * itself renders, with the same props it passes, rather than a mirrored
 * stand-in. Cloudscape's own components ship ESM that this repo's Jest
 * config cannot transform (confirmed by AppRoutes.test.js, which stubs out
 * every page for exactly this reason, and by this file's own
 * `@testing-library/react`-free precedent, AnalysisResultsCacheOverlayNotes.
 * test.js) -- so `Box`/`Icon`/`ExpandableSection` are mocked as plain DOM
 * elements. `SummaryFallbackNotice.js` itself is not mocked: its real
 * conditional logic (notice text, when the warnings list/expandable section
 * appears) is what runs and is asserted on.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { summaryFallbackInfo } from '../../utils/summaryFallback';
import SummaryFallbackNotice from '../../components/SummaryFallbackNotice';

jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div className="mock-box">{children}</div>,
}));
jest.mock('@cloudscape-design/components/icon', () => ({
  __esModule: true,
  default: ({ name }) => <i className="mock-icon" data-name={name} />,
}));
jest.mock('@cloudscape-design/components/expandable-section', () => ({
  __esModule: true,
  default: ({ headerText, children }) => (
    <details className="mock-expandable-section">
      <summary>{headerText}</summary>
      {children}
    </details>
  ),
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

// Mirrors AssignmentGate.js's own hero block: `{summaryFallback.isFallback &&
// (<SummaryFallbackNotice className="gate-hero__fallback-note" text={...}
// warnings={summaryFallback.warnings} detailsHeaderText={...} />)}`.
function GateHero({ source, warnings }) {
  const summaryFallback = summaryFallbackInfo(source, warnings);
  if (!summaryFallback.isFallback) return null;
  return (
    <SummaryFallbackNotice
      className="gate-hero__fallback-note"
      text="The AI summary was replaced with one built from the final assignment."
      warnings={summaryFallback.warnings}
      detailsHeaderText="Why the AI summary was replaced"
    />
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

test('shows the notice and the warnings list when the source is deterministic_fallback', () => {
  act(() => {
    root.render(<GateHero source="deterministic_fallback" warnings={['a warning']} />);
  });
  const note = container.querySelector('.gate-hero__fallback-note');
  expect(note).not.toBeNull();
  expect(note.textContent).toContain(
    'The AI summary was replaced with one built from the final assignment.',
  );
  const details = container.querySelector('.mock-expandable-section');
  expect(details).not.toBeNull();
  expect(details.querySelector('summary').textContent).toBe('Why the AI summary was replaced');
  expect(details.textContent).toContain('a warning');
});

test('shows the notice with no expandable section when there are no warnings', () => {
  act(() => {
    root.render(<GateHero source="deterministic_fallback" warnings={[]} />);
  });
  expect(container.querySelector('.gate-hero__fallback-note')).not.toBeNull();
  expect(container.querySelector('.mock-expandable-section')).toBeNull();
});

test('shows no notice when the source is llm', () => {
  act(() => {
    root.render(<GateHero source="llm" warnings={[]} />);
  });
  expect(container.querySelector('.gate-hero__fallback-note')).toBeNull();
});

test('shows no notice when the source is missing (older artifact)', () => {
  act(() => {
    root.render(<GateHero source={undefined} warnings={undefined} />);
  });
  expect(container.querySelector('.gate-hero__fallback-note')).toBeNull();
});
