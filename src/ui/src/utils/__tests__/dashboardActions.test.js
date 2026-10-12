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

// #529: a finished job's artifacts can be deleted from disk via
// DELETE /assessments/{job_id}/artifacts. "Delete artifacts" takes Cancel's
// place for exactly the jobs that no longer offer it (terminal ones) --
// never offered alongside Cancel, never offered for a job still in flight.
describe('buildDashboardActionItems -- delete artifacts (#529)', () => {
  it.each(['SUCCEEDED', 'CANCELLED', 'FAILED'])(
    'offers Delete artifacts when the selected job is %s (terminal)',
    (status) => {
      const items = buildDashboardActionItems(t, [{ jobId: 'job-1', status }]);
      expect(idsOf(items)).toContain('delete-artifacts');
      const deleteItem = items.find((item) => item.id === 'delete-artifacts');
      expect(deleteItem.text).toBe('dashboard.actions.delete-artifacts');
    },
  );

  it.each(['RUNNING', 'WAITING', 'PENDING'])(
    'omits Delete artifacts when the selected job is %s (not terminal)',
    (status) => {
      const items = buildDashboardActionItems(t, [{ jobId: 'job-1', status }]);
      expect(idsOf(items)).not.toContain('delete-artifacts');
    },
  );

  it('never offers Delete artifacts and Cancel at the same time', () => {
    for (const status of ['SUCCEEDED', 'CANCELLED', 'FAILED', 'RUNNING', 'WAITING', 'PENDING']) {
      const items = buildDashboardActionItems(t, [{ jobId: 'job-1', status }]);
      const ids = idsOf(items);
      expect(ids.includes('cancel') && ids.includes('delete-artifacts')).toBe(false);
    }
  });

  it('does not offer Delete artifacts when nothing is selected', () => {
    const items = buildDashboardActionItems(t, []);
    expect(idsOf(items)).not.toContain('delete-artifacts');
  });

  it('decides off the first selected job only', () => {
    const items = buildDashboardActionItems(t, [
      { jobId: 'job-1', status: 'RUNNING' },
      { jobId: 'job-2', status: 'SUCCEEDED' },
    ]);
    expect(idsOf(items)).not.toContain('delete-artifacts');
  });
});
