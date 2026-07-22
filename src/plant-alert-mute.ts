/**
 * Plant-alert mute flag shared with scripts/alert-monitor.
 *
 * File: data/alert-monitor/mute.json
 * Commands (after stripping @mentions): "mute alerts" / "unmute alerts"
 */

import fs from 'fs';
import path from 'path';

import { DATA_DIR } from './config.js';
import type { NewMessage } from './types.js';

export type MuteCommand = 'mute' | 'unmute';

export interface PlantAlertMuteState {
  muted: boolean;
  updated_at?: string;
  updated_by?: string | null;
  command?: string | null;
  chat_jid?: string | null;
  thread_id?: string | null;
}

export const PLANT_ALERT_MUTE_FILE = path.join(
  DATA_DIR,
  'alert-monitor',
  'mute.json',
);

const MENTION_RE = /@\w[\w_]*/g;
const UNMUTE_RE = /\bunmute\s+alerts\b/i;
const MUTE_RE = /\bmute\s+alerts\b/i;

export function parseMuteCommand(text: string): MuteCommand | null {
  if (!text?.trim()) return null;
  const body = text.replace(MENTION_RE, ' ').replace(/\s+/g, ' ').trim();
  if (UNMUTE_RE.test(body)) return 'unmute';
  if (MUTE_RE.test(body)) return 'mute';
  return null;
}

/** Latest mute/unmute command in a message batch, or null. */
export function findMuteCommandInMessages(
  messages: NewMessage[],
): { command: MuteCommand; message: NewMessage } | null {
  let found: { command: MuteCommand; message: NewMessage } | null = null;
  for (const message of messages) {
    const command = parseMuteCommand(message.content || '');
    if (command) {
      found = { command, message };
    }
  }
  return found;
}

export function readPlantAlertMuteState(): PlantAlertMuteState {
  try {
    if (!fs.existsSync(PLANT_ALERT_MUTE_FILE)) {
      return { muted: false };
    }
    const data = JSON.parse(
      fs.readFileSync(PLANT_ALERT_MUTE_FILE, 'utf-8'),
    ) as PlantAlertMuteState;
    return {
      muted: Boolean(data?.muted),
      updated_at: data?.updated_at,
      updated_by: data?.updated_by,
      command: data?.command,
      chat_jid: data?.chat_jid,
      thread_id: data?.thread_id,
    };
  } catch {
    return { muted: false };
  }
}

export function writePlantAlertMuteState(
  command: MuteCommand,
  opts: {
    updatedBy?: string;
    chatJid?: string;
    threadId?: string;
  } = {},
): PlantAlertMuteState {
  const state: PlantAlertMuteState = {
    muted: command === 'mute',
    updated_at: new Date().toISOString(),
    updated_by: opts.updatedBy ?? null,
    command,
    chat_jid: opts.chatJid ?? null,
    thread_id: opts.threadId ?? null,
  };
  const dir = path.dirname(PLANT_ALERT_MUTE_FILE);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = `${PLANT_ALERT_MUTE_FILE}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(state, null, 2)}\n`, 'utf-8');
  fs.renameSync(tmp, PLANT_ALERT_MUTE_FILE);
  return state;
}

export function muteAckText(command: MuteCommand): string {
  if (command === 'mute') {
    return 'Plant alerts muted. Say "unmute alerts" (and tag me) when you want them again. Grafana OnCall is unchanged.';
  }
  return 'Plant alerts unmuted. Ghost will notify on plant edges again.';
}
