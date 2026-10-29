import { describe, expect, it } from 'vitest';

import { classifyIntent } from './intent.js';

describe('classifyIntent', () => {
  it.each([
    '@Ghost why did airbed lock this morning?',
    'what happened with liquefaction overnight?',
    'can you figure out what triggered the interlock supervisor?',
    'is the container locked?',
    'watchdog tripped again?',
  ])('investigation: %s', (text) => {
    expect(classifyIntent(text)).toBe('investigation');
  });

  it.each([
    '@Ghost plot warm tank level from noon to now',
    'thanks!',
    'why is the sky blue',
    'remind me tomorrow at 9',
    '',
  ])('chat: %s', (text) => {
    expect(classifyIntent(text)).toBe('chat');
  });
});
