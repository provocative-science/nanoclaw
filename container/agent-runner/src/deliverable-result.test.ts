/**
 * Unit tests for scheduled-task result fallback helpers.
 * Run: npx tsx --test src/deliverable-result.test.ts
 */
import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  extractAssistantText,
  resolveDeliverableResult,
  stripInternalTags,
} from './deliverable-result.js';

describe('stripInternalTags', () => {
  it('strips internal blocks', () => {
    assert.equal(
      stripInternalTags('<internal>hidden</internal>*Subsystem:* liq'),
      '*Subsystem:* liq',
    );
  });
});

describe('extractAssistantText', () => {
  it('joins text blocks', () => {
    assert.equal(
      extractAssistantText({
        message: {
          content: [
            { type: 'text', text: 'Hello ' },
            { type: 'tool_use', name: 'Write' },
            { type: 'text', text: 'world' },
          ],
        },
      }),
      'Hello world',
    );
  });

  it('returns null when only tools', () => {
    assert.equal(
      extractAssistantText({
        message: { content: [{ type: 'tool_use', name: 'Write' }] },
      }),
      null,
    );
  });
});

describe('resolveDeliverableResult', () => {
  it('prefers non-empty SDK result', () => {
    assert.equal(
      resolveDeliverableResult('final brief', 'mid-turn draft'),
      'final brief',
    );
  });

  it('falls back when SDK result is null', () => {
    assert.equal(
      resolveDeliverableResult(null, '*Subsystem:* liquefaction\n*State:* ERROR'),
      '*Subsystem:* liquefaction\n*State:* ERROR',
    );
  });

  it('falls back when SDK result is internal-only', () => {
    assert.equal(
      resolveDeliverableResult(
        '<internal>archive written</internal>',
        '*Subsystem:* liquefaction',
      ),
      '*Subsystem:* liquefaction',
    );
  });

  it('does not fall back when assistant text is internal-only', () => {
    assert.equal(
      resolveDeliverableResult(null, '<internal>Already processed</internal>'),
      null,
    );
  });
});
