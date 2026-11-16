// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import ApiManager from '../classes/ApiManager';
import AssessmentNotFound, { AssessmentLookupPending } from './AssessmentNotFound';

// What the API accepts (src/shared/job_id.py): 1 to 128 ASCII letters, digits,
// underscores and hyphens. Any other id cannot name an assessment.
const JOB_ID_SHAPE = /^[A-Za-z0-9_-]{1,128}$/;

// Wraps a job route (#2307). It looks the assessment up once and mounts the
// page only when it exists, so an unknown id shows one not-found state instead
// of a half-built page, request URLs and an approve button. A 404 and a 422 (an id
// the API refuses) both mean no such assessment. Any other outcome
// (a network error, a server error) mounts the page, which shows its own error.
export default function JobRouteGuard({ children }) {
  const { jobId } = useParams();
  const [lookup, setLookup] = useState({ jobId: null, missing: false });

  const wellFormed = JOB_ID_SHAPE.test(jobId || '');

  useEffect(() => {
    if (!wellFormed) return undefined;
    let cancelled = false;
    new ApiManager()
      .execute([{ id: 'job', path: `assessments/${encodeURIComponent(jobId)}`, method: 'GET' }])
      .then((results) => {
        if (!cancelled) setLookup({ jobId, missing: [404, 422].includes(results.job?.status) });
      })
      .catch(() => {
        if (!cancelled) setLookup({ jobId, missing: false });
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, wellFormed]);

  if (!wellFormed) return <AssessmentNotFound jobId={jobId} />;
  if (lookup.jobId !== jobId) return <AssessmentLookupPending />;
  return lookup.missing ? <AssessmentNotFound jobId={jobId} /> : children;
}
