import fs from 'fs';
import path from 'path';

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

const tmp = vi.hoisted(() => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { mkdtempSync } = require('fs') as typeof import('fs');
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { join } = require('path') as typeof import('path');
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { tmpdir } = require('os') as typeof import('os');
  return mkdtempSync(join(tmpdir(), 'nanoclaw-brief-'));
});

vi.mock('./config.js', () => ({
  DATA_DIR: `${tmp}/data`,
  GHOST_DATA_DIR: `${tmp}/data/ghost`,
  GROUPS_DIR: `${tmp}/groups`,
  KNOWLEDGE_DIR: `${tmp}/data/knowledge`,
}));
vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import { buildBriefing } from './briefing.js';
import { KnowledgeEntry, serializeEntry } from './knowledge.js';

const NOW = Date.parse('2026-10-29T12:00:00Z');

function entry(
  id: string,
  kind: KnowledgeEntry['kind'],
  title: string,
  extra: Partial<KnowledgeEntry> = {},
): void {
  const e: KnowledgeEntry = {
    id,
    kind,
    title,
    tags: ['airbed', 'watchdog'],
    systems: ['airbed'],
    state: 'active',
    confidence: 'likely',
    author: 't',
    source: 't',
    created: '2026-10-01T00:00:00.000Z',
    updated: '2026-10-01T00:00:00.000Z',
    body: `${title} body`,
    ...extra,
  };
  const dir = path.join(tmp, 'data', 'knowledge', 'entries');
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, `${id}.md`), serializeEntry(e));
}

beforeAll(() => {
  entry('k-1-rule', 'rule', 'Times in ET');
  entry('k-2-status', 'status', 'Container Prom down', {
    expires: '2026-11-01T00:00:00.000Z',
  });
  entry('k-3-expired', 'status', 'Old status', {
    expires: '2026-10-01T00:00:00.000Z',
  });
  entry('k-4-lesson', 'lesson', 'Watchdog trips on missing airbed inputs');
  entry('k-5-answer', 'lesson', 'The airbed watchdog answer');
  const alerts = path.join(tmp, 'groups', 'global', 'shared', 'plant-alerts');
  fs.mkdirSync(alerts, { recursive: true });
  fs.writeFileSync(
    path.join(alerts, 'log.jsonl'),
    [
      {
        id: 'a-past',
        fired_at: '2026-10-29T08:00:00Z',
        alert: { subsystem: 'container', kind: 'locked' },
      },
      {
        id: 'a-past',
        recorded_at: '2026-10-29T08:05:00Z',
        brief: 'Container locked brief',
      },
      {
        id: 'a-replayed',
        fired_at: '2026-10-29T11:00:00Z',
        alert: { subsystem: 'airbed', kind: 'locked' },
      },
      {
        id: 'a-future',
        fired_at: '2026-10-29T13:00:00Z',
        alert: { subsystem: 'airbed', kind: 'locked' },
      },
    ]
      .map((r) => JSON.stringify(r))
      .join('\n'),
  );
  fs.mkdirSync(path.join(tmp, 'data', 'ghost'), { recursive: true });
  fs.writeFileSync(
    path.join(tmp, 'data', 'ghost', 'change-digest-brief.md'),
    '## Recent changes\n- deploy x',
  );
});

afterAll(() => fs.rmSync(tmp, { recursive: true, force: true }));

describe('buildBriefing', () => {
  it('compact: rules and live statuses only', () => {
    const b = buildBriefing('why did airbed lock', 'compact', NOW);
    expect(b).toContain('Times in ET');
    expect(b).toContain('Container Prom down');
    expect(b).not.toContain('Old status');
    expect(b).not.toContain('Watchdog trips');
    expect(b).not.toContain('Recent changes');
  });

  it('full: relevant lessons, past alerts, digest', () => {
    const b = buildBriefing('why did the airbed watchdog trip', 'full', NOW);
    expect(b).toContain('Watchdog trips on missing airbed inputs');
    expect(b).toContain('a-past');
    expect(b).toContain('Container locked brief');
    expect(b).not.toContain('a-future');
    expect(b).toContain('### Recent changes');
  });

  it('replay options hide the answer, the replayed alert and the digest', () => {
    const b = buildBriefing('why did the airbed watchdog trip', 'full', {
      now: NOW,
      includeDigest: false,
      excludeEntryIds: ['k-5-answer'],
      excludeAlertIds: ['a-replayed'],
    });
    expect(b).not.toContain('The airbed watchdog answer');
    expect(b).not.toContain('a-replayed');
    expect(b).not.toContain('Recent changes');
  });
});
