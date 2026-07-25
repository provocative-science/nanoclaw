#!/usr/bin/env bash
# Compare working-tree overlay paths to instance/<name> tip.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

INSTANCE="${NANOCLAW_OVERLAY_INSTANCE:-provocative}"
BRANCH="instance/${INSTANCE}"

if ! git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  echo "branch: $BRANCH (missing)"
  exit 1
fi

echo "branch: $BRANCH @ $(git rev-parse --short "$BRANCH")"
echo "current: $(git branch --show-current) @ $(git rev-parse --short HEAD)"

if ! git cat-file -e "${BRANCH}:overlay.manifest" 2>/dev/null; then
  echo "error: overlay.manifest missing on $BRANCH" >&2
  exit 1
fi

DIRTY=0
while IFS= read -r path || [[ -n "$path" ]]; do
  [[ -z "$path" || "$path" =~ ^# ]] && continue
  if [[ ! -e "$path" ]]; then
    echo "MISSING  $path"
    DIRTY=1
    continue
  fi
  if ! git diff --quiet "$BRANCH" -- "$path" 2>/dev/null; then
    echo "DIFFERS $path"
    DIRTY=1
  else
    echo "OK      $path"
  fi
done < <(git show "${BRANCH}:overlay.manifest")

# Leakage checks on current HEAD tree (tracked files)
echo
echo "Leakage scan (tracked files on HEAD vs instance content):"
if git ls-tree -r --name-only HEAD | grep -q '^scripts/alert-monitor/'; then
  echo "WARN: scripts/alert-monitor/ still tracked on HEAD (should live on overlay only)"
  DIRTY=1
else
  echo "OK: scripts/alert-monitor/ not on HEAD"
fi

# Build pattern in parts so this script itself is not a false positive.
STACK_HOST="provocativerob"
STACK_HOST+=".grafana.net"
if git grep -n -I -i "${STACK_HOST//./\\.}" HEAD \
  -- ':!docs/INSTANCE-OVERLAY.md' ':!.env.dev.example' ':!scripts/instance-overlay/*' \
  2>/dev/null | head -5; then
  echo "WARN: ${STACK_HOST} found on HEAD (prefer overlay secrets examples)"
  DIRTY=1
else
  echo "OK: no ${STACK_HOST} on HEAD (or only in allowed docs)"
fi

exit "$DIRTY"
