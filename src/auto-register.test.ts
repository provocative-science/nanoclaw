import fs from 'fs';
import os from 'os';
import path from 'path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import {
  loadAutoRegisterConfig,
  runAutoRegisterAllowlistSync,
  saveAutoRegisterConfig,
  syncAllowedSenders,
} from './auto-register.js';

let tmpDir: string;

function cfgPath(name = 'auto-register-allowlist.json'): string {
  return path.join(tmpDir, name);
}

function writeConfig(config: unknown, name?: string): string {
  const p = cfgPath(name);
  fs.writeFileSync(p, JSON.stringify(config));
  return p;
}

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'auto-register-test-'));
});

afterEach(() => {
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

describe('loadAutoRegisterConfig', () => {
  it('loads syncFromChats when present', () => {
    const p = writeConfig({
      allowedSenders: ['1'],
      syncFromChats: ['tg:-1002010635835'],
    });
    const cfg = loadAutoRegisterConfig(p);
    expect(cfg.syncFromChats).toEqual(['tg:-1002010635835']);
  });
});

describe('syncAllowedSenders', () => {
  it('appends new senders without duplicates', () => {
    const cfg = {
      allowedSenders: ['111', '222'],
      logDenied: true,
    };
    const { config, result } = syncAllowedSenders(cfg, {
      'tg:-100': ['222', '333', '444'],
    });

    expect(result.changed).toBe(true);
    expect(result.added).toEqual(['333', '444']);
    expect(config.allowedSenders).toEqual(['111', '222', '333', '444']);
  });

  it('returns unchanged when all senders already allowed', () => {
    const cfg = {
      allowedSenders: ['111', '222'],
      logDenied: true,
    };
    const { config, result } = syncAllowedSenders(cfg, {
      'tg:-100': ['111', '222'],
    });

    expect(result.changed).toBe(false);
    expect(result.added).toEqual([]);
    expect(config.allowedSenders).toEqual(['111', '222']);
  });
});

describe('runAutoRegisterAllowlistSync', () => {
  it('writes updated config when new senders are found', () => {
    const p = writeConfig({
      allowedSenders: ['111'],
      syncFromChats: ['tg:-1002010635835'],
    });

    const result = runAutoRegisterAllowlistSync(p, () => ['111', '222']);

    expect(result.changed).toBe(true);
    expect(result.added).toEqual(['222']);

    const saved = loadAutoRegisterConfig(p);
    expect(saved.allowedSenders).toEqual(['111', '222']);
  });

  it('skips when syncFromChats is not configured', () => {
    const p = writeConfig({ allowedSenders: ['111'] });
    const result = runAutoRegisterAllowlistSync(p, () => ['222']);
    expect(result.changed).toBe(false);
    expect(result.chats).toEqual([]);
    expect(loadAutoRegisterConfig(p).allowedSenders).toEqual(['111']);
  });
});

describe('saveAutoRegisterConfig', () => {
  it('writes valid JSON with trailing newline', () => {
    const p = cfgPath();
    saveAutoRegisterConfig({ allowedSenders: ['1'], logDenied: true }, p);
    const raw = fs.readFileSync(p, 'utf-8');
    expect(JSON.parse(raw)).toEqual({ allowedSenders: ['1'], logDenied: true });
    expect(raw.endsWith('\n')).toBe(true);
  });
});
