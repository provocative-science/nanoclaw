import fs from 'fs';
import os from 'os';
import path from 'path';

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';

const tmp = vi.hoisted(() => {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { mkdtempSync } = require('fs') as typeof import('fs');
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { join } = require('path') as typeof import('path');
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { tmpdir } = require('os') as typeof import('os');
  return mkdtempSync(join(tmpdir(), 'nanoclaw-kb-'));
});

vi.mock('./config.js', () => ({
  DATA_DIR: `${tmp}/data`,
  GROUPS_DIR: `${tmp}/groups`,
  KNOWLEDGE_DIR: `${tmp}/data/knowledge`,
}));
vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import {
  addEntry,
  handleKbWrite,
  initKnowledgeStore,
  listEntries,
  parseEntry,
  parseReviewCommand,
  readEntry,
  resolveEntry,
  serializeEntry,
  supersedeEntry,
} from './knowledge.js';

const MAIN = { folder: 'telegram_main', isMain: true };
const OTHER = { folder: 'telegram_carbon-capture', isMain: false };
const base = {
  title: 'Bag angle sensor reads 10.0 when unplugged',
  body: 'Seen on airbed.',
  source: 'test',
  tags: ['airbed'],
};

beforeAll(async () => {
  process.env.GHOST_PYTHON_CMD = 'true';
  const notes = path.join(tmp, 'groups', 'global', 'shared', 'notes.md');
  fs.mkdirSync(path.dirname(notes), { recursive: true });
  fs.writeFileSync(notes, '# Notes\nold prose\n');
  await initKnowledgeStore();
});

describe('entry format', () => {
  it('round-trips frontmatter and body', () => {
    const e = {
      id: 'k-20261029-abcdef',
      kind: 'lesson' as const,
      title: 'A: colon "quoted" title',
      tags: ['a'],
      systems: [],
      state: 'active' as const,
      confidence: 'likely' as const,
      author: 'x',
      source: 'y',
      created: '2026-10-29T00:00:00.000Z',
      updated: '2026-10-29T00:00:00.000Z',
      body: 'line 1\n\n---\nline 2',
    };
    expect(parseEntry(serializeEntry(e))).toEqual(e);
  });
});

describe('initKnowledgeStore', () => {
  it('imports seeds and retires notes.md with a kept copy', () => {
    expect(listEntries().length).toBeGreaterThan(10);
    const notes = fs.readFileSync(
      path.join(tmp, 'groups', 'global', 'shared', 'notes.md'),
      'utf-8',
    );
    expect(notes).toContain('Retired');
    const kept = fs
      .readdirSync(path.join(tmp, 'data', 'knowledge'))
      .filter((f) => f.startsWith('notes.md.retired-'));
    expect(kept).toHaveLength(1);
    expect(fs.existsSync(path.join(tmp, 'data', 'knowledge', '.git'))).toBe(
      true,
    );
  });
});

describe('addEntry', () => {
  it('downgrades confirmed from non-main and refuses non-main rules', async () => {
    const r = await addEntry(
      { ...base, kind: 'lesson', confidence: 'confirmed' },
      OTHER,
    );
    expect(r.entry?.confidence).toBe('likely');
    await expect(addEntry({ ...base, kind: 'rule' }, OTHER)).rejects.toThrow(
      /main chat/,
    );
    const rule = await addEntry({ ...base, kind: 'rule' }, MAIN);
    expect(rule.entry?.confidence).toBe('confirmed');
  });

  it('requires tags/source and gives status entries a bounded expiry', async () => {
    await expect(addEntry({ ...base, tags: [] }, OTHER)).rejects.toThrow(/tag/);
    await expect(addEntry({ ...base, source: '' }, OTHER)).rejects.toThrow(
      /source/,
    );
    const now = new Date('2026-10-29T00:00:00Z');
    const s = await addEntry(
      { ...base, kind: 'status', ttl_days: 400 },
      OTHER,
      now,
    );
    expect(s.entry?.ttl_days).toBe(90);
    const d = await addEntry({ ...base, kind: 'status' }, OTHER, now);
    expect(d.entry?.expires).toBe('2026-11-05T00:00:00.000Z');
  });
});

describe('supersede / resolve permissions', () => {
  it('turns a non-main contradiction of a confirmed entry into a review item', async () => {
    const rule = await addEntry(
      { ...base, kind: 'rule', title: 'Always check the console first' },
      MAIN,
    );
    const r = await supersedeEntry(
      rule.id!,
      { ...base, title: 'Console is optional', reason: 'r' },
      OTHER,
    );
    expect(r.entry?.state).toBe('needs_review');
    expect(r.entry?.contradicts).toEqual([rule.id]);
    expect(readEntry(rule.id!)?.state).toBe('active');

    await resolveEntry(r.id!, 'confirm', 'agreed', MAIN);
    expect(readEntry(rule.id!)).toMatchObject({
      state: 'superseded',
      superseded_by: r.id,
    });
    expect(readEntry(r.id!)).toMatchObject({
      state: 'active',
      confidence: 'confirmed',
      supersedes: [rule.id],
    });
  });

  it('lets an author resolve its own unconfirmed entry but queues others', async () => {
    const mine = await addEntry({ ...base, kind: 'note' }, OTHER);
    expect((await resolveEntry(mine.id!, 'retracted', 'wrong', OTHER)).ok).toBe(
      true,
    );
    expect(readEntry(mine.id!)?.state).toBe('retracted');

    const theirs = await addEntry(
      { ...base, kind: 'note' },
      { folder: 'telegram_vinay', isMain: false },
    );
    await resolveEntry(theirs.id!, 'resolved', 'done', OTHER);
    expect(readEntry(theirs.id!)?.state).toBe('needs_review');

    expect((await resolveEntry(theirs.id!, 'confirm', '', MAIN)).ok).toBe(true);
    const e = readEntry(theirs.id!);
    expect(e?.state).toBe('active');
    expect(e?.confidence).toBe('confirmed');
    expect(e?.review_reason).toBeUndefined();
  });
});

describe('handleKbWrite', () => {
  it('writes a kb_result file for the container', async () => {
    await handleKbWrite(
      { op: 'add', requestId: 'req-1', entry: { ...base, kind: 'fact' } },
      'telegram_vinay',
      false,
    );
    const p = path.join(
      tmp,
      'data',
      'ipc',
      'telegram_vinay',
      'input',
      'kb_result_req-1.json',
    );
    const result = JSON.parse(fs.readFileSync(p, 'utf-8'));
    expect(result).toMatchObject({ type: 'kb_result', ok: true });
    expect(result.entry).toBeUndefined();
  });
});

describe('parseReviewCommand', () => {
  it('parses Spark review replies', () => {
    expect(parseReviewCommand('still k-20261029-m07')).toEqual({
      outcome: 'still_true',
      id: 'k-20261029-m07',
      reason: '',
    });
    expect(
      parseReviewCommand('@Ghost Resolved k-20261029-m21 sorbent swapped'),
    ).toEqual({
      outcome: 'resolved',
      id: 'k-20261029-m21',
      reason: 'sorbent swapped',
    });
    expect(parseReviewCommand('retract k-1')?.outcome).toBe('retracted');
    expect(parseReviewCommand('why is k-1 still here')).toBeNull();
  });
});

afterAll(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
  void os;
});
