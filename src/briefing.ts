/**
 * Per-run briefing prepended to agent prompts: standing rules, live statuses,
 * relevant lessons from the knowledge store, recent plant alerts and the
 * host-generated change digest. Built on the host so every investigation
 * starts from the same verified context instead of whatever the agent
 * remembers to look up.
 */
import fs from 'fs';
import path from 'path';

import { GHOST_DATA_DIR, GROUPS_DIR } from './config.js';
import { KnowledgeEntry, listEntries } from './knowledge.js';
import { logger } from './logger.js';

export type BriefingLevel = 'compact' | 'full';

export interface BriefingOptions {
  now?: number;
  includeDigest?: boolean;
  /** Replay evals: directory holding a digest rebuilt as of the replayed moment. */
  ghostDataDir?: string;
  /** Replay evals: knowledge entries that encode the expected answer. */
  excludeEntryIds?: string[];
  /** Replay evals: the alert being replayed (its archived brief is the old answer). */
  excludeAlertIds?: string[];
}

const MAX_FULL_CHARS = 9_000;
const MAX_COMPACT_CHARS = 2_500;
const EVENT_WINDOW_MS = 72 * 3_600_000;
const DIGEST_MAX_AGE_MS = 6 * 3_600_000;

const STOP = new Set(
  'the a an and or of to in on at for is are was were be it this that with from by as what why how when did does do i you we our'.split(
    ' ',
  ),
);

function tokens(text: string): Set<string> {
  return new Set(
    (text || '')
      .toLowerCase()
      .split(/[^a-z0-9_]+/)
      .flatMap((t) => (t.includes('_') ? [t, ...t.split('_')] : [t]))
      .filter((t) => t.length > 2 && !STOP.has(t)),
  );
}

function relevance(entry: KnowledgeEntry, promptTokens: Set<string>): number {
  let score = 0;
  for (const tag of [...entry.tags, ...entry.systems]) {
    for (const part of tag.split(/[-_.]/))
      if (promptTokens.has(part)) score += 3;
    if (promptTokens.has(tag)) score += 2;
  }
  for (const t of tokens(entry.title)) if (promptTokens.has(t)) score += 1;
  return score;
}

function isLive(entry: KnowledgeEntry, now: number): boolean {
  if (entry.state !== 'active') return false;
  if (
    entry.kind === 'status' &&
    entry.expires &&
    Date.parse(entry.expires) < now
  )
    return false;
  return true;
}

