# AI Native Agent for DeepSeek Harness

[中文](README.md) | **English**

Runs an existing Python AI Native Agent as a tool plugin for DeepSeek Harness. Constructor, Critic, Guardian, learning proposals and knowledge review all share the Harness `llm` service, while the Git clone, file scope, real verification, auditing and System Record logic stay in the Python core.

Pinned to **Harness 0.2.0-rc.2 / Cordis 4.0.4**. Requires Node.js 22+ (for development builds), Python 3.11+ and Git. The prebuilt package already contains the Python core, so no pip install is needed and it does not depend on absolute paths from a development directory.

## Why do this

The ability to write code can be delegated to a model. The **authority to change a real repository cannot**. Every design decision in this plugin serves that single distinction: the model only proposes and judges, deterministic code does the file writing, command running and knowledge persistence, and there are explicit gates between the two. The role prompts can be pointed at any model you like; the discipline does not change with it.

The three lines of responsibility are kept sharp:

| Layer | Owner | What it covers |
| --- | --- | --- |
| Intelligence | Harness (replaceable) | Role prompts, `llm` routing and adapters, model credentials |
| Discipline | Python core (fixed) | Git clone isolation, path allowlists, real verification, auditing, System Record |
| Decision | Human | Whether the patch is applied to the original repository |

So the model never holds an API key (the Harness adapter keeps the credentials and passes only a route and model ID to Python), and it cannot invent its own verification commands (those come from trusted configuration and cannot be added by tool arguments or model proposals).

### Key trade-offs

**One complete candidate per round, not incremental patches.** Every proposal is applied as a complete candidate relative to `base_commit`: `reset --hard` first, then any leftovers from the previous round are removed, then the current round is written out in full. On a revision the model must restate the whole intent instead of patching its previous output. This prevents incremental edits from drifting and accumulating across the revision loop, and keeps "candidate N" independently reviewable.

**Knowledge must be anchored to real verification evidence.** Every `evidence_ids` entry a learning proposal claims must resolve to an ID produced by this round's actual verification, with `returncode == 0`, not timed out, and deduplicated; otherwise the claim is treated as fabricated and is not saved. The model cannot simply assert that something was verified: a claim that "passes the boundary tests" but cites an evidence ID from a failed command, or one that does not exist, causes the entire knowledge proposal to be rejected.

**Guardian gate before and after the edits.** Risk is assessed once at the proposal stage and again once the candidate exists; YELLOW/RED, or an explicit request for human review, stops the run. The first gate rejects high-risk intent before any file is written, and the second prevents the candidate from being altered after verification.

**Measure the difference rather than trust the statement.** The candidate directory is snapshotted with SHA-256 before and after every operation and compared file by file, and after verification the candidate diff is re-checked against the diff that was verified. If verification itself mutates the candidate, its evidence is invalidated. A verification report also cannot claim success: an aggregate flag cannot cover a failed command or missing evidence.

**`ACCEPTED` does not mean "applied".** Once verification and review pass, an `accepted.patch` is exported and **not a single byte of the original repository is modified**. Whether to apply it is a human decision.

**Code acceptance is not knowledge acceptance.** Patch acceptance and knowledge persistence are separate outcomes: knowledge goes through one more Critic review, and if it is rejected the patch is still delivered while the knowledge is not written, with the reason reported.

### What one run looks like

```text
ai_native_run(project, goal)
  └─ acquire the System Record file lock; read repository context and System Record
      └─ round loop (bounded by max_rounds)
          1. Constructor proposes a complete candidate + side_effects
          2. Guardian precheck ── not GREEN or human review requested → HUMAN_REQUIRED (no file written)
          3. write into the isolated clone (reset --hard to base_commit, then stage in full)
          4. run the trusted verification commands ── failure → back to 1 with evidence
          5. Critic review ── BLOCK → BLOCKED; REVISE → back to 1
          6. Guardian final check ── not GREEN → HUMAN_REQUIRED
          7. export accepted.patch
          8. learning proposal → evidence validation → knowledge review
      └─ return ACCEPTED + patch path + verification evidence + audit path (original repository untouched)
```

Two details are worth noting. The `reset --hard` in step 3 means **every round restarts from `base_commit`**: round two does not continue on top of round one's edits, it redoes the entire candidate. And after step 6 the diff is checked once more, so the candidate that was reviewed is the candidate that gets exported.

## Capabilities

**A candidate patch, never applied directly.** Each `ai_native_run` produces a complete patch together with verification evidence, review conclusions and an audit trail, while the original repository stays clean (the test suite asserts that `git status` is empty). Review the patch and apply it through your existing process.

