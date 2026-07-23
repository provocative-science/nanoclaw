# Grafana IRM / OnCall setup for plant alerts

Stack: `https://provocativerob.grafana.net`  
Datasources: `grafanacloud-prom` (Mimir/PromQL), `grafanacloud-logs` (Loki/LogQL)

This is the pager path parallel to the NanoClaw Ghost host monitor. Confirm metric
names in Explore before enabling rules — co3ntrol-rs flattens telemetry dynamically.

## Wire IRM

1. **IRM → Integrations → Monitoring Systems** → Quick-connect **Grafana Alerting**
   (creates contact point e.g. `Spark Grandpre` → Carbon Capture integration).
2. Apply plant rules + policy (needs a token with `alert.provisioning:write`;
   the NanoClaw MCP SA is metrics:read today):

```bash
# After granting write on the SA, or with an admin SA token:
GRAFANA_TOKEN=glsa_... ./scripts/alert-monitor/deploy/apply-grafana-plant-alerts.sh
# or: source secrets/mcp.env && ./scripts/alert-monitor/deploy/apply-grafana-plant-alerts.sh
```

Payloads live in:
- `grafana-plant-alert-rules.json` — 6 rules in folder **Carbon Capture** (`f889bs`), labels `team=plant`
- `grafana-notification-policy.json` — `team=plant` → **Spark Grandpre** (before critical→bobiverse)

3. **IRM → Escalation chains / Schedules** → who gets notified how.
4. Test the contact point.

## Alert rules (PromQL)

Create Grafana Alerting rules on `grafanacloud-prom`. Suggested `for:` = 1–2m
except where noted. Labels: `team=plant`, `subsystem`, `severity`, `alertname`.

### Container lock (primary)

```promql
co3ntrol_supervisor_lock{subsystem="container"} == 1
```

- `severity=critical`
- Sticky until lock clears (operator `POST /supervisor/clear_lock`).

### Interlock-only (no double-page while locked)

```promql
max_over_time(co3ntrol_interlock_triggered{subsystem="container"}[2m]) == 1
  and on() group_left()
(co3ntrol_supervisor_lock{subsystem="container"} == 0)
```

Adjust the `and` join if label sets differ; the intent is: fire interlock OnCall
**only when unlocked**. Lock already covers “automation blocked while locked.”

`co3ntrol_interlock_triggered` is tick-sparse (often 1 only on the blocked-command
sample) — use `max_over_time(...[2m])`.


### Container automation abort (Loki)

Datasource: `grafanacloud-logs` (not Prom). Fires when legacy Python
`co3ntrol/automation/main.py` on bop-host writes an abort line to
`/var/log/co3ntrol/automation-console.log` (Alloy → Loki,
`service_name="co3ntrol-automation"`).

```logql
sum(count_over_time(
  {subsystem="container", service_name="co3ntrol-automation"}
    |~ "failed!|Exiting for health reasons|Control set failed"
  [5m]
))
```

- UID: `plant-container-automation-abort`
- `severity=critical`, `for: 1m`, `noDataState: OK`
- Matches purge/vent `failed!`, `Exiting for health reasons`, or
  `Control set failed (...); aborting.` (all use `sys.exit(1)`).
- Do **not** match routine lines (`Begin filter`, `Purging`, `Setting …`,
  `response failed` retries without `failed!`).
- Explore (raw lines):

```logql
{subsystem="container", service_name="co3ntrol-automation"}
```

### Liquefaction tower warning

```promql
(co3ntrol_liquefaction_controls_tower_warning_indicator{subsystem="liquefaction"} == 1)
  and on(subsystem)
(co3ntrol_liquefaction_sensors_system_state_code{subsystem="liquefaction"} != 2)
```

- `severity=warning`
- Sticky while the tower warning light is on **and** liquefaction is not in
  DEBUG (`system_state_code != 2`). Multiple HAZOP bits do **not** re-page;
  Ghost host monitor narrates new bits.
- While `system_state_code == 2` (DEBUG), warnings are suppressed in both
  Grafana OnCall and Ghost (errors still page).

### Liquefaction tower error

```promql
co3ntrol_liquefaction_controls_tower_error_indicator{subsystem="liquefaction"} == 1
```

- `severity=critical`
- May page in addition to warning (severity escalate). Do **not** create
  per-HAZOP-bit OnCall rules. **Not** suppressed in DEBUG.

### Liquefaction system state ERROR

```promql
co3ntrol_liquefaction_sensors_system_state_code{subsystem="liquefaction"} == 3
```

- `severity=critical`
- Sticky while `system_state_code == 3` (ERROR). Complements tower error
  (pages on state even when the tower light is not the only signal).
- Ghost host monitor edges on RUN/OFF/DEBUG → ERROR transitions even if
  `active_errors` mask is 0.

## Verify metrics in Explore

Loki (automation abort stream):

```logql
{subsystem="container", service_name="co3ntrol-automation"}
```

```promql
co3ntrol_supervisor_lock{subsystem="container"}
co3ntrol_interlock_triggered{subsystem="container"}
co3ntrol_liquefaction_controls_tower_warning_indicator{subsystem="liquefaction"}
co3ntrol_liquefaction_controls_tower_error_indicator{subsystem="liquefaction"}
co3ntrol_liquefaction_sensors_system_state_code{subsystem="liquefaction"}
```

If a name is missing, check Alloy scrape on lichost / bop-host and the live
`/metrics` payload from co3ntrol-rs.

## Roles

| Path | Purpose |
|------|---------|
| Grafana IRM / OnCall | Page humans (PromQL + Loki rules) |
| Host monitor → Ghost IPC | Rich narrative + archive; **also** polls Loki for automation aborts |

### Ghost option B — automation abort (Loki API)

Ghost does **not** wait for the Grafana OnCall rule to fire. `alert-monitor`
on bob49 queries Loki for
`{subsystem="container", service_name="co3ntrol-automation"}` abort lines
(via Grafana datasource proxy or direct Loki basic auth), rising-edge +
cooldown `container:job_failed:log`, kind `job_failed`.

Requires `GRAFANA_URL` + `GRAFANA_SERVICE_ACCOUNT_TOKEN` (logs-capable SA;
falls back to `secrets/mcp.env`) or `LOKI_URL` / `LOKI_USER` / `LOKI_TOKEN`.
See `alert-monitor.env.example`.

Grafana OnCall may still page on the same LogQL rule — that is a separate
pager path. Mute Ghost with `mute alerts` does **not** mute OnCall.

**Ghost mute:** Tag Ghost with `mute alerts` / `unmute alerts` in Telegram.
NanoClaw writes `data/alert-monitor/mute.json`; the host monitor skips IPC
while muted. Grafana OnCall is **not** muted by this command.

Do not make the Python monitor the primary OnCall webhook unless Explore shows
these gauges missing on Mimir.
