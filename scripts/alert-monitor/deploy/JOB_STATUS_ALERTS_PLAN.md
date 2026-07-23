# Plan: Container job-status metrics + alerts (option 2)

**Goal:** Page Grafana OnCall and Ghost Telegram when the container automation job
(`container_system`) fails or unexpectedly stops — not only when
`supervisor_lock` / interlocks fire.

**Why today is silent:** Abort paths in `jobs/container_system.py` call bare
`exit()` (code **0**). Automation marks that as **Succeeded**. Lock stays `0`.
Existing plant rules only watch lock + interlocks, so neither Grafana nor Ghost
fires.

---

## Scope

| In | Out (for now) |
|----|----------------|
| Prometheus job-status gauges from **automation** (`:4000`) | Mirroring job state into control `:3000` telemetry |
| Alloy scrape of automation `/metrics` on container host | Liquefaction job metrics |
| Grafana rule(s) for failed / unexpected stop | Full job-phase alerting (purge/extract/…) |
| Ghost `alert-monitor` rising-edge on job fail/stop | Changing supervisor watchdog semantics |
| Fix abort → non-zero exit so `Failed` is truthful | Loki-only abort alerts (optional follow-on) |

---

## Architecture

```text
jobs/container_system.py
        │  exit code
        ▼
automation (:4000) ── SharedStatus (Running|Succeeded|Failed|Cancelled)
        │
        ├── GET /api/v1/jobs/{id}/status   (already exists)
        └── GET /metrics   ◄── NEW  (co3ntrol_job_*)
                │
                ▼
        Alloy on bop-host ── external_labels subsystem=container
                │
                ▼
        Grafana Cloud Mimir
                ├── Grafana alert rule(s) → IRM / OnCall
                └── (optional) Ghost poll of /metrics or status API
```

**Emit metrics from automation, not control.** Job state already lives in
`automation` `SharedStatus`. Control `:3000` has no job awareness; Alloy today
only scrapes control. Adding a second scrape on `:4000` is cleaner than
plumbing job state into the hardware telemetry loop.

---

## Workstreams

### 0. Prerequisite — make aborts actually `Failed`

**Repo:** `co3ntrol-rs` (`jobs/container_system.py`)

Replace bare `exit()` on abort / health-exit paths with `sys.exit(1)` (or raise).
Known sites: purge pump-down fail, vent fails, final vent fail, unhealthy exit.

Without this, gauges will report **Succeeded** on the exact “error and shut
down” cases we care about, and alerts will still miss them.

Ship this **with** or **immediately before** the metrics work.

Acceptance:

- Abort → automation `GET .../jobs/container_system/status` shows `"failed"`.
- Clean cycle completion still `"succeeded"`.
- Cancel still `"cancelled"`.

---

### 1. Automation `/metrics` exposition

**Repo:** `co3ntrol-rs` (`automation/`)

Add `GET /metrics` (Prometheus text) next to existing status routes, rendered
from `SharedStatus` / per-job `JobStatus`.

**Suggested metrics** (keep cardinality low — one series set per known job id):

| Metric | Type | Meaning |
|--------|------|---------|
| `co3ntrol_job_running{job_id}` | gauge 0/1 | `state == Running` |
| `co3ntrol_job_state_code{job_id}` | gauge | enum: `0=unknown/idle`, `1=running`, `2=succeeded`, `3=failed`, `4=cancelled` |
| `co3ntrol_job_started_at_us{job_id}` | gauge | last start timestamp (µs) |
| `co3ntrol_job_ended_at_us{job_id}` | gauge | last end timestamp (µs), `0` if running |
| `co3ntrol_job_last_failed{job_id}` | gauge 0/1 | sticky until next start: last terminal state was Failed |

Notes:

- Prefer **numeric** gauges (Alloy/Mimir friendly). Avoid a high-cardinality
  `state="failed"` label family unless we also keep a single enum gauge.
- `job_id` values initially: `container_system` (extend later).
- Reuse the same auth pattern as control metrics if Alloy already has a Bearer
  for `:3000` (document token reuse / separate token).
- Unit tests: render Running / Succeeded / Failed / Cancelled snapshots.

Acceptance:

- `curl -sS localhost:4000/metrics | grep co3ntrol_job_` shows expected lines
  while a job runs and after fail/succeed.

---

### 2. Alloy scrape on container host

**Host:** bop-host (container / `subsystem="container"`)

Extend live Alloy config (e.g. `/etc/alloy/config.alloy`) to scrape:

```text
127.0.0.1:4000/metrics
```

with the same `external_labels` as control scrape (`facility`, `system_id`,
`subsystem="container"`). Update `ALLOY_README.md` / deploy notes in
`co3ntrol-rs`.

Acceptance:

- Explore: `co3ntrol_job_state_code{subsystem="container",job_id="container_system"}`
  has fresh samples in Mimir.

---

### 3. Grafana plant alert rules

**Repo:** `nanoclaw` (`scripts/alert-monitor/deploy/`)

Add rule(s) to `grafana-plant-alert-rules.json`, folder Carbon Capture
(`f889bs`), labels `team=plant`, `subsystem=container`. Apply via
`apply-grafana-plant-alerts.sh`. Document in `GRAFANA_ONCALL.md`.

