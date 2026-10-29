# NanoClaw

Personal Claude assistant. See [README.md](README.md) for philosophy and setup. See [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) for architecture decisions.

## Quick Context

Single Node.js process with skill-based channel system. Channels (WhatsApp, Telegram, Slack, Discord, Gmail) are skills that self-register at startup. Messages route to Claude Agent SDK running in containers (Linux VMs). Each group has isolated filesystem and memory.

On servers (e.g. bob49) this checkout lives under **`~/provocative/nanoclaw`** — all-lowercase `provocative`. There is no parallel `~/Provocative/nanoclaw` deployment path for NanoClaw.

## Key Files

| File | Purpose |
|------|---------|
| `src/index.ts` | Orchestrator: state, message loop, agent invocation |
| `src/channels/registry.ts` | Channel registry (self-registration at startup) |
| `src/ipc.ts` | IPC watcher and task processing |
| `src/router.ts` | Message formatting and outbound routing |
| `src/config.ts` | Trigger pattern, paths, intervals |
| `src/container-runner.ts` | Spawns agent containers with mounts |
| `src/task-scheduler.ts` | Runs scheduled tasks |
| `src/db.ts` | SQLite operations |
| `groups/{name}/CLAUDE.md` | Per-group memory (isolated) |
| `container/skills/` | Skills loaded inside agent containers (browser, status, formatting) |
| `config/mcp.json.template` | MCP stdio definitions with `${VAR}` placeholders for `envsubst` |
| `secrets/mcp.env.example` | Example MCP env vars (safe to commit) |
| `secrets/mcp.env` | Real MCP env vars — **gitignored**; never commit or paste into tickets/chats |
| `scripts/render-mcp.sh` | Sources `secrets/mcp.env`, renders template → every `groups/*/.mcp.json` |
| `src/knowledge.ts` | Knowledge store (`data/knowledge/`, own git repo): entries, `kb_write` IPC, Spark's review commands |
| `src/briefing.ts` | Per-run `<briefing>` (rules, statuses, lessons, recent alerts, change digest) prepended to prompts |
| `src/intent.ts` | Chat vs investigation tier (investigation = strongest model + skeptic verifier) |
| `container/agent-runner/src/verifier.ts` | Skeptic pass on investigation answers (different model, evidence log) |
| `container/telemetry-mcp/` | Telemetry MCP (mounted read-only into containers): S3 tools + `lookup_signal`, `why_locked`, `recent_changes`, `plant_snapshot`, `find_transitions` |
| `scripts/ghost/` | Host jobs: signal dictionary, change digest, knowledge recheck/gardener (`run_jobs.sh`, systemd units in `deploy/`), replay evals in `evals/` |

## Secrets / Credentials / Proxy (OneCLI)

API keys, secret keys, OAuth tokens, and auth credentials are managed by the OneCLI gateway — which handles secret injection into containers at request time, so no keys or tokens are ever passed to containers directly. Run `onecli --help`.

### MCP server secrets (Notion, Grafana, …)

Stdio MCP servers configured for groups read **`groups/{name}/.mcp.json`**. That file is **generated** from the tracked template + a local env file so tokens are not stored in git.

| Path | Role |
|------|------|
| `config/mcp.json.template` | JSON with `${NOTION_TOKEN}`, `${GRAFANA_URL}`, `${GRAFANA_SERVICE_ACCOUNT_TOKEN}`, etc. Edit this when adding a new MCP server or changing non-secret defaults. |
| `secrets/mcp.env.example` | Names and placeholder values; copy to `secrets/mcp.env` on a new machine. |
| `secrets/mcp.env` | Real values. Listed in `.gitignore` as `secrets/mcp.env`. Use **single-quoted** values when values might contain shell metacharacters. |
| `scripts/render-mcp.sh` | `set -a; source secrets/mcp.env; envsubst` on the template, `python3 -m json.tool` validation, then writes **`groups/*/.mcp.json`** with mode `600`. Requires `envsubst` (e.g. Debian `gettext-base`) and `python3`. |

**Workflow**

