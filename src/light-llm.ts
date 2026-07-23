/**
 * Host light-path LLM runs (OpenAI-compatible; no container / tools).
 */

import { ASSISTANT_NAME } from './config.js';
import { logger } from './logger.js';
import {
  completeChatCompletion,
  lightPathSystemPrompt,
} from './openai-client.js';
import { loadQwenProfile } from './qwen-profile.js';
import { isLightModel } from './model.js';

export function canRunLightModel(model: string): boolean {
  return isLightModel(model);
}

/**
 * Run a light-path completion for an allowlisted light model id.
 * Currently only the Qwen profile is configured.
 */
export async function runLightCompletion(
  model: string,
  userPrompt: string,
): Promise<string> {
  if (!isLightModel(model)) {
    throw new Error(`Not a light model: ${model}`);
  }

  const profile = loadQwenProfile();
  if (!profile) {
    throw new Error(
      'Qwen light path is not configured (set QWEN_API_KEY in secrets/qwen.env, then restart nanoclaw)',
    );
  }

  // Prefer the allowlisted model id from the escalate/task; fall back to profile model.
  const modelId = model || profile.model;

  logger.info(
    { model: modelId, baseUrl: profile.baseUrl },
    'Light-path OpenAI-compat completion',
  );

  return completeChatCompletion({
    baseUrl: profile.baseUrl,
    apiKey: profile.apiKey,
    model: profile.model || modelId,
    messages: [
      { role: 'system', content: lightPathSystemPrompt(ASSISTANT_NAME) },
      { role: 'user', content: userPrompt },
    ],
  });
}
