#!/usr/bin/env bash
# Status for tools vs instance deploy-branch model.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

INSTANCE="${NANOCLAW_OVERLAY_INSTANCE:-provocative}"
BRANCH="instance/${INSTANCE}"
TOOLS="${NANOCLAW_TOOLS_BRANCH:-main}"

echo "=== workflow status ==="
./scripts/instance-overlay/verify-workflow.sh || true
echo

if git rev-parse --verify "$BRANCH" >/dev/null 2>&1; then
  echo "=== $BRANCH vs tools ==="
  TOOLS_REF="$TOOLS"
  git rev-parse --verify "origin/$TOOLS" >/dev/null 2>&1 && TOOLS_REF="origin/$TOOLS"
  if git merge-base "$BRANCH" "$TOOLS_REF" >/dev/null 2>&1; then
    echo "merge-base: $(git merge-base "$BRANCH" "$TOOLS_REF" | cut -c1-8)"
    echo "behind tools: $(git rev-list --count "$BRANCH".."$TOOLS_REF")"
    echo "ahead of tools: $(git rev-list --count "$TOOLS_REF".."$BRANCH")"
    echo
    echo "instance-only commits (not on tools):"
    git log --oneline "$TOOLS_REF".."$BRANCH" | head -20
  else
    echo "WARN: $BRANCH has no merge-base with $TOOLS_REF (orphan leftover?)"
  fi
else
  echo "branch $BRANCH not found locally"
fi
