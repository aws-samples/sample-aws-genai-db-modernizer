#!/usr/bin/env bash
# Cheap pre-merge failsafe: run before pushing or asking for a merge.
#
#   ./ci/pre_merge.sh [--quick] [--strict-main-tests | --no-main-tests]
#
# fetch main, count commits behind, check for merge conflicts without touching
# the worktree, check contract versions, warn on stale generated files, list the
# new and changed tests that pass on main's code, then lint, tests (skipped by
# --quick) and the needs_llm_run verdict. Exits non-zero on a conflict, a lint or
# test failure, a contract version problem, or (with --strict-main-tests) a new or
# changed test that passes on main.
# shellcheck source=lib.sh source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

quick=0
main_tests=warn
for arg in "$@"; do
  case "$arg" in
    --quick) quick=1 ;;
    --strict-main-tests) main_tests=strict ;;
    --no-main-tests) main_tests=skip ;;
    -h | --help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "usage: ci/pre_merge.sh [--quick] [--strict-main-tests | --no-main-tests]" >&2; exit 2 ;;
  esac
done

helper() { uv run python ci/pre_merge.py "$@"; }
summary=()
failed=0
note() { printf '%s\n' "$*"; summary+=("$*"); }
fail() { failed=1; note "FAIL $*"; }

# a. Fetch upstream main.
remote="$(git remote -v | helper pick-remote)"
base="$remote/main"
if git fetch -q "$remote" main 2>/dev/null; then
  note "ok   fetch: $base is up to date with the remote"
elif git rev-parse -q --verify "$base" >/dev/null; then
  note "warn fetch: could not reach $remote, using the local $base"
else
  fail "fetch: could not fetch $remote main and there is no local $base"
  echo "pre_merge: cannot continue without $base" >&2
  exit 1
fi

# b. Behind count.
behind="$(git rev-list --count "HEAD..$base")"
ahead="$(git rev-list --count "$base..HEAD")"
if [ "$behind" -eq 0 ]; then
  note "ok   behind: 0 commits behind $base ($ahead ahead)"
else
  note "warn behind: $behind commit(s) behind $base ($ahead ahead); rebase and re-test before merging"
fi

# c. Conflict check against main. merge-tree writes objects only: no index, worktree or ref changes.
conflict_files=""
set +e
mt_out="$(git merge-tree --write-tree --name-only --no-messages HEAD "$base" 2>/dev/null)"
mt_rc=$?
set -e
if [ "$mt_rc" -eq 0 ]; then
  note "ok   conflicts: none against $base"
elif [ "$mt_rc" -eq 1 ]; then
  conflict_files="$(printf '%s\n' "$mt_out" | helper merge-tree-files)"
  fail "conflicts: $(printf '%s\n' "$conflict_files" | wc -l | tr -d ' ') conflicting file(s) against $base"
  printf '%s\n' "$conflict_files" | sed 's/^/       /'
else
  # git older than 2.38: no `merge-tree --write-tree`. Approximate with files both sides touched.
  mb="$(git merge-base HEAD "$base")"
  conflict_files="$(comm -12 \
    <(git diff --name-only "$mb" HEAD | sort) \
    <(git diff --name-only "$mb" "$base" | sort))"
  if [ -z "$conflict_files" ]; then
    note "ok   conflicts: no file changed on both sides (old git, approximate check)"
  else
    note "warn conflicts: files changed on both sides, may conflict (old git, approximate):"
    printf '%s\n' "$conflict_files" | sed 's/^/       /'
    failed=1
  fi
fi

# d. Contract versions.
changed="$(git diff --name-only "$base...HEAD")"
contracts="$(printf '%s\n' "$changed" | grep -E '^src/contracts/[^/]+\.py$' || true)"
if [ -z "$contracts" ]; then
  note "ok   contracts: no contract file changed"
else
  # shellcheck disable=SC2086 # one path per word is intended
  if contract_out="$(helper contracts "$base" $contracts)"; then
    note "ok   contracts: versions checked ($(printf '%s\n' "$contracts" | wc -l | tr -d ' ') file(s))"
    printf '%s\n' "$contract_out" | sed 's/^/       /'
  else
    fail "contracts: version problem"
    printf '%s\n' "$contract_out" | sed 's/^/       /'
  fi
