#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``SubagentStop``: record a finished subagent as reported in the job's dispatch ledger
(#2215). The logic, and why, are in ``record_dispatch.py``; this file is its second
wiring, so each settings entry names one script. Never blocks."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import record_dispatch  # noqa: E402

if __name__ == "__main__":
    sys.exit(record_dispatch.main())
