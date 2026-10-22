import { memo } from 'react';
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { displayEngine } from "../utils/engineNames";

// At most this many queries are named; the rest are counted.
const SHOWN = 20;

/**
 * #1329: the queries no engine in the customer's engine scope can serve, from the
 * assignment's own `unservable_queries` (the API's assignment payload), so a customer
 * never approves the assignment, or reads the job summary, without seeing them. They
 * have no engine, are in no wave or design, and the assignment review cannot edit
 * them: a job's scope is fixed, so a new assessment with a wider scope serves them.
 * Shown by AssignmentGate.js and JobMonitoringSummary.js; nothing when there are none.
 */
const UnservableAssignmentNotice = memo(({ unservable, t }) => {
  const rows = Array.isArray(unservable) ? unservable : [];
  if (rows.length === 0) return null;
  const engines = [...new Set(rows.map(r => r.closest_engine).filter(Boolean))].map(displayEngine);
  const shown = rows.slice(0, SHOWN);
  const more = rows.length - shown.length;
  return (
    <div data-section="unservable-assignment">
      <Alert
        type="warning"
        header={t('assignment-unservable.header', { count: rows.length })}
      >
        <SpaceBetween size="xs">
          <Box>{t('assignment-unservable.intro')}</Box>
          <Box>
            {engines.length > 0
              ? t('assignment-unservable.serve', { engines: engines.join(', ') })
              : t('assignment-unservable.serve-wider')}
          </Box>
          {shown.map(r => (
            <Box key={r.query_id} fontSize="body-s">
              <span style={{ fontFamily: 'monospace' }} data-unservable-query={r.query_id}>{r.query_id}</span>
              {`: ${r.reason}`}
            </Box>
          ))}
          {more > 0 && <Box fontSize="body-s">{t('assignment-unservable.more', { count: more })}</Box>}
        </SpaceBetween>
      </Alert>
    </div>
  );
});

export default UnservableAssignmentNotice;
