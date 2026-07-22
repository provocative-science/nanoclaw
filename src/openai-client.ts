/**
 * Generic OpenAI-compatible chat/completions client (host light path).
 */

export interface OpenAIChatMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

export interface OpenAICompatProfile {
  baseUrl: string;
  apiKey: string;
  model: string;
}

export interface CompleteChatCompletionOpts {
  baseUrl: string;
  apiKey: string;
  model: string;
  messages: OpenAIChatMessage[];
  timeoutMs?: number;
  fetchImpl?: typeof fetch;
}

const DEFAULT_TIMEOUT_MS = 60_000;

export class OpenAICompatError extends Error {
  constructor(
    message: string,
    readonly status?: number,
    readonly body?: string,
  ) {
    super(message);
    this.name = 'OpenAICompatError';
  }
}

/**
 * POST /chat/completions against any OpenAI-compatible endpoint.
 * Returns choices[0].message.content.
 */
export async function completeChatCompletion(
  opts: CompleteChatCompletionOpts,
): Promise<string> {
  const {
    baseUrl,
    apiKey,
    model,
    messages,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    fetchImpl = fetch,
  } = opts;

  const url = `${baseUrl.replace(/\/$/, '')}/chat/completions`;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const res = await fetchImpl(url, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ model, messages }),
      signal: controller.signal,
    });

    const text = await res.text();
    if (!res.ok) {
      throw new OpenAICompatError(
        `OpenAI-compat HTTP ${res.status}`,
        res.status,
        text.slice(0, 500),
      );
    }

    let data: unknown;
    try {
      data = JSON.parse(text);
    } catch {
      throw new OpenAICompatError(
        'OpenAI-compat response is not JSON',
        res.status,
        text.slice(0, 500),
      );
    }

    const content = extractContent(data);
    if (content == null || content === '') {
      throw new OpenAICompatError(
        'OpenAI-compat response missing choices[0].message.content',
        res.status,
        text.slice(0, 500),
      );
    }
    return content;
  } finally {
    clearTimeout(timer);
  }
}

function extractContent(data: unknown): string | null {
  if (!data || typeof data !== 'object') return null;
  const choices = (data as { choices?: unknown }).choices;
  if (!Array.isArray(choices) || choices.length === 0) return null;
  const message = (choices[0] as { message?: { content?: unknown } })?.message;
  if (!message || typeof message.content !== 'string') return null;
  return message.content;
}

/** Short system prompt for light-path Ghost replies (no tools). */
export function lightPathSystemPrompt(assistantName: string): string {
  return (
    `You are ${assistantName}, a concise technical assistant for the Provocative Science team. ` +
    `You are running in light mode: you have no tools, files, or MCP access. ` +
    `Answer only from the user message. Be brief and practical. ` +
    `Use *bold* for emphasis when writing for Telegram/WhatsApp.`
  );
}
