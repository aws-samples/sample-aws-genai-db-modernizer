#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# The mysqld integration tests for step 1's routine scripts (#2274): the generated MySQL
# scripts run on a real MySQL 8.0 server, as the customer runs them. ci/test.sh deselects
# `-m integration`, so these tests have this entry point and their own CI job.
# Usage: ci/test-mysqld.sh [extra pytest args]
#
# The tests start a throwaway mysqld on a Unix socket in a temp folder, with no TCP
# listener (--skip-networking), and stop it and delete its data folder when they end.
# Always runs under --fail-on-skip: a missing or unstartable server fails the run
# instead of skipping silently.
#
# Env vars read:
#   CI                - set by GitHub Actions. Under CI the pinned server below is always
#                       used, so the pin is what is tested. Without it, a `mysqld` and
#                       `mysql` already on PATH are used (a local developer's install).
#   MYSQL_CACHE_DIR   - where the pinned server is unpacked (default
#                       ~/.cache/db-modernizer-mysql). The CI job caches this folder.
#   MYSQL_TARBALL     - a local copy of the pinned archive, for a runner that cannot
#                       reach the download host. It is checked against the same SHA-256.
#   MYSQL_FORCE_PINNED=1 - use the pinned server even outside CI (Linux x86_64 only).
source "$(dirname "$0")/lib.sh"

# The server: MySQL Community 8.0 generic Linux binaries (glibc 2.28, x86_64). A tarball
# with a SHA-256 is the only pin that stays exact: a distro package moves with each
# security update (and the older build leaves the archive), and the vendor's APT
# repository serves only its newest 8.0. The archive's SHA-256 matches the MD5 the vendor
# publishes for the same file; the two binaries' SHA-256 (MYSQLD_BIN_SHA256,
# MYSQL_BIN_SHA256) are checked on every run, a restored cache included.
MYSQL_VERSION="8.0.45"
MYSQL_SHA256="c09137539ab42590c8682d498716e6b97a45d83d2188339fa2e980dd54b6e0af"
MYSQLD_BIN_SHA256="b5a36c8b9446c00ce255c8de085d2c92dfe9321a2683d3b28c1b9ac991d500ca"
MYSQL_BIN_SHA256="4aa11cec65dc04ce1211dda87684be04f6c83c2ca455a8064b7d380d08e04086"
MYSQL_NAME="mysql-${MYSQL_VERSION}-linux-glibc2.28-x86_64"
MYSQL_URL="https://cdn.mysql.com/archives/mysql-8.0/${MYSQL_NAME}.tar.xz"

# Exactly the tests that need a server. Add a file here when it joins them.
MYSQLD_TEST_FILES=(
  tests/integration/test_routine_scripts_mysql.py
  tests/integration/test_triggers_scripts_mysql.py
  tests/integration/test_view_scripts_mysql.py
)

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# The scratch download folder goes away on any exit, a failed curl included.
SCRATCH=""
trap '[ -z "$SCRATCH" ] || rm -rf "$SCRATCH"' EXIT

# Are the unpacked server and client the pinned build? Checked on every run, so a cache
# the runner restores meets the same bar as a fresh download.
binaries_ok() {
  [ -x "$1/bin/mysqld" ] && [ -x "$1/bin/mysql" ] \
    && [ "$(sha256_of "$1/bin/mysqld")" = "$MYSQLD_BIN_SHA256" ] \
    && [ "$(sha256_of "$1/bin/mysql")" = "$MYSQL_BIN_SHA256" ]
}

install_pinned_server() {
  local cache="${MYSQL_CACHE_DIR:-$HOME/.cache/db-modernizer-mysql}"
  local base="$cache/$MYSQL_NAME"
  if ! binaries_ok "$base"; then
    if [ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]; then
      echo "error: the pinned MySQL $MYSQL_VERSION is a Linux x86_64 build; install mysqld and the mysql client (8.0) and put them on PATH" >&2
      exit 2
    fi
    mkdir -p "$cache"
    local archive="${MYSQL_TARBALL:-}"
    SCRATCH="$(mktemp -d "$cache/download.XXXXXX")"
    if [ -z "$archive" ]; then
      archive="$SCRATCH/$MYSQL_NAME.tar.xz"
      log "downloading MySQL $MYSQL_VERSION"
      curl -fsSL --retry 5 --retry-delay 5 --retry-all-errors -o "$archive" "$MYSQL_URL"
    fi
    if [ "$(sha256_of "$archive")" != "$MYSQL_SHA256" ]; then
      echo "error: $archive does not match the pinned SHA-256 for MySQL $MYSQL_VERSION" >&2
      exit 2
    fi
    log "unpacking the server and client"
    rm -rf "$base"
    tar -xJf "$archive" -C "$cache" --wildcards \
      "$MYSQL_NAME/bin/mysqld" "$MYSQL_NAME/bin/mysql" "$MYSQL_NAME/share/*" \
      "$MYSQL_NAME/lib/private/*" "$MYSQL_NAME/lib/plugin/*"
    if ! binaries_ok "$base"; then
      echo "error: the unpacked server or client does not match the pinned SHA-256" >&2
      exit 2
    fi
  fi
  export PATH="$base/bin:$PATH"
  # The generic build needs libaio, libnuma and libtinfo from the system. Ubuntu 24.04
  # renamed libaio's soname (libaio.so.1t64), which the binary does not know, so a
  # symlink under the cache folder (restored with the cache) stands in for it.
  local compat="$cache/libcompat"
  export LD_LIBRARY_PATH="$compat${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  local missing
  missing="$(ldd "$base/bin/mysqld" "$base/bin/mysql" 2>/dev/null | grep 'not found' || true)"
  if [ -n "$missing" ]; then
    log "installing the system libraries mysqld needs"
    sudo apt-get update -qq
    sudo apt-get install -y -qq libaio1t64 libnuma1 libtinfo6 2>/dev/null \
      || sudo apt-get install -y -qq libaio1 libnuma1 libtinfo6
    mkdir -p "$compat"
    if [ -e /usr/lib/x86_64-linux-gnu/libaio.so.1t64 ]; then
      ln -sf /usr/lib/x86_64-linux-gnu/libaio.so.1t64 "$compat/libaio.so.1"
    fi
    missing="$(ldd "$base/bin/mysqld" "$base/bin/mysql" | grep 'not found' || true)"
    if [ -n "$missing" ]; then
      echo "error: mysqld still misses libraries: $missing" >&2
      exit 2
    fi
  fi
}

if [ -n "${CI:-}" ] || [ -n "${MYSQL_FORCE_PINNED:-}" ] \
   || ! command -v mysqld >/dev/null 2>&1 || ! command -v mysql >/dev/null 2>&1; then
  install_pinned_server
fi
log "using $(command -v mysqld): $(mysqld --version)"

log "pytest -m integration (mysqld; ${#MYSQLD_TEST_FILES[@]} files)"
uv run pytest -m integration --fail-on-skip -p no:cacheprovider \
  "${PYTEST_TIME_LIMIT[@]}" "${MYSQLD_TEST_FILES[@]}" "$@"
