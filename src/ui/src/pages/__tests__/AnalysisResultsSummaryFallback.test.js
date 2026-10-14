/**
 * #269: the Results page's Executive Summary fallback notice renders exactly
 * when `summaryFallbackInfo(...)` reports `isFallback` -- never for a normal
 * LLM summary, and never for an older artifact with no `summary_source`
 * field at all. The reason clause comes from `fallbackWithheldReason`, ported
 * from `formatting.fallback_withheld_reason` (#393).
 *
 * Round-2 review: exercises the real `SummaryFallbackNotice` component
 * (src/ui/src/components/SummaryFallbackNotice.js) that AnalysisResults-02.js
 * itself renders, with the same props it passes, rather than a mirrored
 * stand-in. Cloudscape's own components ship ESM that this repo's Jest
 * config cannot transform (confirmed by AppRoutes.test.js, which stubs out
 * every page for exactly this reason) -- so `Box`/`Icon` are mocked as plain
 * DOM elements (this notice never renders an `ExpandableSection` for the
 * synthesis summary -- see AssignmentGateSummaryFallback.test.js for that
 * case). `SummaryFallbackNotice.js` itself is not mocked: its real
 * conditional logic is what runs and is asserted on.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { summaryFallbackInfo } from '../../utils/summaryFallback';
import { fallbackWithheldReason } from '../../utils/fallbackWithheldReason';
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

// Mirrors AnalysisResults-02.js's own Executive Summary block:
// `{summaryFallback.isFallback && (<SummaryFallbackNotice text={t(...,
// { reason: fallbackWithheldReason(summaryFallback.warnings) })} />)}`.
function ExecutiveSummaryFallback({ source, warnings }) {
  const summaryFallback = summaryFallbackInfo(source, warnings);
  if (!summaryFallback.isFallback) return null;
  const reason = fallbackWithheldReason(summaryFallback.warnings);
  return (
    <SummaryFallbackNotice
      text={`The generated narrative was withheld because it ${reason}; this summary is `
        + 'built from the effective assignment.'}
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

test('shows the notice, with the check-specific reason, when the source is deterministic_fallback', () => {
  act(() => {
    root.render(
      <ExecutiveSummaryFallback
        source="deterministic_fallback"
        warnings={['[high confidence] names an internal field, "tco_analysis".']}
      />,
    );
  });
  const note = container.querySelector('.mock-box');
  expect(note).not.toBeNull();
  expect(note.textContent.trim()).toBe(
    "The generated narrative was withheld because it named one of this codebase's "
    + 'internal field names; this summary is built from the effective assignment.',
  );
  // No expandable detail for the synthesis summary (unlike the reality-check
  // summary on the Assignment review page).
  expect(container.querySelector('.mock-expandable-section')).toBeNull();
});

test('shows no notice when the source is llm', () => {
  act(() => {
    root.render(<ExecutiveSummaryFallback source="llm" warnings={[]} />);
  });
  expect(container.querySelector('.mock-box')).toBeNull();
});

test('shows no notice when the source is missing (older artifact)', () => {
  act(() => {
    root.render(<ExecutiveSummaryFallback source={undefined} warnings={undefined} />);
  });
  expect(container.querySelector('.mock-box')).toBeNull();
});
