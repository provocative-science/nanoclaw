/**
 * Cross-agent knowledge store (replaces groups/global/shared/notes.md).
 *
 * Entries are markdown files with a frontmatter envelope (one `key: <JSON>`
 * line per field, so both this module and scripts/ghost/kb.py parse them
 * without a YAML dependency). The directory is its own git repo; the host is
 * the only writer — agents read it read-only at /workspace/knowledge and
 * write through the `kb_write` IPC task, so authorship is the verified IPC
 * source folder, not a claim made inside the container.
 */
import { execFile } from 'child_process';
import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import { promisify } from 'util';

import { DATA_DIR, GROUPS_DIR, KNOWLEDGE_DIR } from './config.js';
import { logger } from './logger.js';

const execFileAsync = promisify(execFile);

export const KNOWLEDGE_KINDS = [
  'rule',
  'status',
  'lesson',
  'correction',
  'fact',
  'note',
] as const;
export type KnowledgeKind = (typeof KNOWLEDGE_KINDS)[number];

export const KNOWLEDGE_STATES = [
  'active',
  'superseded',
  'resolved',
  'retracted',
  'needs_review',
] as const;
export type KnowledgeState = (typeof KNOWLEDGE_STATES)[number];

export const CONFIDENCES = ['confirmed', 'likely', 'unverified'] as const;
export type Confidence = (typeof CONFIDENCES)[number];

export const CHECK_TYPES = ['stream_fresh', 's3_sql', 'prom'] as const;

export interface StatusCheck {
  type: (typeof CHECK_TYPES)[number];
  /** stream_fresh: host + max_age_hours; true when newest object is younger. */
  host?: string;
  stream?: string;
  max_age_hours?: number;
  /** s3_sql / prom: query returning one scalar, compared with op/value. */
  sql?: string;
  query?: string;
  op?: '<' | '<=' | '>' | '>=' | '==' | '!=';
  value?: number | string | boolean;
  /** The status text is true while the check result equals this. */
  holds_when?: boolean;
}

export interface CheckResult {
  at: string;
  result: 'holds' | 'no_longer_holds' | 'error';
  observed?: unknown;
  detail?: string;
}

export interface KnowledgeEntry {
  id: string;
  kind: KnowledgeKind;
  title: string;
  tags: string[];
  systems: string[];
  state: KnowledgeState;
  confidence: Confidence;
  author: string;
  source: string;
  created: string;
  updated: string;
  expires?: string;
  ttl_days?: number;
  check?: StatusCheck;
  last_check?: CheckResult;
  supersedes?: string[];
  superseded_by?: string;
  contradicts?: string[];
  evidence?: string[];
  resolution?: string;
  review_reason?: string;
  body: string;
}

const FIELD_ORDER: (keyof KnowledgeEntry)[] = [
  'id',
  'kind',
  'title',
  'tags',
  'systems',
  'state',
  'confidence',
  'author',
  'source',
  'created',
  'updated',
  'expires',
  'ttl_days',
  'check',
  'last_check',
  'supersedes',
  'superseded_by',
  'contradicts',
  'evidence',
  'resolution',
  'review_reason',
];

const ENTRIES_DIR = () => path.join(KNOWLEDGE_DIR, 'entries');
const SEED_DIR = path.join(process.cwd(), 'scripts', 'ghost', 'knowledge-seed');
export const MAX_STATUS_TTL_DAYS = 90;
const DEFAULT_STATUS_TTL_DAYS = 7;

export function serializeEntry(entry: KnowledgeEntry): string {
  const lines = ['---'];
  for (const key of FIELD_ORDER) {
    const value = entry[key];
    if (value === undefined || value === null) continue;
    if (Array.isArray(value) && value.length === 0 && key !== 'tags') continue;
    lines.push(`${key}: ${JSON.stringify(value)}`);
  }
  lines.push('---', '', entry.body.trim(), '');
  return lines.join('\n');
}

