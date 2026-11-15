// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// A duration the API could not measure arrives as null, a negative sentinel
// (-1) or a non-number. It reads as unknown, never as "-1h -1m" (#2304).
export const UNKNOWN_DURATION = '—';

export function formatDuration(seconds) {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) {
    return UNKNOWN_DURATION;
  }
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${hours}h ${minutes}m`;
}
