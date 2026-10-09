#!/usr/bin/env bash
#
# maintainer-sync.sh
#
# Syncs one commit of the source repo to a target deployment repo using
# rsync. The source repo is the source of truth. Target-only files are
# protected.
#
# The script exports a single git ref (default HEAD) with `git archive`
# into a throwaway directory and rsyncs from there -- never from the
# working tree -- so untracked files in the source checkout can never
# reach the target. If the source repo itself has uncommitted tracked
# changes, the script warns that they will NOT be synced (only the
# committed ref is).
#
# Before copying files, the script brings the target clone up to date: it
# acquires an exclusive lock against concurrent runs on the same clone,
# fetches the target remote, refuses outright (before touching anything)
# if the target's local default branch is ahead of or has diverged from
# the remote, stashes any uncommitted or untracked changes in the target
# clone, and fast-forwards the target's default branch to the remote tip.
# The sync branch is then built from that up-to-date default branch.
#
# Files the target's own .gitignore excludes (local env files, dependency
# folders, etc.) are protected from `rsync --delete` with a generated
# filter, independently of `.sync-config`.
#
# Every sync commit records a `Source-Commit: <sha>` trailer, so anyone
# can see which source commit the target branch matches, and so the
# script can tell its own commits apart from anyone else's. If the
# remote target branch already exists and carries a commit without that
# trailer, the script refuses to overwrite it unless `--force` is given.
# Before pushing, the script prints the diff against the target's default
# branch and refuses to push if that diff touches a path listed under
# `[protect]` in the sync config -- those paths must never change through
# a sync. A `[protect]` entry anchored with a leading slash (e.g.
# `/*.env`) is still matched at any depth if it also contains a glob
# (`*`/`?`), since the match is a shell glob and `*` crosses `/`
# boundaries too; this is deliberate and fails safe (over-protecting,
# never under-protecting). The push itself uses an explicit refspec and
# an explicit lease on the remote sha observed right after the fetch, so
# a concurrent push by someone else is never silently overwritten.
#
# Required environment variables:
#   TARGET_REPO_PATH  - Absolute path to local target repo clone
#
# Options (each has an environment variable equivalent, used when the
# option is not passed; the option always wins):
#   --ref <ref>       - Git ref/commit to sync (default: HEAD)
#                        env: SYNC_REF
#   --branch <name>   - Target branch name (default: the current source
#                        branch; exp/* is mapped to feat/*, as before).
#                        Must be a plain branch short name: a refs/...,
#                        heads/... or origin/... path is rejected outright
#                        (the latter would create a local branch shadowing
#                        the remote-tracking ref of the same name). Refused
#                        if it matches the target repo's default branch
#                        case-insensitively (a case-only difference like
#                        "Main" vs "main" collides on a case-insensitive
#                        filesystem), or if it can't be determined
#                        (detached HEAD with no --branch/SYNC_TARGET_BRANCH).
#                        env: SYNC_TARGET_BRANCH
#   --message <text>  - Commit message (default: "chore: sync changes
#                        from source branch <branch>"). A
#                        "Source-Commit: <12-character sha>" trailer is always
#                        appended, however the message is chosen.
#                        env: SYNC_COMMIT_MESSAGE
#   --force           - Overwrite a remote target branch even if it has
#                        a commit without a Source-Commit trailer (no
#                        environment variable equivalent, deliberately).
#
# Other optional environment variables:
#   SYNC_DRY_RUN      - Set to "true" to preview changes without applying.
#                        The target remote is still fetched. The preview
#                        is built for real (export, rsync, commit) inside
#                        a disposable `git worktree` of
#                        'origin/<default branch>', so the printed diff
#                        stat and protect-path check reflect what a real
#                        run would do; nothing in the actual target clone
#                        is stashed, checked out, committed or pushed. The
#                        target's own hooks still run for real against
#                        that disposable worktree (same as a real run),
#                        since the preview commit is built the same way.
#   SYNC_CONFIG       - Path to sync config file (default:
#                        <worktree>/.sync-config, read from the preview
#                        worktree in a dry run so it reflects the target
#                        default branch, not a possibly-stale checkout)
#
# Usage:
#   export TARGET_REPO_PATH=/path/to/target/clone
#   ./scripts/maintainer-sync.sh [--ref <ref>] [--branch <name>] [--message <text>] [--force]
#
set -euo pipefail