export function parseEntry(text: string): KnowledgeEntry {
  const match = text.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/);
  if (!match) throw new Error('missing frontmatter');
  const fields: Record<string, unknown> = {};
  for (const line of match[1].split('\n')) {
    if (!line.trim()) continue;
    const idx = line.indexOf(':');
    if (idx < 0) throw new Error(`bad frontmatter line: ${line}`);
    const key = line.slice(0, idx).trim();
    const raw = line.slice(idx + 1).trim();
    try {
      fields[key] = JSON.parse(raw);
    } catch {
      fields[key] = raw;
    }
  }
  const entry = {
    ...(fields as Omit<KnowledgeEntry, 'body'>),
    body: match[2].trim(),
  };
  if (!Array.isArray(entry.tags)) entry.tags = [];
  if (!Array.isArray(entry.systems)) entry.systems = [];
  return entry;
}

export function newEntryId(now = new Date()): string {
  const day = now.toISOString().slice(0, 10).replace(/-/g, '');
  return `k-${day}-${crypto.randomBytes(3).toString('hex')}`;
}

function entryPath(id: string): string {
  if (!/^k-[\w-]+$/.test(id)) throw new Error(`invalid entry id: ${id}`);
  return path.join(ENTRIES_DIR(), `${id}.md`);
}

export function readEntry(id: string): KnowledgeEntry | null {
  const p = entryPath(id);
  if (!fs.existsSync(p)) return null;
  return parseEntry(fs.readFileSync(p, 'utf-8'));
}

export function listEntries(): KnowledgeEntry[] {
  const dir = ENTRIES_DIR();
  if (!fs.existsSync(dir)) return [];
  const out: KnowledgeEntry[] = [];
  for (const file of fs.readdirSync(dir)) {
    if (!file.endsWith('.md')) continue;
    try {
      out.push(parseEntry(fs.readFileSync(path.join(dir, file), 'utf-8')));
    } catch (err) {
      logger.warn({ file, err }, 'Skipping unparseable knowledge entry');
    }
  }
  return out;
}

function writeEntryFile(entry: KnowledgeEntry): void {
  fs.mkdirSync(ENTRIES_DIR(), { recursive: true });
  const p = entryPath(entry.id);
  const tmp = `${p}.tmp`;
  fs.writeFileSync(tmp, serializeEntry(entry));
  fs.renameSync(tmp, p);
}

// ---------------------------------------------------------------------------
// Git (serialized; the Python recheck job retries on index.lock)
// ---------------------------------------------------------------------------

let gitChain: Promise<unknown> = Promise.resolve();

async function git(...args: string[]): Promise<string> {
  const { stdout } = await execFileAsync(
    'git',
    ['-C', KNOWLEDGE_DIR, ...args],
    {
      timeout: 30_000,
    },
  );
  return stdout;
}

export function commitKnowledge(message: string): Promise<void> {
  const run = async () => {
    for (let attempt = 0; attempt < 5; attempt++) {
      try {
        await git('add', '-A');
        const status = await git('status', '--porcelain');
        if (!status.trim()) return;
        await git('commit', '-q', '-m', message);
        return;
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        if (!msg.includes('index.lock') || attempt === 4) {
          logger.warn({ err: msg }, 'Knowledge store commit failed');
          return;
        }
        await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
      }
    }
  };
  const next = gitChain.then(run, run);
  gitChain = next;
  return next;
}

const README = `# NanoClaw knowledge store

Written only by the NanoClaw host (agents use the kb_* MCP tools). Each file in
entries/ is one entry: a frontmatter envelope (one \`key: <JSON>\` line per
field) and a markdown body. Every change is a git commit, so history is
\`git log -p entries/<id>.md\`.

Kinds: rule (standing instruction from Spark), status (time-bound state of the
plant, must expire; optional machine check), lesson (diagnostic lesson),
correction (something an agent got wrong), fact (stable reference), note
(anything else).
`;

