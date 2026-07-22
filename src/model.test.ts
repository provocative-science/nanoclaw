import { afterEach, describe, expect, it } from 'vitest';

import {
  MODEL_ALIASES,
  isLightModel,
  modelAllowlistHint,
  normalizeModel,
  resolveDefaultModel,
} from './model.js';

describe('normalizeModel', () => {
  it('resolves aliases', () => {
    expect(normalizeModel('sonnet')).toBe('claude-sonnet-4-6');
    expect(normalizeModel('OPUS')).toBe('claude-opus-4-6');
    expect(normalizeModel(' Haiku ')).toBe('claude-haiku-4-5');
    expect(normalizeModel('qwen')).toBe('qwen3.6-35b');
  });

  it('accepts full IDs case-insensitively', () => {
    expect(normalizeModel('claude-sonnet-4-6')).toBe('claude-sonnet-4-6');
    expect(normalizeModel('Claude-Opus-4-6')).toBe('claude-opus-4-6');
    expect(normalizeModel('qwen3.6-35b')).toBe('qwen3.6-35b');
  });

  it('rejects unknown models', () => {
    expect(normalizeModel('gpt-4')).toBeNull();
    expect(normalizeModel('claude-sonnet-4-5')).toBeNull();
    expect(normalizeModel('')).toBeNull();
    expect(normalizeModel(null)).toBeNull();
    expect(normalizeModel(undefined)).toBeNull();
  });

  it('accepts extra allowlist entries from env', () => {
    process.env.NANOCLAW_MODEL_ALLOWLIST = 'claude-sonnet-4-5,custom-model';
    expect(normalizeModel('claude-sonnet-4-5')).toBe('claude-sonnet-4-5');
    expect(normalizeModel('custom-model')).toBe('custom-model');
    delete process.env.NANOCLAW_MODEL_ALLOWLIST;
  });
});

describe('isLightModel', () => {
  it('marks qwen as light', () => {
    expect(isLightModel(MODEL_ALIASES.qwen)).toBe(true);
    expect(isLightModel(MODEL_ALIASES.sonnet)).toBe(false);
  });
});

describe('resolveDefaultModel', () => {
  afterEach(() => {
    delete process.env.NANOCLAW_DEFAULT_MODEL;
  });

  it('defaults to sonnet', () => {
    delete process.env.NANOCLAW_DEFAULT_MODEL;
    expect(resolveDefaultModel()).toBe(MODEL_ALIASES.sonnet);
  });

  it('respects NANOCLAW_DEFAULT_MODEL alias', () => {
    process.env.NANOCLAW_DEFAULT_MODEL = 'haiku';
    expect(resolveDefaultModel()).toBe(MODEL_ALIASES.haiku);
  });

  it('falls back to sonnet on invalid env', () => {
    process.env.NANOCLAW_DEFAULT_MODEL = 'nope';
    expect(resolveDefaultModel()).toBe(MODEL_ALIASES.sonnet);
  });

  it('does not default to light models', () => {
    process.env.NANOCLAW_DEFAULT_MODEL = 'qwen';
    expect(resolveDefaultModel()).toBe(MODEL_ALIASES.sonnet);
  });
});

describe('modelAllowlistHint', () => {
  it('mentions aliases', () => {
    expect(modelAllowlistHint()).toContain('sonnet');
    expect(modelAllowlistHint()).toContain('opus');
  });
});
