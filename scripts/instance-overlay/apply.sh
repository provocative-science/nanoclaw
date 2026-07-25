#!/usr/bin/env bash
# Apply instance overlay files from branch instance/<name> onto the working tree.
# Does NOT copy live .env tokens. Run from repo root.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

INSTANCE="${NANOCLAW_OVERLAY_INSTANCE:-provocative}"
BRANCH="instance/${INSTANCE}"

if ! git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  echo "error: branch $BRANCH not found. Fetch it or create with capture.sh" >&2
  exit 1
fi

if ! git cat-file -e "${BRANCH}:overlay.manifest" 2>/dev/null; then
  echo "error: ${BRANCH}:overlay.manifest is missing" >&2
  exit 1
fi

echo "Applying overlay from $BRANCH ..."
while IFS= read -r path || [[ -n "$path" ]]; do
  [[ -z "$path" || "$path" =~ ^# ]] && continue
  git checkout "$BRANCH" -- "$path"
  echo "  + $path"
done < <(git show "${BRANCH}:overlay.manifest")

echo "Done. Overlay applied (working tree may show modifications vs current branch)."
echo "Reminder: do not set production TELEGRAM_BOT_TOKEN in .env for this tree."
