/**
 * OpenAI-compatible light LLM profile for the Qwen endpoint.
 * Credentials stay on the host (secrets/qwen.env); never mount into containers.
 */

import path from 'path';

import { readEnvFileFromPath } from './env.js';
import type { OpenAICompatProfile } from './openai-client.js';

const QWEN_ENV_PATH = path.join(process.cwd(), 'secrets', 'qwen.env');

const DEFAULT_BASE_URL = 'https://inference.provocative.earth/v1';
const DEFAULT_MODEL = 'qwen3.6-35b';

/**
 * Load the Qwen OpenAI-compat profile from secrets/qwen.env (and process.env overrides).
 * Returns null if no API key is configured.
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

  const baseUrl = (
    process.env.QWEN_BASE_URL?.trim() ||
    fileVals.QWEN_BASE_URL?.trim() ||
    DEFAULT_BASE_URL
  ).replace(/\/$/, '');

  const model =
    process.env.QWEN_MODEL?.trim() ||
    fileVals.QWEN_MODEL?.trim() ||
    DEFAULT_MODEL;

  return { baseUrl, apiKey, model };
}