# --- Clean up throwaway state on any exit ---

LOCK_DIR=""
LOCK_ACQUIRED=false
EXPORT_DIR=""
IGNORED_FILTER=""
WORKTREE_DIR=""

cleanup() {
  if [ -n "$WORKTREE_DIR" ]; then
    # Just this worktree, not a blanket `worktree prune`: prune can also
    # drop the user's other, unrelated worktree registrations for this
    # same target clone.
    git -C "$TARGET_REPO_PATH" worktree remove --force "$WORKTREE_DIR" 2>/dev/null || true
    rm -rf "$(dirname "$WORKTREE_DIR")" 2>/dev/null || true
  fi
  [ -n "$EXPORT_DIR" ] && rm -rf "$EXPORT_DIR"
  [ -n "$IGNORED_FILTER" ] && rm -f "$IGNORED_FILTER"
  if [ "$LOCK_ACQUIRED" = "true" ] && [ -n "$LOCK_DIR" ]; then
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  return 0
}
trap cleanup EXIT

# --- Parse arguments ---

REF_ARG=""
BRANCH_ARG=""
MESSAGE_ARG=""
FORCE_ARG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --ref)
      REF_ARG="${2:?--ref requires a value}"
      shift 2
      ;;
    --branch)
      BRANCH_ARG="${2:?--branch requires a value}"
      shift 2
      ;;
    --message)
      MESSAGE_ARG="${2:?--message requires a value}"
      shift 2
      ;;
    --force)
      FORCE_ARG="true"
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--ref <ref>] [--branch <name>] [--message <text>] [--force]"
      echo "See the comment header in this file for the full option and environment variable list."
      exit 0
      ;;
    *)
      echo "Error: Unknown argument: $1"
      exit 1
      ;;
  esac
done

# --- Validation ---

if [ -z "${TARGET_REPO_PATH:-}" ]; then
  echo "Error: TARGET_REPO_PATH environment variable is not set."
  echo "Set it to the absolute path of your local target repo clone."
  exit 1
fi

# Resolve to an absolute, symlink-free path right away: the script later
# `cd`s into the target (and a disposable worktree), so a relative
# TARGET_REPO_PATH would silently stop resolving correctly from then on.
# CDPATH= avoids a user's CDPATH redirecting `cd` to an unexpected
# directory (or printing the new directory to stdout, corrupting the
# capture).
# shellcheck disable=SC1007 # intentional: CDPATH= scopes the empty value to this one `cd`
if ! TARGET_REPO_PATH=$(CDPATH= cd "$TARGET_REPO_PATH" 2>/dev/null && pwd -P); then
  echo "Error: TARGET_REPO_PATH does not exist or is not a directory."
  exit 1
fi

if [ ! -d "$TARGET_REPO_PATH/.git" ]; then
  echo "Error: TARGET_REPO_PATH ($TARGET_REPO_PATH) is not a valid git repository."
  exit 1
fi

# Same reasoning for an explicitly-set SYNC_CONFIG: resolve it to absolute
# now, before any `cd`, so a relative path stays correct later. (A default,
# unset SYNC_CONFIG is resolved against the target worktree further down,
# once that worktree is known -- see the "Parse config" section.) Resolve
# the directory and reassemble the path as two separate statements: in
# `VAR=$(cmd1)/$(cmd2)`, the assignment's exit status reflects only the
# last command substitution (basename, which essentially never fails),
# so a failing `cd` on a missing directory would otherwise never surface.
if [ -n "${SYNC_CONFIG:-}" ]; then
  # shellcheck disable=SC1007 # intentional: CDPATH= scopes the empty value to this one `cd`
  sync_config_dir=$(CDPATH= cd "$(dirname "$SYNC_CONFIG")" 2>/dev/null && pwd -P) || {
    echo "Error: Directory of SYNC_CONFIG does not exist."
    exit 1
  }
  SYNC_CONFIG="$sync_config_dir/$(basename "$SYNC_CONFIG")"
fi

