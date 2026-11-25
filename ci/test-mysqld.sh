#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# The mysqld integration tests for step 1's routine scripts (#2274): the generated MySQL
# scripts run on a real MySQL server (8.0 and 8.4 LTS), as the customer runs them. ci/test.sh deselects
# `-m integration`, so these tests have this entry point and their own CI job.
# Usage: ci/test-mysqld.sh [extra pytest args]
#
# The tests start a throwaway mysqld on a Unix socket in a temp folder, with no TCP
# listener (--skip-networking), and stop it and delete its data folder when they end.
# The collector's LOB-size tests (#2620) also start a throwaway MariaDB the same way.
# Always runs under --fail-on-skip: a missing or unstartable server fails the run
# instead of skipping silently.
#
# Env vars read:
#   MYSQL_SERIES      - which pinned server: 8.0 (the default) or 8.4 (LTS). The CI job runs
#                       both as a matrix (#2348).
#   CI                - set by GitHub Actions. Under CI the pinned server below is always
#                       used, so the pin is what is tested. Without it, a `mysqld` and
#                       `mysql` already on PATH are used (a local developer's install),
#                       and the same for `mariadbd` and `mariadb` (put them on PATH after
#                       MySQL's, as a MariaDB install also ships a `mysql` and `mysqld`).
#   MYSQL_CACHE_DIR   - where the pinned server is unpacked (default
#                       ~/.cache/db-modernizer-mysql). The CI job caches this folder.
#   MYSQL_TARBALL     - a local copy of the pinned archive, for a runner that cannot
#                       reach the download host. It is checked against the same SHA-256.
#   MARIADB_TARBALL   - the same for the pinned MariaDB archive.
#   MYSQL_FORCE_PINNED=1 - use the pinned servers even outside CI (Linux x86_64 only).
source "$(dirname "$0")/lib.sh"

# The server: MySQL Community generic Linux binaries (glibc 2.28, x86_64), one pin per
# series. A tarball with a SHA-256 is the only pin that stays exact: a distro package
# moves with each security update (and the older build leaves the archive), and the
# vendor's APT repository serves only its newest release. The archive's SHA-256 matches
# the MD5 the vendor publishes for the same file; the two binaries' SHA-256
# (MYSQLD_BIN_SHA256, MYSQL_BIN_SHA256) are checked on every run, a restored cache
# included. 8.0 is Aurora MySQL 3's line; 8.4 is the LTS line.
MYSQL_SERIES="${MYSQL_SERIES:-8.0}"
case "$MYSQL_SERIES" in
  8.0)
    MYSQL_VERSION="8.0.45"
    MYSQL_SHA256="c09137539ab42590c8682d498716e6b97a45d83d2188339fa2e980dd54b6e0af"
    MYSQLD_BIN_SHA256="b5a36c8b9446c00ce255c8de085d2c92dfe9321a2683d3b28c1b9ac991d500ca"
    MYSQL_BIN_SHA256="4aa11cec65dc04ce1211dda87684be04f6c83c2ca455a8064b7d380d08e04086"
    ;;
  8.4)
    MYSQL_VERSION="8.4.11"
    MYSQL_SHA256="94e204cc94dede3746d2773fa5818f28f555cd8368c75ca0612eac124e6f3e58"
    MYSQLD_BIN_SHA256="0ae85e8c4efb8457e9230e42a1b54b9b542a3b27a711f0ac4b190649bc857dc9"
    MYSQL_BIN_SHA256="599968be33b0b6073db238ede220a6312abd155b782d65e42ff320db8507cf87"
    ;;
  *)
    echo "error: MYSQL_SERIES must be 8.0 or 8.4, not $MYSQL_SERIES" >&2
    exit 2
    ;;
esac
MYSQL_NAME="mysql-${MYSQL_VERSION}-linux-glibc2.28-x86_64"
MYSQL_URL="https://cdn.mysql.com/archives/mysql-${MYSQL_SERIES}/${MYSQL_NAME}.tar.xz"

