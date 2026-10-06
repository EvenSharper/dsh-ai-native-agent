import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, mkdir, writeFile, readFile, readdir, access } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { execFileSync } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { Context } from '@deepseek-ai/cordis';
import LlmRuntime, { LlmAdapter } from '@deepseek-ai/dsh-llm';
import ToolRuntime from '@deepseek-ai/dsh-tools';
import SystemPrompt from '@deepseek-ai/dsh-system-prompt';
import * as plugin from '../lib/index.js';
import { completeRole } from '../lib/llm.js';

const python = process.env.AI_NATIVE_TEST_PYTHON ?? 'python';
const pythonPath = execFileSync(python, ['-c', 'import sys; print(sys.executable)'], { encoding: 'utf8', windowsHide: true }).trim();
const roles = new Map();
for (const role of ['constructor', 'critic', 'guardian', 'learning', 'knowledge_review']) {
  roles.set(await readFile(new URL('../runtime/agent/prompts/' + role + '.md', import.meta.url), 'utf8'), role);
}
const exists = async (path) => access(path).then(() => true, () => false);

async function fixture() {
  // Keep each temporary fixture for diagnosis; tests never delete a user directory.
  const root = await mkdtemp(join(tmpdir(), 'ai-native-harness-'));
  const repo = join(root, 'repo');
  const state = join(root, 'state');
  await mkdir(join(repo, 'tests'), { recursive: true });
  await mkdir(state);
  await writeFile(join(repo, 'value.py'), 'value = 0\n');
  await writeFile(join(repo, 'tests', 'test_value.py'),
    'import unittest\nfrom value import value\nclass Contract(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value, 2)\n');
  const git = (...args) => execFileSync('git', ['-c', 'core.hooksPath=' + join(root, 'empty-hooks'),
    '-c', 'commit.gpgsign=false', ...args], { cwd: repo, encoding: 'utf8', windowsHide: true });
  await mkdir(join(root, 'empty-hooks'));
  git('init', '--quiet'); git('config', 'core.autocrlf', 'false'); git('add', '.');
  git('-c', 'user.name=Harness test', '-c', 'user.email=test@localhost', 'commit', '--quiet', '-m', 'fixture');
  const configPath = join(state, 'agent.json');
  const config = { schema_version: 1, repo, provider: { type: 'harness' },
    editable_paths: ['value.py'], protected_paths: ['tests/**'], max_rounds: 3,
    verification: [{ name: 'tests', argv: [pythonPath, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'], timeout_seconds: 10 }] };
  await writeFile(configPath, JSON.stringify(config));
  await writeFile(join(state, 'system_record.json'), JSON.stringify({
    schema_version: 1, domain_concepts: [], invariants: [], truth_sources: [], state_transitions: [],
    external_contracts: [], known_failure_modes: [], forbidden_simplifications: [], decisions: [], unknowns: [], knowledge: [],
  }));
  return { root, repo, state, configPath, config, git };
}

class FixtureAdapter extends LlmAdapter {
  calls = [];
  round = 0;
  constructor(answer) { super(); this.answer = answer; }
  async *stream(options) {
    this.calls.push(options);
    const role = roles.get(options.system);
    assert.ok(role, 'one of the packaged role prompts must be used');
    const payload = JSON.parse(options.messages[0].content[0].text);
    assert.equal(options.tools, undefined);
    const value = await this.answer(role, payload, options);
    const text = typeof value === 'string' ? value : JSON.stringify(value);
    yield { type: 'block-start', index: 0, blockType: 'text' };
    yield { type: 'text-delta', index: 0, text };
    yield { type: 'block-end', index: 0, block: { type: 'text', text } };
    yield { type: 'finish', reason: { kind: 'stop' } };
  }
}

function answers({ revise = false, guardian = 'GREEN' } = {}) {
  let round = 0;
  return (role, { untrusted_inputs: data }) => {
    if (role === 'constructor') {
      round++;
      if (revise && round === 2) assert.equal(data.previous_round_feedback.reason, 'Verification failed');
      return { intent: 'Set value to the contractual value',
        changes: [{ path: 'value.py', content: 'value = ' + (revise && round === 1 ? 1 : 2) + '\n' }],
        side_effects: ['local_files'] };
    }
    if (role === 'guardian') return { risk: guardian, reasons: ['fixture review'], human_review_required: false };
    if (role === 'critic' || role === 'knowledge_review') return { verdict: 'ACCEPT', reasons: ['fixture evidence review'] };
    if (role === 'learning') return { claims: [{
      statement: 'Candidate passes the value contract', source: 'verification', confidence: 'high',
      scope: 'value.py', kind: 'fact', validity: 'This candidate only',
      evidence_ids: data.verification.results.map((item) => item.evidence_id),
    }] };
    throw new Error('Unexpected role');
  };
}

async function app(t, f, answer = answers(), extra = {}) {
  const ctx = new Context();
  const fibers = [];
  const use = async (p, config) => { const fiber = ctx.plugin(p, config); fibers.push(fiber); await fiber.await(); return fiber; };
  t.after(async () => { for (const fiber of fibers.reverse()) await fiber.dispose(); });
  await use(SystemPrompt);
  await use(LlmRuntime);
  await use(ToolRuntime);
  const adapter = new FixtureAdapter(answer);
  await use({ name: 'fixture-model', inject: ['llm'], apply(c) { c.llm.registerAdapter(['fixture'], adapter); } });
  const settings = { projects: [{ id: 'sample', configPath: f.configPath }], python,
    provider: 'fixture', model: 'fixture-model', allowExecution: true, ...extra };
  const fiber = await use(plugin, settings);
  let sequence = 0;
  const call = (name, args = {}, signal = new AbortController().signal) => ctx.tools.execute({
    callId: 'fixture-' + ++sequence, name, arguments: args, signal,
  });
  return { ctx, fiber, adapter, settings, call };
}

test('real Cordis/Tools/LLM -> Python -> failed verification -> revision -> reviewed patch and knowledge', async (t) => {
  const f = await fixture();
  const a = await app(t, f, answers({ revise: true }));
  const response = await a.call('ai_native_run', { project: 'sample', goal: '将 value 修正为 2' });
  assert.equal(response.isError, false, JSON.stringify(response));
  const report = response.value;
  assert.equal(report.status, 'ACCEPTED', JSON.stringify(report));
  assert.equal(report.rounds, 2);
  assert.equal(report.knowledge_status, 'saved');
  assert.equal(report.source_applied, false);
  assert.equal(await readFile(join(f.repo, 'value.py'), 'utf8'), 'value = 0\n');
  assert.equal(f.git('status', '--porcelain'), '');
  f.git('apply', '--check', report.patch);
  assert.equal(a.adapter.calls.length, 8);
  const inspect = await a.call('ai_native_inspect', { project: 'sample', run_id: report.run_id });
  assert.deepEqual(inspect.value, report);
  assert.equal(await exists(join(f.state, 'system_record.json.lock')), false);
});

test('execution remains disabled when model supplies an extra permission argument', async (t) => {
  const f = await fixture();
  const a = await app(t, f, answers(), { allowExecution: false });
  const response = await a.call('ai_native_run', { project: 'sample', goal: 'fix', allow_execution: true });
  assert.equal(response.isError, false);
  assert.equal(response.value.status, 'HUMAN_REQUIRED');
  assert.equal(a.adapter.calls.length, 0);
  assert.equal(await exists(join(response.value.run_dir, 'worktree')), false);
});

test('Guardian yellow stops before editing or verification', async (t) => {
  const f = await fixture();
  const a = await app(t, f, answers({ guardian: 'YELLOW' }));
  const result = (await a.call('ai_native_run', { project: 'sample', goal: 'fix' })).value;
  assert.equal(result.status, 'HUMAN_REQUIRED');
  assert.equal(await exists(join(result.run_dir, 'accepted.patch')), false);
  assert.equal(await readFile(join(result.run_dir, 'worktree', 'value.py'), 'utf8'), 'value = 0\n');
});

test('duplicate JSON fields cannot bypass Python schema validation', async (t) => {
  const f = await fixture();
  const a = await app(t, f, () => '{"intent":"a","intent":"b","changes":[],"side_effects":[]}');
  const result = (await a.call('ai_native_run', { project: 'sample', goal: 'fix' })).value;
  assert.equal(result.status, 'ERROR');
  assert.match(result.reasons.join(' '), /JSON\/schema/);
  assert.equal(await exists(join(result.run_dir, 'accepted.patch')), false);
});

test('project IDs and report IDs cannot select arbitrary files', async (t) => {
  const f = await fixture();
  const a = await app(t, f);
  assert.equal((await a.call('ai_native_run', { project: f.configPath, goal: 'fix' })).isError, true);
  assert.equal((await a.call('ai_native_inspect', { project: 'sample', run_id: '../secrets' })).isError, true);
  assert.equal(a.adapter.calls.length, 0);
});

test('unload cancels an active model request, releases the lock, unregisters tools, and permits reload', async (t) => {
  const f = await fixture();
  let entered;
  const started = new Promise((resolve) => { entered = resolve; });
  const a = await app(t, f, async (_role, _payload, options) => {
    entered();
    await delay(60_000, undefined, { signal: options.signal });
    return {};
  });
  const running = a.call('ai_native_run', { project: 'sample', goal: 'fix' });
  await started;
  assert.equal((await a.call('ai_native_run', { project: 'sample', goal: 'other' })).isError, true);
  await a.fiber.dispose();
  assert.equal((await running).isError, true);
  assert.equal(await exists(join(f.state, 'system_record.json.lock')), false);
  const dirs = await readdir(join(f.state, 'runs'));
  const report = JSON.parse(await readFile(join(f.state, 'runs', dirs[0], 'result.json'), 'utf8'));
  assert.equal(report.status, 'CANCELLED');
  assert.equal(a.ctx.tools.schemas().filter((s) => s.name.startsWith('ai_native_')).length, 0);
  const replacement = a.ctx.plugin(plugin, a.settings);
  await replacement.await();
  assert.equal(a.ctx.tools.schemas().filter((s) => s.name.startsWith('ai_native_')).length, 3);
  await replacement.dispose();
});

test('cancellation interrupts an actual verification process and records CANCELLED', async (t) => {
  const f = await fixture();
  const pidPath = join(f.root, 'verification.pid');
  f.config.verification = [{ name: 'slow', timeout_seconds: 120, argv: [pythonPath, '-B', '-c',
    'import os,time; from pathlib import Path; Path(' + JSON.stringify(pidPath) + ').write_text(str(os.getpid())); time.sleep(90)'] }];
  await writeFile(f.configPath, JSON.stringify(f.config));
  const a = await app(t, f);
  const abort = new AbortController();
  const running = a.call('ai_native_run', { project: 'sample', goal: 'fix' }, abort.signal);
  for (let n = 0; n < 500 && !await exists(pidPath); n++) await delay(20);
  assert.equal(await exists(pidPath), true, 'verification must actually start');
  const pid = Number(await readFile(pidPath, 'utf8'));
  abort.abort(new Error('test cancellation'));
  assert.equal((await running).isError, true);
  assert.throws(() => process.kill(pid, 0), /ESRCH|no such process/);
  assert.equal(await exists(join(f.state, 'system_record.json.lock')), false);
  const dirs = await readdir(join(f.state, 'runs'));
  const report = JSON.parse(await readFile(join(f.state, 'runs', dirs[0], 'result.json'), 'utf8'));
  assert.equal(report.status, 'CANCELLED');
});

test('missing Python executable is a tool error, not a successful domain result', async (t) => {
  const f = await fixture();
  const a = await app(t, f, answers(), { python: join(f.root, 'missing-python') });
  const response = await a.call('ai_native_run', { project: 'sample', goal: 'fix' });
  assert.equal(response.isError, true);
});

test('pre-aborted tool never starts a run', async (t) => {
  const f = await fixture();
  const a = await app(t, f);
  const abort = new AbortController(); abort.abort();
  assert.equal((await a.call('ai_native_run', { project: 'sample', goal: 'fix' }, abort.signal)).isError, true);
  assert.equal(await exists(join(f.state, 'runs')), false);
});

test('role streams must finish normally and cannot return tool calls or oversized text', async () => {
  const config = { provider: 'fixture', model: 'fixture', maxTokens: 20, modelTimeoutMs: 1000, maxMessageBytes: 1024 };
  const request = { type: 'model_request', id: 1, role: 'constructor', system: 'prompt', prompt: 'payload' };
  const variants = [
    [{ type: 'block-start', index: 0, blockType: 'tool-call' }],
    [{ type: 'finish', reason: { kind: 'max-tokens' } }],
    [{ type: 'block-start', index: 0, blockType: 'text' }, { type: 'text-delta', index: 0, text: 'x'.repeat(2000) }],
    [],
  ];
  for (const chunks of variants) {
    const ctx = { llm: { async *stream() { yield* chunks; } } };
    await assert.rejects(completeRole(ctx, config, request, new AbortController().signal));
  }
});
