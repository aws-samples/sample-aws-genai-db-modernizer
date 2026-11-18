// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useState } from 'react';

// Under 600 px, the same breakpoint Cloudscape calls "xxs" for a stacked layout (#2345).
const PHONE_QUERY = '(max-width: 599px)';

function matches() {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    && window.matchMedia(PHONE_QUERY).matches;
}

/** True while the window is narrower than 600 px; follows resizes. */
export default function usePhoneWidth() {
  const [isPhone, setIsPhone] = useState(matches);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined;
    const query = window.matchMedia(PHONE_QUERY);
    const onChange = () => setIsPhone(query.matches);
    onChange();
    query.addEventListener?.('change', onChange);
    return () => query.removeEventListener?.('change', onChange);
  }, []);
  return isPhone;
}