function firstLine(body: string, max = 220): string {
  const line = body.split('\n').find((l) => l.trim()) || '';
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

function statusLine(e: KnowledgeEntry): string {
  const check = e.last_check
    ? ` — check ${e.last_check.result} ${e.last_check.at.slice(0, 16)}`
    : e.check
      ? ' — check pending'
      : '';
  return `- [${e.id}] ${e.title} (expires ${e.expires?.slice(0, 10)}, ${e.confidence}${check})`;
}

function recentPlantEvents(now: number, excludeIds: Set<string>): string[] {
  const logPath = path.join(
    GROUPS_DIR,
    'global',
    'shared',
    'plant-alerts',
    'log.jsonl',
  );
  if (!fs.existsSync(logPath)) return [];
  const lines = fs
    .readFileSync(logPath, 'utf-8')
    .trim()
    .split('\n')
    .slice(-200);
  const byId = new Map<
    string,
    { fired?: string; alert?: string; brief?: string }
  >();
  for (const line of lines) {
    try {
      const rec = JSON.parse(line);
      const when = Date.parse(rec.fired_at || rec.recorded_at || '');
      if (!when || when > now || now - when > EVENT_WINDOW_MS) continue;
      if (excludeIds.has(rec.id)) continue;
      const item = byId.get(rec.id) || {};
      if (rec.alert) {
        item.fired = rec.fired_at || rec.alert.fired_at;
        item.alert = [rec.alert.subsystem, rec.alert.kind, rec.alert.phase]
          .filter(Boolean)
          .join(' ');
      }
      if (rec.brief) item.brief = firstLine(String(rec.brief), 260);
      byId.set(rec.id, item);
    } catch {
      continue;
    }
  }
  return [...byId.entries()]
    .slice(-8)
    .map(
      ([id, v]) =>
        `- ${v.fired?.slice(0, 16) ?? '?'} ${v.alert ?? ''} (${id})${v.brief ? ` — Ghost said: ${v.brief}` : ''}`,
    );
}

function changeDigest(now: number, dir = GHOST_DATA_DIR): string | null {
  const p = path.join(dir, 'change-digest-brief.md');
  if (!fs.existsSync(p)) return null;
  const age = now - fs.statSync(p).mtimeMs;
  const text = fs.readFileSync(p, 'utf-8').trim();
  if (age > DIGEST_MAX_AGE_MS) {
    return `${text}\n(digest is ${Math.round(age / 3_600_000)}h old — host job may be down)`;
  }
  return text;
}

function cap(text: string, max: number): string {
  return text.length <= max
    ? text
    : `${text.slice(0, max - 40)}\n…(briefing truncated)`;
}

export function buildBriefing(
  promptText: string,
  level: BriefingLevel,
  options: BriefingOptions | number = {},
): string {
  const opts: BriefingOptions =
    typeof options === 'number' ? { now: options } : options;
  const now = opts.now ?? Date.now();
  const excluded = new Set(opts.excludeEntryIds ?? []);
  let entries: KnowledgeEntry[] = [];
  try {
    entries = listEntries().filter((e) => !excluded.has(e.id));
  } catch (err) {
    logger.warn({ err }, 'Briefing: knowledge store unreadable');
  }
  const live = entries.filter((e) => isLive(e, now));
  const rules = live.filter((e) => e.kind === 'rule');
  const statuses = live
    .filter((e) => e.kind === 'status')
    .sort((a, b) => (b.updated || '').localeCompare(a.updated || ''));

  const parts: string[] = [];
  if (rules.length) {
    parts.push(
      '### Standing rules (from Spark)',
      ...rules
        .slice(0, 15)
        .map((e) => `- [${e.id}] ${e.title}: ${firstLine(e.body, 300)}`),
    );
  }
  if (statuses.length) {
    parts.push(
      '### Current plant statuses (time-bound; verify before relying on them)',
      ...statuses.slice(0, 12).map(statusLine),
    );
  }

  if (level === 'full') {
    const promptTokens = tokens(promptText);
    const scored = live
      .filter((e) => e.kind !== 'rule' && e.kind !== 'status')
      .map((e) => ({
        e,
        s:
          relevance(e, promptTokens) +
          (e.kind === 'correction' || e.kind === 'lesson' ? 1 : 0),
      }))
      .filter((x) => x.s > 1)
      .sort((a, b) => b.s - a.s)
      .slice(0, 10);
    if (scored.length) {
      parts.push(
        '### Relevant lessons, corrections and facts',
        ...scored.map(
          ({ e }) =>
            `- [${e.id}] (${e.kind}, ${e.confidence}) ${e.title}: ${firstLine(e.body)}`,
        ),
      );
    }
    const events = recentPlantEvents(now, new Set(opts.excludeAlertIds ?? []));
    if (events.length)
      parts.push('### Plant alerts in the last 72h', ...events);
    const digest =
      opts.includeDigest === false
        ? null
        : changeDigest(now, opts.ghostDataDir);
    if (digest) parts.push(digest.replace(/^## /m, '### '));
    const review = entries.filter((e) => e.state === 'needs_review').length;
    if (review)
      parts.push(`(${review} knowledge entries await Spark's review)`);
  }
  if (!parts.length) return '';
  const header =
    level === 'full'
      ? '## Ghost briefing (host-generated; knowledge store ids in brackets — use kb_get for detail, kb_add to record what you learn)'
      : '## Ghost briefing (compact)';
  return cap(
    `<briefing>\n${header}\n\n${parts.join('\n')}\n</briefing>\n\n`,
    level === 'full' ? MAX_FULL_CHARS : MAX_COMPACT_CHARS,
  );
}
