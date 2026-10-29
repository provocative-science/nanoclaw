/**
 * Ghost replay evals: re-run past plant alerts and known misses against the
 * current prompt, tools, knowledge store and models, with telemetry hidden
 * after each case's cutoff, then score answer + tool evidence.
 *
 * Run from the NanoClaw checkout whose data/ and groups/ you want to use
 * (normally production), after `npm run build`:
 *
 *   npx tsx scripts/ghost/evals/run.ts                       # all cases once
 *   npx tsx scripts/ghost/evals/run.ts --case oct29-airbed-valve-lock --repeat 3
 *   npx tsx scripts/ghost/evals/run.ts --model opus --no-verifier
 *   npx tsx scripts/ghost/evals/run.ts --dry-run              # print prompts only
 *   npx tsx scripts/ghost/evals/run.ts --score <run.json> --case <id>
 *
 * Isolation: a dedicated non-main group (telegram_ghost-eval) with evalMode on,
 * so nothing is sent to chat and nothing is written to the knowledge store.
 * Grafana is only available for cases with allow_grafana (live Prom is not
 * time-cut, so those cases can see the future if the agent queries it).
 * Each run costs a full investigation (strongest model + verifier).
 */
import { execFileSync } from 'child_process';
import fs from 'fs';
import path from 'path';

import { OneCLI } from '@onecli-sh/sdk';

import { buildBriefing } from '../../../src/briefing.js';
import {
  ASSISTANT_NAME,
  DATA_DIR,
  GROUPS_DIR,
  ONECLI_URL,
  TIMEZONE,
} from '../../../src/config.js';
import { runContainerAgent } from '../../../src/container-runner.js';
import {
  EvalCase,
  EvalRecord,
  EvalScore,
  scoreRun,
} from '../../../src/eval-score.js';
import { resolveGroupIpcPath } from '../../../src/group-folder.js';
import {
  normalizeModel,
  resolveInvestigationFallbackModel,
  resolveVerifierModel,
} from '../../../src/model.js';
import { formatMessages } from '../../../src/router.js';
import type { NewMessage, RegisteredGroup } from '../../../src/types.js';

const HERE = path.dirname(new URL(import.meta.url).pathname);
const CASES_DIR = path.join(HERE, 'cases');
const EVAL_FOLDER = 'telegram_ghost-eval';
const RUN_TIMEOUT_MS = 25 * 60_000;

interface Args {
  cases: string[];
  repeat: number;
  model: string;
  verifier: boolean;
  dryRun: boolean;
  scoreFile?: string;
}

function parseArgs(argv: string[]): Args {
  const args: Args = {
    cases: [],
    repeat: 1,
    model: 'investigation',
    verifier: true,
    dryRun: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--case') args.cases.push(argv[++i]);
    else if (a === '--repeat')
      args.repeat = Math.max(1, Number(argv[++i]) || 1);
    else if (a === '--model') args.model = argv[++i];
    else if (a === '--no-verifier') args.verifier = false;
    else if (a === '--dry-run') args.dryRun = true;
    else if (a === '--score') args.scoreFile = argv[++i];
    else throw new Error(`unknown argument ${a}`);
  }
  return args;
}

function loadCases(ids: string[]): EvalCase[] {
  const all = fs
    .readdirSync(CASES_DIR)
    .filter((f) => f.endsWith('.json'))
    .map(
      (f) =>
        JSON.parse(
          fs.readFileSync(path.join(CASES_DIR, f), 'utf-8'),
        ) as EvalCase,
    );
  if (!ids.length) return all;
  const picked = all.filter((c) => ids.includes(c.id));
  const missing = ids.filter((id) => !picked.some((c) => c.id === id));
  if (missing.length) throw new Error(`unknown case(s): ${missing.join(', ')}`);
  return picked;
}

function alertPayload(c: EvalCase): Record<string, unknown> {
  if (c.alert) return c.alert;
  const logPath = path.join(
    GROUPS_DIR,
    'global',
    'shared',
    'plant-alerts',
    'log.jsonl',
  );
  for (const line of fs.readFileSync(logPath, 'utf-8').split('\n')) {
    if (!line.trim()) continue;
    const rec = JSON.parse(line);
    if (rec.id === c.alert_id && rec.alert) return rec.alert;
  }
  throw new Error(`${c.id}: alert ${c.alert_id} not found in ${logPath}`);
}

