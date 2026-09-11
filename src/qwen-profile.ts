/**
 * OpenAI-compatible light LLM profile for the Qwen endpoint.
 * Credentials stay on the host (secrets/qwen.env); never mount into containers.
 */

import path from 'path';

import { readEnvFileFromPath } from './env.js';
import type { OpenAICompatProfile } from './openai-client.js';

const QWEN_ENV_PATH = path.join(process.cwd(), 'secrets', 'qwen.env');

/**
 * Load the Qwen OpenAI-compat profile from secrets/qwen.env (and process.env overrides).
 * Returns null if API key or base URL is not configured.
 * Instance-specific defaults belong in secrets/qwen.env (via instance overlay examples).
 */
export function loadQwenProfile(): OpenAICompatProfile | null {
  const fileVals = readEnvFileFromPath(QWEN_ENV_PATH, [
    'QWEN_API_KEY',
    'QWEN_BASE_URL',
    'QWEN_MODEL',
  ]);

  const apiKey =
    process.env.QWEN_API_KEY?.trim() || fileVals.QWEN_API_KEY?.trim() || '';
  if (!apiKey || /^PASTE_/i.test(apiKey) || apiKey.includes('REPLACE')) {
    return null;
  }

  const baseUrlRaw =
    process.env.QWEN_BASE_URL?.trim() || fileVals.QWEN_BASE_URL?.trim() || '';
  if (
    !baseUrlRaw ||
    /^https:\/\/YOUR_/i.test(baseUrlRaw) ||
    baseUrlRaw.includes('REPLACE')
  ) {
    return null;
  }
  const baseUrl = baseUrlRaw.replace(/\/$/, '');

  const model =
    process.env.QWEN_MODEL?.trim() || fileVals.QWEN_MODEL?.trim() || '';
  if (!model || /^YOUR_/i.test(model) || model.includes('REPLACE')) {
    return null;
  }

  return { baseUrl, apiKey, model };
}
