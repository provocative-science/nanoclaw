# Instance deploy branch workflow

NanoClaw **tools** live on `main` (and short-lived feature branches).  
Each plant/deploy lives on a long-lived **`instance/<name>`** branch that **contains full tools history plus instance commits**.

```text
main                        # generic tools only
  └─ (merge) ──► instance/provocative   # tools + Ghost / alert-monitor / stack examples
```

**Rules (non-negotiable)**

1. **Never merge** `instance/*` into `main`.
2. **Never rebase** `instance/*` onto `main` — always **`git merge`** (via `sync-from-main.sh`).
3. Generic code changes land on **`main` first**, then sync into the instance branch.
4. Plant memory, alert-monitor, and stack-flavored examples are committed **only** on `instance/*`.
5. Live secrets (`.env`, `secrets/*.env`) stay **untracked** — never on either branch.

Override instance name with `NANOCLAW_OVERLAY_INSTANCE` (default `provocative`).  
Override tools ref with `NANOCLAW_TOOLS_BRANCH` (default `main`).

## Scripts

| Script | Purpose |
|--------|---------|
| `verify-workflow.sh` | Pass/fail checks for tools vs instance shape |
| `sync-from-main.sh` | Merge tools → current `instance/*` (merge only) |
| `commit-overlay.sh` | Commit instance-owned paths on `instance/*` only |
| `status.sh` | Human-readable ahead/behind + verify |
| `apply.sh` / `capture.sh` | Legacy wrappers (apply exits with instructions) |

Instance-owned paths are listed in [`scripts/instance-overlay/overlay.manifest`](../scripts/instance-overlay/overlay.manifest).

## Daily workflows

### A. Change generic tools

```bash
git switch main   # or a feature branch off main
# ... edit src/, container/, etc. ...
git commit
git push
# open PR into main; merge

# Then update the plant deploy branch:
git switch instance/provocative
./scripts/instance-overlay/sync-from-main.sh    # merges origin/main
./scripts/instance-overlay/verify-workflow.sh
git push origin instance/provocative
```

Until the tools PR is on `main`, sync from the tools tip explicitly:

```bash
NANOCLAW_TOOLS_BRANCH=chore/instance-overlay-split ./scripts/instance-overlay/sync-from-main.sh
# or: ./scripts/instance-overlay/sync-from-main.sh origin/chore/instance-overlay-split
```

### B. Change plant / instance config

```bash
git switch instance/provocative
# edit groups/*/CLAUDE.md, scripts/alert-monitor/, secrets/*.env.example, INSTANCE.md, …
./scripts/instance-overlay/commit-overlay.sh "Explain the plant/config change"
git push origin instance/provocative
```

Do **not** make these edits on `main`.

### C. Pull upstream NanoClaw into tools, then instance

```bash
git switch main
# /update-nanoclaw or: git fetch upstream && git merge upstream/main
npm run build && npm test
git push origin main

git switch instance/provocative
./scripts/instance-overlay/sync-from-main.sh
# resolve conflicts: instance-owned paths → keep ours; tools paths → prefer theirs
./scripts/instance-overlay/verify-workflow.sh
git push origin instance/provocative
```

### D. Run / deploy this plant

```bash
git switch instance/provocative
./scripts/instance-overlay/verify-workflow.sh
# .env from .env.dev.example on nanoclaw-dev — no prod TELEGRAM_BOT_TOKEN
npm run build
# production cutover later: point systemd WorkingDirectory at a checkout of instance/provocative
```

## Conflict cheat sheet (`sync-from-main`)

| Path | Resolution |
|------|------------|
| `groups/**/CLAUDE.md`, `scripts/alert-monitor/**`, `INSTANCE.md`, stack `secrets/*.env.example` | **ours** (instance) |
| `src/**`, `container/**`, `package.json`, overlay scripts on tools | **theirs** (tools), unless you intentionally diverged |

`sync-from-main.sh` prints the `git checkout --ours` lines for manifest paths when a merge stops on conflicts.

## Checklists

### Tools / cloud (plant-free)

- [ ] On `main` or a tools feature branch — **not** `instance/*` for generic work
- [ ] `./scripts/instance-overlay/verify-workflow.sh` passes
- [ ] No prod `.env` / `TELEGRAM_BOT_TOKEN`
- [ ] `npm run build` (and tests)

### bob49-dev (`~/provocative/nanoclaw-dev` only)

- [ ] Prod systemd still `WorkingDirectory=~/provocative/nanoclaw`
- [ ] Dev checkout on `instance/provocative` for plant testing
- [ ] `.env` from `.env.dev.example` — Telegram unset; `NANOCLAW_INSTANCE=dev`
- [ ] `verify-workflow.sh` passes; sync if behind tools
- [ ] Do not run a second consumer of the prod Telegram token

### Creating another instance

```bash
git switch main
git pull
git switch -c instance/other-project
# replace manifest paths with that project's content
cp scripts/instance-overlay/overlay.manifest overlay.manifest  # optional root copy
./scripts/instance-overlay/commit-overlay.sh "Initial overlay for other-project"
git push -u origin instance/other-project
```

## Telegram safety

| Environment | Branch | Telegram |
|-------------|--------|----------|
| Production | (today: mixed tree; later: `instance/provocative`) | Live bot — sole consumer |
| nanoclaw-dev / cloud | tools or instance for file tests | Token **unset**; `NANOCLAW_INSTANCE=dev` |

## Anti-patterns

- Rebasing `instance/*` onto `main`
- Merging `instance/*` into `main` / opening a PR from instance → main for plant files
- Committing Ghost CLAUDE or alert-monitor on `main`
- Using legacy `apply.sh` to dirty a tools working tree (it now exits with instructions)
- Copying prod `.env` into nanoclaw-dev
