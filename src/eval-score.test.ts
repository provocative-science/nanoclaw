import { describe, expect, it } from 'vitest';

import { EvalCase, scoreRun } from './eval-score.js';

const c: EvalCase = {
  id: 't',
  kind: 'chat',
  description: '',
  cutoff: '2026-10-29T12:00:00Z',
  ground_truth: '',
  expect: {
    must_mention: [
      { name: 'watchdog', pattern: 'extraction[_ ]bag[_ ]watchdog' },
    ],
    must_not: [
      {
        name: 'lock as cause',
        pattern: 'root cause[^\\n]{0,40}supervisor lock',
      },
    ],
    must_call: [{ name: 'why_locked', pattern: 'mcp__telemetry__why_locked' }],
    evidence: [{ name: 'modbus', pattern: 'MemoryParityError' }],
    contract: true,
    max_tool_calls: 3,
  },
};

describe('scoreRun', () => {
  it('passes a well-evidenced answer', () => {
    const s = scoreRun(c, {
      draft: '',
      final:
        'extraction_bag_watchdog fired on missing inputs.\n• Validated: Modbus dropout — why_locked → MemoryParityError\n• Ruled out: bag angle',
      tools: [
        {
          tool: 'mcp__telemetry__why_locked',
          input: '{}',
          output: 'Modbus(Device(MemoryParityError))',
        },
      ],
    });
    expect(s.passed).toBe(s.total);
    expect(s.checks.find((x) => x.group === 'evidence')?.detail).toBe(
      'retrieved and cited',
    );
  });

  it('fails a confident story without evidence', () => {
    const s = scoreRun(c, {
      draft: '',
      final:
        'Root cause: the software supervisor lock. extraction_bag_watchdog.',
      tools: [],
    });
    const failed = s.checks.filter((x) => !x.pass).map((x) => x.group);
    expect(failed).toEqual(
      expect.arrayContaining(['must_not', 'call', 'evidence', 'contract']),
    );
    expect(s.checks.find((x) => x.group === 'mention')?.pass).toBe(true);
  });

  it('falls back to the draft and enforces the tool budget', () => {
    const tools = Array.from({ length: 4 }, () => ({
      tool: 'Bash',
      input: '',
      output: '',
    }));
    const s = scoreRun(c, {
      draft: 'extraction bag watchdog',
      final: '',
      tools,
    });
    expect(s.checks.find((x) => x.group === 'mention')?.pass).toBe(true);
    expect(s.checks.find((x) => x.group === 'budget')?.pass).toBe(false);
  });
});