# The MariaDB server for the collector's MariaDB cases (#2620): the vendor's generic Linux
# binaries (systemd build, x86_64) of 10.11, the LTS line. The 10.5 build's client links
# ncurses 5, which Ubuntu 24.04 no longer ships, so the collector's 10.5 floor is not the
# pin. MARIADB_SHA256 was taken when the pin was set: it matches the vendor's
# sha256sums.txt, and the archive's GPG signature (the .asc beside it) was verified then
# against the MariaDB signing key, fingerprint 177F4010FE56CA3336300305F1656F24C74CD1D8.
# Committed here, it no longer depends on any download host. The server's and client's
# SHA-256, and one SHA-256 over every file and link unpacked (MARIADB_TREE_SHA256, see
# tree_sha256), are checked on every run, so a restored cache meets the same bar as a
# download. Every matrix leg runs it, so the pin does not depend on MYSQL_SERIES.
MARIADB_VERSION="10.11.20"
MARIADB_SHA256="3e28539f79de3ca9df72f4a4b93f9092eb4f05a9648391ddf37c44fb4ed84727"
MARIADBD_BIN_SHA256="794f323ae291a7c121958f9e7c8639128dd041eed8b61b0d8c7a71b8b8870a86"
MARIADB_BIN_SHA256="312cbdd10ad8816203254d2f4db6688fc7771153c9b315a6d39fa1d8264e4fad"
MARIADB_TREE_SHA256="0a80a9efd876f58652b842e66b500de179f5b63d2897fee11fb1ba743f5d63dd"
MARIADB_NAME="mariadb-${MARIADB_VERSION}-linux-systemd-x86_64"
# The vendor's mirror network first (it redirects to a nearby mirror and is fast), then the
# vendor's archive, which keeps every release but is slow. The checksum covers both.
MARIADB_PATH="mariadb-${MARIADB_VERSION}/bintar-linux-systemd-x86_64/${MARIADB_NAME}.tar.gz"
MARIADB_URLS=("https://mirror.mariadb.org/${MARIADB_PATH}" "https://archive.mariadb.org/${MARIADB_PATH}")

# Exactly the tests that need a server. Add a file here when it joins them.
MYSQLD_TEST_FILES=(
  tests/integration/test_routine_scripts_mysql.py
  tests/integration/test_triggers_scripts_mysql.py
  tests/integration/test_view_scripts_mysql.py
  tests/unit/tools/database/test_disposable_engine_definer.py
  tests/integration/test_collect_mysql_lob_lengths.py
)

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

sha256_stdin() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1
  else shasum -a 256 | cut -d' ' -f1; fi
}

# One SHA-256 over everything under a folder: a line per file (its content's SHA-256 and
# its path) and per link (its path and target), in byte order of the paths. A file added,
# removed or changed, or a link moved, changes it.
tree_sha256() {
  (
    cd "$1" || exit 1
    find . \( -type f -o -type l \) -print0 | LC_ALL=C sort -z | while IFS= read -r -d '' f; do
      if [ -L "$f" ]; then printf 'link %s %s\n' "$f" "$(readlink "$f")"
      else printf '%s %s\n' "$(sha256_of "$f")" "$f"; fi
    done
  ) | sha256_stdin
}

# Every download: https only, also after a redirect, and a connection that stalls (under
# 100 kB/s for a minute) is dropped, so a retry or the next URL runs before the job times out.
CURL_OPTS=(-fsSL --proto '=https' --proto-redir '=https' --connect-timeout 20
  --speed-limit 100000 --speed-time 60 --retry 3 --retry-delay 5 --retry-all-errors)

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

# The same for MariaDB, and the whole unpacked folder too: the fixture runs its
# scripts/mariadb-install-db, and the server loads its plugins and share files.
mariadb_binaries_ok() {
  [ -x "$1/bin/mariadbd" ] && [ -x "$1/bin/mariadb" ] \
    && [ "$(sha256_of "$1/bin/mariadbd")" = "$MARIADBD_BIN_SHA256" ] \
    && [ "$(sha256_of "$1/bin/mariadb")" = "$MARIADB_BIN_SHA256" ] \
    && [ "$(tree_sha256 "$1")" = "$MARIADB_TREE_SHA256" ]
}

install_pinned_server() {
  local cache="${MYSQL_CACHE_DIR:-$HOME/.cache/db-modernizer-mysql}"
  local base="$cache/$MYSQL_NAME"
  if ! binaries_ok "$base"; then
    if [ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]; then
      echo "error: the pinned MySQL $MYSQL_VERSION is a Linux x86_64 build; install mysqld and the mysql client ($MYSQL_SERIES) and put them on PATH" >&2
      exit 2
    fi
    mkdir -p "$cache"
    local archive="${MYSQL_TARBALL:-}"
    SCRATCH="$(mktemp -d "$cache/download.XXXXXX")"
    if [ -z "$archive" ]; then
      archive="$SCRATCH/$MYSQL_NAME.tar.xz"
      log "downloading MySQL $MYSQL_VERSION"
      curl "${CURL_OPTS[@]}" -o "$archive" "$MYSQL_URL"
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
  system_libraries "$cache" "$base/bin/mysqld" "$base/bin/mysql"
}

# The generic builds need libaio, libnuma and libtinfo (MySQL) and libsystemd and libcrypt
# (MariaDB) from the system. Ubuntu 24.04 renamed libaio's soname (libaio.so.1t64), which
# the binaries do not know, so a link in a folder under the cache stands in for it. That
# folder is on LD_LIBRARY_PATH, so it is rebuilt on every run and a restored cache never
# adds a library to it.
link_libaio() {
  if [ -e /usr/lib/x86_64-linux-gnu/libaio.so.1t64 ]; then
    ln -sf /usr/lib/x86_64-linux-gnu/libaio.so.1t64 "$1/libaio.so.1"
  fi
}