# Lock against concurrent runs against the same target clone. mkdir is
# atomic, so this is safe even if two runs start at the same instant.
# Only the run that created the lock removes it.
LOCK_DIR="$TARGET_REPO_PATH/.git/maintainer-sync.lock"
if mkdir "$LOCK_DIR" 2>/dev/null; then
  LOCK_ACQUIRED=true
else
  echo "Error: Another maintainer-sync run appears to be in progress against $TARGET_REPO_PATH"
  echo "  ($LOCK_DIR exists)."
  echo "  If no run is actually active (e.g. a previous one crashed): rmdir $LOCK_DIR"
  exit 1
fi

if [ -f "$TARGET_REPO_PATH/.git/index.lock" ]; then
  echo "Error: Target repo has a git lock file (.git/index.lock)."
  echo "  Another git process may be running, or a previous one crashed."
  echo "  If no git process is active: rm $TARGET_REPO_PATH/.git/index.lock"
  exit 1
fi

TARGET_DEFAULT_BRANCH=$(git -C "$TARGET_REPO_PATH" symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's#^refs/remotes/origin/##')
TARGET_DEFAULT_BRANCH="${TARGET_DEFAULT_BRANCH:-main}"

SOURCE_REPO_PATH=$(git rev-parse --show-toplevel)
REF="${REF_ARG:-${SYNC_REF:-HEAD}}"

if ! SOURCE_COMMIT=$(git -C "$SOURCE_REPO_PATH" rev-parse --verify "${REF}^{commit}" 2>/dev/null); then
  echo "Error: '$REF' does not resolve to a commit in $SOURCE_REPO_PATH."
  exit 1
fi

if [ -n "$(git -C "$SOURCE_REPO_PATH" status --porcelain --untracked-files=no)" ]; then
  echo "Warning: The source repo has uncommitted tracked changes."
  echo "  Only the committed ref ('$REF') is synced; uncommitted edits are NOT included."
fi

CURRENT_BRANCH=$(git -C "$SOURCE_REPO_PATH" rev-parse --abbrev-ref HEAD)
BRANCH="${BRANCH_ARG:-${SYNC_TARGET_BRANCH:-$CURRENT_BRANCH}}"

if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
  echo "Error: Can't determine a branch name (detached HEAD)."
  echo "  Pass --branch <name> or set SYNC_TARGET_BRANCH."
  exit 1
fi

