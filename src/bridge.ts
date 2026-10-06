import { spawn, type ChildProcess } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export type Report = { status: string; [key: string]: Json };
export interface ModelRequest {
  type: 'model_request';
  id: number;
  role: string;
  system: string;
  prompt: string;
}
export interface BridgeOptions {
  python: string;
  timeoutMs: number;
  cancelGraceMs: number;
  maxMessageBytes: number;
}
export type Complete = (request: ModelRequest, signal: AbortSignal) => Promise<string>;

const runtime = fileURLToPath(new URL('../runtime/', import.meta.url));
const bootstrap = 'import sys; sys.path.insert(0, sys.argv[1]); from agent.harness_bridge import main; raise SystemExit(main())';
const roles = new Set(['constructor', 'critic', 'guardian', 'learning', 'knowledge_review']);

function environment(): NodeJS.ProcessEnv {
  const keep = new Set(['PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT',
    'TEMP', 'TMP', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMDRIVE', 'HOME', 'USERPROFILE']);
  return Object.fromEntries(Object.entries(process.env).filter(([key]) => keep.has(key.toUpperCase())));
}

async function forceStop(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null || !child.pid) return;
  if (process.platform === 'win32') {
    await new Promise<void>((resolve) => {
      const killer = spawn(join(process.env.SystemRoot ?? process.env.SYSTEMROOT ?? 'C:\\Windows', 'System32', 'taskkill.exe'),
        ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' });
      killer.once('error', () => { child.kill(); resolve(); });
      killer.once('close', () => { if (child.exitCode === null) child.kill(); resolve(); });
    });
  } else {
    try { process.kill(-child.pid, 'SIGKILL'); } catch { child.kill('SIGKILL'); }
  }
}

/** One process per invocation: no shell interpolation or model-chosen executable. */
export async function invokeBridge(
  options: BridgeOptions, request: Record<string, Json>,
  complete: Complete, signal: AbortSignal,
): Promise<Report> {
  signal.throwIfAborted();
  const abort = new AbortController();
  const relay = () => abort.abort(signal.reason);
  signal.addEventListener('abort', relay, { once: true });
  const timer = setTimeout(() => abort.abort(new Error('AI Native Agent run timed out')), options.timeoutMs);
  const child = spawn(options.python, ['-I', '-u', '-B', '-c', bootstrap, runtime], {
    stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
    detached: process.platform !== 'win32', env: environment(),
  });
  let launchError: Error | undefined;
  const closed = new Promise<number | null>((resolve) => {
    child.once('error', (error) => { launchError = error; resolve(null); });
    child.once('close', (code) => resolve(code));
  });
  // Never echo child stderr: report transport failures independently of payloads.
  child.stderr.resume();
  child.stdin.on('error', () => {});
  let killer: NodeJS.Timeout | undefined;
  let stopping: Promise<void> | undefined;
  const send = (value: unknown) => {
    const line = JSON.stringify(value) + '\n';
    if (Buffer.byteLength(line) > options.maxMessageBytes) throw new Error('Harness bridge message too large');
    if (child.stdin.destroyed) throw new Error('Harness bridge input closed');
    child.stdin.write(line);
  };
  const cancel = () => {
    try { send({ type: 'cancel' }); } catch { /* Child may already be closed. */ }
    killer = setTimeout(() => { stopping = forceStop(child); }, options.cancelGraceMs);
  };
  abort.signal.addEventListener('abort', cancel, { once: true });
  let report: Report | undefined;
  let pending = Buffer.alloc(0);
  let lastRequestId = 0;
  try {
    if (signal.aborted) relay();
    abort.signal.throwIfAborted();
    send(request);
    for await (const data of child.stdout) {
      pending = Buffer.concat([pending, data as Buffer]);
      let newline: number;
      while ((newline = pending.indexOf(10)) >= 0) {
        if (newline + 1 > options.maxMessageBytes) throw new Error('Harness bridge message too large');
        const line = pending.subarray(0, newline).toString('utf8');
        pending = pending.subarray(newline + 1);
        const message = JSON.parse(line);
        if (report) throw new Error('Unexpected data after Harness result');
        if (message?.type === 'model_request') {
          if (!Number.isInteger(message.id) || message.id !== ++lastRequestId ||
              !roles.has(message.role) || typeof message.system !== 'string' || typeof message.prompt !== 'string') {
            throw new Error('Invalid Harness model request');
          }
          if (abort.signal.aborted) continue;
          try {
            const text = await complete(message, abort.signal);
            if (!abort.signal.aborted) send({ type: 'model_response', id: message.id, text });
          } catch {
            if (!abort.signal.aborted) send({ type: 'model_response', id: message.id, error: 'LLM_FAILED' });
          }
        } else if (message?.type === 'result' && message.value &&
                   typeof message.value.status === 'string' && !Array.isArray(message.value)) {
          report = message.value as Report;
        } else if (message?.type === 'error') {
          throw new Error(typeof message.message === 'string' ? message.message : 'Harness bridge failed');
        } else {
          throw new Error('Invalid Harness bridge response');
        }
      }
      if (pending.length > options.maxMessageBytes) throw new Error('Harness bridge message too large');
    }
    const code = await closed;
    abort.signal.throwIfAborted();
    if (launchError) throw new Error('Cannot start configured Python executable: ' + options.python);
    if (code !== 0 || !report || pending.length) throw new Error('Python bridge exited without a complete result');
    return report;
  } finally {
    if (child.exitCode === null && child.signalCode === null && !launchError && !abort.signal.aborted) {
      abort.abort(new Error('Harness bridge stopped'));
    }
    await closed;
    if (stopping) await stopping;
    clearTimeout(timer);
    clearTimeout(killer);
    signal.removeEventListener('abort', relay);
    abort.signal.removeEventListener('abort', cancel);
    child.stdin.destroy();
  }
}
