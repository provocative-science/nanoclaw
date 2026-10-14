import { GrammyError, HttpError } from 'grammy';
import { describe, expect, it, vi } from 'vitest';

import {
  getTelegramErrorCode,
  getTelegramRetryAfterSeconds,
  isRetryableTelegramError,
  isTelegramMarkdownParseError,
  retryDelayMs,
  withTelegramRetry,
} from './telegram-retry.js';

function grammyError(
  code: number,
  description: string,
  parameters: Record<string, unknown> = {},
): GrammyError {
  return new GrammyError(
    `Call to 'sendMessage' failed! (${code}: ${description})`,
    { ok: false, error_code: code, description, parameters },
    'sendMessage',
    {},
  );
}

describe('telegram-retry', () => {
  describe('getTelegramErrorCode', () => {
    it('reads GrammyError, duck-typed objects, and message text', () => {
      expect(getTelegramErrorCode(grammyError(429, 'Too Many Requests'))).toBe(
        429,
      );
      expect(getTelegramErrorCode({ error_code: 502 })).toBe(502);
      expect(
        getTelegramErrorCode(
          new Error("Call to 'sendPhoto' failed! (503: Bad Gateway)"),
        ),
      ).toBe(503);
      expect(getTelegramErrorCode(new Error('nope'))).toBeUndefined();
    });
  });

  describe('getTelegramRetryAfterSeconds', () => {
    it('prefers parameters.retry_after and falls back to message text', () => {
      expect(
        getTelegramRetryAfterSeconds(
          grammyError(429, 'Too Many Requests', { retry_after: 7 }),
        ),
      ).toBe(7);
      expect(
        getTelegramRetryAfterSeconds(new Error('flood: retry after 12')),
      ).toBe(12);
      expect(getTelegramRetryAfterSeconds(new Error('x'))).toBeUndefined();
    });
  });

  describe('isRetryableTelegramError', () => {
    it('retries flood waits, 5xx, transport errors, and network-ish failures', () => {
      expect(isRetryableTelegramError(grammyError(429, 'Too Many'))).toBe(true);
      expect(isRetryableTelegramError(grammyError(502, 'Bad Gateway'))).toBe(
        true,
      );
      expect(
        isRetryableTelegramError(new HttpError('fetch failed', new Error())),
      ).toBe(true);
      expect(isRetryableTelegramError(new Error('socket hang up'))).toBe(true);
    });

    it('does not retry permanent client errors', () => {
      expect(
        isRetryableTelegramError(grammyError(400, "can't parse entities")),
      ).toBe(false);
      expect(isRetryableTelegramError(grammyError(403, 'Forbidden'))).toBe(
        false,
      );
      expect(isRetryableTelegramError(new Error('boom'))).toBe(false);
    });
  });

  describe('isTelegramMarkdownParseError', () => {
    it('only matches 400 entity-parse failures', () => {
      expect(
        isTelegramMarkdownParseError(
          grammyError(400, "Bad Request: can't parse entities: ..."),
        ),
      ).toBe(true);
      expect(
        isTelegramMarkdownParseError(grammyError(400, 'chat not found')),
      ).toBe(false);
      expect(isTelegramMarkdownParseError(grammyError(429, 'Too Many'))).toBe(
        false,
      );
    });
  });

  describe('retryDelayMs', () => {
    it('honours retry_after up to the cap, else exponential backoff', () => {
      expect(
        retryDelayMs(
          grammyError(429, 'Too Many', { retry_after: 3 }),
          1,
          60_000,
        ),
      ).toBe(3000);
      expect(
        retryDelayMs(
          grammyError(429, 'Too Many', { retry_after: 999 }),
          1,
          60_000,
        ),
      ).toBe(60_000);
      const d2 = retryDelayMs(new Error('socket hang up'), 2, 60_000);
      expect(d2).toBeGreaterThanOrEqual(2000);
      expect(d2).toBeLessThan(2250);
    });
  });

  describe('withTelegramRetry', () => {
    it('retries transient failures then succeeds', async () => {
      const sleep = vi.fn().mockResolvedValue(undefined);
      const fn = vi
        .fn()
        .mockRejectedValueOnce(grammyError(429, 'Too Many', { retry_after: 1 }))
        .mockRejectedValueOnce(grammyError(502, 'Bad Gateway'))
        .mockResolvedValue('ok');

      await expect(withTelegramRetry(fn, { sleep })).resolves.toBe('ok');
      expect(fn).toHaveBeenCalledTimes(3);
      expect(sleep).toHaveBeenCalledTimes(2);
      expect(sleep.mock.calls[0][0]).toBe(1000);
    });

    it('does not retry permanent errors', async () => {
      const sleep = vi.fn().mockResolvedValue(undefined);
      const fn = vi.fn().mockRejectedValue(grammyError(403, 'Forbidden'));

      await expect(withTelegramRetry(fn, { sleep })).rejects.toThrow(
        /Forbidden/,
      );
      expect(fn).toHaveBeenCalledTimes(1);
      expect(sleep).not.toHaveBeenCalled();
    });

    it('gives up after maxAttempts', async () => {
      const sleep = vi.fn().mockResolvedValue(undefined);
      const fn = vi.fn().mockRejectedValue(grammyError(503, 'Unavailable'));

      await expect(
        withTelegramRetry(fn, { sleep, maxAttempts: 3 }),
      ).rejects.toThrow(/Unavailable/);
      expect(fn).toHaveBeenCalledTimes(3);
      expect(sleep).toHaveBeenCalledTimes(2);
    });
  });
});