# Map the source branch to the target branch name.
# The target (GitLab) only builds feat/* branches, so exp/* branches are
# translated to feat/* on the target. All other branches map unchanged.
TARGET_BRANCH="$BRANCH"
if [[ "$BRANCH" == exp/* ]]; then
  TARGET_BRANCH="feat/${BRANCH#exp/}"
fi

# Reject qualified ref forms outright -- this must be a short branch name,
# not a ref path. "refs/heads/<default>" or "heads/<default>" would slip
# past the default-branch check below by surface spelling only;
# "origin/<anything>" or "remotes/origin/<anything>" would create a local
# branch that shadows the remote-tracking ref of the same name and breaks
# later runs; "tags/<anything>" would be similarly misleading next to a
# tag of the same name.
case "$TARGET_BRANCH" in
  refs/*|heads/*|origin/*|remotes/*|tags/*)
    echo "Error: --branch/SYNC_TARGET_BRANCH must be a plain branch name, not a ref path or"
    echo "  remote-tracking/tag name ('$TARGET_BRANCH')."
    exit 1
    ;;
esac

if ! git check-ref-format --branch "$TARGET_BRANCH" >/dev/null 2>&1; then
  echo "Error: '$TARGET_BRANCH' is not a valid branch name."
  exit 1
fi

# Case-insensitive: "Main" vs "main" are different refs to git, but the
# same path on a case-insensitive filesystem (e.g. default macOS APFS),
# so writing "Main" there corrupts the real "main" ref on disk.
target_branch_lower=$(printf '%s' "$TARGET_BRANCH" | tr '[:upper:]' '[:lower:]')
target_default_lower=$(printf '%s' "$TARGET_DEFAULT_BRANCH" | tr '[:upper:]' '[:lower:]')
if [ "$target_branch_lower" = "$target_default_lower" ]; then
  echo "Error: Target branch '$TARGET_BRANCH' matches the target repo's default branch"
  echo "  '$TARGET_DEFAULT_BRANCH' (case-insensitively). Refusing to sync directly onto it."
  echo "  Pass a different --branch."
  exit 1
fi

DRY_RUN="${SYNC_DRY_RUN:-false}"
FORCE="${FORCE_ARG:-false}"

# The default commit subject names where the content actually came from:
# the source branch, if there is one -- never $BRANCH, which may have been
# overridden by --branch/SYNC_TARGET_BRANCH to a *target* branch name
# unrelated to the source. Detached HEAD (only reachable here when
# --branch/SYNC_TARGET_BRANCH was given explicitly) falls back to the
# synced commit's short sha instead.
if [ "$CURRENT_BRANCH" != "HEAD" ] && [ -n "$CURRENT_BRANCH" ]; then
  DEFAULT_COMMIT_SUBJECT="chore: sync changes from source branch ${CURRENT_BRANCH}"
else
  SOURCE_SHORT_SHA=$(git -C "$SOURCE_REPO_PATH" rev-parse --short "$SOURCE_COMMIT")
  DEFAULT_COMMIT_SUBJECT="chore: sync changes from source commit ${SOURCE_SHORT_SHA}"
fi
COMMIT_MESSAGE="${MESSAGE_ARG:-${SYNC_COMMIT_MESSAGE:-$DEFAULT_COMMIT_SUBJECT}}"

echo "==> Source repo: $SOURCE_REPO_PATH (ref: $REF -> $SOURCE_COMMIT)"
echo "==> Source branch: $BRANCH"
echo "==> Target repo: $TARGET_REPO_PATH"
[ "$TARGET_BRANCH" != "$BRANCH" ] && echo "==> Target branch: $TARGET_BRANCH (mapped from $BRANCH)"
[ "$DRY_RUN" = "true" ] && echo "==> DRY RUN MODE (no changes will be applied)"
[ "$FORCE" = "true" ] && echo "==> --force: will overwrite a remote target branch with foreign commits"
echo ""

# --- Fetch the target remote ---

echo "==> Fetching target remote..."
git -C "$TARGET_REPO_PATH" fetch origin --prune

# --- Refuse to silently overwrite someone else's commits on the target branch ---

REMOTE_SHA=""
if REMOTE_SHA=$(git -C "$TARGET_REPO_PATH" rev-parse "refs/remotes/origin/$TARGET_BRANCH" 2>/dev/null); then
  FOREIGN_COMMIT=""
  while IFS= read -r candidate_sha; do
    [ -z "$candidate_sha" ] && continue
    trailer=$(git -C "$TARGET_REPO_PATH" log -1 --format='%(trailers:key=Source-Commit,valueonly)' "$candidate_sha")
    if [ -z "$trailer" ]; then
      FOREIGN_COMMIT="$candidate_sha"
      break
    fi
  done < <(git -C "$TARGET_REPO_PATH" log --format="%H" \
    "refs/remotes/origin/${TARGET_DEFAULT_BRANCH}..refs/remotes/origin/${TARGET_BRANCH}")

  if [ -n "$FOREIGN_COMMIT" ]; then
    if [ "$FORCE" = "true" ]; then
      echo "==> 'origin/$TARGET_BRANCH' has a commit without a Source-Commit trailer ($FOREIGN_COMMIT); --force will overwrite it."
    elif [ "$DRY_RUN" = "true" ]; then
      echo "==> 'origin/$TARGET_BRANCH' has a commit without a Source-Commit trailer ($FOREIGN_COMMIT): would refuse (pass --force to overwrite)."
    else
      echo ""
      echo "Error: 'origin/$TARGET_BRANCH' has a commit without a Source-Commit trailer ($FOREIGN_COMMIT)."
      echo "  This branch may predate this script adding trailers, or someone pushed to it"
      echo "  directly. Refusing to overwrite it."
      echo "  Inspect it first:   git -C $TARGET_REPO_PATH log refs/remotes/origin/$TARGET_BRANCH"
      echo "  If you intend to overwrite it anyway, rerun once with --force."
      exit 1
    fi
  fi
else
  REMOTE_SHA=""
fi

# --- Refuse if the target's local default branch is ahead of, or has
#     diverged from, the remote -- before touching anything else ---

# Both sides are fully qualified (refs/heads/..., refs/remotes/origin/...):
# a bare branch name here is ambiguous if the clone also has a tag of the
# same name (e.g. a "main" tag) -- git then prints a warning instead of
# the counts, which 2>&1 would feed straight into AHEAD_BEHIND, breaking
# the numeric comparisons below silently (every check reads as "false").
# Discard stderr and validate the output is exactly two integers instead.
if ! AHEAD_BEHIND=$(git -C "$TARGET_REPO_PATH" rev-list --left-right --count \
  "refs/heads/${TARGET_DEFAULT_BRANCH}...refs/remotes/origin/${TARGET_DEFAULT_BRANCH}" 2>/dev/null) \
  || ! [[ "$AHEAD_BEHIND" =~ ^[0-9]+[[:space:]]+[0-9]+$ ]]; then
  echo "Error: Could not compare target's local '$TARGET_DEFAULT_BRANCH' with 'origin/$TARGET_DEFAULT_BRANCH'."
  echo "  Does the target clone have a local '$TARGET_DEFAULT_BRANCH' branch, and did the fetch succeed?"
  exit 1
fi
read -r TARGET_AHEAD TARGET_BEHIND <<< "$AHEAD_BEHIND"

TARGET_DIRTY=false
if ! git -C "$TARGET_REPO_PATH" diff --quiet 2>/dev/null \
  || ! git -C "$TARGET_REPO_PATH" diff --cached --quiet 2>/dev/null \
  || [ -n "$(git -C "$TARGET_REPO_PATH" ls-files --others --exclude-standard)" ]; then
  TARGET_DIRTY=true
fi

if [ "$DRY_RUN" = "true" ]; then
  if [ "$TARGET_AHEAD" -gt 0 ] && [ "$TARGET_BEHIND" -gt 0 ]; then
    echo "==> Target '$TARGET_DEFAULT_BRANCH' has diverged from 'origin/$TARGET_DEFAULT_BRANCH'" \
      "($TARGET_AHEAD local, $TARGET_BEHIND remote commit(s)): would stop with an error, before touching anything."
  elif [ "$TARGET_AHEAD" -gt 0 ]; then
    echo "==> Target '$TARGET_DEFAULT_BRANCH' is $TARGET_AHEAD commit(s) ahead of 'origin/$TARGET_DEFAULT_BRANCH': would stop with an error, before touching anything."
  else
    if [ "$TARGET_DIRTY" = "true" ]; then
      echo "==> Target repo has uncommitted or untracked changes: would stash them."
    fi
    if [ "$TARGET_BEHIND" -gt 0 ]; then
      echo "==> Target '$TARGET_DEFAULT_BRANCH' is behind 'origin/$TARGET_DEFAULT_BRANCH' by $TARGET_BEHIND commit(s): would fast-forward."
    else
      echo "==> Target '$TARGET_DEFAULT_BRANCH' is up to date with 'origin/$TARGET_DEFAULT_BRANCH'."
    fi
  fi

  echo "==> Building a preview from 'origin/$TARGET_DEFAULT_BRANCH' in a temporary worktree..."
  WORKTREE_PARENT=$(mktemp -d /tmp/maintainer-sync-worktree-XXXXXX)
  WORKTREE_DIR="$WORKTREE_PARENT/preview"
  git -C "$TARGET_REPO_PATH" worktree add --detach --quiet "$WORKTREE_DIR" "refs/remotes/origin/$TARGET_DEFAULT_BRANCH"
  WORK_TREE="$WORKTREE_DIR"
else
  if [ "$TARGET_AHEAD" -gt 0 ]; then
    echo ""
    if [ "$TARGET_BEHIND" -gt 0 ]; then
      echo "Error: Target '$TARGET_DEFAULT_BRANCH' has diverged from 'origin/$TARGET_DEFAULT_BRANCH'"
      echo "  ($TARGET_AHEAD local, $TARGET_BEHIND remote commit(s))."
    else
      echo "Error: Target '$TARGET_DEFAULT_BRANCH' is $TARGET_AHEAD commit(s) ahead of 'origin/$TARGET_DEFAULT_BRANCH'."
    fi
    echo "  Refusing to touch the target clone before resolving this. Nothing has been stashed or changed."
    echo "    cd $TARGET_REPO_PATH"
    echo "    git log --oneline refs/remotes/origin/$TARGET_DEFAULT_BRANCH..$TARGET_DEFAULT_BRANCH   # commits only local"
    exit 1
  fi

  if [ "$TARGET_DIRTY" = "true" ]; then
    STASH_MESSAGE="maintainer-sync: ${BRANCH} $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "==> Target repo has uncommitted or untracked changes; stashing them."
    echo "    The target repo is a deployment mirror — do NOT develop there directly."
    echo "    All work should happen in the source repo (this one)."
    git -C "$TARGET_REPO_PATH" stash push --include-untracked -m "$STASH_MESSAGE"
    echo "    Stashed as: $STASH_MESSAGE"
    echo "    List stashes:  git -C $TARGET_REPO_PATH stash list"
    echo "    Restore later: git -C $TARGET_REPO_PATH stash pop"
  fi

  echo "==> Updating target '$TARGET_DEFAULT_BRANCH' to latest 'origin/$TARGET_DEFAULT_BRANCH'..."
  git -C "$TARGET_REPO_PATH" checkout "$TARGET_DEFAULT_BRANCH"
  if ! git -C "$TARGET_REPO_PATH" merge --ff-only "refs/remotes/origin/$TARGET_DEFAULT_BRANCH"; then
    echo ""
    echo "Error: Target '$TARGET_DEFAULT_BRANCH' can't be fast-forwarded to 'origin/$TARGET_DEFAULT_BRANCH' (changed concurrently?)."
    exit 1
  fi
  WORK_TREE="$TARGET_REPO_PATH"
fi

# --- Parse config: build rsync exclude list ---
#
# Read from $WORK_TREE, not $TARGET_REPO_PATH: in a dry run, $WORK_TREE is
# the preview worktree built from the target's current default branch, and
# $TARGET_REPO_PATH's own checked-out tree can be on a different, possibly
# older, commit entirely.

SYNC_CONFIG="${SYNC_CONFIG:-$WORK_TREE/.sync-config}"
echo "==> Config: $SYNC_CONFIG"

if [ ! -f "$SYNC_CONFIG" ]; then
  echo "Error: Sync config not found at $SYNC_CONFIG"
  echo "The .sync-config file should live in the target repo root."
  exit 1
fi

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

  # Trim leading/trailing whitespace without reparsing shell words --
  # piping through xargs would mangle a line containing quotes or
  # backslashes.
  line="${line#"${line%%[![:space:]]*}"}"
  line="${line%"${line##*[![:space:]]}"}"

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

# --- Export the ref to a throwaway directory ---

echo "==> Exporting $REF ($SOURCE_COMMIT)..."
EXPORT_DIR=$(mktemp -d /tmp/maintainer-sync-export-XXXXXX)
git -C "$SOURCE_REPO_PATH" archive "$SOURCE_COMMIT" | tar -x -C "$EXPORT_DIR"

# --- Protect the target's own gitignored files from rsync --delete ---
#
# The export has no ignored files (git archive never includes them), so
# without this, `rsync --delete` would remove every ignored file in the
# target that .sync-config does not separately exclude (local env files,
# local settings, dependency folders) -- and they are not covered by the
# stash either, since stash does not touch ignored files by default.
#
# -c core.quotePath=false and -z keep non-ASCII names literal and NUL-safe.
# A name containing a glob metacharacter ([, ], *, ? or a literal
# backslash) must be escaped, or rsync reads it as a pattern rather than a
# literal path. A whole ignored directory is listed as a single "dir/"
# entry (git's --directory), which "P /dir/" alone fails to protect
# recursively against GNU rsync when the source also has content under
# that same path -- "P /dir/***" is needed to protect everything
# underneath it too -- but that trailing "***" itself is a wildcard, and
# on BOTH GNU rsync and openrsync a wildcard rule parses backslash as an
# escape character, while a rule with no wildcard at all parses backslash
# literally (verified against both). So a literal backslash needs BOTH
# forms emitted on separate rules, one of which never matches and is
# harmless: escaped (doubled backslash) for a directory rule, which
# always carries the "***" wildcard suffix; either form for a plain file
# rule, which carries no wildcard at all.
#
# If the export (the source) also has content at an ignored path, protect
# ("P") is not enough -- rsync still updates a path that exists on both
# sides -- so exclude ("-") it from the transfer entirely instead, and
# warn: the target's own ignored copy wins over whatever the source has
# force-added at the same path.

echo "==> Protecting the target's own gitignored files from deletion..."
IGNORED_FILTER=$(mktemp /tmp/maintainer-sync-ignored-XXXXXX)
git -C "$WORK_TREE" -c core.quotePath=false ls-files -z -o -i --exclude-standard --directory |
  while IFS= read -r -d '' p; do
    escaped="$p"
    case "$escaped" in *[][*?\\]*) escaped=$(printf '%s' "$escaped" | sed 's/[][*?\\]/\\&/g') ;; esac

    case "$p" in
      */)
        # A whole ignored directory (git collapses it to one entry when
        # nothing inside is tracked): always protect-only, never exclude
        # from transfer. Checking export overlap at the directory level
        # would be too coarse -- the source may legitimately add new
        # files under a path the target otherwise ignores, without that
        # meaning the whole directory should be excluded.
        printf 'P /%s***\n' "$escaped"
        case "$p" in *\\*) printf 'P /%s***\n' "$p" ;; esac
        ;;
      *)
        if [ -e "$EXPORT_DIR/$p" ]; then
          printf -- '- /%s\n' "$escaped"
          case "$p" in *\\*) printf -- '- /%s\n' "$p" ;; esac
          echo "Warning: '$p' is ignored in the target and the source also has it (likely" >&2
          echo "  force-added there); skipped, ignored in target." >&2
        else
          printf 'P /%s\n' "$escaped"
          case "$p" in *\\*) printf 'P /%s\n' "$p" ;; esac
        fi
        ;;
    esac
  done > "$IGNORED_FILTER"

