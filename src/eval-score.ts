/**
 * Scoring for Ghost replay evals (scripts/ghost/evals). A case says what a
 * correct answer must mention, must not claim, which tools it must use and
 * which evidence must appear in tool outputs — so a lucky guess without the
 * evidence still fails, and so does the right evidence with a wrong story.
 */

export interface PatternCheck {
  name: string;
  /** Case-insensitive regular expression. */
  pattern: string;
}

export interface EvalExpectations {
  must_mention?: PatternCheck[];
  must_not?: PatternCheck[];
  /** Matched against tool names (e.g. "mcp__telemetry__why_locked"). */
  must_call?: PatternCheck[];
  /** Matched against tool outputs: proves the evidence was actually retrieved. */
  evidence?: PatternCheck[];
  /** Require the Validated / Ruled out / Inconclusive evidence structure. */
  contract?: boolean;
  max_tool_calls?: number;
}

export interface EvalCase {
  id: string;
  kind: 'chat' | 'alert';
  description: string;
  known_miss?: boolean;
  /** Replay instant: the agent is told it is now, and telemetry after it is hidden. */
  cutoff: string;
  allow_grafana?: boolean;
  messages?: { sender: string; time: string; text: string }[];
  alert_id?: string;
  alert?: Record<string, unknown>;
  exclude_kb?: string[];
  ground_truth: string;
  expect: EvalExpectations;
}

export interface EvalToolCall {
  tool: string;
  input: string;
  output: string;
}

export interface EvalRecord {
  draft: string;
  final: string;
  tools: EvalToolCall[];
  verifier?: { verdict: string; issues: string[] } | null;
}

export interface CheckOutcome {
  group: 'mention' | 'must_not' | 'call' | 'evidence' | 'contract' | 'budget';
  name: string;
  pass: boolean;
  detail?: string;
}

export interface EvalScore {
  caseId: string;
  passed: number;
  total: number;
  score: number;
  toolCalls: number;
  verifierVerdict?: string;
  checks: CheckOutcome[];
}

const CONTRACT_RE = /\b(validated|ruled out|inconclusive)\b/i;

function re(pattern: string): RegExp {
  return new RegExp(pattern, 'i');
}

export function scoreRun(c: EvalCase, record: EvalRecord): EvalScore {
  const text = record.final || record.draft || '';
  const checks: CheckOutcome[] = [];
  const exp = c.expect;

  for (const m of exp.must_mention ?? []) {
    checks.push({
      group: 'mention',
      name: m.name,
      pass: re(m.pattern).test(text),
    });
  }
  for (const m of exp.must_not ?? []) {
    const hit = text.match(re(m.pattern));
    checks.push({
      group: 'must_not',
      name: m.name,
      pass: !hit,
      ...(hit ? { detail: `found: "${hit[0].slice(0, 120)}"` } : {}),
    });
  }
  for (const m of exp.must_call ?? []) {
    const r = re(m.pattern);
    const calls = record.tools.filter((t) => r.test(t.tool));
    checks.push({
      group: 'call',
      name: m.name,
      pass: calls.length > 0,
      detail: `${calls.length} call(s)`,
    });
  }
  for (const m of exp.evidence ?? []) {
    const r = re(m.pattern);
    const found = record.tools.some((t) => r.test(t.output));
    const cited = r.test(text);
    checks.push({
      group: 'evidence',
      name: m.name,
      pass: found,
      detail: found
        ? cited
          ? 'retrieved and cited'
          : 'retrieved, not cited'
        : 'never retrieved',
    });
  }
  if (exp.contract) {
    const labels = new Set(
      (text.match(new RegExp(CONTRACT_RE.source, 'gi')) ?? []).map((s) =>
        s.toLowerCase(),
      ),
    );
    checks.push({
      group: 'contract',
      name: 'evidence contract',
      pass: labels.size >= 2,
      detail:
        [...labels].join(', ') ||
        'no Validated / Ruled out / Inconclusive labels',
    });
  }
  if (exp.max_tool_calls) {
    checks.push({
      group: 'budget',
      name: `≤ ${exp.max_tool_calls} tool calls`,
      pass: record.tools.length <= exp.max_tool_calls,
      detail: `${record.tools.length}`,
    });
  }
  const passed = checks.filter((x) => x.pass).length;
  return {
    caseId: c.id,
    passed,
    total: checks.length,
    score: checks.length ? passed / checks.length : 0,
    toolCalls: record.tools.length,
    verifierVerdict: record.verifier?.verdict,
    checks,
  };
}
