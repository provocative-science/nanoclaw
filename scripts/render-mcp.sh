#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$ROOT/secrets/mcp.env"
TEMPLATE="$ROOT/config/mcp.json.template"
OUT_TMP="$(mktemp)"
trap 'rm -f "$OUT_TMP"' EXIT

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — cp secrets/mcp.env.example secrets/mcp.env and edit." >&2
  exit 1
fi
if [[ ! -f "$TEMPLATE" ]]; then
  echo "Missing $TEMPLATE" >&2
  exit 1
fi
if ! command -v envsubst >/dev/null 2>&1; then
  echo "envsubst not found (install gettext-base)." >&2
  exit 1
fi

set -a
# shellcheck disable=1090
source "$ENV_FILE"
set +a

export NOTION_TOKEN GRAFANA_URL GRAFANA_SERVICE_ACCOUNT_TOKEN

envsubst < "$TEMPLATE" > "$OUT_TMP"
python3 -m json.tool "$OUT_TMP" >/dev/null

count=0
for d in "$ROOT"/groups/*/; do
  [[ -d "$d" ]] || continue
  install -m 600 "$OUT_TMP" "${d}.mcp.json"
  count=$((count + 1))
done

echo "Rendered MCP config into $count group directories (groups/*/.mcp.json)."
echo "Canonical source: groups/global/.mcp.json (agent-runner merges global + per-group overrides)."
echo "Restart nanoclaw after changing secrets."
echo "Notion MCP uses npx at agent runtime (network). Grafana uses uvx (installed in the agent image)."