function buildAlertPrompt(c: EvalCase): string {
  const code = [
    'import json, sys',
    `sys.path.insert(0, ${JSON.stringify(path.resolve(HERE, '..', '..', 'alert-monitor'))})`,
    'from prompt import build_prompt',
    'payload = json.load(sys.stdin)',
    'print(build_prompt(payload["alert"], alert_id=payload["id"]))',
  ].join('\n');
  return execFileSync('python3', ['-c', code], {
    input: JSON.stringify({
      alert: alertPayload(c),
      id: `${c.alert_id}-replay`,
    }),
    encoding: 'utf-8',
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
  });
}

function buildPrompt(c: EvalCase): string {
  const cutoff = Date.parse(c.cutoff);
  let body: string;
  let intentText: string;
  if (c.kind === 'alert') {
    body = buildAlertPrompt(c);
    intentText = body;
  } else {
    const messages: NewMessage[] = (c.messages ?? []).map((m, i) => ({
      id: `eval-${i}`,
      chat_jid: 'eval:ghost',
      sender: m.sender,
      sender_name: m.sender,
      content: m.text,
      timestamp: m.time,
    }));
    body = formatMessages(messages, TIMEZONE);
    intentText = messages.map((m) => m.content).join('\n');
  }
  const briefing = buildBriefing(intentText, 'full', {
    now: cutoff,
    includeDigest: false,
    excludeEntryIds: c.exclude_kb,
    excludeAlertIds: c.alert_id ? [c.alert_id] : [],
  });
  const clock =
    `<replay>The current time is ${new Date(cutoff).toISOString()} (UTC). Treat it as now: ` +
    'telemetry after it is not available yet, and recent-changes context is not included.</replay>\n\n';
  return clock + briefing + body;
}

function prepareGroup(): RegisteredGroup {
  const dir = path.join(GROUPS_DIR, EVAL_FOLDER);
  fs.mkdirSync(dir, { recursive: true });
  for (const entry of fs.readdirSync(dir)) {
    if (entry === '.mcp.json') continue;
    fs.rmSync(path.join(dir, entry), { recursive: true, force: true });
  }
  const mcp = path.join(dir, '.mcp.json');
  if (!fs.existsSync(mcp)) {
    const source = path.join(GROUPS_DIR, 'global', '.mcp.json');
    if (!fs.existsSync(source)) {
      throw new Error(
        `no ${mcp}: run ./scripts/render-mcp.sh (it renders every groups/*/.mcp.json)`,
      );
    }
    fs.copyFileSync(source, mcp);
    fs.chmodSync(mcp, 0o600);
  }
  fs.rmSync(path.join(DATA_DIR, 'sessions', EVAL_FOLDER, '.claude'), {
    recursive: true,
    force: true,
  });
  const codebase = path.join(process.cwd(), 'codebase');
  return {
    name: 'Ghost evals',
    folder: EVAL_FOLDER,
    trigger: `@${ASSISTANT_NAME}`,
    added_at: new Date().toISOString(),
    requiresTrigger: false,
    isMain: false,
    ...(fs.existsSync(codebase)
      ? {
          containerConfig: {
            additionalMounts: [
              { hostPath: codebase, containerPath: 'codebase', readonly: true },
            ],
          },
        }
      : {}),
  };
}

function latestEvalRecord(): EvalRecord | null {
  const dir = path.join(GROUPS_DIR, EVAL_FOLDER, 'eval-ipc');
  if (!fs.existsSync(dir)) return null;
  const runs = fs
    .readdirSync(dir)
    .filter((f) => f.startsWith('run-') && f.endsWith('.json'))
    .sort();
  if (!runs.length) return null;
  return JSON.parse(
    fs.readFileSync(path.join(dir, runs[runs.length - 1]), 'utf-8'),
  ) as EvalRecord;
}

