/** Strip <internal>...</internal> blocks (host also strips before send). */
export function stripInternalTags(text: string): string {
  return text.replace(/<internal>[\s\S]*?<\/internal>/g, '').trim();
}

/** Extract concatenated text blocks from an SDK assistant message. */
export function extractAssistantText(message: {
  message?: { content?: unknown };
}): string | null {
  const content = message.message?.content;
  if (!Array.isArray(content)) {
    if (typeof content === 'string' && content.trim()) return content;
    return null;
  }
  const text = content
    .filter((c: { type?: string }) => c?.type === 'text')
    .map((c: { text?: string }) => c?.text || '')
    .join('');
  return text.trim() ? text : null;
}

/**
 * Prefer SDK result text; if empty/internal-only, fall back to the last
 * deliverable assistant text from the same turn (fixes plant-alert drops
 * when the model ends on archive tool calls after composing the brief).
 */
export function resolveDeliverableResult(
  sdkResult: string | null | undefined,
  lastAssistantText: string | null | undefined,
): string | null {
  if (sdkResult && stripInternalTags(sdkResult)) return sdkResult;
  if (lastAssistantText && stripInternalTags(lastAssistantText)) {
    return lastAssistantText;
  }
  return sdkResult?.trim() ? sdkResult : null;
}
