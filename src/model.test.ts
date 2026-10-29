import { afterEach, describe, expect, it } from 'vitest';

import {
  MODEL_ALIASES,
  isLightModel,
  modelAllowlistHint,
  normalizeModel,
  resolveDefaultModel,
  resolveInvestigationFallbackModel,
  resolveInvestigationModel,
  resolveVerifierModel,
} from './model.js';

describe('normalizeModel', () => {
  it('resolves aliases', () => {
    expect(normalizeModel('sonnet')).toBe('claude-sonnet-5');
    expect(normalizeModel('OPUS')).toBe('claude-opus-5');
    expect(normalizeModel('fable')).toBe('claude-fable-5-1');
    expect(normalizeModel(' Haiku ')).toBe('claude-haiku-4-5');
    expect(normalizeModel('qwen')).toBe('qwen3.6-35b');
  });

  it('accepts full IDs case-insensitively (including legacy 4.6)', () => {
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

describe('investigation tier models', () => {
  afterEach(() => {
    delete process.env.NANOCLAW_INVESTIGATION_MODEL;
    delete process.env.NANOCLAW_INVESTIGATION_FALLBACK_MODEL;
    delete process.env.GHOST_VERIFIER_MODEL;
  });

  it('defaults to fable with opus fallback', () => {
    expect(resolveInvestigationModel()).toBe('claude-fable-5-1');
    expect(resolveInvestigationFallbackModel()).toBe('claude-opus-5');
    expect(normalizeModel('investigation')).toBe('claude-fable-5-1');
  });

  it('respects env overrides and ignores light models', () => {
    process.env.NANOCLAW_INVESTIGATION_MODEL = 'opus';
    expect(resolveInvestigationModel()).toBe('claude-opus-5');
    process.env.NANOCLAW_INVESTIGATION_MODEL = 'qwen';
    expect(resolveInvestigationModel()).toBe('claude-fable-5-1');
  });

  it('never verifies with the investigating model', () => {
    expect(resolveVerifierModel('claude-fable-5-1')).toBe('claude-opus-5');
    expect(resolveVerifierModel('claude-opus-5')).toBe('claude-sonnet-5');
    process.env.GHOST_VERIFIER_MODEL = 'fable';
    expect(resolveVerifierModel('claude-fable-5-1')).toBe('claude-opus-5');
  });
});