async function runCase(
  c: EvalCase,
  args: Args,
  onecli: OneCLI,
): Promise<{ score: EvalScore; record: EvalRecord }> {
  const group = prepareGroup();
  await onecli
    .ensureAgent({
      name: group.name,
      identifier: EVAL_FOLDER.replace(/_/g, '-'),
    })
    .catch(() => undefined);
  const model = normalizeModel(args.model);
  if (!model) throw new Error(`model ${args.model} is not allowlisted`);
  const ipcInput = path.join(resolveGroupIpcPath(EVAL_FOLDER), 'input');
  fs.mkdirSync(ipcInput, { recursive: true });
  for (const f of fs.readdirSync(ipcInput))
    fs.rmSync(path.join(ipcInput, f), { force: true });
  const close = () => fs.writeFileSync(path.join(ipcInput, '_close'), '');
  const timer = setTimeout(close, RUN_TIMEOUT_MS);
  let delivered = '';
  try {
    await runContainerAgent(
      group,
      {
        prompt: buildPrompt(c),
        groupFolder: EVAL_FOLDER,
        chatJid: 'eval:ghost',
        isMain: false,
        isScheduledTask: c.kind === 'alert',
        assistantName: ASSISTANT_NAME,
        model,
        tier: 'investigation',
        fallbackModel: resolveInvestigationFallbackModel(),
        ...(args.verifier
          ? { verifierModel: resolveVerifierModel(model) }
          : {}),
        telemetryTimeCutoff: new Date(Date.parse(c.cutoff)).toISOString(),
        evalMode: true,
        evalAllowGrafana: c.allow_grafana === true,
        evalExcludeKb: c.exclude_kb ?? [],
      },
      () => undefined,
      async (output) => {
        if (output.result) delivered = String(output.result);
        if (output.result || output.status === 'error') close();
      },
    );
  } finally {
    clearTimeout(timer);
  }
  const record: EvalRecord = latestEvalRecord() ?? {
    draft: delivered,
    final: delivered,
    tools: [],
    verifier: null,
  };
  if (!record.final) record.final = delivered;
  return { score: scoreRun(c, record), record };
}

function printScore(s: EvalScore, run: number): void {
  const pct = Math.round(s.score * 100);
  const verdict = s.verifierVerdict ? `, verifier ${s.verifierVerdict}` : '';
  console.log(
    `\n${s.caseId} #${run}: ${s.passed}/${s.total} (${pct}%), ${s.toolCalls} tool calls${verdict}`,
  );
  for (const ch of s.checks) {
    console.log(
      `  ${ch.pass ? '✓' : '✗'} [${ch.group}] ${ch.name}${ch.detail ? ` — ${ch.detail}` : ''}`,
    );
  }
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  const cases = loadCases(args.cases);

  if (args.scoreFile) {
    if (cases.length !== 1) throw new Error('--score needs exactly one --case');
    const record = JSON.parse(
      fs.readFileSync(args.scoreFile, 'utf-8'),
    ) as EvalRecord;
    printScore(scoreRun(cases[0], record), 1);
    return;
  }
  if (args.dryRun) {
    for (const c of cases)
      console.log(`\n===== ${c.id} (cutoff ${c.cutoff})\n${buildPrompt(c)}`);
    return;
  }

  const onecli = new OneCLI({ url: ONECLI_URL });
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const outDir = path.join(DATA_DIR, 'ghost', 'evals', stamp);
  fs.mkdirSync(outDir, { recursive: true });
  const scores: EvalScore[] = [];
  for (const c of cases) {
    for (let run = 1; run <= args.repeat; run++) {
      const { score, record } = await runCase(c, args, onecli);
      scores.push(score);
      fs.writeFileSync(
        path.join(outDir, `${c.id}-${run}.json`),
        JSON.stringify({ case: c.id, score, record }, null, 2),
      );
      printScore(score, run);
    }
  }
  const mean = scores.reduce((a, s) => a + s.score, 0) / (scores.length || 1);
  console.log(
    `\nOverall ${Math.round(mean * 100)}% over ${scores.length} run(s). Records: ${outDir}`,
  );
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