# --- rsync export → target ---

echo "==> Syncing files from source to target..."

# --checksum: compare by content. rsync's default size+mtime check skips an
# edit that keeps the file size (e.g. "1.4" -> "1.5"), so the target drifts.
RSYNC_ARGS=(
  -av
  --checksum
  --delete
  --exclude=".git/"
  --exclude=".git"
  --filter="merge $IGNORED_FILTER"
  "${RSYNC_EXCLUDES[@]+"${RSYNC_EXCLUDES[@]}"}"
)

rsync "${RSYNC_ARGS[@]}" "$EXPORT_DIR/" "$WORK_TREE/"

# Optional heads-up: a file force-added in the source at a path that
# wasn't already present (and ignored) in the target before this sync --
# so the exclude above never saw it -- lands on disk fresh from rsync
# (rsync does not know the target's gitignore) but `git add -A` below
# will not stage it, since it is still ignored in the target -- it is
# silently left out of the commit. -c core.quotePath=false and -z/read -d
# '' keep non-ASCII names intact, same as the filter above.
while IFS= read -r -d '' ignored_path; do
  [ -z "$ignored_path" ] && continue
  if [ -e "$EXPORT_DIR/${ignored_path%/}" ]; then
    echo "Warning: '$ignored_path' is force-added in the source but ignored in the target;"
    echo "  it was copied to disk but will NOT be included in the sync commit."
  fi
