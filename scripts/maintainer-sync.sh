#!/usr/bin/env bash
#
# maintainer-sync.sh
#
# Syncs the current source branch to a target deployment repo using rsync.
# The source repo is the source of truth. Target-only files are protected.
#
# Required environment variables:
#   TARGET_REPO_PATH  - Absolute path to local target repo clone
#
# Optional environment variables:
#   SYNC_DRY_RUN      - Set to "true" to preview changes without applying
#   SYNC_CONFIG       - Path to sync config file (default: $TARGET_REPO_PATH/.sync-config)
#
# Usage:
#   export TARGET_REPO_PATH=/path/to/target/clone
#   ./scripts/maintainer-sync.sh
#
set -euo pipefail

# --- Validation ---

if [ -z "${TARGET_REPO_PATH:-}" ]; then
  echo "Error: TARGET_REPO_PATH environment variable is not set."
  echo "Set it to the absolute path of your local target repo clone."
  exit 1
fi

if [ ! -d "$TARGET_REPO_PATH/.git" ]; then
  echo "Error: TARGET_REPO_PATH ($TARGET_REPO_PATH) is not a valid git repository."
  exit 1
fi

# Check target repo is clean — you should NOT be working directly in the target repo.
# The target repo is a deployment mirror only. All development happens in the source repo.
if ! git -C "$TARGET_REPO_PATH" diff --quiet 2>/dev/null || ! git -C "$TARGET_REPO_PATH" diff --cached --quiet 2>/dev/null; then
  echo "Error: Target repo has uncommitted changes."
  echo ""
  echo "  The target repo is a deployment mirror — do NOT develop there directly."
  echo "  All work should happen in the source repo (this one)."
  echo ""
  echo "  If you have unintended changes in the target, resolve them first:"
  echo "    cd $TARGET_REPO_PATH && git stash"
  exit 1
fi

if [ -f "$TARGET_REPO_PATH/.git/index.lock" ]; then
  echo "Error: Target repo has a git lock file (.git/index.lock)."
  echo "  Another git process may be running, or a previous one crashed."
  echo "  If no git process is active: rm $TARGET_REPO_PATH/.git/index.lock"
  exit 1
fi

SOURCE_REPO_PATH=$(git rev-parse --show-toplevel)
BRANCH=$(git rev-parse --abbrev-ref HEAD)

if [ "$BRANCH" = "main" ] || [ "$BRANCH" = "HEAD" ]; then
  echo "Error: You must be on a feature branch, not 'main' or detached HEAD."
  exit 1
fi

# Map the source branch to the target branch name.
# The target (GitLab) only builds feat/* branches, so exp/* branches are
# translated to feat/* on the target. All other branches map unchanged.
TARGET_BRANCH="$BRANCH"
if [[ "$BRANCH" == exp/* ]]; then
  TARGET_BRANCH="feat/${BRANCH#exp/}"
fi

SYNC_CONFIG="${SYNC_CONFIG:-$TARGET_REPO_PATH/.sync-config}"
DRY_RUN="${SYNC_DRY_RUN:-false}"

if [ ! -f "$SYNC_CONFIG" ]; then
  echo "Error: Sync config not found at $SYNC_CONFIG"
  echo "The .sync-config file should live in the target repo root."
  exit 1
fi

echo "==> Source repo: $SOURCE_REPO_PATH (branch: $BRANCH)"
echo "==> Target repo: $TARGET_REPO_PATH"
[ "$TARGET_BRANCH" != "$BRANCH" ] && echo "==> Target branch: $TARGET_BRANCH (mapped from $BRANCH)"
echo "==> Config: $SYNC_CONFIG"
[ "$DRY_RUN" = "true" ] && echo "==> DRY RUN MODE (no changes will be applied)"
echo ""

# --- Parse config: build rsync exclude list ---

RSYNC_EXCLUDES=()
PROTECTED_FILES=()

current_section=""
while IFS= read -r line || [ -n "$line" ]; do
  # Skip comments and empty lines
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  [[ -z "${line// }" ]] && continue

  # Detect section headers
  if [[ "$line" =~ ^\[(.+)\]$ ]]; then
    current_section="${BASH_REMATCH[1]}"
    continue
  fi

  # Strip leading/trailing whitespace
  line=$(echo "$line" | xargs)

  case "$current_section" in
    exclude)
      RSYNC_EXCLUDES+=("--exclude=$line")
      ;;
    protect)
      PROTECTED_FILES+=("$line")
      RSYNC_EXCLUDES+=("--exclude=$line")
      ;;
  esac
done < "$SYNC_CONFIG"

# --- Backup protected files ---

BACKUP_DIR=$(mktemp -d /tmp/maintainer-sync-backup-XXXXXX)
echo "==> Backing up ${#PROTECTED_FILES[@]} protected files..."

for file in "${PROTECTED_FILES[@]}"; do
  src="$TARGET_REPO_PATH/$file"
  if [ -e "$src" ]; then
    dest="$BACKUP_DIR/$file"
    mkdir -p "$(dirname "$dest")"
    cp -a "$src" "$dest"
  fi
done

# --- rsync source → target ---

echo "==> Syncing files from source to target..."

RSYNC_ARGS=(
  -av
  --delete
  --exclude=".git/"
  --exclude=".git"
  "${RSYNC_EXCLUDES[@]}"
)

if [ "$DRY_RUN" = "true" ]; then
  RSYNC_ARGS+=("--dry-run")
fi

rsync "${RSYNC_ARGS[@]}" "$SOURCE_REPO_PATH/" "$TARGET_REPO_PATH/"

# --- Restore protected files ---

echo "==> Restoring protected files..."
for file in "${PROTECTED_FILES[@]}"; do
  backup="$BACKUP_DIR/$file"
  if [ -e "$backup" ]; then
    dest="$TARGET_REPO_PATH/$file"
    mkdir -p "$(dirname "$dest")"
    cp -a "$backup" "$dest"
  fi
done

rm -rf "$BACKUP_DIR"

if [ "$DRY_RUN" = "true" ]; then
  echo ""
  echo "==> Dry run complete. No changes applied."
  exit 0
fi

# --- Commit and push in target repo ---

echo "==> Creating commit in target repo..."
cd "$TARGET_REPO_PATH"

# Create or switch to the target branch
git checkout -B "$TARGET_BRANCH"

# Stage all changes
git add -A

# Check if there are changes to commit
if git diff --cached --quiet; then
  echo "No changes to sync. Target is already up to date."
  exit 0
fi

# First commit attempt — hooks may auto-fix files (openapi regen, formatting, etc.)
if ! git commit -m "chore: sync changes from source branch ${BRANCH}" 2>/dev/null; then
  echo "==> Hooks modified files, committing auto-fixes..."
  git add -A
  if ! git commit -m "chore: sync changes from source branch ${BRANCH}"; then
    echo ""
    echo "Error: Pre-commit hooks failed. Review the errors above and fix in the source repo."
    exit 1
  fi
fi

echo "==> Pushing to target remote..."
if ! git push origin "$TARGET_BRANCH" --force-with-lease; then
  echo ""
  echo "Error: Push failed. You may need to re-authenticate."
  exit 1
fi

echo ""
echo "Done! Source branch '$BRANCH' synced to target branch '$TARGET_BRANCH'."
echo "CI pipeline should trigger automatically."