/** Create the repo, import seed entries, retire notes.md once migrated. */
export async function initKnowledgeStore(): Promise<void> {
  fs.mkdirSync(ENTRIES_DIR(), { recursive: true });
  if (!fs.existsSync(path.join(KNOWLEDGE_DIR, '.git'))) {
    await git('init', '-q');
    await git('config', 'user.name', 'NanoClaw knowledge');
    await git('config', 'user.email', 'knowledge@nanoclaw.local');
    fs.writeFileSync(path.join(KNOWLEDGE_DIR, 'README.md'), README);
    logger.info({ dir: KNOWLEDGE_DIR }, 'Knowledge store initialized');
  }
  let imported = 0;
  if (fs.existsSync(SEED_DIR)) {
    for (const file of fs.readdirSync(SEED_DIR)) {
      if (!file.endsWith('.md')) continue;
      const entry = parseEntry(
        fs.readFileSync(path.join(SEED_DIR, file), 'utf-8'),
      );
      if (fs.existsSync(entryPath(entry.id))) continue;
      writeEntryFile(entry);
      imported++;
    }
  }
  if (imported > 0) {
    await commitKnowledge(
      `kb: import ${imported} seed entries (notes.md migration)`,
    );
    logger.info({ imported }, 'Knowledge seed entries imported');
  }
  const notes = path.join(GROUPS_DIR, 'global', 'shared', 'notes.md');
  const marker = path.join(KNOWLEDGE_DIR, '.notes-md-retired');
  if (
    fs.existsSync(notes) &&
    fs.existsSync(SEED_DIR) &&
    !fs.existsSync(marker)
  ) {
    const retired = path.join(
      KNOWLEDGE_DIR,
      `notes.md.retired-${new Date().toISOString().slice(0, 10)}`,
    );
    fs.copyFileSync(notes, retired);
    fs.writeFileSync(
      notes,
      '# Retired\n\nShared notes moved to the knowledge store. Use the kb_search / kb_add tools ' +
        '(see "Knowledge store" in /workspace/global/CLAUDE.md). Do not add notes here.\n',
    );
    fs.writeFileSync(marker, new Date().toISOString() + '\n');
    await commitKnowledge('kb: retire notes.md (copy kept in repo)');
    logger.info({ retired }, 'notes.md retired into knowledge store');
  }
}

// ---------------------------------------------------------------------------
// Validation + operations (called from the kb_write IPC task)
// ---------------------------------------------------------------------------

export interface KbActor {
  folder: string;
  isMain: boolean;
}

export interface KbResult {
  ok: boolean;
  id?: string;
  message: string;
  entry?: KnowledgeEntry;
}

const TAG_RE = /^[a-z0-9][a-z0-9_.-]{0,40}$/;

function cleanTags(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  return [
    ...new Set(
      raw
        .map((t) => String(t).trim().toLowerCase().replace(/\s+/g, '-'))
        .filter((t) => TAG_RE.test(t)),
    ),
  ].slice(0, 12);
}

function asString(v: unknown, max: number): string {
  return typeof v === 'string' ? v.trim().slice(0, max) : '';
}

function validateCheck(raw: unknown): StatusCheck | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const c = raw as Record<string, unknown>;
  if (!CHECK_TYPES.includes(c.type as StatusCheck['type'])) {
    throw new Error(`check.type must be one of ${CHECK_TYPES.join(', ')}`);
  }
  const check: StatusCheck = { type: c.type as StatusCheck['type'] };
  if (check.type === 'stream_fresh') {
    check.host = asString(c.host, 80);
    check.stream = asString(c.stream, 40) || 'telemetry';
    check.max_age_hours = Number(c.max_age_hours ?? 3);
    if (!check.host) throw new Error('stream_fresh check needs host');
  } else {
    if (check.type === 's3_sql') check.sql = asString(c.sql, 4000);
    if (check.type === 'prom') check.query = asString(c.query, 2000);
    if (!check.sql && !check.query)
      throw new Error(`${check.type} check needs sql/query`);
    check.op = (
      ['<', '<=', '>', '>=', '==', '!='].includes(String(c.op)) ? c.op : '=='
    ) as StatusCheck['op'];
    check.value = c.value as StatusCheck['value'];
  }
  check.holds_when = c.holds_when === undefined ? true : Boolean(c.holds_when);
  return check;
}

