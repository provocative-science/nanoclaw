## Provocative Science Deployment

This fork runs a Telegram-based AI assistant for the Provocative Science team. It answers questions about our mounted codebases; Postgres-era telemetry refers to the legacy **co2ntrol** stack on the pirateship (see repos below).

### What It Does

- **Codebase Q&A** — The agent has read access to the cloned repos under `codebase/` (Rust API, GUI, PLC firmware, hardware firmware, and legacy reference) and can answer architecture, implementation, and usage questions via Telegram
- **Telegram channels** — Bot: @provocative_ax_me_bot. Main private chat (no trigger word needed) and group chats (triggered by `@Ghost`)
- **Container isolation** — Each group's agent runs in its own Docker container with read-only access to mounted codebases
- **Model tiering** — Default is Claude Sonnet; escalate per message or pin a model on scheduled tasks (see below)

### Model commands (Telegram)

Default model is **Claude Sonnet 4.6**. Put a model directive at the **start** of the message (after `@Ghost` in groups):

| Command | Model | Notes |
|---------|--------|--------|
| `/sonnet …` or `model:sonnet` | Claude Sonnet 4.6 | Default full agent (tools, MCP, sessions) |
| `/opus …` or `model:opus` | Claude Opus 4.6 | Full agent — harder deep-dives |
| `/haiku …` or `model:haiku` | Claude Haiku 4.5 | Full agent — cheaper/faster |
| `/qwen …` or `model:qwen` | `qwen3.6-35b` | **Light path only** — no tools/MCP; host OpenAI-compatible API |

Examples:

```text
@Ghost /opus why is column pressure low?
@Ghost /qwen say hi
@Ghost model:haiku summarize the last alert
```

Invalid `model:…` values get a short allowlist reply and do not run the agent.

**Scheduled tasks:** optional `model` on `schedule_task` / `update_task` (`sonnet` \| `opus` \| `haiku` \| `qwen`, or full IDs). Plant alerts stay on `sonnet`. Use `qwen` only for format/Q&A-style wakes, not plant investigation.

**Qwen setup (bob49):** copy `secrets/qwen.env.example` → `secrets/qwen.env`, set `QWEN_API_KEY` (from laptop `~/.config/dotfiles-autocommit.env`), then `systemctl --user restart nanoclaw`.

### Deploying to a New Machine

**Prerequisites:** Linux (x86_64), Node.js 22+, Docker, [Claude Code](https://claude.ai/download)

**1. Clone and set up the fork:**

```bash
git clone git@github.com:provocative-science/nanoclaw.git
cd nanoclaw
git remote add upstream git@github.com:qwibitai/nanoclaw.git
```

**2. Clone the codebases:**

```bash
mkdir -p codebase
git clone git@github.com:provocative-science/co3ntrol-rs.git codebase/co3ntrol-rs
git clone git@github.com:provocative-science/co3ntrol-gui.git codebase/co3ntrol-gui
git clone git@github.com:provocative-science/high-pressure-co2-plc.git codebase/high-pressure-co2-plc
git clone git@github.com:provocative-science/riser-firmware.git codebase/riser-firmware
git clone git@github.com:provocative-science/backplane-firmware.git codebase/backplane-firmware
git clone git@github.com:provocative-science/co2ntrol.git codebase/co2ntrol
```

The superseded **co3ntrol** repo is intentionally omitted—use **co3ntrol-rs** / **co3ntrol-gui** instead. **co2ntrol** is optional legacy reference (pirateship / Postgres-era).

Requires a GitHub account with access to the provocative-science org.

**3. Set up secrets:**

Install OneCLI and its CLI:

```bash
curl -fsSL onecli.sh/install | sh
curl -fsSL onecli.sh/cli/install | sh
onecli config set api-host http://127.0.0.1:10254
```

Add the Anthropic API key:

```bash
onecli secrets create --name Anthropic --type anthropic --value YOUR_KEY --host-pattern api.anthropic.com
```

Create `.env`:

```bash
cat > .env << 'EOF'
TZ=UTC
ONECLI_URL=http://127.0.0.1:10254
TELEGRAM_BOT_TOKEN=<your-telegram-bot-token>
EOF
```

Sync to container environment:

```bash
mkdir -p data/env && cp .env data/env/env
```

**4. Build and start:**

```bash
bash setup.sh
npm run build
./container/build.sh
```

Register your Telegram chat and start the service:

```bash
claude  # then run /setup to complete registration and service setup
```

### Repos

| Repo | Description |
|------|-------------|
| [co3ntrol-rs](https://github.com/provocative-science/co3ntrol-rs) | Rust API backend: Modbus TCP to the Liquefaction Controller and BOP/DAC pod, HTTP API for the GUI and automation, telemetry to Alloy / Grafana Cloud and JSONL → Wasabi parquet |
| [co3ntrol-gui](https://github.com/provocative-science/co3ntrol-gui) | Web GUI for liquefaction and BOP/DAC controls (uses **co3ntrol-rs**) |
| [high-pressure-co2-plc](https://github.com/provocative-science/high-pressure-co2-plc) | Liquefaction Controller PLC firmware |
| [riser-firmware](https://github.com/provocative-science/riser-firmware) | Riser firmware |
| [backplane-firmware](https://github.com/provocative-science/backplane-firmware) | Backplane firmware |
| [co2ntrol](https://github.com/provocative-science/co2ntrol) | **Legacy reference** — original stack on the pirateship; produced Postgres telemetry data (not the active control path) |

### Planned

- Liquefaction parquet in the same Wasabi bucket (`host=lichost` expected) once that node is piped
- Telegram group notifications when anomalies are detected

