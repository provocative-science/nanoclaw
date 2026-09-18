/**
 * Claude API thinking mode for the Agent SDK query() options.
 *
 * Sonnet 5 / Opus 5 / Opus 4.7+ reject thinking.type=enabled (400).
 * Haiku 4.5 still uses extended thinking and rejects adaptive.
 */

export type QueryThinkingOptions = {
  thinking?: { type: 'adaptive' };
  effort?: 'high';
};

export function usesAdaptiveThinkingOnly(model: string): boolean {
  const id = model.toLowerCase();
  if (id.includes('haiku')) return false;
  if (id.includes('fable') || id.includes('mythos')) return true;
  if (/claude-sonnet-5/.test(id)) return true;
  if (/claude-opus-5/.test(id)) return true;
  if (/claude-opus-4-[78]/.test(id)) return true;
  return false;
}

/** Spread into query({ options }) so Sonnet 5 does not send thinking.type.enabled. */
export function queryThinkingOptions(
  model: string | undefined,
): QueryThinkingOptions {
  if (!model || !usesAdaptiveThinkingOnly(model)) return {};
  return { thinking: { type: 'adaptive' }, effort: 'high' };
}
