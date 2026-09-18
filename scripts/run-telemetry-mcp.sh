#!/usr/bin/env bash
# Launch plant telemetry MCP (stdio) using credentials from secrets/mcp.env.
# Used by Cursor (.cursor/mcp.json) and for local smoke tests.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ROOT}/secrets/mcp.env"
SERVER="${ROOT}/container/telemetry-mcp/server.py"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy secrets/mcp.env.example and set TELEMETRY_S3_*." >&2
  exit 1
fi

set -a
# shellcheck disable=1090
source "$ENV_FILE"
set +a

: "${TELEMETRY_S3_ACCESS_KEY:?TELEMETRY_S3_ACCESS_KEY not set in secrets/mcp.env}"
: "${TELEMETRY_S3_SECRET_KEY:?TELEMETRY_S3_SECRET_KEY not set in secrets/mcp.env}"
: "${TELEMETRY_S3_REGION:=us-east-1}"
: "${TELEMETRY_S3_ENDPOINT:=https://s3.us-east-1.wasabisys.com}"
: "${TELEMETRY_S3_BUCKET:=provocative-dac-dev-beef}"

export TELEMETRY_S3_ACCESS_KEY TELEMETRY_S3_SECRET_KEY TELEMETRY_S3_REGION TELEMETRY_S3_ENDPOINT TELEMETRY_S3_BUCKET
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy NODE_USE_ENV_PROXY || true

if command -v uvx >/dev/null 2>&1; then
  exec uvx --with 'mcp>=1.12.0,<2' --with duckdb --with boto3 python "$SERVER"
fi

if python3 -c "import mcp, duckdb, boto3" >/dev/null 2>&1; then
  exec python3 "$SERVER"
fi

echo "Need uvx (or python3 with mcp, duckdb, boto3) to run the telemetry MCP." >&2
exit 1
