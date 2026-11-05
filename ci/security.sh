#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# The one security scan (#1844): semgrep with the rules ci/security.toml pins, gitleaks
# and bandit. Pre-commit, the GitHub `security` job and ci/pre_merge.sh all run this.
#
#   ./ci/security.sh                       every tracked file
#   ./ci/security.sh --quick [--base REF]  the files changed since the merge base with REF
#   ./ci/security.sh FILE...               those files (the pre-commit hook)
#
# Exit 0 clean, 1 on a finding, 2 when a scanner could not run (a fetch, a checksum, a
# missing tool, or a scanner that exits with an error rather than findings).
# shellcheck source=lib.sh source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
exec uv run python ci/security.py "$@"
