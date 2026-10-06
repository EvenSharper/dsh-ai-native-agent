import type { Context } from '@deepseek-ai/cordis';
import { BlockAssembler } from '@deepseek-ai/dsh-llm';
import type { ModelRequest } from './bridge.js';

export interface ModelOptions {
  provider: string;
  model: string;
  maxTokens: number;
  modelTimeoutMs: number;
  maxMessageBytes: number;
}

/** All roles use the host's registered adapter; no API key enters Python. */
export async function completeRole(
  ctx: Context, config: ModelOptions, request: ModelRequest, outerSignal: AbortSignal,
): Promise<string> {
  const deadline = new AbortController();
  const timer = setTimeout(() => deadline.abort(new Error('Role model request timed out')), config.modelTimeoutMs);
  const signal = AbortSignal.any([outerSignal, deadline.signal]);
  const assembler = new BlockAssembler();
  const open = new Set<number>();
  let finished = false;
  let bytes = 0;
  try {
    signal.throwIfAborted();
    for await (const chunk of ctx.llm.stream({
      provider: config.provider, model: config.model, maxTokens: config.maxTokens,
      system: request.system, messages: [{ role: 'user', content: [{ type: 'text', text: request.prompt }] }],
      signal,
    })) {
      signal.throwIfAborted();
      if (finished) throw new Error('Data after LLM finish');
      if (chunk.type === 'block-start') {
        if (!['text', 'reasoning'].includes(chunk.blockType) || open.has(chunk.index)) {
          throw new Error('Unexpected model block');
        }
        open.add(chunk.index);
      } else if (chunk.type === 'block-end') {
        if (!open.delete(chunk.index) || !['text', 'reasoning'].includes(chunk.block.type)) {
          throw new Error('Unexpected model block end');
        }
      } else if (chunk.type === 'finish') {
        finished = true;
        if (chunk.reason.kind !== 'stop' || open.size) throw new Error('Incomplete or refused role response');
      }
      // Include deltas and final blocks in a conservative memory budget.
      bytes += Buffer.byteLength(JSON.stringify(chunk));
      if (bytes > config.maxMessageBytes) throw new Error('Role response exceeded size limit');
      assembler.push(chunk);
    }
    signal.throwIfAborted();
    if (!finished) throw new Error('Role stream ended without a finish reason');
    const text = assembler.blocks().filter((block) => block.type === 'text').map((block) => block.text).join('');
    if (!text.trim()) throw new Error('Role returned no JSON text');
    // Keep the exact string; Python detects duplicate fields and validates schemas.
    return text;
  } finally {
    clearTimeout(timer);
  }
}
