#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Print which real-model runs (e2e-llm, bedrock-smoke) the diff against BASE
# (default origin/main) needs. Fails (exit 2, no verdict) on any git error.
# The path lists live in ci/needs_llm_run.py. No model call, no network.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
exec uv run python ci/needs_llm_run.py --base "${1:-origin/main}"
