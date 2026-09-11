#!/usr/bin/env bash
# Commit instance-owned path changes on instance/<name> only.
# Usage: ./scripts/instance-overlay/commit-overlay.sh "Describe plant/config change"
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

CURRENT="$(git branch --show-current)"
if [[ ! "$CURRENT" =~ ^instance/ ]]; then
  echo "error: refuse to commit overlay on non-instance branch (${CURRENT:-detached})" >&2
  echo "  git switch instance/provocative" >&2
  exit 1
fi

MSG="${1:-}"
if [[ -z "$MSG" ]]; then
  echo "usage: $0 \"commit message\"" >&2
  exit 1
fi

MANIFEST_FILE="scripts/instance-overlay/overlay.manifest"
[[ -f overlay.manifest ]] && MANIFEST_FILE=overlay.manifest
if [[ ! -f "$MANIFEST_FILE" ]]; then
  echo "error: missing $MANIFEST_FILE" >&2
  exit 1
fi

PATHS=()
while IFS= read -r path || [[ -n "$path" ]]; do
  [[ -z "$path" || "$path" =~ ^# ]] && continue
  PATHS+=("$path")
done < "$MANIFEST_FILE"

# Always keep root manifest in sync if present
[[ -f overlay.manifest ]] && PATHS+=("overlay.manifest")

git add -A -- "${PATHS[@]}"
if [[ -z "$(git diff --cached --name-only)" ]]; then
  echo "nothing to commit for overlay paths"
  exit 0
fi

git commit -m "$MSG"
echo "Committed on $CURRENT. Push: git push origin $CURRENT"
echo "Do NOT merge this branch into main."
