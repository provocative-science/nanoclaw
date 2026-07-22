import { describe, expect, it } from 'vitest';

import {
  findModelEscalateInMessages,
  invalidModelAckText,
  parseModelEscalate,
} from './model-escalate.js';
import type { NewMessage } from './types.js';

function msg(content: string, overrides: Partial<NewMessage> = {}): NewMessage {
  return {
    id: '1',
    chat_jid: 'tg:1',
    sender: 'tg:1',
    sender_name: 'Spark',
    content,
    timestamp: '2026-07-16T00:00:00.000Z',
    is_from_me: false,
    ...overrides,
  };
}

describe('parseModelEscalate', () => {
  it('parses slash aliases', () => {
    const r = parseModelEscalate('@Ghost /opus dig into this lock');
    expect(r).toEqual({
      ok: true,
      model: 'claude-opus-4-6',
      cleanedContent: 'dig into this lock',
    });
  });

  it('parses /haiku and /sonnet and /qwen', () => {
    expect(
      parseModelEscalate('/haiku say hi')?.ok &&
        (parseModelEscalate('/haiku say hi') as { model: string }).model,
    ).toBe('claude-haiku-4-5');
    expect(parseModelEscalate('/sonnet plot dewpoint')).toMatchObject({
      ok: true,
      model: 'claude-sonnet-4-6',
      cleanedContent: 'plot dewpoint',
    });
    expect(parseModelEscalate('@Ghost /qwen say hi')).toMatchObject({
      ok: true,
      model: 'qwen3.6-35b',
      cleanedContent: 'say hi',
    });
  });

  it('parses model: alias and full id', () => {
    expect(parseModelEscalate('model:haiku format this')).toMatchObject({
      ok: true,
      model: 'claude-haiku-4-5',
      cleanedContent: 'format this',
    });
    expect(parseModelEscalate('model:claude-opus-4-6 why')).toMatchObject({
      ok: true,
      model: 'claude-opus-4-6',
      cleanedContent: 'why',
    });
  });

  it('rejects unknown model: values', () => {
    const r = parseModelEscalate('model:gpt-4 hello');
    expect(r).toMatchObject({ ok: false, raw: 'gpt-4' });
    expect(r && !r.ok && r.hint).toContain('sonnet');
  });

  it('returns null when no directive', () => {
    expect(parseModelEscalate('@Ghost what is pressure?')).toBeNull();
    expect(parseModelEscalate('please use opus somehow')).toBeNull();
  });

  it('allows slash with empty remainder', () => {
    expect(parseModelEscalate('/opus')).toMatchObject({
      ok: true,
      model: 'claude-opus-4-6',
      cleanedContent: '',
    });
  });
});

describe('findModelEscalateInMessages', () => {
  it('uses the latest message with a directive', () => {
    const found = findModelEscalateInMessages([
      msg('@Ghost /haiku old', { timestamp: '2026-07-16T00:00:01.000Z' }),
      msg('@Ghost /opus new', { timestamp: '2026-07-16T00:00:02.000Z' }),
    ]);
    expect(found?.result).toMatchObject({
      ok: true,
      model: 'claude-opus-4-6',
      cleanedContent: 'new',
    });
  });
});

describe('invalidModelAckText', () => {
  it('includes raw and hint', () => {
    const t = invalidModelAckText('nope', 'sonnet | opus');
    expect(t).toContain('nope');
    expect(t).toContain('sonnet');
  });
});
