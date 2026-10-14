import { GrammyError, HttpError } from 'grammy';

import { logger } from '../logger.js';

/** Default attempts including the first try. */
export const TELEGRAM_RETRY_MAX_ATTEMPTS = 5;

/** Cap Telegram's retry_after so a long flood wait can't stall the process forever. */
export const TELEGRAM_RETRY_MAX_WAIT_MS = 60_000;

export type TelegramRetryOptions = {
  maxAttempts?: number;
  maxRetryAfterMs?: number;
  /** Injected for tests; defaults to setTimeout. */
  sleep?: (ms: number) => Promise<void>;
  /** Label for log lines (e.g. sendPhoto). */
  label?: string;
};

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Duck-type / instanceof check for Bot API error codes. */
export function getTelegramErrorCode(err: unknown): number | undefined {
  if (err instanceof GrammyError) return err.error_code;
  if (err && typeof err === 'object' && 'error_code' in err) {
    const code = Number((err as { error_code: unknown }).error_code);
    if (Number.isFinite(code)) return code;
  }
  // grammY message shape: "Call to 'sendPhoto' failed! (429: Too Many Requests: ...)"
  const msg = err instanceof Error ? err.message : String(err);
  const m = msg.match(/\((\d{3}):/);
  if (m) return Number(m[1]);
  return undefined;
}

export function getTelegramRetryAfterSeconds(err: unknown): number | undefined {
  if (err instanceof GrammyError) {
    const n = err.parameters?.retry_after;
    if (typeof n === 'number' && n > 0) return n;
  }
  if (err && typeof err === 'object' && 'parameters' in err) {
    const n = (err as { parameters?: { retry_after?: unknown } }).parameters
      ?.retry_after;
    if (typeof n === 'number' && n > 0) return n;
  }
  const msg = err instanceof Error ? err.message : String(err);
  const m = msg.match(/retry after (\d+)/i);
  if (m) return Number(m[1]);
  return undefined;
}

/**
 * True for transient Bot API / transport failures worth retrying.
 * Permanent client errors (400 parse, 401, 403, 404, …) are not retryable.
 */
export function isRetryableTelegramError(err: unknown): boolean {
  if (err instanceof HttpError) return true;
  const code = getTelegramErrorCode(err);
  if (code === 429 || code === 502 || code === 503 || code === 504) return true;
  if (code !== undefined) return false;

  // Network-ish failures without a Telegram error_code
  if (err instanceof Error) {
    const name = err.name;
    const msg = err.message.toLowerCase();
    if (
      name === 'FetchError' ||
      name === 'AbortError' ||
      msg.includes('network') ||
      msg.includes('econnreset') ||
      msg.includes('etimedout') ||
      msg.includes('socket hang up')
    ) {
      return true;
    }
  }
  return false;
}

/**
 * Markdown parse failures are 400s from Telegram. Other failures (429/5xx)
 * must not trigger the plain-text fallback — that doubles flood pressure.
 */
export function isTelegramMarkdownParseError(err: unknown): boolean {
  const code = getTelegramErrorCode(err);
  if (code !== 400) return false;
  const msg = (
    err instanceof GrammyError
      ? err.description
      : err instanceof Error
        ? err.message
        : String(err)
  ).toLowerCase();
  return (
    msg.includes("can't parse entities") ||
    msg.includes('can’t parse entities') ||
    msg.includes('parse entities') ||
    msg.includes('find end of the entity')
  );
}

export function retryDelayMs(
  err: unknown,
  attempt: number,
  maxRetryAfterMs: number,
): number {
  const retryAfterSec = getTelegramRetryAfterSeconds(err);
  if (retryAfterSec != null) {
    return Math.min(retryAfterSec * 1000, maxRetryAfterMs);
  }
  // Exponential backoff with light jitter: 1s, 2s, 4s, …
  const base = Math.min(1000 * 2 ** (attempt - 1), maxRetryAfterMs);
  const jitter = Math.floor(Math.random() * 250);
  return base + jitter;
}

/**
 * Run an async Telegram API call with retries on flood waits and transient 5xx.
 */
export async function withTelegramRetry<T>(
  fn: () => Promise<T>,
  opts: TelegramRetryOptions = {},
): Promise<T> {
  const maxAttempts = opts.maxAttempts ?? TELEGRAM_RETRY_MAX_ATTEMPTS;
  const maxRetryAfterMs = opts.maxRetryAfterMs ?? TELEGRAM_RETRY_MAX_WAIT_MS;
  const sleep = opts.sleep ?? defaultSleep;
  const label = opts.label ?? 'telegram API';

  let lastErr: unknown;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      if (!isRetryableTelegramError(err) || attempt >= maxAttempts) {
        throw err;
      }
      const delay = retryDelayMs(err, attempt, maxRetryAfterMs);
      logger.warn(
        {
          label,
          attempt,
          maxAttempts,
          delayMs: delay,
          errorCode: getTelegramErrorCode(err),
          err,
        },
        'Retrying Telegram API call after transient failure',
      );
      await sleep(delay);
    }
  }
  throw lastErr;
}
