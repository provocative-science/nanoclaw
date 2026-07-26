#!/usr/bin/env bash
# Enforce tools vs instance deploy-branch workflow.
# Exit 0 = OK, 1 = violations.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

CURRENT="$(git branch --show-current || true)"
INSTANCE="${NANOCLAW_OVERLAY_INSTANCE:-provocative}"
INSTANCE_BRANCH="instance/${INSTANCE}"
TOOLS="${NANOCLAW_TOOLS_BRANCH:-main}"
MANIFEST="scripts/instance-overlay/overlay.manifest"
RC=0

warn() { echo "WARN: $*"; RC=1; }
ok() { echo "OK: $*"; }
fail() { echo "FAIL: $*"; RC=1; }

echo "current branch: ${CURRENT:-detached} @ $(git rev-parse --short HEAD)"
echo

# --- orphan / wrong shape detection for instance branch tip ---
is_orphan_like() {
  local branch="$1"
  # Orphan tip has no merge-base with tools main (or only one parentless root with few files)
  if ! git merge-base "$branch" "$TOOLS" >/dev/null 2>&1 \
    && ! git merge-base "$branch" "origin/$TOOLS" >/dev/null 2>&1 2>/dev/null; then
    # try origin/main
    if ! git merge-base "$branch" origin/main >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

if [[ "$CURRENT" =~ ^instance/ ]]; then
  echo "== instance deploy branch checks =="

  if [[ ! -f overlay.manifest && ! -f "$MANIFEST" ]]; then
    fail "overlay.manifest missing (expected root overlay.manifest or $MANIFEST)"
  else
    ok "manifest present"
  fi

  MANIFEST_FILE="$MANIFEST"
  [[ -f overlay.manifest ]] && MANIFEST_FILE=overlay.manifest

  while IFS= read -r path || [[ -n "$path" ]]; do
    [[ -z "$path" || "$path" =~ ^# ]] && continue
    if [[ ! -e "$path" ]]; then
      fail "instance-owned path missing: $path"
    else
      ok "has $path"
    fi
  done < "$MANIFEST_FILE"

  # Must share history with tools (not orphan). Prefer local tools branch tip.
  TOOLS_REF="$TOOLS"
  if [[ "$TOOLS" == origin/* ]]; then
    :
  elif git rev-parse --verify "refs/heads/$TOOLS" >/dev/null 2>&1; then
    TOOLS_REF="$TOOLS"
  elif git rev-parse --verify "refs/remotes/origin/$TOOLS" >/dev/null 2>&1; then
    TOOLS_REF="origin/$TOOLS"
  fi
  if git merge-base HEAD "$TOOLS_REF" >/dev/null 2>&1; then
    ok "shares history with $TOOLS_REF"
    BEHIND="$(git rev-list --count HEAD.."$TOOLS_REF" 2>/dev/null || echo 0)"
    AHEAD="$(git rev-list --count "$TOOLS_REF"..HEAD 2>/dev/null || echo 0)"
    echo "    vs $TOOLS_REF: ahead $AHEAD, behind $BEHIND"
    if [[ "$BEHIND" -gt 0 ]]; then
      warn "behind tools by $BEHIND commit(s) — run ./scripts/instance-overlay/sync-from-main.sh"
    else
      ok "not behind tools"
    fi
  else
    fail "no merge-base with $TOOLS_REF — this looks like an orphan overlay; recreate from tools + overlay commit"
  fi

  if [[ -n "$(git status --porcelain)" ]]; then
    warn "working tree dirty"
  else
    ok "working tree clean"
  fi

else
  echo "== tools branch checks (must stay plant-free) =="

  if [[ -z "$CURRENT" ]]; then
    fail "detached HEAD — checkout main or a tools feature branch"
  fi

  if git ls-tree -r --name-only HEAD | grep -q '^scripts/alert-monitor/'; then
    fail "scripts/alert-monitor/ is tracked on tools branch — belongs on $INSTANCE_BRANCH only"
  else
    ok "alert-monitor not on tools HEAD"
  fi

  if git grep -q -I -i 'Ghost in the CO2 Machine' HEAD -- groups/global/CLAUDE.md 2>/dev/null; then
    fail "groups/global/CLAUDE.md on tools still has plant Ghost content — use $INSTANCE_BRANCH"
  else
    ok "groups/global/CLAUDE.md has no plant Ghost marker"
  fi

  STACK_HOST="provocativerob"
  STACK_HOST+=".grafana.net"
  if git grep -n -I -i "${STACK_HOST//./\\.}" HEAD \
    -- ':!docs/INSTANCE-OVERLAY.md' ':!.env.dev.example' ':!scripts/instance-overlay/*' \
    2>/dev/null | head -5; then
    fail "${STACK_HOST} found on tools HEAD"
  else
    ok "no ${STACK_HOST} on tools HEAD"
  fi

  # Working tree must not silently carry uncommitted plant overlay on tools
  if [[ -d scripts/alert-monitor ]]; then
    warn "scripts/alert-monitor/ present in working tree on tools branch — switch to $INSTANCE_BRANCH or remove"
  fi
  if [[ -f groups/global/CLAUDE.md ]] && grep -q 'Ghost in the CO2 Machine' groups/global/CLAUDE.md 2>/dev/null; then
    warn "working tree groups/global/CLAUDE.md looks like plant overlay — switch to $INSTANCE_BRANCH"
  fi

  if git rev-parse --verify "$INSTANCE_BRANCH" >/dev/null 2>&1; then
    if is_orphan_like "$INSTANCE_BRANCH"; then
      # refine: merge-base with HEAD (tools)
      if git merge-base "$INSTANCE_BRANCH" HEAD >/dev/null 2>&1; then
        ok "$INSTANCE_BRANCH shares history with current tools tip"
      else
        fail "$INSTANCE_BRANCH has no merge-base with HEAD (orphan?) — recreate deploy branch"
      fi
    else
      ok "$INSTANCE_BRANCH exists"
    fi
  else
    warn "$INSTANCE_BRANCH missing locally — fetch or create from tools + overlay commit"
  fi
fi

echo
if [[ "$RC" -eq 0 ]]; then
  echo "verify-workflow: PASS"
else
  echo "verify-workflow: FAIL (see above)"
fi
exit "$RC"
