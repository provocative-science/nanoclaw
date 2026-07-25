# Instance overlay

NanoClaw **tools** live on the default branch (`main` / feature branches). **Instance** knowledge (Ghost plant memory, alert-monitor, stack-flavored secret examples, deploy runbooks) lives on a private orphan branch:

```text
instance/<name>     # e.g. instance/provocative
```

Maintain with:

| Script | Purpose |
|--------|---------|
| `scripts/instance-overlay/apply.sh` | Checkout overlay paths onto the working tree |
| `scripts/instance-overlay/capture.sh` | Commit overlay paths back onto `instance/<name>` |
| `scripts/instance-overlay/status.sh` | Diff working tree vs overlay tip + leakage scan |

Override instance name with `NANOCLAW_OVERLAY_INSTANCE` (default `provocative`).

**Never merge** `instance/*` into `main`. **Never** put live `.env` tokens on the overlay branch.

## Sync model

```text
upstream (qwibitai/nanoclaw) ──► base/tools branch (main)
                                      │
                                      ├── apply instance/provocative ──► plant deploy
                                      └── apply (none / other overlay) ──► other NanoClaw
```

## Cloud checklist (build / guards only)

- [ ] No production `.env` / `TELEGRAM_BOT_TOKEN` in the environment
- [ ] `cp .env.dev.example .env` (Telegram left unset)
- [ ] `NANOCLAW_INSTANCE=dev` and `NANOCLAW_ALLOW_NO_CHANNELS=1`
- [ ] `npm run build` (and `npm test` if configured)
- [ ] Confirm logs would skip Telegram if the host were started
- [ ] Do **not** run alert-monitor against live plant chats
- [ ] Do **not** run `claw` with production Telegram credentials

## bob49-dev checklist (apply + claw; prod stays sole Telegram)

- [ ] Work only in `~/provocative/nanoclaw-dev` (not `~/provocative/nanoclaw`)
- [ ] `systemctl --user show nanoclaw -p WorkingDirectory` still points at **prod** path
- [ ] `.env` from `.env.dev.example` — **no** prod `TELEGRAM_BOT_TOKEN`
- [ ] `git fetch origin instance/provocative` (or local branch present)
- [ ] `./scripts/instance-overlay/apply.sh`
- [ ] `./scripts/instance-overlay/status.sh` (expect OK / intentional diffs only)
- [ ] Optional: `claw` smoke with `CONTAINER_IMAGE=nanoclaw-agent-dev:latest`
- [ ] Confirm no second process is polling the production bot token

## Telegram safety

| Environment | Telegram |
|-------------|----------|
| Production (`~/provocative/nanoclaw`) | Live bot token — sole consumer |
| `nanoclaw-dev` / cloud | Token **unset**; `NANOCLAW_INSTANCE=dev` refuses Telegram unless `NANOCLAW_ALLOW_DEV_TELEGRAM=1` with a **separate** test bot |

Sharing the production token between two hosts causes missed updates and/or **duplicate Ghost replies**.

## Capturing overlay edits

```bash
# After editing overlay files on a tree with apply.sh already run:
./scripts/instance-overlay/capture.sh "Describe the overlay change"
git push origin instance/provocative   # private branch; no live secrets
```