done < <(git -C "$WORK_TREE" -c core.quotePath=false ls-files -z -o -i --exclude-standard --directory)

# --- Commit (in the target, or in the dry-run preview worktree) ---

echo "==> Creating commit..."
cd "$WORK_TREE"

if [ "$DRY_RUN" != "true" ]; then
  # Create or switch to the target branch, built from the up-to-date default branch
  git checkout -B "$TARGET_BRANCH" "refs/remotes/origin/$TARGET_DEFAULT_BRANCH"
fi

# Stage all changes
git add -A

# Check if there are changes to commit
if git diff --cached --quiet; then
  echo "No changes to sync. Target is already up to date."
  exit 0
fi

# 12 hex characters: unique in practice, and short enough that secret
# scanners (detect-secrets flags 16+ hex characters in a commit message as a
# possible key) don't reject the sync commit in a target with hooks installed.
SOURCE_COMMIT_SHORT=$(git -C "$SOURCE_REPO_PATH" rev-parse --short=12 "$SOURCE_COMMIT")
FULL_COMMIT_MESSAGE=$(git interpret-trailers --trailer "Source-Commit: ${SOURCE_COMMIT_SHORT}" <<< "$COMMIT_MESSAGE")

# First commit attempt — hooks may auto-fix files (openapi regen, formatting,
# etc.). Its output is always shown, even on the first attempt: a hook can
# also fail for a real, unrelated reason, and silently discarding that
# output would hide it behind a confusing second failure.
if ! COMMIT_OUTPUT=$(git commit -m "$FULL_COMMIT_MESSAGE" 2>&1); then
  echo "$COMMIT_OUTPUT"
  echo "==> First commit attempt failed; retrying once after re-staging (hooks may have modified files)..."
  git add -A
  if ! COMMIT_OUTPUT=$(git commit -m "$FULL_COMMIT_MESSAGE" 2>&1); then
    echo "$COMMIT_OUTPUT"
    echo ""
    echo "Error: Pre-commit hooks failed. Review the errors above and fix in the source repo."
    exit 1
  fi
  echo "$COMMIT_OUTPUT"
