import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import {
  queryThinkingOptions,
  usesAdaptiveThinkingOnly,
} from './thinking.js';

describe('usesAdaptiveThinkingOnly', () => {
  it('is true for Sonnet 5', () => {
    assert.equal(usesAdaptiveThinkingOnly('claude-sonnet-5'), true);
    assert.equal(usesAdaptiveThinkingOnly('claude-sonnet-5-20260901'), true);
  });

  it('is false for Haiku and Sonnet 4.6', () => {
    assert.equal(usesAdaptiveThinkingOnly('claude-haiku-4-5'), false);
    assert.equal(usesAdaptiveThinkingOnly('claude-sonnet-4-6'), false);
  });
});

describe('queryThinkingOptions', () => {
  it('sets adaptive thinking for Sonnet 5', () => {
    assert.deepEqual(queryThinkingOptions('claude-sonnet-5'), {
      thinking: { type: 'adaptive' },
      effort: 'high',
    });
  });

  it('omits thinking for older models and missing model', () => {
    assert.deepEqual(queryThinkingOptions('claude-sonnet-4-6'), {});
    assert.deepEqual(queryThinkingOptions(undefined), {});
  });
});