1. One-time: `cp secrets/mcp.env.example secrets/mcp.env && chmod 600 secrets/mcp.env`
2. Edit `secrets/mcp.env` with real tokens (rotate anything that ever leaked).
3. From repo root: `./scripts/render-mcp.sh`
4. Restart the nanoclaw process (e.g. `systemctl --user restart nanoclaw`) so agents pick up new `groups/*/.mcp.json`.

**Do not** put long-lived secrets directly into `groups/*/.mcp.json` by hand — the next `./scripts/render-mcp.sh` overwrites those files. Change `secrets/mcp.env` or `config/mcp.json.template` instead.

**Adding a new stdio MCP server:** extend `config/mcp.json.template` with `${NEW_SECRET}`, add the variable to `secrets/mcp.env.example` and `secrets/mcp.env`, and add `export NEW_SECRET` (alongside the existing exports) in `scripts/render-mcp.sh` so `envsubst` can see it. If the Claude Agent SDK rejects tools, add `mcp__<serverKey>__*` to `allowedTools` in `container/agent-runner/src/index.ts` (includes `mcp__nanoclaw__*`, `mcp__notion__*`, `mcp__grafana__*`, `mcp__telemetry__*`).

**Notion MCP:** uses `@notionhq/notion-mcp-server` over stdio with an **internal integration** secret (`NOTION_TOKEN`). Create the integration and grant page access per [Notion’s MCP server docs](https://www.npmjs.com/package/@notionhq/notion-mcp-server). This differs from Cursor’s hosted Notion MCP (`https://mcp.notion.com/mcp`), which relies on OAuth and is not suited to unattended containers.

**Grafana MCP:** the template uses the official `uvx mcp-grafana` invocation per Grafana docs. The **agent container image** installs `uv` / `uvx` (see `container/Dockerfile`) so that command works; wiring still comes from this envsubst pipeline and `loadGroupMcpServersFromDisk()` merging `groups/*/.mcp.json` into the SDK.

**Plant telemetry MCP (Wasabi S3):** parquet dumps of co3ntrol-rs JSONL. Two buckets: **`provocative-dac-dev-beef`** (airbed nested hive) and **`provocative-dac-dev-a7f3`** (container-ship `telemetry/machine=bop-host/`). Set `TELEMETRY_S3_BUCKETS` in `secrets/mcp.env`. The stdio wrapper **unsets `HTTP(S)_PROXY`** (DuckDB httpfs cannot parse OneCLI proxy URLs). Tools are `mcp__telemetry__*`. Agent usage notes live in [`groups/global/CLAUDE.md`](groups/global/CLAUDE.md). Local Cursor: `scripts/run-telemetry-mcp.sh`.

**Ghost investigation support:**

- **Model tiers:** diagnostic plant questions and plant alerts (`plant-alert-*` tasks, model `investigation`) run on `NANOCLAW_INVESTIGATION_MODEL` (default `fable` = `claude-fable-5-1`, adaptive thinking, high effort) with SDK fallback `NANOCLAW_INVESTIGATION_FALLBACK_MODEL` (default `opus`). A skeptic pass on `GHOST_VERIFIER_MODEL` (default `opus`, never the investigator) reviews the answer against the tool evidence before delivery; `GHOST_VERIFIER=off` disables it. Chat stays on `NANOCLAW_DEFAULT_MODEL`.
- **Knowledge store** (`data/knowledge/`, replaces `groups/global/shared/notes.md`): agents read it read-only at `/workspace/knowledge` and write through `mcp__nanoclaw__kb_*` → `kb_write` IPC; the host is the only writer and commits every change. Seeds in `scripts/ghost/knowledge-seed/` are imported on startup. Status entries carry machine checks (`scripts/ghost/kb_check.py`) re-run hourly; the weekly review goes to the main group (Spark's DM) and Spark answers `still|resolved|confirm|retract <id>` without an agent run.
- **Host jobs:** `scripts/ghost/run_jobs.sh hourly|daily|weekly` (change digest + status recheck; signal dictionary + gardener; review). Install the units from `scripts/ghost/deploy/` into `~/.config/systemd/user/` and enable the three timers. Outputs land in `data/ghost/` (mounted read-only at `/workspace/ghost`).
- **Change check:** the hourly digest extracts device I/O remaps (signal → backplane station/riser/pin, from `control/src/system/*.rs`) per commit on each host's deployed branch, describes them in manifold-coil terms from co3ntrol-rs `documents/airbed-telemetry-io.md` (flags moves onto spare coils and reverts), and pairs them with the controller restarts that followed; `why_locked` and `recent_changes` rank them against the signals that failed. Each change is judged on mechanism (direct match on the failing output vs. same riser, and whether the output was written near onset): a remap explains an output that won't actuate while its readback follows the command, not a station dropout.
- **Replay evals:** `npx tsx scripts/ghost/evals/run.ts [--case id] [--repeat N]` from the checkout whose data you want (`NANOCLAW_CODEBASE_DIR` overrides `./codebase`); isolated group `telegram_ghost-eval`, telemetry cut off at each case's time, a change digest rebuilt `--as-of` that time and a `git archive` code snapshot of each repo as of then; scores answer + tool evidence.

**Observability (co3ntrol-rs / Grafana Cloud):** live Prom scrape → Alloy → Mimir/Loki, plus the S3 parquet archive, are documented for agents in [`groups/global/CLAUDE.md`](groups/global/CLAUDE.md) (mounted in containers as `/workspace/global/CLAUDE.md`).

## Skills

Four types of skills exist in NanoClaw. See [CONTRIBUTING.md](CONTRIBUTING.md) for the full taxonomy and guidelines.

- **Feature skills** — merge a `skill/*` branch to add capabilities (e.g. `/add-telegram`, `/add-slack`)
- **Utility skills** — ship code files alongside SKILL.md (e.g. `/claw`)
- **Operational skills** — instruction-only workflows, always on `main` (e.g. `/setup`, `/debug`)
- **Container skills** — loaded inside agent containers at runtime (`container/skills/`)

| Skill | When to Use |
|-------|-------------|
| `/setup` | First-time installation, authentication, service configuration |
| `/customize` | Adding channels, integrations, changing behavior |
| `/debug` | Container issues, logs, troubleshooting |
| `/update-nanoclaw` | Bring upstream NanoClaw updates into a customized install |
| `/init-onecli` | Install OneCLI Agent Vault and migrate `.env` credentials to it |
| `/qodo-pr-resolver` | Fetch and fix Qodo PR review issues interactively or in batch |
| `/get-qodo-rules` | Load org- and repo-level coding rules from Qodo before code tasks |

## Contributing

Before creating a PR, adding a skill, or preparing any contribution, you MUST read [CONTRIBUTING.md](CONTRIBUTING.md). It covers accepted change types, the four skill types and their guidelines, SKILL.md format rules, PR requirements, and the pre-submission checklist (searching for existing PRs/issues, testing, description format).

## Development

Run commands directly—don't tell the user to run them.

**Git workflow** (full rule: `.cursor/rules/keep-repo-clean.mdc`): before editing, `git fetch origin` and `git pull --ff-only` on `main`; cut branches and worktrees from `origin/main`. Commit any uncommitted work you find first, commit and push your own work when done, and deploy to bob49 only by pulling `origin/main` into a clean `~/provocative/nanoclaw`. Build test images under their own tag (`./container/build.sh <tag>`): a plain build overwrites the `:latest` image production spawns.

```bash
npm run dev          # Run with hot reload
npm run build        # Compile TypeScript
./container/build.sh # Rebuild agent container
```

Service management:
```bash
# macOS (launchd)
launchctl load ~/Library/LaunchAgents/com.nanoclaw.plist
launchctl unload ~/Library/LaunchAgents/com.nanoclaw.plist
launchctl kickstart -k gui/$(id -u)/com.nanoclaw  # restart

# Linux (systemd)
systemctl --user start nanoclaw
systemctl --user stop nanoclaw
systemctl --user restart nanoclaw
```

## Troubleshooting

**WhatsApp not connecting after upgrade:** WhatsApp is now a separate skill, not bundled in core. Run `/add-whatsapp` (or `npx tsx scripts/apply-skill.ts .claude/skills/add-whatsapp && npm run build`) to install it. Existing auth credentials and groups are preserved.

## Container Build Cache

The container buildkit caches the build context aggressively. `--no-cache` alone does NOT invalidate COPY steps — the builder's volume retains stale files. To force a truly clean rebuild, prune the builder then re-run `./container/build.sh`.