function buildEntry(
  input: Record<string, unknown>,
  actor: KbActor,
  now: Date,
  /** A non-main contradiction of a confirmed entry: may be a rule, goes to review. */
  proposal = false,
): KnowledgeEntry {
  const kind = (
    KNOWLEDGE_KINDS.includes(input.kind as KnowledgeKind) ? input.kind : 'note'
  ) as KnowledgeKind;
  const title = asString(input.title, 160);
  const body = asString(input.body, 8000);
  const source = asString(input.source, 400);
  const tags = cleanTags(input.tags);
  if (title.length < 5) throw new Error('title is required (5-160 chars)');
  if (!body) throw new Error('body is required');
  if (!source)
    throw new Error(
      'source is required (who/what this came from: chat, alert id, query)',
    );
  if (tags.length === 0) throw new Error('at least one tag is required');
  if (kind === 'rule' && !actor.isMain && !proposal) {
    throw new Error(
      'only the main chat (Spark) can add standing rules; use kind "lesson" or "note"',
    );
  }
  let confidence = (
    CONFIDENCES.includes(input.confidence as Confidence)
      ? input.confidence
      : 'unverified'
  ) as Confidence;
  if (confidence === 'confirmed' && !actor.isMain) confidence = 'likely';
  if (kind === 'rule' && actor.isMain) confidence = 'confirmed';

  const entry: KnowledgeEntry = {
    id: newEntryId(now),
    kind,
    title,
    tags,
    systems: cleanTags(input.systems),
    state: 'active',
    confidence,
    author: actor.folder,
    source,
    created: now.toISOString(),
    updated: now.toISOString(),
    body,
  };
  const evidence = Array.isArray(input.evidence)
    ? input.evidence.map((e) => String(e).slice(0, 600)).slice(0, 12)
    : [];
  if (evidence.length) entry.evidence = evidence;

  if (kind === 'status') {
    let ttl = Number(input.ttl_days ?? DEFAULT_STATUS_TTL_DAYS);
    if (input.expires) {
      const exp = new Date(String(input.expires));
      if (isNaN(exp.getTime())) throw new Error('expires is not a valid date');
      ttl = (exp.getTime() - now.getTime()) / 86_400_000;
    }
    if (!(ttl > 0)) throw new Error('status entries must expire in the future');
    ttl = Math.min(ttl, MAX_STATUS_TTL_DAYS);
    entry.ttl_days = Math.round(ttl * 10) / 10;
    entry.expires = new Date(now.getTime() + ttl * 86_400_000).toISOString();
    entry.check = validateCheck(input.check);
  }
  return entry;
}

function checkCommandArgs(): string[] {
  const raw =
    process.env.GHOST_PYTHON_CMD || 'uvx --with duckdb --with boto3 python';
  return raw.split(/\s+/).filter(Boolean);
}

/** Run scripts/ghost/kb_check.py for one entry (host-side; records last_check). */
export async function runStatusCheck(id: string): Promise<void> {
  const [cmd, ...args] = checkCommandArgs();
  try {
    await execFileAsync(
      cmd,
      [
        ...args,
        path.join(process.cwd(), 'scripts', 'ghost', 'kb_check.py'),
        '--entry',
        id,
        '--record',
      ],
      {
        timeout: 180_000,
        env: { ...process.env, NANOCLAW_ROOT: process.cwd() },
      },
    );
  } catch (err) {
    logger.warn(
      { id, err: err instanceof Error ? err.message : String(err) },
      'Status check run failed',
    );
  }
}

