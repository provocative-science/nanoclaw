/**
 * Parse interactive model escalate directives from chat messages.
 *
 * Supported (after stripping @mentions):
 *   /opus …   /haiku …   /sonnet …   /qwen …
 *   model:opus …   model:claude-sonnet-4-6 …   model:qwen …
 */

import {
  modelAllowlistHint,
  normalizeModel,
} from './model.js';
import type { NewMessage } from './types.js';

const MENTION_RE = /@\w[\w_]*/g;

export type ModelEscalateResult =
  | { ok: true; model: string; cleanedContent: string }
  | { ok: false; raw: string; hint: string }
  | null;

/**
 * Parse a single message body for a leading model directive.
 * Returns null if no directive is present.
 */
export function parseModelEscalate(text: string): ModelEscalateResult {
  if (!text?.trim()) return null;

  // Strip @mentions but keep the rest of the structure
  const withoutMentions = text.replace(MENTION_RE, ' ');
  const trimmed = withoutMentions.replace(/^\s+/, '');

  // /opus | /haiku | /sonnet | /qwen at start
  const slashMatch = trimmed.match(
    /^\/(opus|haiku|sonnet|qwen)(?:\s+|$)/i,
  );
  if (slashMatch) {
    const alias = slashMatch[1].toLowerCase();
    const model = normalizeModel(alias)!;
    const cleanedContent = trimmed.slice(slashMatch[0].length).trim();
    return { ok: true, model, cleanedContent };
  }

  // model:<id> at start
  const modelMatch = trimmed.match(/^model:(\S+)(?:\s+|$)/i);
  if (modelMatch) {
    const raw = modelMatch[1];
    const model = normalizeModel(raw);
    const cleanedContent = trimmed.slice(modelMatch[0].length).trim();
    if (!model) {
      return { ok: false, raw, hint: modelAllowlistHint() };
    }
    return { ok: true, model, cleanedContent };
  }

  return null;
}

/**
 * Find escalate directive on the latest user message that has one.
 * Prefer the last message in the batch (most recent user intent).
 */
export function findModelEscalateInMessages(
  messages: NewMessage[],
): { result: NonNullable<ModelEscalateResult>; message: NewMessage } | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    const result = parseModelEscalate(message.content || '');
    if (result) {
      return { result, message };
    }
  }
  return null;
}

export function invalidModelAckText(raw: string, hint: string): string {
  return `Unknown model "${raw}". Allowed: ${hint}`;
}
