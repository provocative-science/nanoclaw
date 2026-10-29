/**
 * Skeptic pass for plant investigations: a different model checks the draft
 * brief against the evidence the investigator actually gathered (tool calls
 * and their outputs) before anything reaches Telegram. Fail-open: any error
 * or timeout delivers the original draft.
 */
import fs from 'fs';
import path from 'path';
import { query } from '@anthropic-ai/claude-agent-sdk';

import { queryThinkingOptions } from './thinking.js';

export interface EvidenceItem {
  tool: string;
  input: string;
  output: string;
}

export interface VerifierOutcome {
  verdict: 'pass' | 'revise' | 'error';
  issues: string[];
  text: string;
  model: string;
  durationMs: number;
}

const MAX_ITEM_OUTPUT = 2_500;
const MAX_EVIDENCE_CHARS = 60_000;
const MAX_ITEMS = 200;
const VERIFIER_TIMEOUT_MS = 4 * 60_000;

export class EvidenceLog {
  private pending = new Map<string, { tool: string; input: string }>();
  items: EvidenceItem[] = [];

  onToolUse(id: string, name: string, input: unknown): void {
    this.pending.set(id, {
      tool: name,
      input: JSON.stringify(input ?? {}).slice(0, 1_500),
    });
  }

  onToolResult(id: string, content: unknown): void {
    const call = this.pending.get(id);
    if (!call) return;
    this.pending.delete(id);
    let text: string;
    if (typeof content === 'string') text = content;
    else if (Array.isArray(content))
      text = content
        .map((c) =>
          c && typeof c === 'object' && 'text' in c
            ? String((c as { text: unknown }).text)
            : '',
        )
        .join('\n');
    else text = JSON.stringify(content ?? '');
    const output =
      text.length > MAX_ITEM_OUTPUT
        ? `${text.slice(0, MAX_ITEM_OUTPUT)}… [${text.length - MAX_ITEM_OUTPUT} chars truncated]`
        : text;
    this.items.push({ ...call, output });
    if (this.items.length > MAX_ITEMS) this.items.shift();
  }

  usedPlantTools(): boolean {
    return this.items.some(
      (i) =>
        i.tool.startsWith('mcp__telemetry__') ||
        i.tool.startsWith('mcp__grafana__'),
    );
  }

  render(): string {
    const relevant = this.items.filter(
      (i) => !i.tool.startsWith('mcp__nanoclaw__send'),
    );
    const chunks: string[] = [];
    let total = 0;
    for (let i = relevant.length - 1; i >= 0; i--) {
      const it = relevant[i];
      const chunk = `### [${i + 1}] ${it.tool}\ninput: ${it.input}\noutput: ${it.output}\n`;
      if (total + chunk.length > MAX_EVIDENCE_CHARS) {
        chunks.unshift(`(${i + 1} earlier tool calls omitted)\n`);
        break;
      }
      chunks.unshift(chunk);
      total += chunk.length;
    }
    return chunks.join('\n') || '(no tool calls)';
  }

  reset(): void {
    this.items = [];
    this.pending.clear();
  }
}

const SKEPTIC_SYSTEM = `You are the skeptic reviewer for "Ghost", an AI that writes operator briefs about an industrial CO2 capture plant (co3ntrol-rs controllers: airbed, container ship, liquefaction). Another model wrote the draft below. Operators act on these briefs, so a confident wrong root cause is worse than an honest "inconclusive".

Check the draft against the evidence log (the investigator's actual tool calls and outputs):
1. Every number, timestamp, state, signal name and GUI label in the draft must appear in, or follow directly from, the evidence. Flag invented or misread values.
2. Causal language ("root cause", "caused by", "because") is only allowed when the evidence directly shows it (e.g. the watchdog named in console/active_recovery AND its input values at the lock edge). Otherwise it must be stated as a hypothesis with its status.
3. The draft must follow the evidence contract: hypotheses each marked Validated / Ruled out / Inconclusive with the query and values behind them, and "Root cause" only for a validated cause. Missing data is "inconclusive", not "fine".
4. Check that alternative explanations the evidence points to were not skipped (e.g. many fields going null at once = device/IO fault; a restart just before = deploy/crash; stale inputs on a watchdog that trips on missing data).
5. Timezones must be explicit and consistent.

You may run at most 3 read-only telemetry queries to spot-check the single most important claim. Do not start a new investigation.

Reply with ONLY a JSON object in a \`\`\`json fenced block:
{"verdict": "pass" | "revise", "issues": ["short description", ...], "revised_message": "<full corrected message, only when verdict is revise>"}
Use "pass" when there are no material problems (style nits are not material). For "revise", minimally edit the draft: correct wrong values, downgrade unsupported claims to hypotheses/unverified, add a missing "Inconclusive" where evidence is absent. Keep the original format, language and length. Never add new speculation.`;