**Primary — job failed (critical):**

```promql
co3ntrol_job_last_failed{subsystem="container",job_id="container_system"} == 1
```

- `for:` **0–1m** (failure is already terminal; don’t wait 2m like sticky lock).
- Sticky until job is started again (metric clears on next start).

**Secondary — unexpected stop while expected to run (optional v1.1):**

Only if ops can define “should be running” (e.g. a desired-state gauge, or
“was Running in last 15m and now Succeeded/Failed without operator cancel”).
Defer if ambiguous; **Failed** alone covers the abort case once exit codes are
fixed.

Acceptance:

- Force abort (non-zero) → rule Pending/Firing → OnCall contact point fires.
- Successful cycle → no fire.
- Cancel → no Failed fire.

---

### 4. Ghost alert-monitor edge

**Repo:** `nanoclaw` (`scripts/alert-monitor/`)

Extend container polling beyond lock/interlocks.

**Preferred poll target:** automation status API (structured JSON), not scraping
Prometheus text:

```text
GET $AUTOMATION_BASE_URL/api/v1/jobs/container_system/status
```

New env (example):

```text
AUTOMATION_BASE_URL=http://10.3.0.94:4000   # bop-host automation
# AUTH if required — same Bearer family as BOP if shared
```

**Edge logic:**

- Prime on first sample.
- Fire on transition **into** `failed` (and optionally `running → succeeded`
  only if we later want “clean stop” notices — default **no**).
- Payload: `kind=job_failed`, `job_id`, `state`, `error`, timestamps, plus
  existing phase/BOP sensor snapshot if control telemetry still available.
- Cooldown key: `container:job_failed:container_system`.
- Archive to `plant-alerts/` like other fires; deliver to Carbon Capture.

If automation `:4000` is not reachable from bob49, either:

- open/firewall allow bob49 → bop-host:4000, or
- fall back to PromQL via Grafana API (heavier; avoid for v1), or
- have Alloy + a tiny sidecar — prefer fixing network to `:4000`.

Acceptance:

- Abort → `[IPC] fired kind=job_failed` → Telegram brief in Carbon Capture.
- Mute / DEBUG rules unchanged (container job fail is not liq DEBUG).

---

### 5. Docs / operator UX

- `GRAFANA_ONCALL.md` — new PromQL + severity notes.
- `groups/global/CLAUDE.md` — under plant alerts / observability: document
  `co3ntrol_job_*` and that container “shut down on error” maps to **job
  Failed**, not necessarily supervisor lock.
- Alert prompt snippet: for `job_failed`, say which abort class if `error`
  string present; still pull recent BOP/DAC metrics for context.

---

## Rollout order

1. **Fix `sys.exit(1)` on aborts** (co3ntrol-rs jobs) — deploy to bop-host.
2. **Ship automation `/metrics`** — deploy automation binary; verify locally.
3. **Alloy scrape `:4000`** — confirm series in Mimir.
4. **Grafana rule** — apply provisioning; test abort → OnCall.
5. **Ghost monitor** — `AUTOMATION_BASE_URL`, edge + tests; restart
   `alert-monitor`; test abort → Telegram.
6. **End-to-end drill** — one controlled abort during a maintenance window;
   confirm both paths; confirm successful cycle stays quiet.

---

## Test plan

| Case | Expect Grafana | Expect Ghost |
|------|----------------|--------------|
| Purge/vent abort (`exit 1`) | `job_last_failed` fires | `job_failed` IPC |
| Clean cycle complete | quiet | quiet |
| Operator cancel | quiet (not Failed) | quiet |
| Supervisor lock only (no job fail) | existing lock rule | existing lock edge |
| Automation down / scrape missing | rule `noDataState` policy TBD (`OK` vs alert) | monitor logs reach error; optional “automation unreachable” later |

Unit tests:

- automation metrics renderer for each `JobState`
- `alert-monitor` edge tests: prime → failed fires once; stuck failed no re-fire; new run clears then fail fires again

---

## Open decisions (resolve before coding)

1. **Auth on `:4000/metrics`** — public on localhost only (Alloy co-located) vs Bearer?
2. **`noDataState` for job rule** — `OK` (match plant rules today) vs page if metrics disappear while production expected?
3. **Should cancel page?** — default no.
4. **bob49 → bop-host:4000** — confirm network path for Ghost; if blocked, fix firewall before monitor work.
5. **Sticky `last_failed` clear** — on next `start` only (recommended) vs time-based auto-clear.

---

## Success criteria

- A container cycle that aborts for purge/vent/health produces:
  - Mimir: `co3ntrol_job_last_failed{subsystem="container",job_id="container_system"} == 1`
  - Grafana OnCall page within ~1m
  - Ghost brief in Carbon Capture with shared `plant-alerts/` archive entry
- A healthy completed cycle does not page.
- Lock/interlock alerts continue to work unchanged.

---

## Rough effort

| Piece | Size |
|-------|------|
| Abort exit-code fix | S |
| Automation `/metrics` + tests | M |
| Alloy + docs | S |
| Grafana rule + apply | S |
| Ghost poll + edges + tests | M |

**Depends on:** deploy access to bop-host automation + Alloy; Grafana provisioning write token (already used for plant rules).
