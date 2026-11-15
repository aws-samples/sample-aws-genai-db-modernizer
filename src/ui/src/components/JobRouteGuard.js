// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import ApiManager from '../classes/ApiManager';
import AssessmentNotFound from './AssessmentNotFound';

// Wraps a job route (#2307). It looks the assessment up once and mounts the
// page only when it exists, so an unknown id shows one not-found state instead
// of a half-built page, request URLs and an approve button. Any other outcome
// (a network error, a server error) mounts the page, which shows its own error.
export default function JobRouteGuard({ children }) {
  const { jobId } = useParams();
  const [lookup, setLookup] = useState({ jobId: null, missing: false });

  useEffect(() => {
    let cancelled = false;
    new ApiManager()
      .execute([{ id: 'job', path: `assessments/${encodeURIComponent(jobId)}`, method: 'GET' }])
      .then((results) => {
        if (!cancelled) setLookup({ jobId, missing: results.job?.status === 404 });
      })
      .catch(() => {
        if (!cancelled) setLookup({ jobId, missing: false });
      });
    return () => {
      cancelled = true;
    };
  }, [jobId]);

  if (lookup.jobId !== jobId) return null;
  return lookup.missing ? <AssessmentNotFound jobId={jobId} /> : children;
}
