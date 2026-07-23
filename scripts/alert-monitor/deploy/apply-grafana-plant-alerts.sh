#!/usr/bin/env bash
# Apply plant alert rules + notification policy to Grafana Cloud.
# Requires a token with alert.provisioning:write (the NanoClaw MCP SA is read-only today).
#
#   GRAFANA_TOKEN=glsa_... ./scripts/alert-monitor/deploy/apply-grafana-plant-alerts.sh
# Or source secrets/mcp.env after granting that SA write access.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
RULES="$ROOT/scripts/alert-monitor/deploy/grafana-plant-alert-rules.json"
POLICY="$ROOT/scripts/alert-monitor/deploy/grafana-notification-policy.json"

if [[ -f "$ROOT/secrets/mcp.env" && -z "${GRAFANA_URL:-}" ]]; then
  set -a
  # shellcheck disable=1091
  source "$ROOT/secrets/mcp.env"
  set +a
fi

: "${GRAFANA_URL:?Set GRAFANA_URL or source secrets/mcp.env}"
TOKEN="${GRAFANA_TOKEN:-${GRAFANA_SERVICE_ACCOUNT_TOKEN:-}}"
: "${TOKEN:?Set GRAFANA_TOKEN or GRAFANA_SERVICE_ACCOUNT_TOKEN}"

BASE="${GRAFANA_URL%/}"
AUTH=(-H "Authorization: Bearer ${TOKEN}" -H "Accept: application/json" -H "Content-Type: application/json" -H "X-Disable-Provenance: true")

echo "Applying alert rules from $RULES ..."
python3 - "$BASE" "$TOKEN" "$RULES" <<'PY'
import json, sys, urllib.request, urllib.error
base, tok, path = sys.argv[1], sys.argv[2], sys.argv[3]
rules = json.load(open(path))

def req(method, url, body=None):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {tok}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Disable-Provenance": "true",
    })
    try:
        with urllib.request.urlopen(r) as resp:
            raw = resp.read().decode()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

for rule in rules:
    uid = rule["uid"]
    code, _ = req("GET", f"{base}/api/v1/provisioning/alert-rules/{uid}")
    if code == 200:
        code, body = req("PUT", f"{base}/api/v1/provisioning/alert-rules/{uid}", rule)
        action = "updated"
    else:
        code, body = req("POST", f"{base}/api/v1/provisioning/alert-rules", rule)
        action = "created"
    if code in (200, 201):
        print(f"OK {action} {uid}")
    else:
        print(f"FAIL {action} {uid} -> HTTP {code}")
        print(body[:1000] if isinstance(body, str) else body)
        sys.exit(1)
PY

echo "Applying notification policy from $POLICY ..."
code=$(curl -sS -o /tmp/gf-policy-out.json -w '%{http_code}' "${AUTH[@]}" \
  -X PUT "$BASE/api/v1/provisioning/policies" --data-binary @"$POLICY")
echo "policy PUT -> HTTP $code"
if [[ "$code" != "200" && "$code" != "202" ]]; then
  head -c 800 /tmp/gf-policy-out.json; echo
  exit 1
fi
echo "Done."
