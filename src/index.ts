import { realpathSync, statSync } from 'node:fs';
import { isAbsolute } from 'node:path';
import { Service, type Context } from '@deepseek-ai/cordis';
import Schema from '@deepseek-ai/schemastery';
import { defineTool } from '@deepseek-ai/dsh-tools';
import { invokeBridge, type Report, type Json } from './bridge.js';
import { completeRole } from './llm.js';

export const name = 'ai-native-agent';
export const inject = ['tools', 'llm'];

export interface Config {
  projects: { id: string; configPath: string }[];
  python: string;
  provider: string;
  model: string;
  allowExecution: boolean;
  timeoutMs: number;
  modelTimeoutMs: number;
  cancelGraceMs: number;
  maxTokens: number;
  maxMessageBytes: number;
}
export const Config: Schema<Config> = Schema.object({
  projects: Schema.array(Schema.object({
    id: Schema.string().required(),
    configPath: Schema.string().required(),
  })).default([]),
  python: Schema.string().default('python'),
  provider: Schema.string().default(''),
  model: Schema.string().default(''),
  allowExecution: Schema.boolean().default(false),
  timeoutMs: Schema.number().min(1000).max(86_400_000).step(1).default(1_800_000),
  modelTimeoutMs: Schema.number().min(1000).max(3_600_000).step(1).default(180_000),
  cancelGraceMs: Schema.number().min(1000).max(60_000).step(1).default(10_000),
  maxTokens: Schema.number().min(1).step(1).default(16_384),
  maxMessageBytes: Schema.number().min(1024).max(32_000_000).step(1).default(32_000_000),
});

declare module '@deepseek-ai/cordis' {
  interface Context { aiNativeAgent: AiNativeAgent }
}

/** Service API reused by tools and available to other Cordis consumers. */
export class AiNativeAgent extends Service {
  private readonly lifetime = new AbortController();
  private readonly active = new Set<Promise<Report>>();
  private readonly running = new Set<string>();
  private readonly projects: Map<string, string>;

  constructor(ctx: Context, private readonly options: Config) {
    // Validate before publishing a service or registering any tools.
    const entries = options.projects.map(({ id, configPath }) => {
      if (!/^[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(id)) throw new Error('Invalid project id: ' + id);
      if (!isAbsolute(configPath) || !statSync(configPath).isFile()) throw new Error('configPath must be an existing absolute file');
      return [id, realpathSync(configPath)] as const;
    });
    if (new Set(entries.map(([id]) => id)).size !== entries.length) throw new Error('Duplicate project id');
    if (entries.length && (!options.provider.trim() || !options.model.trim())) {
      throw new Error('Configured projects require an explicit Harness provider and model');
    }
    if (!options.python.trim()) throw new Error('python executable cannot be empty');
    super(ctx, 'aiNativeAgent');
    this.projects = new Map(entries);
    ctx.effect(() => () => {
      this.lifetime.abort(new Error('AI Native Agent plugin unloaded'));
      return Promise.allSettled([...this.active]).then(() => {});
    });
  }

  listProjects() {
    return {
      projects: [...this.projects.keys()].map((id) => ({ id })),
      allow_execution: this.options.allowExecution,
      provider: this.options.provider, model: this.options.model,
    };
  }

  private invoke(project: string, operation: 'run' | 'inspect', goalOrId: string, signal: AbortSignal): Promise<Report> {
    signal.throwIfAborted();
    this.lifetime.signal.throwIfAborted();
    const path = this.projects.get(project);
    if (!path) throw new Error('Unknown project; call ai_native_projects for configured IDs');
    if (!goalOrId.trim()) throw new Error('goal or run_id must be nonempty');
    if (operation === 'run' && this.running.has(path)) throw new Error('This project already has an active run');
    if (operation === 'run') this.running.add(path);
    const request: Record<string, Json> = operation === 'run'
      ? { type: 'run', config_path: path, goal: goalOrId, allow_execution: this.options.allowExecution }
      : { type: 'inspect', config_path: path, run_id: goalOrId };
    const task = invokeBridge(this.options, request,
      (message, callSignal) => completeRole(this.ctx, this.options, message, callSignal),
      AbortSignal.any([signal, this.lifetime.signal]));
    this.active.add(task);
    void task.finally(() => {
      this.active.delete(task);
      if (operation === 'run') this.running.delete(path);
    }).catch(() => {});
    return task;
  }

  run(project: string, goal: string, signal: AbortSignal) { return this.invoke(project, 'run', goal, signal); }
  inspect(project: string, runId: string, signal: AbortSignal) { return this.invoke(project, 'inspect', runId, signal); }
}

const reportSchema = {
  type: 'object', additionalProperties: true,
  properties: { status: { type: 'string', required: true } },
} as const;

function renderReport(_args: unknown, value: Report) {
  const keys = ['status', 'run_id', 'rounds', 'run_dir', 'patch', 'audit_log', 'source_applied',
    'knowledge_status', 'reasons', 'stopped_at', 'review', 'guardian', 'knowledge_error'];
  const summary = Object.fromEntries(keys.filter((key) => key in value).map((key) => [key, value[key]]));
  return [{ type: 'text' as const, text: JSON.stringify(summary, null, 2) }];
}

export function apply(ctx: Context, config: Config) {
  const service = new AiNativeAgent(ctx, config);
  ctx.tools.register(defineTool({
    name: 'ai_native_projects',
    description: 'List configured AI Native Agent projects and execution availability.',
    parameters: {},
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }],
    },
    async execute(_args, exec) { exec.signal.throwIfAborted(); return service.listProjects(); },
  }));
  ctx.tools.register(defineTool({
    name: 'ai_native_run',
    description: 'Run Constructor, Critic and Guardian on a configured project. Verifies changes in a separate Git clone and returns an accepted patch, review outcome and audit paths. ACCEPTED is a candidate; source files are not automatically updated. Host configuration controls command execution.',
    parameters: {
      project: { type: 'string', required: true, description: 'ID from ai_native_projects' },
      goal: { type: 'string', required: true, description: 'Concrete code change requested by the user' },
    },
    timeoutMs: config.timeoutMs,
    output: { schema: reportSchema, render: renderReport },
    async execute(args, exec) { return service.run(args.project, args.goal, exec.signal); },
  }));
  ctx.tools.register(defineTool({
    name: 'ai_native_inspect',
    description: 'Read a saved AI Native Agent report, including verification evidence, by configured project and run ID.',
    parameters: {
      project: { type: 'string', required: true },
      run_id: { type: 'string', required: true },
    },
    output: { schema: reportSchema, render: renderReport },
    async execute(args, exec) { return service.inspect(args.project, args.run_id, exec.signal); },
  }));
}