else
  echo "$COMMIT_OUTPUT"
fi

echo ""
echo "==> Diff against '$TARGET_DEFAULT_BRANCH':"
git diff --stat "refs/remotes/origin/${TARGET_DEFAULT_BRANCH}...HEAD"

# --- Protect-path guard: the one gap the excludes above can't cover is a
#     hook editing a protected file during the commit itself ---

CHANGED_PATHS=$(git diff --name-only "refs/remotes/origin/${TARGET_DEFAULT_BRANCH}...HEAD")
PROTECTED_HIT=""
while IFS= read -r changed_file; do
  [ -z "$changed_file" ] && continue
  for p in "${PROTECTED_FILES[@]+"${PROTECTED_FILES[@]}"}"; do
    # A leading slash anchors the entry at the root: "/conf" only matches
    # "conf" or "conf/*", never "docs/conf" (unlike the any-depth forms
    # below). Without a leading slash, "conf/", "conf" and "secrets.env"
    # all still match at any depth, as before. The pattern is deliberately
    # unquoted so a glob in a protect entry still expands.
    case "$p" in
      /*)
        norm_p="${p#/}"
        norm_p="${norm_p%/}"
        # shellcheck disable=SC2254 # unquoted on purpose: a glob in a protect entry must still expand
        case "$changed_file" in
          $norm_p|$norm_p/*)
            PROTECTED_HIT="$changed_file (matches protected path '$p')"
            ;;
        esac
        ;;
      *)
        norm_p="${p%/}"
        # shellcheck disable=SC2254 # unquoted on purpose: a glob in a protect entry must still expand
        case "$changed_file" in
          $norm_p|$norm_p/*|*/$norm_p|*/$norm_p/*)
            PROTECTED_HIT="$changed_file (matches protected path '$p')"
            ;;
        esac
        ;;
    esac
    [ -n "$PROTECTED_HIT" ] && break 2
  done
done <<< "$CHANGED_PATHS"

if [ -n "$PROTECTED_HIT" ]; then
  echo ""
  if [ "$DRY_RUN" = "true" ]; then
    echo "==> Would refuse to push: this sync would change a protected path: $PROTECTED_HIT"
  else
    echo "Error: This sync would change a protected path: $PROTECTED_HIT"
    echo "  Protected paths never change through a sync. Not pushing."
    echo "  The commit is on local branch '$TARGET_BRANCH' in $TARGET_REPO_PATH for inspection."
    exit 1
  fi
fi

if [ "$DRY_RUN" = "true" ]; then
  echo ""
  echo "==> Dry run complete. No changes applied; nothing pushed."
  exit 0
fi

echo "==> Pushing to target remote..."
LEASE="refs/heads/$TARGET_BRANCH:$REMOTE_SHA"
if ! PUSH_OUTPUT=$(git push origin "refs/heads/$TARGET_BRANCH:refs/heads/$TARGET_BRANCH" --force-with-lease="$LEASE" 2>&1); then
  echo ""
  echo "Error: Push failed."
  echo "$PUSH_OUTPUT"
  exit 1
fi
echo "$PUSH_OUTPUT"

echo ""
echo "Done! Source ref '$REF' ($SOURCE_COMMIT) synced to target branch '$TARGET_BRANCH'."
echo "CI pipeline should trigger automatically."