export async function addEntry(
  input: Record<string, unknown>,
  actor: KbActor,
  now = new Date(),
): Promise<KbResult> {
  const entry = buildEntry(input, actor, now);
  writeEntryFile(entry);
  await commitKnowledge(
    `kb: add ${entry.id} [${entry.kind}] ${entry.title} (by ${actor.folder})`,
  );
  if (entry.check) void runStatusCheck(entry.id);
  return {
    ok: true,
    id: entry.id,
    message:
      entry.confidence !== (input.confidence ?? 'unverified')
        ? `added ${entry.id} (confidence set to ${entry.confidence}; only the main chat can confirm)`
        : `added ${entry.id}`,
    entry,
  };
}

export async function supersedeEntry(
  oldId: string,
  input: Record<string, unknown>,
  actor: KbActor,
  now = new Date(),
): Promise<KbResult> {
  const old = readEntry(oldId);
  if (!old) return { ok: false, message: `no entry ${oldId}` };
  const contradiction = old.confidence === 'confirmed' && !actor.isMain;
  const replacement = buildEntry(
    { kind: old.kind, tags: old.tags, systems: old.systems, ...input },
    actor,
    now,
    contradiction,
  );
  const reason = asString(input.reason, 400);
  if (contradiction) {
    replacement.contradicts = [old.id];
    replacement.state = 'needs_review';
    replacement.review_reason = `contradicts confirmed ${old.id}${reason ? `: ${reason}` : ''}`;
    old.contradicts = [
      ...new Set([...(old.contradicts || []), replacement.id]),
    ];
    old.updated = now.toISOString();
    writeEntryFile(old);
    writeEntryFile(replacement);
    await commitKnowledge(
      `kb: ${replacement.id} contradicts confirmed ${old.id} (by ${actor.folder})`,
    );
    return {
      ok: true,
      id: replacement.id,
      message: `${old.id} is confirmed, so ${replacement.id} was recorded as a contradiction for Spark's review; ${old.id} stays active until then`,
      entry: replacement,
    };
  }
  replacement.supersedes = [old.id];
  old.state = 'superseded';
  old.superseded_by = replacement.id;
  old.updated = now.toISOString();
  if (reason) old.resolution = reason;
  writeEntryFile(replacement);
  writeEntryFile(old);
  await commitKnowledge(
    `kb: ${replacement.id} supersedes ${old.id} (by ${actor.folder})`,
  );
  if (replacement.check) void runStatusCheck(replacement.id);
  return {
    ok: true,
    id: replacement.id,
    message: `${replacement.id} supersedes ${old.id}`,
    entry: replacement,
  };
}

export type ResolveOutcome =
  | 'resolved'
  | 'retracted'
  | 'still_true'
  | 'confirm';

export async function resolveEntry(
  id: string,
  outcome: ResolveOutcome,
  reason: string,
  actor: KbActor,
  now = new Date(),
): Promise<KbResult> {
  const entry = readEntry(id);
  if (!entry) return { ok: false, message: `no entry ${id}` };
  const own = entry.author === actor.folder;
  const note = reason.trim().slice(0, 400);
  if (
    !actor.isMain &&
    !(own && entry.confidence !== 'confirmed' && outcome !== 'confirm')
  ) {
    entry.state = 'needs_review';
    entry.review_reason = `${actor.folder} proposes "${outcome}"${note ? `: ${note}` : ''}`;
    entry.updated = now.toISOString();
    writeEntryFile(entry);
    await commitKnowledge(
      `kb: ${id} flagged for review (${outcome} proposed by ${actor.folder})`,
    );
    return {
      ok: true,
      id,
      message: `${id} queued for Spark's review (${outcome} proposed)`,
    };
  }
  switch (outcome) {
    case 'resolved':
    case 'retracted':
      entry.state = outcome;
      entry.resolution = `${outcome} by ${actor.folder} ${now.toISOString().slice(0, 10)}${note ? `: ${note}` : ''}`;
      break;
    case 'still_true': {
      entry.state = 'active';
      if (entry.kind === 'status') {
        const ttl = entry.ttl_days ?? DEFAULT_STATUS_TTL_DAYS;
        entry.expires = new Date(
          now.getTime() + ttl * 86_400_000,
        ).toISOString();
      }
      break;
    }
    case 'confirm':
      if (!actor.isMain)
        return { ok: false, message: 'only the main chat can confirm entries' };
      entry.state = 'active';
      entry.confidence = 'confirmed';
      for (const oldId of entry.contradicts ?? []) {
        const old = readEntry(oldId);
        if (!old || old.state !== 'active') continue;
        old.state = 'superseded';
        old.superseded_by = entry.id;
        old.updated = now.toISOString();
        writeEntryFile(old);
        entry.supersedes = [...new Set([...(entry.supersedes ?? []), oldId])];
      }
      break;
  }
  delete entry.review_reason;
  entry.updated = now.toISOString();
  writeEntryFile(entry);
  await commitKnowledge(`kb: ${id} ${outcome} (by ${actor.folder})`);
  return {
    ok: true,
    id,
    message: `${id} → ${outcome === 'still_true' ? `still true (expires ${entry.expires?.slice(0, 10) ?? 'n/a'})` : outcome}`,
  };
}

