/**
 * Operator-declared plant modes, enforced by scripts/alert-monitor (plant_modes.py).
 *
 * File: data/alert-monitor/plant-modes.json. While a subsystem is offline, in
 * maintenance or muted, the monitor records alert events but starts no agent
 * run; muted liquefaction HAZOP bits are dropped from edge detection.
 * Written from chat ("@Ghost mute container alerts for 12h",
 * "@Ghost container offline") and from Ghost's set_plant_mode tool.
 */
import fs from 'fs';
import path from 'path';

import { DATA_DIR } from './config.js';
import { logger } from './logger.js';
import type { NewMessage } from './types.js';

export const PLANT_MODES_FILE = path.join(
  DATA_DIR,
  'alert-monitor',
  'plant-modes.json',
);

export const PLANT_SUBSYSTEMS = [
  'liquefaction',
  'container',
  'airbed',
] as const;
export type PlantSubsystem = (typeof PLANT_SUBSYSTEMS)[number];
export type PlantMode = 'offline' | 'maintenance' | 'muted';
export type PlantModeRequestMode = PlantMode | 'in_service';

export interface SubsystemMode {
  mode: PlantMode;
  until: string | null;
  reason: string;
  set_by: string;
  set_at: string;
  chat_jid: string | null;
  notify: 'none' | 'line';
}

export interface MutedBit {
  subsystem: PlantSubsystem;
  severity: 'warning' | 'error';
  bit: number;
  until: string | null;
  reason: string;
  set_by: string;
  set_at: string;
}

export interface PlantModes {
  subsystems: Partial<Record<PlantSubsystem, SubsystemMode>>;
  muted_bits: MutedBit[];
}

export interface PlantModeRequest {
  subsystem: string;
  mode: string;
  /** ISO timestamp or duration like "90m", "12h", "3d". */
  until?: string | null;
  reason?: string;
  setBy: string;
  chatJid?: string | null;
  notify?: 'none' | 'line';
  bits?: { severity: 'warning' | 'error'; bits: number[] };
}

export interface PlantModeResult {
  ok: boolean;
  message: string;
}

const MAX_WINDOW_MS = 90 * 86_400_000;
const DEFAULT_MAINTENANCE_MS = 4 * 3_600_000;
const MODE_LABEL: Record<PlantMode, string> = {
  offline: 'OFFLINE',
  maintenance: 'IN MAINTENANCE',
  muted: 'MUTED',
};

export function readPlantModes(file = PLANT_MODES_FILE): PlantModes {
  try {
    const raw = JSON.parse(
      fs.readFileSync(file, 'utf-8'),
    ) as Partial<PlantModes>;
    return {
      subsystems:
        raw.subsystems && typeof raw.subsystems === 'object'
          ? raw.subsystems
          : {},
      muted_bits: Array.isArray(raw.muted_bits) ? raw.muted_bits : [],
    };
  } catch {
    return { subsystems: {}, muted_bits: [] };
  }
}

function writePlantModes(modes: PlantModes, file = PLANT_MODES_FILE): void {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const tmp = `${file}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(modes, null, 2)}\n`, 'utf-8');
  fs.renameSync(tmp, file);
}

function normalizeSubsystem(raw: string): PlantSubsystem | null {
  const s = raw.trim().toLowerCase();
  if (/^liqu[ei]faction$|^liq$/.test(s)) return 'liquefaction';
  if (/^(container|bop|dac|dacs|pink container)$/.test(s)) return 'container';
  if (s === 'airbed') return 'airbed';
  return null;
}

const DURATION_RE =
  /^(\d+(?:\.\d+)?)\s*(m|mins?|minutes?|h|hrs?|hours?|d|days?)$/i;

export function parseDurationMs(raw: string): number | null {
  const m = raw.trim().match(DURATION_RE);
  if (!m) return null;
  const n = Number(m[1]);
  const unit = m[2][0].toLowerCase();
  return n * (unit === 'm' ? 60_000 : unit === 'h' ? 3_600_000 : 86_400_000);
}

/** ISO timestamp or duration → epoch ms; null = no end; Error on bad input. */
function resolveUntil(
  raw: string | null | undefined,
  now: number,
): number | null {
  if (raw === undefined || raw === null || raw.trim() === '') return null;
  const dur = parseDurationMs(raw);
  const at = dur !== null ? now + dur : Date.parse(raw);
  if (!Number.isFinite(at))
    throw new Error(
      `cannot parse until "${raw}" (use ISO time with offset or e.g. 12h)`,
    );
  if (!/[zZ]|[+-]\d\d:?\d\d$/.test(raw) && dur === null)
    throw new Error(
      `until "${raw}" has no timezone; give an offset (e.g. -05:00) or Z`,
    );
  if (at <= now)
    throw new Error(`until ${new Date(at).toISOString()} is in the past`);
  if (at - now > MAX_WINDOW_MS)
    throw new Error('until is more than 90 days out');
  return at;
}

