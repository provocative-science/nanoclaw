import fs from 'fs';
import path from 'path';

import { AUTO_REGISTER_ALLOWLIST_PATH, DEFAULT_TRIGGER } from './config.js';
import { getDistinctSendersInChat } from './db.js';
import { isValidGroupFolder } from './group-folder.js';
import { logger } from './logger.js';
import { ContainerConfig, RegisteredGroup } from './types.js';

export interface AutoRegisterConfig {
  allowedSenders: string[];
  /** Group chat JIDs whose message senders are merged into allowedSenders daily. */
  syncFromChats?: string[];
  defaultContainerConfig?: ContainerConfig;
  defaultTrigger?: string;
  logDenied?: boolean;
}

export interface AllowlistSyncResult {
  added: string[];
  total: number;
  changed: boolean;
  chats: string[];
}

const DEFAULT_CONFIG: AutoRegisterConfig = {
  allowedSenders: [],
  logDenied: true,
};

export function loadAutoRegisterConfig(
  pathOverride?: string,
): AutoRegisterConfig {
  const filePath = pathOverride ?? AUTO_REGISTER_ALLOWLIST_PATH;

  let raw: string;
  try {
    raw = fs.readFileSync(filePath, 'utf-8');
  } catch (err: unknown) {
    if ((err as NodeJS.ErrnoException).code === 'ENOENT') return DEFAULT_CONFIG;
    logger.warn({ err, path: filePath }, 'auto-register: cannot read config');
    return DEFAULT_CONFIG;
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    logger.warn({ path: filePath }, 'auto-register: invalid JSON');
    return DEFAULT_CONFIG;
  }

  const obj = parsed as Record<string, unknown>;

  if (
    !Array.isArray(obj.allowedSenders) ||
    !obj.allowedSenders.every((v: unknown) => typeof v === 'string')
  ) {
    logger.warn(
      { path: filePath },
      'auto-register: invalid or missing allowedSenders',
    );
    return DEFAULT_CONFIG;
  }

  let syncFromChats: string[] | undefined;
  if (Array.isArray(obj.syncFromChats)) {
    syncFromChats = obj.syncFromChats.filter(
      (v: unknown): v is string => typeof v === 'string' && v.length > 0,
    );
  }

  return {
    allowedSenders: obj.allowedSenders as string[],
    syncFromChats,
    defaultContainerConfig: obj.defaultContainerConfig as
      | ContainerConfig
      | undefined,
    defaultTrigger:
      typeof obj.defaultTrigger === 'string' ? obj.defaultTrigger : undefined,
    logDenied: obj.logDenied !== false,
  };
}

export function saveAutoRegisterConfig(
  cfg: AutoRegisterConfig,
  pathOverride?: string,
): void {
  const filePath = pathOverride ?? AUTO_REGISTER_ALLOWLIST_PATH;
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmpPath = `${filePath}.tmp`;
  fs.writeFileSync(tmpPath, `${JSON.stringify(cfg, null, 2)}\n`, {
    mode: 0o600,
  });
  fs.renameSync(tmpPath, filePath);
}

export function syncAllowedSenders(
  cfg: AutoRegisterConfig,
  sendersByChat: Record<string, string[]>,
): { config: AutoRegisterConfig; result: AllowlistSyncResult } {
  const chatJids = Object.keys(sendersByChat);
  const existing = new Set(cfg.allowedSenders);
  const added: string[] = [];

  for (const jid of chatJids) {
    for (const sender of sendersByChat[jid]) {
      if (sender && !existing.has(sender)) {
        existing.add(sender);
        added.push(sender);
      }
    }
  }

  if (added.length === 0) {
    return {
      config: cfg,
      result: {
        added: [],
        total: cfg.allowedSenders.length,
        changed: false,
        chats: chatJids,
      },
    };
  }

  return {
    config: {
      ...cfg,
      allowedSenders: [...cfg.allowedSenders, ...added],
    },
    result: {
      added,
      total: cfg.allowedSenders.length + added.length,
      changed: true,
      chats: chatJids,
    },
  };
}

export function runAutoRegisterAllowlistSync(
  pathOverride?: string,
  getSenders: (chatJid: string) => string[] = getDistinctSendersInChat,
): AllowlistSyncResult {
  const filePath = pathOverride ?? AUTO_REGISTER_ALLOWLIST_PATH;
  const cfg = loadAutoRegisterConfig(filePath);
  if (!cfg.syncFromChats?.length) {
    return {
      added: [],
      total: cfg.allowedSenders.length,
      changed: false,
      chats: [],
    };
  }

  const sendersByChat: Record<string, string[]> = {};
  for (const jid of cfg.syncFromChats) {
    sendersByChat[jid] = getSenders(jid);
  }

  const { config, result } = syncAllowedSenders(cfg, sendersByChat);
  if (result.changed) {
    saveAutoRegisterConfig(config, filePath);
    logger.info(
      { added: result.added, total: result.total, chats: result.chats },
      'Auto-register allowlist synced from group chats',
    );
  }

  return result;
}

export function isAutoRegisterAllowed(
  sender: string,
  cfg: AutoRegisterConfig,
): boolean {
  return cfg.allowedSenders.includes(sender);
}

/**
 * Sanitize a display name into a valid group folder suffix.
 * Strips non-ASCII, collapses separators, lowercases.
 */
function sanitizeFolderName(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
}

export function buildAutoRegistration(
  senderName: string,
  chatJid: string,
  senderId: string,
  cfg: AutoRegisterConfig,
  existingFolders: Set<string>,
): RegisteredGroup {
  const baseName = sanitizeFolderName(senderName) || 'dm';
  let folder = `telegram_${baseName}`;

  // Handle collisions by appending sender ID
  if (!isValidGroupFolder(folder) || existingFolders.has(folder)) {
    folder = `telegram_dm_${senderId}`;
  }

  return {
    name: senderName || `DM ${senderId}`,
    folder,
    trigger: cfg.defaultTrigger || DEFAULT_TRIGGER,
    added_at: new Date().toISOString(),
    containerConfig: cfg.defaultContainerConfig,
    requiresTrigger: false,
    isMain: false,
  };
}
