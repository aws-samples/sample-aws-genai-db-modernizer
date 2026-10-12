import { isTerminalStatus } from './jobStatus';

/**
 * Builds the Dashboard's action-dropdown items for the current table
 * selection. `t` is the function `useTranslation()` returns; `selectedItems`
 * is the Dashboard's selected-rows state (each item has a `status` field,
 * the raw job-level status from the API).
 *
 * Cancelling only works for a job that's still running, waiting or pending
 * (#518) -- the "Cancel analysis" item is left out once the first selected
 * job is already in a terminal status (SUCCEEDED/CANCELLED/FAILED), so the
 * dropdown never offers an action that would do nothing (#519). Once a job
 * is terminal there is nothing left to cancel, but there is something new
 * to do instead: delete its artifacts from disk (#529,
 * DELETE /assessments/{job_id}/artifacts) -- "Delete artifacts" replaces
 * "Cancel analysis" for exactly the jobs that no longer offer it.
 */
export function buildDashboardActionItems(t, selectedItems) {
  const items = [
    {
      text: t('dashboard.actions.view-results'),
      id: 'view-results',
    },
    {
      text: t('dashboard.actions.view-analysis'),
      id: 'view-analysis',
    },
  ];

  if (!selectedItems[0] || !isTerminalStatus(selectedItems[0].status)) {
    items.push({
      text: t('dashboard.actions.cancel-analysis'),
      id: 'cancel',
    });
  } else {
    items.push({
      text: t('dashboard.actions.delete-artifacts'),
      id: 'delete-artifacts',
    });
  }

  return items;
}
