#!/usr/bin/env bash
# Ghost host jobs (signal dictionary, change digest, knowledge upkeep).
# Usage: run_jobs.sh hourly|daily|weekly
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
export NANOCLAW_ROOT="${NANOCLAW_ROOT:-$(cd "$HERE/../.." && pwd)}"
export PYTHONDONTWRITEBYTECODE=1
read -r -a PY <<<"${GHOST_PYTHON_CMD:-uvx --with duckdb --with boto3 python}"

status=0
run() {
  echo "[run_jobs] $*" >&2
  if ! "${PY[@]}" "$HERE/$1" "${@:2}"; then
    echo "[run_jobs] $1 failed" >&2
    status=1
  fi
}

case "${1:-}" in
  hourly)
    run change_digest.py
    run kb_recheck.py
    ;;
  daily)
    run build_dictionary.py
    run kb_garden.py
    ;;
  weekly)
    run kb_garden.py --review
    ;;
  *)
    echo "usage: $0 hourly|daily|weekly" >&2
    exit 2
    ;;
esac
exit "$status"
