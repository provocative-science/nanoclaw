#!/usr/bin/env bash
# Launch Grafana MCP (stdio) using credentials from secrets/mcp.env.
# Used by Cursor (.cursor/mcp.json) and for local smoke tests.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ROOT}/secrets/mcp.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy secrets/mcp.env.example and set GRAFANA_*." >&2
  exit 1
fi

set -a
# shellcheck disable=1090
source "$ENV_FILE"
set +a

: "${GRAFANA_URL:?GRAFANA_URL not set in secrets/mcp.env}"
: "${GRAFANA_SERVICE_ACCOUNT_TOKEN:?GRAFANA_SERVICE_ACCOUNT_TOKEN not set in secrets/mcp.env}"

export PATH="${HOME}/.local/bin:${PATH}"

if command -v uvx >/dev/null 2>&1; then
  exec uvx mcp-grafana
fi

if command -v mcp-grafana >/dev/null 2>&1; then
  exec mcp-grafana
fi

if command -v docker >/dev/null 2>&1; then
  # Official image defaults to SSE; force stdio for Cursor/Claude clients.
  exec docker run --rm -i \
    -e "GRAFANA_URL=${GRAFANA_URL}" \
    -e "GRAFANA_SERVICE_ACCOUNT_TOKEN=${GRAFANA_SERVICE_ACCOUNT_TOKEN}" \
    grafana/mcp-grafana -t stdio
fi

echo "Need uvx, mcp-grafana, or docker to run Grafana MCP." >&2
echo "Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
exit 1
