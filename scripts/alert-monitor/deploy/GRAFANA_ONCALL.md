# Grafana IRM / OnCall setup for plant alerts

Stack: `https://provocativerob.grafana.net`  
Datasource: `grafanacloud-provocativerob-prom` (uid `grafanacloud-prom`)

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
- `grafana-plant-alert-rules.json` — 5 rules in folder **Carbon Capture** (`f889bs`), labels `team=plant`
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
| Grafana IRM | Page humans |
| Host monitor → Ghost IPC | Rich narrative + Grafana MCP analysis |

**Ghost mute:** Tag Ghost with `mute alerts` / `unmute alerts` in Telegram.
NanoClaw writes `data/alert-monitor/mute.json`; the host monitor skips IPC
while muted. Grafana OnCall is **not** muted by this command.

Do not make the Python monitor the primary OnCall webhook unless Explore shows
these gauges missing on Mimir.
