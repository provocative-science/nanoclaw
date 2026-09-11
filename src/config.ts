import os from 'os';
import path from 'path';

import { readEnvFile } from './env.js';
import { isValidTimezone } from './timezone.js';

// Read config values from .env (falls back to process.env).
const envConfig = readEnvFile([
  'ASSISTANT_NAME',
  'ASSISTANT_HAS_OWN_NUMBER',
  'ONECLI_URL',
  'TZ',
]);

export const ASSISTANT_NAME =
  process.env.ASSISTANT_NAME || envConfig.ASSISTANT_NAME || 'Ghost';
export const ASSISTANT_HAS_OWN_NUMBER =
  (process.env.ASSISTANT_HAS_OWN_NUMBER ||
    envConfig.ASSISTANT_HAS_OWN_NUMBER) === 'true';
export const POLL_INTERVAL = 2000;
export const SCHEDULER_POLL_INTERVAL = 60000;
export const AUTO_REGISTER_SYNC_INTERVAL_MS = 24 * 60 * 60 * 1000;

// Absolute paths needed for container mounts
const PROJECT_ROOT = process.cwd();
const HOME_DIR = process.env.HOME || os.homedir();

function envOrDotenv(key: string): string | undefined {
  if (process.env[key]) return process.env[key];
  return readEnvFile([key])[key];
}

function expandHomePrefix(value: string): string {
  if (value.startsWith('${HOME}')) {
    return HOME_DIR + value.slice('${HOME}'.length);
  }
  if (value.startsWith('$HOME')) {
    return HOME_DIR + value.slice('$HOME'.length);
  }
  if (value.startsWith('~/')) {
    return path.join(HOME_DIR, value.slice(2));
  }
  return value;
}

/** `dev` refuses Telegram unless NANOCLAW_ALLOW_DEV_TELEGRAM=1 (avoids dual-bot replies). */
export function getNanoclawInstance(): string {
  return (envOrDotenv('NANOCLAW_INSTANCE') || 'prod').toLowerCase();
}

export function isDevInstance(): boolean {
  return getNanoclawInstance() === 'dev';
}

export function allowDevTelegram(): boolean {
  return envOrDotenv('NANOCLAW_ALLOW_DEV_TELEGRAM') === '1';
}

/** When set, host may start with zero connected chat channels (dev / claw-only). */
export function allowNoChannels(): boolean {
  return envOrDotenv('NANOCLAW_ALLOW_NO_CHANNELS') === '1';
}

// Allowlists live outside the project root. Override for isolated dev instances.
const CONFIG_HOME = expandHomePrefix(
  envOrDotenv('NANOCLAW_CONFIG_HOME') ||
    path.join(HOME_DIR, '.config', 'nanoclaw'),
);
export const MOUNT_ALLOWLIST_PATH = path.join(
  CONFIG_HOME,
  'mount-allowlist.json',
);
export const SENDER_ALLOWLIST_PATH = path.join(
  CONFIG_HOME,
  'sender-allowlist.json',
);
export const AUTO_REGISTER_ALLOWLIST_PATH = path.join(
  CONFIG_HOME,
  'auto-register-allowlist.json',
);
export const STORE_DIR = path.resolve(PROJECT_ROOT, 'store');
export const GROUPS_DIR = path.resolve(PROJECT_ROOT, 'groups');
export const DATA_DIR = path.resolve(PROJECT_ROOT, 'data');

export const CONTAINER_IMAGE =
  process.env.CONTAINER_IMAGE || 'nanoclaw-agent:latest';
export const CONTAINER_TIMEOUT = parseInt(
  process.env.CONTAINER_TIMEOUT || '1800000',
  10,
);
export const CONTAINER_MAX_OUTPUT_SIZE = parseInt(
  process.env.CONTAINER_MAX_OUTPUT_SIZE || '10485760',
  10,
); // 10MB default
export const ONECLI_URL =
  process.env.ONECLI_URL || envConfig.ONECLI_URL || 'http://localhost:10254';
export const MAX_MESSAGES_PER_PROMPT = Math.max(
  1,
  parseInt(process.env.MAX_MESSAGES_PER_PROMPT || '10', 10) || 10,
);
export const IPC_POLL_INTERVAL = 1000;
export const IDLE_TIMEOUT = parseInt(process.env.IDLE_TIMEOUT || '1800000', 10); // 30min default — how long to keep container alive after last result
export const MAX_CONCURRENT_CONTAINERS = Math.max(
  1,
  parseInt(process.env.MAX_CONCURRENT_CONTAINERS || '5', 10) || 5,
);

function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export function buildTriggerPattern(trigger: string): RegExp {
  return new RegExp(`(?:^|\\s)${escapeRegex(trigger.trim())}\\b`, 'i');
}

export const DEFAULT_TRIGGER = `@${ASSISTANT_NAME}`;

export function getTriggerPattern(trigger?: string): RegExp {
  const normalizedTrigger = trigger?.trim();
  return buildTriggerPattern(normalizedTrigger || DEFAULT_TRIGGER);
}

export const TRIGGER_PATTERN = buildTriggerPattern(DEFAULT_TRIGGER);

// Timezone for scheduled tasks, message formatting, etc.
// Validates each candidate is a real IANA identifier before accepting.
function resolveConfigTimezone(): string {
  const candidates = [
    process.env.TZ,
    envConfig.TZ,
    Intl.DateTimeFormat().resolvedOptions().timeZone,
  ];
  for (const tz of candidates) {
    if (tz && isValidTimezone(tz)) return tz;
  }
  return 'UTC';
}
export const TIMEZONE = resolveConfigTimezone();
