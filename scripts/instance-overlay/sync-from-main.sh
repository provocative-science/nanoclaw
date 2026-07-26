#!/usr/bin/env bash
# Merge the tools branch into the current instance/* deploy branch.
# NEVER rebase. NEVER merge instance/* into main.
#
# Usage (from repo root, on instance/<name>):
#   ./scripts/instance-overlay/sync-from-main.sh
#   ./scripts/instance-overlay/sync-from-main.sh origin/main
#   NANOCLAW_TOOLS_BRANCH=chore/instance-overlay-split ./scripts/instance-overlay/sync-from-main.sh
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

CURRENT="$(git branch --show-current)"
if [[ ! "$CURRENT" =~ ^instance/ ]]; then
  echo "error: must be on instance/<name> (currently: ${CURRENT:-detached})" >&2
  echo "  git switch instance/provocative" >&2
  exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
  echo "error: working tree dirty — commit or stash before syncing tools" >&2
  git status -sb >&2
  exit 1
fi

TOOLS="${1:-${NANOCLAW_TOOLS_BRANCH:-main}}"
# Prefer remote-tracking ref when a bare branch name is given
if git rev-parse --verify "refs/remotes/origin/${TOOLS#origin/}" >/dev/null 2>&1 && [[ "$TOOLS" != origin/* ]]; then
  if git rev-parse --verify "refs/remotes/origin/$TOOLS" >/dev/null 2>&1; then
    TOOLS="origin/$TOOLS"
  fi
fi

if ! git rev-parse --verify "$TOOLS" >/dev/null 2>&1; then
  echo "error: tools ref not found: $TOOLS (fetch origin?)" >&2
  exit 1
fi

echo "Merging $TOOLS ($(git rev-parse --short "$TOOLS")) into $CURRENT ..."
echo "Strategy: merge only (no rebase)."
echo

set +e
git merge --no-ff --no-edit "$TOOLS"
MERGE_RC=$?
set -e

if [[ "$MERGE_RC" -ne 0 ]]; then
  echo >&2
  echo "error: merge conflicts. Resolve, then git commit." >&2
  echo >&2
  echo "For instance-owned paths, keep the instance (ours) version:" >&2
  if [[ -f scripts/instance-overlay/overlay.manifest ]]; then
    while IFS= read -r path || [[ -n "$path" ]]; do
      [[ -z "$path" || "$path" =~ ^# ]] && continue
      echo "  git checkout --ours -- $path && git add -- $path" >&2
    done < scripts/instance-overlay/overlay.manifest
  fi
  echo >&2
  echo "For tools paths, keep theirs (incoming tools):" >&2
  echo "  git checkout --theirs -- <path> && git add -- <path>" >&2
  echo >&2
  echo "Abort: git merge --abort" >&2
  exit "$MERGE_RC"
fi

echo
echo "OK: $CURRENT now includes $TOOLS"
echo "Next: ./scripts/instance-overlay/verify-workflow.sh"
echo "Then: git push origin $CURRENT"