**Domain statuses instead of vague failure.** A ready patch is `ACCEPTED` (awaiting application), a Critic block is `BLOCKED`, a non-GREEN Guardian is `HUMAN_REQUIRED`, an exhausted revision budget is `EXHAUSTED`, cancellation is `CANCELLED`, and a model or verification fault captured by the core is `ERROR`. These are all inspectable domain outcomes, kept distinct from "the tool call errored".

**Hard boundaries.** `editable_paths` is an allowlist, and beyond `protected_paths` there is a set of paths that can **never be changed** (tests, `agent.json`, `system_record*`, `.git`, credential and key files, and more) regardless of what the model claims. A single file change is capped at 500KB, a proposal at 50 files, workspace context at 120KB and command output at 120KB.

**Execution isolation and cancellation.** The candidate is prepared in an independent clone (sharing no index, object store or refs) and `origin` is removed. Verification commands run under a converged environment with HOME redirected into the run directory, and on Windows a Job Object constrains descendant processes so the whole tree is terminated on timeout or teardown. Cancelling the tool call or unloading the plugin sends a cancellation to Python and aborts the LLM call; Python stops the verification subprocess, releases the record lock and records `CANCELLED`.

**Concurrency protection.** One run at a time per configuration file, enforced across instances by the System Record file lock, which is released when the run finishes or aborts. A lock left behind by a killed process or a host crash is deliberately not stolen: confirm the process is gone and handle it by hand. That is an intentional choice, so that two runs never write the same record concurrently.

**Audit and knowledge.** The full event stream of a run is written to `audit.jsonl` and the final report to `result.json`. Knowledge that passes review is written into the System Record at candidate scope (marked `applicability: verified_candidate_only` and `applied_to_source: false`) and supports `supersedes` relationships.

## Build and install

From the root of this repository:

```powershell
npm.cmd ci
npm.cmd test
npm.cmd pack
```

This produces `dsh-ai-native-agent-0.1.0.tgz`. `npm run build` generates `lib/` and syncs the Python core from `runtime/agent`; in a standalone checkout `runtime/agent` is committed to the repository, so the build uses that copy as-is. Install into the Harness profile you use (`web`, `desktop` and so on -- substitute your own profile name):

```powershell
# web profile
dsh plugin --profile web add C:/absolute/path/dsh-ai-native-agent-0.1.0.tgz
dsh --profile web --dump-config

# desktop profile
dsh plugin --profile desktop add C:/absolute/path/dsh-ai-native-agent-0.1.0.tgz
dsh --profile desktop --dump-config
```

The `dsh.bundle.patch` manifest entry inserts the plugin row with ID `ai-native-agent` automatically. The default project list is empty and execution is off, so you can install first and configure later. This repository ships a prebuilt tarball; there is no prepare step for installing directly from Git.

## Configuring a project

The target must be a committed, clean Git repository, and the state directory must live outside it. Initialize from the root of this repository:

```powershell
python -m agent init --repo C:/work/my-project --state-dir C:/work/my-project-agent --provider harness --editable "src/**"
```

If you only have the installed tarball and not this repository, you can instead create the two files following `examples/agent.json` and `examples/system_record.json`. Adjust `repo`, `editable_paths`, `protected_paths`, `verification` and `max_rounds`. Verification commands come from this trusted configuration and cannot be added by tool arguments or model proposals. Keep:

```json
"provider": { "type": "harness" }
```

This provider exists only for the plugin bridge; running `python -m agent run` directly reports a clear error.

Copy `examples/project.patch.yml` and fill it in:

```yaml
- id: ai-native-agent
  config:
    python: C:/Python314/python.exe
    provider: YOUR_HARNESS_PROVIDER_ID
    model: YOUR_HARNESS_MODEL_ID
    projects:
      - id: my-project
        configPath: C:/work/my-project-agent/agent.json
    allowExecution: true
```

`provider` and `model` must be **routes and model IDs already configured in Harness**, not API URLs. Credentials are managed by the Harness adapter: they are never written into the plugin configuration and never sent to Python. All roles use the model selected here, called independently each time; there is no attempt to manufacture three "votes" for a single judgement.

`allowExecution: true` permits the locally configured verification commands to run. While it stays `false` the run returns `HUMAN_REQUIRED` without calling a model or creating a candidate clone. The Git clone isolates file modifications, but the Python executor does not go through the Harness shell/OS sandbox; enable it only for trusted projects or inside a dedicated execution environment. This is the execution boundary of the existing agent.

Start with that configuration:

```powershell
dsh web --patch C:/work/project.patch.yml
```

You can also merge the override row into the profile's `cordis.patch.yml`. A patch replaces the entire `config` row, so an override must restate every custom field. Do not insert a second row with the same ID.

## Using it from Harness

A typical prompt:

> List ai_native_projects, then use ai_native_run on my-project to fix the specified issue while preserving the existing test contract.

Tool interface:

| Tool | Arguments | Returns |
| --- | --- | --- |
| `ai_native_projects` | none | Project IDs, execution switch, model route |
| `ai_native_run` | `project`, `goal` | Status, run_id, patch location, review and verification evidence |
| `ai_native_inspect` | `project`, `run_id` | The complete saved report |

The full result is canonical JSON that Harness programmatic tool calls can read directly. The text rendering only summarises status, paths and review outcomes; complete verification output is also stored in `result.json` and `audit.jsonl`. The plugin additionally exposes `ctx.aiNativeAgent` with `listProjects()`, `run(project, goal, signal)` and `inspect(project, runId, signal)` for other Cordis consumers.

`ACCEPTED` means the patch passed verification and review and that **the original repository has not been modified yet**. Inspect the returned `accepted.patch` and apply it through your existing process. A Guardian YELLOW/RED returns `HUMAN_REQUIRED`, a Critic BLOCK returns `BLOCKED`, and an exhausted revision budget returns `EXHAUSTED`. These are all inspectable domain outcomes; model or verification faults captured by the core return `ERROR`, while bridge startup, protocol or plugin configuration faults surface as tool errors.

Cancelling the call or unloading the plugin sends a cancellation request to Python and aborts the LLM call; Python stops the verification subprocess, records `CANCELLED` and releases the record lock. Only one run is allowed per configuration file at a time, protected across instances by the System Record file lock. Force-killing the process or a host crash can still leave a lock behind; confirm the process has exited and then handle it by hand. The plugin does not steal an existing lock or automatically resume an interrupted run.

## Tunable parameters

| Field | Default | Meaning |
| --- | --- | --- |
| `projects` | `[]` | Project ID and absolute path to agent.json |
| `python` | `python` | Python executable, not a shell command |
| `provider`, `model` | empty | Required once projects are configured |
| `allowExecution` | `false` | Permission to execute verification locally |
| `timeoutMs` | 1800000 | Time limit for the whole tool call |
| `modelTimeoutMs` | 180000 | Time limit for a single role model call |
| `cancelGraceMs` | 10000 | Grace period before Python is force-terminated |
| `maxTokens` | 16384 | Token limit per role response |
| `maxMessageBytes` | 32000000 | Limit for one bridge line and collected model stream |

Role output must be a complete JSON object; responses with duplicate fields, truncation, tool calls or no normal finish marker are rejected. Harness does not require every adapter to support JSON mode, so the role prompts ask for JSON and Python independently validates structure and evidence. There is no automatic fallback to a fixed script or another vendor's model.

## Development verification

```powershell
python -B -m unittest discover -s tests -v
npm.cmd test
```

The plugin tests use real Cordis, a real tool registry, the real LLM service, real Python processes, Git and real verification commands; the model adapter serves clearly marked offline responses and consumes no API quota. Coverage includes revision after failure, knowledge persistence, Guardian blocking, disabled execution, path boundaries, response validation, cancelling a verification process, unload cleanup and reloading. Real vendor model quality still has to be validated with your own Harness model configuration.

Layout:

- `src/index.ts`: config, service, the three tools, lifecycle.
- `src/llm.ts`: consumes the Harness llm stream and checks completion.
- `src/bridge.ts`: bounded JSON-lines, subprocess, timeouts and cancellation.
- `runtime/agent/`: the shipped Python core and prompts. When developed alongside the upstream core repository, the build re-copies from `../agent/` or `../agent/agent/`; in a standalone checkout it uses this committed copy directly.
- `runtime/agent/harness_bridge.py`: the Python-side provider and run bridge.

Following the official documentation: [first plugin](https://deepseek-harness.github.io/deepseek-harness/en/develop/basic/), [tools](https://deepseek-harness.github.io/deepseek-harness/en/develop/basic/tool), [configuration](https://deepseek-harness.github.io/deepseek-harness/en/develop/basic/config), [packaging and installation](https://deepseek-harness.github.io/deepseek-harness/en/develop/basic/publish), [tool authoring contract](https://deepseek-harness.github.io/deepseek-harness/en/reference/cookbook/adding-a-tool). Shared Harness service packages use peerDependencies and keep matching devDependencies for standalone testing.
