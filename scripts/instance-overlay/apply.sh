#!/usr/bin/env bash
# Legacy entrypoint — overlay is no longer path-checkout onto a tools branch.
# Deploy / develop plant config on the instance/* branch instead.
set -euo pipefail

INSTANCE="${NANOCLAW_OVERLAY_INSTANCE:-provocative}"
BRANCH="instance/${INSTANCE}"

cat <<EOF >&2
The orphan-branch apply model was replaced by a full deploy branch.

  Tools (generic):     main  (and feature branches)
  Plant deploy:        ${BRANCH}   = tools history + instance commits

To use the Provocative instance:

  git fetch origin
  git switch ${BRANCH}
  ./scripts/instance-overlay/verify-workflow.sh

To bring in latest tools (merge only, never rebase):

  git switch ${BRANCH}
  ./scripts/instance-overlay/sync-from-main.sh

See docs/INSTANCE-OVERLAY.md
EOF
exit 1