fi
# Pending bumps (ADR-008 Decision 8): fine while the PR is open, refused by the merge queue.
pending="$(helper pending)"
if [ -n "$pending" ]; then
  note "warn contracts: $(printf '%s\n' "$pending" | wc -l | tr -d ' ') pending bump(s); after the last rebase, before the merge queue, run: uv run python scripts/resolve_contract_versions.py --base $base"
  printf '%s\n' "$pending" | sed 's/^/       /'
fi
# Every contract change since the merge base moves its version or has a pending bump,
# even when the branch regenerated its own snapshot (ADR-008 Decision 8).
if against_out="$(uv run python scripts/resolve_contract_versions.py --check --against "$(git merge-base HEAD "$base")" 2>&1)"; then
  note "ok   contracts: every contract change since the merge base is versioned or declared"
else
  fail "contracts: a contract changed without a version bump or a pending bump"
  printf '%s\n' "$against_out" | grep -v '^ok\|^pending' | sed 's/^/       /'
fi

# e. Generated files that differ from main must still match their generator.
check_generated() {  # check_generated NAME "path|path" command...
  local name="$1" paths="$2" p hit=0
  shift 2
  for p in ${paths//|/ }; do
    printf '%s\n' "$changed" | grep -qxF "$p" && hit=1
  done
  [ "$hit" -eq 1 ] || return 0
  if "$@" >/dev/null 2>&1; then
    note "ok   generated: $name differs from main and its generator check passes"
  else
    note "warn generated: $name differs from main but its generator check fails; regenerate it on top of main, never hand-merge ($*)"
  fi
}
check_generated "routing snapshot" "tests/e2e/baselines/routing_snapshot.json" \
  uv run pytest tests/e2e/test_routing_snapshot.py -p no:xdist -q
check_generated "contract schema snapshot" "tests/contract/schema_snapshot.json" \
  uv run pytest tests/contract/test_schema_snapshot.py -p no:xdist -q
check_generated "rendered catalog" "docs/architecture/rule-catalog.md|src/ui/src/utils/engineCatalog.generated.js" \
  uv run python scripts/render_catalog.py --check

tmp="$(mktemp)"
main_wt=""
cleanup() {
  rm -f "$tmp"
  if [ -n "$main_wt" ]; then
    git worktree remove --force "$main_wt" >/dev/null 2>&1 || true
    rm -rf "$(dirname "$main_wt")"
  fi
}
trap cleanup EXIT

# f. New and changed tests against main's code (AGENTS.md self-review item 1): a
# temporary worktree of main with this branch's committed tests/ tree runs them.
if [ "$main_tests" = skip ]; then
  note "skip main-tests: --no-main-tests"
else
  git worktree prune >/dev/null 2>&1 || true  # entries a killed earlier run left behind
  plan="$(uv run python ci/main_tests.py changed "$base")" || plan="error"
  run_ids=()
  while IFS= read -r line; do run_ids+=("$line"); done < <(printf '%s\n' "$plan" | sed -n 's/^run //p')
  marked_ids="$(printf '%s\n' "$plan" | sed -n 's/^marked //p')"
  badmark="$(printf '%s\n' "$plan" | sed -n 's/^badmark //p')"
  skipped="$(printf '%s\n' "$plan" | grep -c '^skip ' || true)"
  extra=""
  [ -n "$marked_ids" ] && extra="$extra; $(printf '%s\n' "$marked_ids" | wc -l | tr -d ' ') marked passes_on_main (listed below)"
  [ "$skipped" -gt 0 ] && extra="$extra; $skipped e2e/integration file(s) not run"
  details=""
  [ -n "$marked_ids" ] && details="$(printf '%s\n' "$marked_ids" | sed 's/^/marked:   /')"
  if [ -n "$badmark" ]; then
    note "warn main-tests: a module-level pytestmark passes_on_main is not honoured; mark each test (its tests run as usual):"
    printf '%s\n' "$badmark" | sed 's/^/       /'
  fi
  if [ "$plan" = error ]; then
    note "warn main-tests: could not list the changed tests against $base"
  elif [ "${#run_ids[@]}" -eq 0 ]; then
    note "ok   main-tests: no new or changed committed test to run on $base$extra"
  else
    py="$(uv run python -c 'import sys; print(sys.executable)')"
    main_wt="$(mktemp -d)/main"
    head_sha="$(git rev-parse HEAD)"
    if ! git worktree add -q --detach "$main_wt" "$base" >/dev/null 2>&1 \
      || ! git -C "$main_wt" checkout -q "$head_sha" -- tests; then
      note "warn main-tests: could not make a worktree of $base"
    elif ! where="$(cd "$main_wt" && PYTHONPATH="$main_wt" "$py" -c 'import os, src; print(os.path.realpath(src.__path__[0]))' 2>/dev/null)" \
      || [ "$where" != "$(cd "$main_wt" && pwd -P)/src" ]; then
      note "warn main-tests: main's src/ did not shadow this branch's (got ${where:-nothing}); not run"
    else
      # One pytest per file, up to 4 at a time: with node ids, one file that fails to
      # import on main stops pytest from running the others.
      junits="$(dirname "$main_wt")/junit"
      mkdir -p "$junits"
      n=0
      while IFS= read -r file; do
        n=$((n + 1))
        ids=()
        for id in "${run_ids[@]}"; do if [ "${id%%::*}" = "$file" ]; then ids+=("$id"); fi; done
        (cd "$main_wt" && PYTHONPATH="$main_wt" "$py" -m pytest -q -o addopts= -p no:cacheprovider \
          -p no:xdist --junitxml="$junits/$n.xml" "${ids[@]}" >"$junits/$n.log" 2>&1) &
        if [ $((n % 4)) -eq 0 ]; then wait; fi
      done < <(printf '%s\n' "${run_ids[@]}" | sed 's/::.*//' | sort -u)
      wait
      if ! ls "$junits"/*.xml >/dev/null 2>&1; then
        cat "$junits"/*.log 2>/dev/null | tail -n 20
        note "warn main-tests: pytest wrote no report on $base's code (last lines above)"
      else
        report="$(uv run python ci/main_tests.py report "$junits" "${run_ids[@]}")"
        count() { printf '%s\n' "$report" | grep -c "^$1 " || true; }
        n_pass="$(count pass)"; n_noimport="$(count noimport)"; n_skipped="$(count skipped)"; n_fail="$(count fail)"
        summary_line="${#run_ids[@]} new or changed test(s) on $base's code: $n_fail fail, $n_noimport fail only because their file does not import on main, $n_skipped skipped there, $n_pass pass$extra"
        details="$(printf '%s\n%s\n' "$(printf '%s\n' "$report" | sed -n 's/^pass /passes:   /p; s/^noimport /noimport: /p; s/^skipped /skipped:  /p')" "$details")"
        if [ "$n_pass" -eq 0 ]; then
          note "ok   main-tests: $summary_line"
        else
          msg="main-tests: $summary_line. A test that passes on main does not show this change works; make it fail on main, or mark a guard of existing behaviour @pytest.mark.passes_on_main"
          if [ "$main_tests" = strict ]; then fail "$msg"; else note "warn $msg"; fi
        fi
      fi
    fi
  fi
  if [ -n "$(printf '%s' "$details" | tr -d '[:space:]')" ]; then
    printf '%s\n' "$details" | sed '/^$/d; s/^/       /'
  fi
fi

# g. Lint.
if ./ci/lint.sh >"$tmp" 2>&1; then
  note "ok   lint: ci/lint.sh passed"
else
  tail -n 20 "$tmp"
  fail "lint: ci/lint.sh failed (last lines above)"
fi

# h. Tests, and the version-pin check (a test that compares with a literal
# contract version fails once the resolver moves it; about a minute).
if [ "$quick" -eq 1 ]; then
  note "skip version-pins: --quick"
elif uv run python scripts/check_version_pins.py >"$tmp" 2>&1; then
  note "ok   version-pins: $(tail -n 1 "$tmp")"
else
  sed -n '/^error/,$p' "$tmp" | head -n 20
  fail "version-pins: a test pins a contract version (list above)"
fi
if [ "$quick" -eq 1 ]; then
  note "skip tests: --quick"
elif ./ci/test.sh -q >"$tmp" 2>&1; then
  note "ok   tests: $(tail -n 1 "$tmp")"
else
  tail -n 20 "$tmp"
  fail "tests: ci/test.sh -q failed (last lines above)"
fi

# i. Real-model runs the branch needs (informational).
if verdict="$(./ci/needs_llm_run.sh "$base" 2>&1)"; then
  note "info llm: $(printf '%s\n' "$verdict" | grep -E '^[a-z0-9-]+: ' | tr '\n' ';' | sed 's/;$//; s/;/; /g')"
else
  note "warn llm: needs_llm_run could not decide ($verdict)"
fi

log "pre_merge summary"
for line in "${summary[@]}"; do echo "$line"; done
if [ "$failed" -eq 0 ]; then
  echo "pre_merge: OK"
else
  echo "pre_merge: FAILED"
fi
exit "$failed"
