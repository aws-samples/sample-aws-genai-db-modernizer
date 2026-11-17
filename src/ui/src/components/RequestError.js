// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React from 'react';
import { useTranslation } from 'react-i18next';
import Button from '@cloudscape-design/components/button';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { failureKind, failureMessageKey, isRetryable } from '../utils/requestFailure';

/**
 * #2346: how a failed request shows inside a section, a tab or a table cell. It prints a
 * fixed message for the kind of failure (utils/requestFailure.js) and, when trying again
 * can help, a Retry link. The request URL, status code and server text never reach the
 * page; log them with `reportFailure` where the request fails. A page-level failure is a
 * Flashbar item built by `failureFlash` instead.
 *
 * `failure` is the kind `reportFailure` returned, or the failed request itself.
 * `lead` is an optional short sentence before the message.
 * The line sits in a live region, so a screen reader announces it.
 */
export default function RequestError({ failure, lead, onRetry, t: tProp }) {
  const { t: tHook } = useTranslation();
  const t = tProp || tHook;
  const kind = failureKind(failure);
  const text = t(failureMessageKey(kind));
  const canRetry = Boolean(onRetry) && isRetryable(kind);
  return (
    <span role="status">
      <StatusIndicator type="error">
        {lead ? `${lead} ${text}` : text}
        {canRetry && <>{' '}<Button variant="inline-link" onClick={onRetry}>{t('common.actions.retry')}</Button></>}
      </StatusIndicator>
    </span>
  );
}