function parseVerdict(
  text: string,
): { verdict: 'pass' | 'revise'; issues: string[]; revised?: string } | null {
  const fenced = text.match(/```json\s*([\s\S]*?)```/);
  const raw = fenced
    ? fenced[1]
    : text.slice(text.indexOf('{'), text.lastIndexOf('}') + 1);
  try {
    const parsed = JSON.parse(raw);
    if (parsed.verdict !== 'pass' && parsed.verdict !== 'revise') return null;
    return {
      verdict: parsed.verdict,
      issues: Array.isArray(parsed.issues)
        ? parsed.issues.map(String).slice(0, 12)
        : [],
      revised:
        typeof parsed.revised_message === 'string'
          ? parsed.revised_message
          : undefined,
    };
  } catch {
    return null;
  }
}

export async function runVerifier(opts: {
  draft: string;
  request: string;
  evidence: EvidenceLog;
  model: string;
  sdkEnv: Record<string, string | undefined>;
  telemetryServer?: {
    command: string;
    args?: string[];
    env?: Record<string, string>;
  };
  log: (msg: string) => void;
}): Promise<VerifierOutcome> {
  const started = Date.now();
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), VERIFIER_TIMEOUT_MS);
  const prompt = `## Request the investigator answered (excerpt)\n${opts.request.slice(-4_000)}\n\n## Draft brief\n${opts.draft}\n\n## Evidence log\n${opts.evidence.render()}`;
  let resultText = '';
  try {
    for await (const message of query({
      prompt,
      options: {
        cwd: '/tmp',
        model: opts.model,
        ...queryThinkingOptions(opts.model),
        systemPrompt: SKEPTIC_SYSTEM,
        allowedTools: opts.telemetryServer ? ['mcp__telemetry__*'] : [],
        disallowedTools: [
          'Bash',
          'Write',
          'Edit',
          'WebFetch',
          'WebSearch',
          'Task',
        ],
        mcpServers: opts.telemetryServer
          ? { telemetry: opts.telemetryServer }
          : {},
        maxTurns: 8,
        env: opts.sdkEnv,
        permissionMode: 'bypassPermissions',
        allowDangerouslySkipPermissions: true,
        settingSources: [],
        abortController: abort,
      },
    })) {
      if (message.type === 'result') {
        resultText =
          'result' in message
            ? String((message as { result?: string }).result ?? '')
            : '';
      }
    }
  } catch (err) {
    clearTimeout(timer);
    opts.log(
      `Verifier failed (delivering draft): ${err instanceof Error ? err.message : String(err)}`,
    );
    return {
      verdict: 'error',
      issues: [],
      text: opts.draft,
      model: opts.model,
      durationMs: Date.now() - started,
    };
  }
  clearTimeout(timer);
  const parsed = parseVerdict(resultText);
  if (!parsed) {
    opts.log(
      `Verifier returned unparseable output (delivering draft): ${resultText.slice(0, 200)}`,
    );
    return {
      verdict: 'error',
      issues: [],
      text: opts.draft,
      model: opts.model,
      durationMs: Date.now() - started,
    };
  }
  const revised =
    parsed.verdict === 'revise' && parsed.revised?.trim()
      ? parsed.revised.trim()
      : null;
  return {
    verdict: revised ? 'revise' : 'pass',
    issues: parsed.issues,
    text: revised ?? opts.draft,
    model: opts.model,
    durationMs: Date.now() - started,
  };
}

/** Keep an audit trail in the group folder (also scored by replay evals). */
export function saveVerifierReport(
  draft: string,
  outcome: VerifierOutcome,
  evidenceCount: number,
): void {
  try {
    const dir = '/workspace/group/verifier';
    fs.mkdirSync(dir, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    fs.writeFileSync(
      path.join(dir, `${stamp}.json`),
      JSON.stringify(
        { at: new Date().toISOString(), evidenceCount, draft, ...outcome },
        null,
        2,
      ),
    );
  } catch {
    /* best-effort */
  }
}

/** Replay evals: everything the scorer needs (tool calls, draft, delivered text). */
export function saveEvalRecord(record: {
  model?: string;
  draft: string;
  final: string;
  verifier: VerifierOutcome | null;
  tools: EvidenceItem[];
}): void {
  try {
    const dir = '/workspace/group/eval-ipc';
    fs.mkdirSync(dir, { recursive: true });
    const stamp = new Date().toISOString().replace(/[:.]/g, '-');
    fs.writeFileSync(
      path.join(dir, `run-${stamp}.json`),
      JSON.stringify({ at: new Date().toISOString(), ...record }, null, 2),
    );
  } catch {
    /* best-effort */
  }
}
