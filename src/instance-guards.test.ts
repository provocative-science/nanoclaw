import { afterEach, describe, expect, it } from 'vitest';

import {
  allowDevTelegram,
  allowNoChannels,
  getNanoclawInstance,
  isDevInstance,
} from './config.js';

describe('instance guards', () => {
  afterEach(() => {
    delete process.env.NANOCLAW_INSTANCE;
    delete process.env.NANOCLAW_ALLOW_DEV_TELEGRAM;
    delete process.env.NANOCLAW_ALLOW_NO_CHANNELS;
  });

  it('defaults instance to prod', () => {
    delete process.env.NANOCLAW_INSTANCE;
    expect(getNanoclawInstance()).toBe('prod');
    expect(isDevInstance()).toBe(false);
  });

  it('detects NANOCLAW_INSTANCE=dev', () => {
    process.env.NANOCLAW_INSTANCE = 'dev';
    expect(isDevInstance()).toBe(true);
  });

  it('allowNoChannels only when flagged', () => {
    expect(allowNoChannels()).toBe(false);
    process.env.NANOCLAW_ALLOW_NO_CHANNELS = '1';
    expect(allowNoChannels()).toBe(true);
  });

  it('allowDevTelegram only when flagged', () => {
    expect(allowDevTelegram()).toBe(false);
    process.env.NANOCLAW_ALLOW_DEV_TELEGRAM = '1';
    expect(allowDevTelegram()).toBe(true);
  });
});
