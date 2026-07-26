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

**Adding a new stdio MCP server:** extend `config/mcp.json.template` with `${NEW_SECRET}`, add the variable to `secrets/mcp.env.example` and `secrets/mcp.env`, and add `export NEW_SECRET` (alongside the existing exports) in `scripts/render-mcp.sh` so `envsubst` can see it. If the Claude Agent SDK rejects tools, add `mcp__<serverKey>__*` to `allowedTools` in `container/agent-runner/src/index.ts` (includes `mcp__nanoclaw__*`, `mcp__notion__*`, `mcp__grafana__*`).

**Notion MCP:** uses `@notionhq/notion-mcp-server` over stdio with an **internal integration** secret (`NOTION_TOKEN`). Create the integration and grant page access per [Notion’s MCP server docs](https://www.npmjs.com/package/@notionhq/notion-mcp-server). This differs from Cursor’s hosted Notion MCP (`https://mcp.notion.com/mcp`), which relies on OAuth and is not suited to unattended containers.

**Grafana MCP:** the template uses the official `uvx mcp-grafana` invocation per Grafana docs. The **agent container image** installs `uv` / `uvx` (see `container/Dockerfile`) so that command works; wiring still comes from this envsubst pipeline and `loadGroupMcpServersFromDisk()` merging `groups/*/.mcp.json` into the SDK.

**Observability (co3ntrol-rs / Grafana Cloud):** OTLP → Alloy → Mimir/Loki/Tempo, resource attributes, metrics vs logs vs traces, and Grafana MCP usage for the CO₂ control plane are documented for agents in [`groups/global/CLAUDE.md`](groups/global/CLAUDE.md) (mounted in containers as `/workspace/global/CLAUDE.md`).

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
