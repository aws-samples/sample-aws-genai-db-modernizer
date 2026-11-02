// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useEffect, useState } from 'react';
import ApiManager from '../classes/ApiManager';
import { fetchExplorerEnabled } from '../utils/graphExplorerFlag';

/**
 * Whether the server has the experimental graph explorer on (#308).
 * `{ checked: false }` until the probe answers; any failure reads as off.
 */
export default function useGraphExplorerFlag() {
  const [state, setState] = useState({ checked: false, enabled: false });
  useEffect(() => {
    let live = true;
    fetchExplorerEnabled(new ApiManager()).then((enabled) => {
      if (live) setState({ checked: true, enabled });
    });
    return () => {
      live = false;
    };
  }, []);
  return state;
}
