#!/usr/bin/env bash
# Legacy name — use commit-overlay.sh on instance/<name>.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
exec "$ROOT/scripts/instance-overlay/commit-overlay.sh" "$@"
