// Maps a backend job/stage status string to a Cloudscape <StatusIndicator>
// `type` and a translated label. The API returns upper-case job-level
// statuses (RUNNING, SUCCEEDED, FAILED, CANCELLED, WAITING, PENDING,
// COMPLETED) and lower-case per-stage/agent statuses (completed, running,
// in-progress, failed, error, pending, success, cancelled, waiting) -- both
// are covered here so Dashboard.js and JobMonitoring.js never disagree on
// what a status means (#383, #518 round 1).

const STATUS_TYPE_MAP = {
  COMPLETED: 'success',
  SUCCEEDED: 'success',
  completed: 'success',
  success: 'success',
  RUNNING: 'in-progress',
  running: 'in-progress',
  'in-progress': 'in-progress',
  WAITING: 'pending',
  waiting: 'pending',
  FAILED: 'error',
  failed: 'error',
  error: 'error',
  CANCELLED: 'stopped',
  cancelled: 'stopped',
  PENDING: 'pending',
  pending: 'pending',
};

// i18n key for each status's label -- none of these are plural (a status is
// always exactly one word/phrase, never a count), so no CLDR _one/_other
// suffixes apply.
const STATUS_LABEL_KEY_MAP = {
  COMPLETED: 'job-status.completed',
  SUCCEEDED: 'job-status.succeeded',
  completed: 'job-status.completed',
  success: 'job-status.succeeded',
  RUNNING: 'job-status.running',
  running: 'job-status.running',
  'in-progress': 'job-status.running',
  WAITING: 'job-status.waiting',
  waiting: 'job-status.waiting',
  FAILED: 'job-status.failed',
  failed: 'job-status.failed',
  error: 'job-status.failed',
  CANCELLED: 'job-status.cancelled',
  cancelled: 'job-status.cancelled',
  PENDING: 'job-status.pending',
  pending: 'job-status.pending',
};

/** Every status this module recognizes -- used by tests to assert full coverage. */
export const KNOWN_STATUSES = Object.keys(STATUS_TYPE_MAP);

/** The i18n key a status's label comes from, or undefined for an unknown status. */
export function getStatusLabelKey(status) {
  return STATUS_LABEL_KEY_MAP[status];
}

/** Cloudscape <StatusIndicator type=...>. Falls back to "info" for an unknown status. */
export function getStatusType(status) {
  return STATUS_TYPE_MAP[status] || 'info';
}

/**
 * Translated label for a status. `t` is the function `useTranslation()` returns.
 * Falls back to Title Case of the raw status for one this module doesn't know.
 */
export function getStatusText(t, status) {
  if (!status) return t('job-status.unknown');
  const key = STATUS_LABEL_KEY_MAP[status];
  if (key) return t(key);
  return status.charAt(0).toUpperCase() + status.slice(1).toLowerCase();
}
