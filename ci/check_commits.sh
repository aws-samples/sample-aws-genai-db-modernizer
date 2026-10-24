#!/usr/bin/env bash
# Lint every commit message added on top of main (#570), the CI counterpart
# of the local `check-commit-message` pre-commit hook (commit-msg stage only
# runs locally; CI never sees it, so this script catches a `--no-verify`
# commit or a message edited after the hook ran), and check every commit's
# author against ci/allowed_commit_authors.txt and the pull request author
# (#1408, scripts/check_commit_authors.py; see ci/README.md).
#
# No-op on a push to main: the range `origin/main..HEAD` is then empty (HEAD
# already IS origin/main), so there is nothing to check. Needs `origin/main`
# available locally -- a normal clone already has it; CI's checkout step
# must fetch full history (`fetch-depth: 0`) for a pull request build, since
# the PR branch and main may not share history within a shallow clone.
source "$(dirname "$0")/lib.sh"

if ! git rev-parse --verify --quiet origin/main >/dev/null; then
  log "commit messages: origin/main not found locally, skipping"
  exit 0
fi

RANGE="origin/main..HEAD"
if [ -z "$(git rev-list "$RANGE")" ]; then
  log "commit messages: no commits ahead of origin/main, nothing to check"
  exit 0
fi

status=0
log "commit messages ($RANGE)"
uv run python scripts/check_commit_message.py --range "$RANGE" || status=1
log "commit authors ($RANGE)"
uv run python scripts/check_commit_authors.py --range "$RANGE" || status=1
exit "$status"
