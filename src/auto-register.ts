import fs from 'fs';

import { AUTO_REGISTER_ALLOWLIST_PATH, DEFAULT_TRIGGER } from './config.js';
import { isValidGroupFolder } from './group-folder.js';
import { logger } from './logger.js';
import { ContainerConfig, RegisteredGroup } from './types.js';

export interface AutoRegisterConfig {
  allowedSenders: string[];
  defaultContainerConfig?: ContainerConfig;
  defaultTrigger?: string;
  logDenied?: boolean;
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

  return {
    allowedSenders: obj.allowedSenders as string[],
    defaultContainerConfig: obj.defaultContainerConfig as
      | ContainerConfig
      | undefined,
    defaultTrigger:
      typeof obj.defaultTrigger === 'string' ? obj.defaultTrigger : undefined,
    logDenied: obj.logDenied !== false,
  };
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