/** IPC entry point: `{type:'kb_write', op, requestId, ...}` from a container. */
export async function handleKbWrite(
  data: Record<string, unknown>,
  sourceGroup: string,
  isMain: boolean,
): Promise<void> {
  const actor: KbActor = { folder: sourceGroup, isMain };
  const requestId =
    asString(data.requestId, 80).replace(/[^\w-]/g, '') || String(Date.now());
  let result: KbResult;
  try {
    const op = String(data.op || '');
    const payload = (
      data.entry && typeof data.entry === 'object' ? data.entry : {}
    ) as Record<string, unknown>;
    if (op === 'add') result = await addEntry(payload, actor);
    else if (op === 'supersede')
      result = await supersedeEntry(
        asString(data.id, 80),
        { ...payload, reason: data.reason },
        actor,
      );
    else if (op === 'resolve')
      result = await resolveEntry(
        asString(data.id, 80),
        String(data.outcome) as ResolveOutcome,
        asString(data.reason, 400),
        actor,
      );
    else result = { ok: false, message: `unknown kb op "${op}"` };
  } catch (err) {
    result = {
      ok: false,
      message: err instanceof Error ? err.message : String(err),
    };
  }
  if (result.entry) delete result.entry;
  logger.info(
    { sourceGroup, op: data.op, ok: result.ok, id: result.id },
    `kb_write: ${result.message}`,
  );
  const inputDir = path.join(DATA_DIR, 'ipc', sourceGroup, 'input');
  try {
    fs.mkdirSync(inputDir, { recursive: true });
    const p = path.join(inputDir, `kb_result_${requestId}.json`);
    fs.writeFileSync(
      `${p}.tmp`,
      JSON.stringify({ type: 'kb_result', requestId, ...result }),
    );
    fs.renameSync(`${p}.tmp`, p);
  } catch (err) {
    logger.warn({ err, sourceGroup }, 'Failed to write kb_result');
  }
}

// ---------------------------------------------------------------------------
// Spark's review commands (main chat only, no trigger needed)
// ---------------------------------------------------------------------------

const REVIEW_RE =
  /^\s*(still|resolved|resolve|confirm|retract)\s+(k-[\w-]+)\b\s*(.*)$/i;

export function parseReviewCommand(
  text: string,
): { outcome: ResolveOutcome; id: string; reason: string } | null {
  const cleaned = (text || '').replace(/@\w[\w_]*/g, ' ').trim();
  const m = cleaned.match(REVIEW_RE);
  if (!m) return null;
  const verb = m[1].toLowerCase();
  const outcome: ResolveOutcome =
    verb === 'still'
      ? 'still_true'
      : verb === 'confirm'
        ? 'confirm'
        : verb === 'retract'
          ? 'retracted'
          : 'resolved';
  return { outcome, id: m[2].toLowerCase(), reason: m[3] || '' };
}
