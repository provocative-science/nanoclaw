import { describe, expect, it } from 'vitest';

import {
  findMuteCommandInMessages,
  parseMuteCommand,
} from './plant-alert-mute.js';
import type { NewMessage } from './types.js';

function msg(content: string, overrides: Partial<NewMessage> = {}): NewMessage {
  return {
    id: '1',
    chat_jid: 'tg:-1002010635835',
    sender: 'tg:1',
    sender_name: 'Spark',
    content,
    timestamp: '2026-07-16T00:00:00.000Z',
    is_from_me: false,
    ...overrides,
  };
}

describe('parseMuteCommand', () => {
  it('parses mute/unmute with Ghost mentions', () => {
    expect(parseMuteCommand('@Ghost mute alerts')).toBe('mute');
    expect(parseMuteCommand('@Ghost unmute alerts')).toBe('unmute');
    expect(
      parseMuteCommand('@Ghost @ghost_in_the_co2_machine_bot mute alerts'),
    ).toBe('mute');
    expect(parseMuteCommand('Please unmute alerts now')).toBe('unmute');
  });

  it('does not false-positive', () => {
    expect(parseMuteCommand('the mute button alerts me')).toBeNull();
    expect(parseMuteCommand('@Ghost status')).toBeNull();
    expect(parseMuteCommand('')).toBeNull();
  });

  it('prefers unmute when both could match', () => {
    expect(parseMuteCommand('unmute alerts')).toBe('unmute');
  });
});

describe('findMuteCommandInMessages', () => {
  it('returns the latest mute command in the batch', () => {
    const found = findMuteCommandInMessages([
      msg('@Ghost mute alerts', { timestamp: '2026-07-16T00:00:01.000Z' }),
      msg('@Ghost unmute alerts', { timestamp: '2026-07-16T00:00:02.000Z' }),
    ]);
    expect(found?.command).toBe('unmute');
  });
});
