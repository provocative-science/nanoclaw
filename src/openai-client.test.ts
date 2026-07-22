import { describe, expect, it, vi } from 'vitest';

import {
  OpenAICompatError,
  completeChatCompletion,
  lightPathSystemPrompt,
} from './openai-client.js';

describe('completeChatCompletion', () => {
  it('returns choices[0].message.content', async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(
        JSON.stringify({
          choices: [{ message: { role: 'assistant', content: 'hello from qwen' } }],
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );

    const text = await completeChatCompletion({
      baseUrl: 'https://example.test/v1',
      apiKey: 'sk-test',
      model: 'qwen3.6-35b',
      messages: [{ role: 'user', content: 'hi' }],
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    expect(text).toBe('hello from qwen');
    expect(fetchImpl).toHaveBeenCalledOnce();
    const call = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toBe('https://example.test/v1/chat/completions');
    expect(call[1].method).toBe('POST');
    const body = JSON.parse(String(call[1].body));
    expect(body.model).toBe('qwen3.6-35b');
    expect(body.messages[0].content).toBe('hi');
  });

  it('strips trailing slash on baseUrl', async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(
        JSON.stringify({
          choices: [{ message: { content: 'ok' } }],
        }),
        { status: 200 },
      ),
    );

    await completeChatCompletion({
      baseUrl: 'https://example.test/v1/',
      apiKey: 'k',
      model: 'm',
      messages: [{ role: 'user', content: 'x' }],
      fetchImpl: fetchImpl as unknown as typeof fetch,
    });

    const call = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
    expect(call[0]).toBe('https://example.test/v1/chat/completions');
  });

  it('throws on HTTP error', async () => {
    const fetchImpl = vi.fn(async () =>
      new Response('nope', { status: 401 }),
    );

    await expect(
      completeChatCompletion({
        baseUrl: 'https://example.test/v1',
        apiKey: 'bad',
        model: 'm',
        messages: [{ role: 'user', content: 'x' }],
        fetchImpl: fetchImpl as unknown as typeof fetch,
      }),
    ).rejects.toBeInstanceOf(OpenAICompatError);
  });

  it('throws when content missing', async () => {
    const fetchImpl = vi.fn(async () =>
      new Response(JSON.stringify({ choices: [] }), { status: 200 }),
    );

    await expect(
      completeChatCompletion({
        baseUrl: 'https://example.test/v1',
        apiKey: 'k',
        model: 'm',
        messages: [{ role: 'user', content: 'x' }],
        fetchImpl: fetchImpl as unknown as typeof fetch,
      }),
    ).rejects.toThrow(/missing choices/);
  });
});

describe('lightPathSystemPrompt', () => {
  it('mentions assistant name and no tools', () => {
    const p = lightPathSystemPrompt('Ghost');
    expect(p).toContain('Ghost');
    expect(p.toLowerCase()).toContain('no tools');
  });
});
