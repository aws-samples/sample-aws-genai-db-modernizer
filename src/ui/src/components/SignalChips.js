// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import EngineBadge from './EngineBadge';

/**
 * The triage signals in a per-query row of the assignment review (#2312): one filled
 * EngineBadge per signal, in the engine's catalog colour with a text colour picked for
 * that fill. A filled badge looks the same in the light and dark themes. A signal with no
 * engine gets the neutral grey.
 */
export default function SignalChips({ signals }) {
  return (
    <span className="signal-badges">
      {signals.map((sig, i) => (
        <EngineBadge key={i} engine={sig.engine}>
          {sig.signal.replace(/_/g, ' ')}
        </EngineBadge>
      ))}
    </span>
  );
}