export function formatEt(iso: string): string {
  const d = new Date(iso);
  const et = d.toLocaleString('en-US', {
    timeZone: 'America/New_York',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
  return `${et} ET (${d.toISOString().slice(0, 16)}Z)`;
}

export function setPlantMode(
  req: PlantModeRequest,
  now = Date.now(),
  file = PLANT_MODES_FILE,
): PlantModeResult {
  const subsystem = normalizeSubsystem(req.subsystem || '');
  if (!subsystem)
    return {
      ok: false,
      message: `unknown subsystem "${req.subsystem}" (liquefaction | container | airbed)`,
    };
  const mode = String(req.mode || '').toLowerCase() as PlantModeRequestMode;
  if (!['offline', 'maintenance', 'muted', 'in_service'].includes(mode))
    return {
      ok: false,
      message: `unknown mode "${req.mode}" (offline | maintenance | muted | in_service)`,
    };
  let until: number | null;
  try {
    until = resolveUntil(req.until, now);
  } catch (err) {
    return { ok: false, message: (err as Error).message };
  }
  const setBy = (req.setBy || 'unknown').slice(0, 80);
  const reason = (req.reason || '').replace(/\s+/g, ' ').trim().slice(0, 300);
  const setAt = new Date(now).toISOString();
  const modes = readPlantModes(file);

  if (req.bits) {
    if (subsystem !== 'liquefaction')
      return {
        ok: false,
        message: 'bit mutes exist only for liquefaction HAZOP bits',
      };
    const { severity } = req.bits;
    const bits = [...new Set(req.bits.bits)].filter(
      (b) => Number.isInteger(b) && b >= 0 && b < 64,
    );
    if (!['warning', 'error'].includes(severity) || !bits.length)
      return {
        ok: false,
        message: 'bits needs severity warning|error and bit numbers 0-63',
      };
    modes.muted_bits = modes.muted_bits.filter(
      (m) =>
        !(
          m.subsystem === subsystem &&
          m.severity === severity &&
          bits.includes(m.bit)
        ),
    );
    if (mode === 'in_service') {
      writePlantModes(modes, file);
      return {
        ok: true,
        message: `liquefaction ${severity} bit(s) ${bits.join(', ')} alert again.`,
      };
    }
    if (mode !== 'muted')
      return { ok: false, message: 'bits only take mode muted or in_service' };
    for (const bit of bits)
      modes.muted_bits.push({
        subsystem,
        severity,
        bit,
        until: until ? new Date(until).toISOString() : null,
        reason,
        set_by: setBy,
        set_at: setAt,
      });
    writePlantModes(modes, file);
    return {
      ok: true,
      message: `liquefaction ${severity} bit(s) ${bits.join(', ')} muted${until ? ` until ${formatEt(new Date(until).toISOString())}` : ' until someone unmutes them'}; other bits still alert.`,
    };
  }

  if (mode === 'in_service') {
    const had = modes.subsystems[subsystem];
    delete modes.subsystems[subsystem];
    writePlantModes(modes, file);
    return {
      ok: true,
      message: had
        ? `${subsystem} is back in service (was ${had.mode}); its alerts are live again.`
        : `${subsystem} had no mode set; its alerts are live.`,
    };
  }
  if (mode === 'maintenance' && until === null)
    until = now + DEFAULT_MAINTENANCE_MS;
  modes.subsystems[subsystem] = {
    mode,
    until: until ? new Date(until).toISOString() : null,
    reason,
    set_by: setBy,
    set_at: setAt,
    chat_jid: req.chatJid ?? null,
    notify: req.notify === 'line' ? 'line' : 'none',
  };
  writePlantModes(modes, file);
  const end = until
    ? `until ${formatEt(new Date(until).toISOString())}`
    : 'until someone says it is back in service';
  return {
    ok: true,
    message: `${subsystem} set to ${mode} ${end}. Its alerts are logged but not investigated${mode === 'muted' ? '' : '; urgency is judged with this status'}. Other subsystems still alert.`,
  };
}

/** Briefing lines for active modes and muted bits. */
export function describePlantModes(
  now = Date.now(),
  file = PLANT_MODES_FILE,
): string[] {
  const modes = readPlantModes(file);
  const live = (until: string | null) => !until || Date.parse(until) > now;
  const lines: string[] = [];
  for (const sub of PLANT_SUBSYSTEMS) {
    const m = modes.subsystems[sub];
    if (!m || !live(m.until) || !MODE_LABEL[m.mode]) continue;
    lines.push(
      `- ${sub}: ${MODE_LABEL[m.mode]} since ${formatEt(m.set_at)} by ${m.set_by}` +
        `${m.until ? `, until ${formatEt(m.until)}` : ''}${m.reason ? ` — "${m.reason}"` : ''}. Alerts suppressed (no agent runs).`,
    );
  }
  for (const b of modes.muted_bits.filter((x) => live(x.until)))
    lines.push(
      `- ${b.subsystem} ${b.severity} bit ${b.bit}: muted by ${b.set_by}${b.until ? ` until ${formatEt(b.until)}` : ''}${b.reason ? ` — "${b.reason}"` : ''}.`,
    );
  return lines;
}

// ---------------------------------------------------------------------------
// Chat fast path (trigger required; handled without an agent run)
// ---------------------------------------------------------------------------

const MENTION_RE = /@\w[\w_]*/g;
const SUB = '(liqu[ei]faction|container|airbed)';
const DUR = '(\\d+(?:\\.\\d+)?\\s*(?:m|mins?|minutes?|h|hrs?|hours?|d|days?))';
const MUTE_SCOPED_RE = new RegExp(
  `\\b(un)?mute\\s+${SUB}\\s+alerts\\b(?:\\s+for\\s+${DUR})?`,
  'i',
);
const DECLARE_RE = new RegExp(
  `^${SUB}\\s+(?:is\\s+)?(offline|maintenance|in\\s+maintenance|back\\s+in\\s+service|in\\s+service|online)(?:\\s+for\\s+${DUR})?[.!]?$`,
  'i',
);

export function parsePlantModeCommand(
  text: string,
): Omit<PlantModeRequest, 'setBy' | 'chatJid'> | null {
  if (!text?.trim()) return null;
  const body = text.replace(MENTION_RE, ' ').replace(/\s+/g, ' ').trim();
  const mute = body.match(MUTE_SCOPED_RE);
  if (mute)
    return {
      subsystem: mute[2],
      mode: mute[1] ? 'in_service' : 'muted',
      until: mute[3] ?? null,
      reason: body.slice(0, 300),
    };
  const decl = body.match(DECLARE_RE);
  if (decl) {
    const word = decl[2].toLowerCase();
    return {
      subsystem: decl[1],
      mode:
        word === 'offline'
          ? 'offline'
          : word.includes('maintenance')
            ? 'maintenance'
            : 'in_service',
      until: decl[3] ?? null,
      reason: body.slice(0, 300),
    };
  }
  return null;
}

export function findPlantModeCommandInMessages(messages: NewMessage[]): {
  request: Omit<PlantModeRequest, 'setBy' | 'chatJid'>;
  message: NewMessage;
} | null {
  let found: ReturnType<typeof findPlantModeCommandInMessages> = null;
  for (const message of messages) {
    const request = parsePlantModeCommand(message.content || '');
    if (request) found = { request, message };
  }
  return found;
}

// ---------------------------------------------------------------------------
// IPC: {type:'plant_mode', requestId, subsystem, mode, until?, reason, set_by, notify?, bits?}
// ---------------------------------------------------------------------------

export function handlePlantModeIpc(
  data: Record<string, unknown>,
  sourceGroup: string,
  chatJid: string | null,
): void {
  const requestId =
    String(data.requestId ?? '')
      .replace(/[^\w-]/g, '')
      .slice(0, 80) || String(Date.now());
  const bits = data.bits as PlantModeRequest['bits'] | undefined;
  const result = setPlantMode({
    subsystem: String(data.subsystem ?? ''),
    mode: String(data.mode ?? ''),
    until: typeof data.until === 'string' ? data.until : null,
    reason: typeof data.reason === 'string' ? data.reason : '',
    setBy: `${typeof data.set_by === 'string' && data.set_by ? data.set_by : 'Ghost'} via ${sourceGroup}`,
    chatJid,
    notify: data.notify === 'line' ? 'line' : 'none',
    bits:
      bits && typeof bits === 'object' && Array.isArray(bits.bits)
        ? bits
        : undefined,
  });
  logger.info(
    { sourceGroup, subsystem: data.subsystem, mode: data.mode, ok: result.ok },
    `plant_mode: ${result.message}`,
  );
  const inputDir = path.join(DATA_DIR, 'ipc', sourceGroup, 'input');
  try {
    fs.mkdirSync(inputDir, { recursive: true });
    const p = path.join(inputDir, `plant_mode_result_${requestId}.json`);
    fs.writeFileSync(
      `${p}.tmp`,
      JSON.stringify({ type: 'plant_mode_result', requestId, ...result }),
    );
    fs.renameSync(`${p}.tmp`, p);
  } catch (err) {
    logger.warn({ err, sourceGroup }, 'Failed to write plant_mode_result');
  }
}