system_libraries() {  # system_libraries CACHE BINARY...
  local compat="$1/libcompat"
  shift
  rm -rf "$compat"
  mkdir -p "$compat"
  link_libaio "$compat"
  case ":${LD_LIBRARY_PATH:-}:" in
    *":$compat:"*) ;;
    *) export LD_LIBRARY_PATH="$compat${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
  esac
  local missing
  missing="$(ldd "$@" 2>/dev/null | grep 'not found' || true)"
  if [ -n "$missing" ]; then
    log "installing the system libraries the servers need"
    sudo apt-get update -qq
    sudo apt-get install -y -qq libaio1t64 libnuma1 libtinfo6 libsystemd0 libcrypt1 2>/dev/null \
      || sudo apt-get install -y -qq libaio1 libnuma1 libtinfo6 libsystemd0 libcrypt1
    link_libaio "$compat"
    missing="$(ldd "$@" | grep 'not found' || true)"
    if [ -n "$missing" ]; then
      echo "error: the servers still miss libraries: $missing" >&2
      exit 2
    fi
  fi
}

# MariaDB unpacks beside MySQL in the cached folder. Only `mariadbd` and `mariadb` go on
# PATH, as links in a folder of their own, so nothing else of the install shadows MySQL's
# tools; the test fixture follows the links to find the install's scripts and share.
install_pinned_mariadb() {
  local cache="${MYSQL_CACHE_DIR:-$HOME/.cache/db-modernizer-mysql}"
  local base="$cache/$MARIADB_NAME"
  if ! mariadb_binaries_ok "$base"; then
    if [ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]; then
      echo "error: the pinned MariaDB $MARIADB_VERSION is a Linux x86_64 build; install mariadbd and the mariadb client (10.5 or newer) and put them on PATH after MySQL's" >&2
      exit 2
    fi
    mkdir -p "$cache"
    local archive="${MARIADB_TARBALL:-}"
    [ -n "$SCRATCH" ] || SCRATCH="$(mktemp -d "$cache/download.XXXXXX")"
    if [ -z "$archive" ]; then
      archive="$SCRATCH/$MARIADB_NAME.tar.gz"
      log "downloading MariaDB $MARIADB_VERSION"
      local url
      for url in "${MARIADB_URLS[@]}"; do
        if curl "${CURL_OPTS[@]}" -o "$archive" "$url"; then
          break
        fi
      done
    fi
    if [ "$(sha256_of "$archive")" != "$MARIADB_SHA256" ]; then
      echo "error: $archive does not match the pinned SHA-256 for MariaDB $MARIADB_VERSION" >&2
      exit 2
    fi
    log "unpacking the MariaDB server and client"
    rm -rf "$base"
    tar -xzf "$archive" -C "$cache" --wildcards \
      "$MARIADB_NAME/bin/mariadbd" "$MARIADB_NAME/bin/mariadb" \
      "$MARIADB_NAME/bin/my_print_defaults" "$MARIADB_NAME/bin/resolveip" \
      "$MARIADB_NAME/scripts/*" "$MARIADB_NAME/share/*" "$MARIADB_NAME/lib/plugin/*"
    if ! mariadb_binaries_ok "$base"; then
      echo "error: the unpacked MariaDB server or client does not match the pinned SHA-256" >&2
      exit 2
    fi
  fi
  local links="$cache/mariadb-path"
  rm -rf "$links"
  mkdir -p "$links"
  ln -sf "$base/bin/mariadbd" "$links/mariadbd"
  ln -sf "$base/bin/mariadb" "$links/mariadb"
  export PATH="$links:$PATH"
  system_libraries "$cache" "$base/bin/mariadbd" "$base/bin/mariadb"
}

if [ -n "${CI:-}" ] || [ -n "${MYSQL_FORCE_PINNED:-}" ] \
   || ! command -v mysqld >/dev/null 2>&1 || ! command -v mysql >/dev/null 2>&1; then
  install_pinned_server
fi
log "using $(command -v mysqld): $(mysqld --version)"
if [ -n "${CI:-}" ] || [ -n "${MYSQL_FORCE_PINNED:-}" ] \
   || ! command -v mariadbd >/dev/null 2>&1 || ! command -v mariadb >/dev/null 2>&1; then
  install_pinned_mariadb
fi
log "using $(command -v mariadbd): $(mariadbd --version)"

log "pytest -m integration (mysqld; ${#MYSQLD_TEST_FILES[@]} files)"
uv run pytest -m integration --fail-on-skip -p no:cacheprovider \
  "${PYTEST_TIME_LIMIT[@]}" "${MYSQLD_TEST_FILES[@]}" "$@"
