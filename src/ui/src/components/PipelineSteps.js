// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useTranslation } from 'react-i18next';
import Steps from '@cloudscape-design/components/steps';
import { HUMAN_STEPS, stepStatuses } from '../utils/pipelineSteps';

/**
 * The phases of a running job as Cloudscape Steps (#2317): a status icon, a name that
 * reads as text, and a note on the phases that wait for a person. It replaces the
 * hand-built tracker, whose labels collided on a phone.
 */
export default function PipelineSteps({ current, complete = false }) {
  const { t } = useTranslation();
  const steps = stepStatuses(current, complete).map(({ id, status }) => ({
    status,
    statusIconAriaLabel: t(`job-summary.steps.status.${status}`),
    header: t(`job-summary.steps.${id}`),
    details: HUMAN_STEPS.has(id) ? t('job-summary.steps.needs-you') : undefined,
  }));
  return <Steps ariaLabel={t('job-summary.steps.label')} steps={steps} />;
}
