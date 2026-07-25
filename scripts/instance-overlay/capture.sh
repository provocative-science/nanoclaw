#!/usr/bin/env bash
# Commit current overlay paths onto orphan branch instance/<name> without merging into main.
# Run from repo root after editing overlay files (or after apply.sh).
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

INSTANCE="${NANOCLAW_OVERLAY_INSTANCE:-provocative}"
BRANCH="instance/${INSTANCE}"
MSG="${1:-Update ${INSTANCE} instance overlay}"

MANIFEST_FILE=""
if [[ -f overlay.manifest ]]; then
  MANIFEST_FILE=overlay.manifest
elif git rev-parse --verify "$BRANCH" >/dev/null 2>&1 && git cat-file -e "${BRANCH}:overlay.manifest" 2>/dev/null; then
  git show "${BRANCH}:overlay.manifest" > /tmp/overlay.manifest.$$
  MANIFEST_FILE=/tmp/overlay.manifest.$$
else
  echo "error: overlay.manifest not found in working tree or on $BRANCH" >&2
  exit 1
fi

mapfile -t PATHS < <(grep -vE '^\s*(#|$)' "$MANIFEST_FILE")
PATHS+=("overlay.manifest")

# Ensure overlay.manifest is present in the tree we commit
if [[ "$MANIFEST_FILE" != "overlay.manifest" ]]; then
  cp "$MANIFEST_FILE" overlay.manifest
fi
[[ -f /tmp/overlay.manifest.$$ ]] && rm -f /tmp/overlay.manifest.$$

MISSING=0
for p in "${PATHS[@]}"; do
  if [[ ! -e "$p" ]]; then
    echo "error: missing path: $p" >&2
    MISSING=1
  fi
done
if [[ "$MISSING" -ne 0 ]]; then
  exit 1
fi

CURRENT_BRANCH="$(git branch --show-current)"
STASH_REF=""
if [[ -n "$(git status --porcelain)" ]]; then
  # Keep WIP; we only stage overlay paths onto the orphan branch via a temp index
  :
fi

# Build a tree from overlay paths only and commit onto BRANCH
TMP_INDEX="$(mktemp)"
export GIT_INDEX_FILE="$TMP_INDEX"
rm -f "$TMP_INDEX"
git read-tree --empty

for p in "${PATHS[@]}"; do
  if [[ -d "$p" ]]; then
    git add -f "$p"
  else
    git add -f "$p"
  fi
done

TREE="$(git write-tree)"
PARENT_ARGS=()
if git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  PARENT_ARGS=(-p "$BRANCH")
fi
COMMIT="$(git commit-tree "$TREE" "${PARENT_ARGS[@]}" -m "$MSG")"
git update-ref "refs/heads/$BRANCH" "$COMMIT"
unset GIT_INDEX_FILE
rm -f "$TMP_INDEX"

echo "Updated $BRANCH → $(git rev-parse --short "$BRANCH")"
echo "Current branch remains: ${CURRENT_BRANCH:-detached}"
echo "Tip: keep $BRANCH private; never merge it into main."
