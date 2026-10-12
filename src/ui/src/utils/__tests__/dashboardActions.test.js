/**
 * #519 / #526 round 1 review: the Dashboard's action-dropdown must never
 * offer "Cancel analysis" for a job that's already finished -- there is
 * nothing left to cancel, so DELETE /assessments/{job_id} would just be a
 * no-op (src/api/routes/assessments.py's cancel_assessment). This isolates
 * that decision in a pure function so it's testable without mounting the
 * full Cloudscape page (no @testing-library/react is installed in this
 * repo -- see AnalysisResultsCacheOverlayNotes.test.js's header comment).
 */
import { buildDashboardActionItems } from '../dashboardActions';

// A fake `t` that returns the raw key, so assertions can check keys
// directly without loading real translations.
const t = (key) => key;

function idsOf(items) {
  return items.map((item) => item.id);
}

describe('buildDashboardActionItems', () => {
  it('always offers view-results and view-analysis', () => {
    const items = buildDashboardActionItems(t, []);
    expect(idsOf(items)).toEqual(expect.arrayContaining(['view-results', 'view-analysis']));
  });

  it('never offers the retired "stop" placeholder action', () => {
    const items = buildDashboardActionItems(t, []);
    expect(idsOf(items)).not.toContain('stop');
  });

  it.each(['SUCCEEDED', 'CANCELLED', 'FAILED'])(
    'omits Cancel when the selected job is %s (terminal)',
    (status) => {
      const items = buildDashboardActionItems(t, [{ jobId: 'job-1', status }]);
      expect(idsOf(items)).not.toContain('cancel');
    },
  );

  it.each(['RUNNING', 'WAITING', 'PENDING'])(
    'offers Cancel when the selected job is %s (not terminal)',
    (status) => {
      const items = buildDashboardActionItems(t, [{ jobId: 'job-1', status }]);
      expect(idsOf(items)).toContain('cancel');
      const cancelItem = items.find((item) => item.id === 'cancel');
      expect(cancelItem.text).toBe('dashboard.actions.cancel-analysis');
    },
  );

  it('offers Cancel when nothing is selected (the dropdown itself is disabled by the caller)', () => {
    const items = buildDashboardActionItems(t, []);
    expect(idsOf(items)).toContain('cancel');
  });

  it('decides off the first selected job only', () => {
    const items = buildDashboardActionItems(t, [
      { jobId: 'job-1', status: 'SUCCEEDED' },
      { jobId: 'job-2', status: 'RUNNING' },
    ]);
    expect(idsOf(items)).not.toContain('cancel');
  });
});
