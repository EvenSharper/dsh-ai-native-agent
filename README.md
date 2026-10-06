# AI Native Agent for DeepSeek Harness

将现有 Python AI Native Agent 作为 Harness 的工具插件运行。Constructor、Critic、Guardian、学习提案和知识审查共用 Harness 的 `llm` 服务；原有 Git 副本、文件范围、真实验证、审计和 System Record 逻辑继续由 Python 核心执行。

适配并固定到 **Harness 0.2.0-rc.2 / Cordis 4.0.4**。需要 Node.js 22+（开发构建）、Python 3.11+、Git。预构建包内已包含 Python 核心，不需要 pip 安装，也不依赖开发目录的绝对路径。

## 构建与安装

在本仓库根目录：

```powershell
npm.cmd ci
npm.cmd test
npm.cmd pack
```

得到 `dsh-ai-native-agent-0.1.0.tgz`。`npm run build` 生成 `lib/` 并从 `runtime/agent` 同步 Python 核心；作为独立仓库签出时，`runtime/agent` 已提交在版本库内，构建直接使用该副本。安装到你使用的 Harness profile，例如 web：

```powershell
dsh plugin --profile web add C:/absolute/path/dsh-ai-native-agent-0.1.0.tgz
dsh --profile web --dump-config
```

manifest 的 `dsh.bundle.patch` 自动插入 ID 为 `ai-native-agent` 的插件行。默认项目列表为空、执行关闭，可以先安装再配置。这里交付预构建 tarball；不提供直接 Git 安装的 prepare 流程。

## 配置一个项目

目标必须是已提交且干净的 Git 仓库。状态目录必须位于目标仓库外。在本仓库根目录初始化：

```powershell
python -m agent init --repo C:/work/my-project --state-dir C:/work/my-project-agent --provider harness --editable "src/**"
```

如果只有已安装的 tarball，没有本仓库，也可以按照 `examples/agent.json` 和 `examples/system_record.json` 创建这两个文件。修改 `repo`、`editable_paths`、`protected_paths`、`verification` 和 `max_rounds`。验证命令来自这份可信配置，不能由工具参数或模型提案增加。保留：

```json
"provider": { "type": "harness" }
```

这个 provider 只供插件桥接使用，直接 `python -m agent run` 会明确报错。

复制 `examples/project.patch.yml`，填写：

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

`provider` 和 `model` 必须是 **Harness 中已经配置的路由和模型 ID**，不是 API URL。密钥由 Harness 的适配器管理，不写入插件配置、不发送给 Python。所有角色使用这里选定的模型，每次独立调用；没有为同一个判断复制三个“投票”。

`allowExecution: true` 表示允许本机执行预先配置的验证命令。保持 `false` 时，运行返回 `HUMAN_REQUIRED`，不调用模型、不创建候选副本。Git 副本隔离文件修改，但 Python 的执行器不经过 Harness 的 shell/OS 沙箱；请仅在可信项目或单独的执行环境中开启。这是现有 agent 的执行边界。

启动并带上该配置：

```powershell
dsh web --patch C:/work/project.patch.yml
```

也可以将这条覆盖行合入该 profile 的 `cordis.patch.yml`。patch 会替换整行 `config`，因此覆盖时需要重述全部自定义字段。不要再插入第二个相同 ID。

## 在 Harness 中使用

输入：

> 列出 ai_native_projects，然后用 ai_native_run 在 my-project 上修复指定问题，保留现有测试契约。

工具接口：

| 工具 | 参数 | 返回 |
| --- | --- | --- |
| `ai_native_projects` | 无 | 项目 ID、执行开关、模型路由 |
| `ai_native_run` | `project`, `goal` | 状态、run_id、补丁位置、审查和验证证据 |
| `ai_native_inspect` | `project`, `run_id` | 已保存的完整报告 |

完整结果是规范 JSON，可由 Harness 的程序化工具调用直接读取。文本展示只摘要状态、路径和审查结果；完整验证输出还保存在 `result.json` 和 `audit.jsonl` 中。插件也向其他 Cordis 消费者提供 `ctx.aiNativeAgent` 的 `listProjects()`、`run(project, goal, signal)`、`inspect(project, runId, signal)`。

`ACCEPTED` 表示补丁通过验证和审查，**原仓库尚未应用修改**。检查返回的 `accepted.patch` 后按现有流程应用。Guardian 的 YELLOW/RED 返回 `HUMAN_REQUIRED`，Critic 的 BLOCK 返回 `BLOCKED`，修订耗尽返回 `EXHAUSTED`。这些都是可检查的领域结果；核心捕获的模型或验证故障返回 ERROR。桥接启动、协议或插件配置故障成为工具错误。

取消调用或卸载插件时，向 Python 发送取消请求并中止 LLM；Python 停止验证子进程、记录 `CANCELLED` 并释放记录锁。每个配置文件同一时刻只允许一次运行，跨实例由 System Record 文件锁保护。强制杀进程或主机崩溃仍可能留下锁；确认进程结束后再人工处理，插件不会偷取已有锁或自动恢复中断运行。

## 可调参数

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `projects` | `[]` | 项目 ID 与 agent.json 的绝对路径 |
| `python` | `python` | Python 可执行文件，不是 shell 命令 |
| `provider`, `model` | 空 | 有项目时必须明确填写 |
| `allowExecution` | `false` | 本机验证执行许可 |
| `timeoutMs` | 1800000 | 整次工具调用时限 |
| `modelTimeoutMs` | 180000 | 单次角色模型调用时限 |
| `cancelGraceMs` | 10000 | 等待 Python 清理后再强制终止 |
| `maxTokens` | 16384 | 每次角色响应的 token 上限 |
| `maxMessageBytes` | 32000000 | 桥接单行和模型流采集上限 |

角色输出必须是完整 JSON 对象；拒绝重复字段、截断、工具调用、无正常结束标记的响应。Harness 不要求所有适配器支持 JSON mode，所以由角色提示词要求 JSON，并在 Python 中独立验证结构与证据。没有自动降级到固定脚本或另一家模型。

## 开发验证

```powershell
python -B -m unittest discover -s tests -v
npm.cmd test
```

插件测试使用真实 Cordis、工具注册表、LLM 服务、Python 进程、Git 和验证命令；模型适配器提供明确标记的离线响应，不消耗 API 配额。覆盖失败后修订、知识保存、Guardian 阻断、关闭执行、路径边界、响应校验、取消验证进程、卸载清理和重新加载。真实供应商模型质量仍需使用你的 Harness 模型配置验证。

目录：

- `src/index.ts`：Config、服务、三个工具、生命周期。
- `src/llm.ts`：消费 Harness llm 流、检查完成状态。
- `src/bridge.ts`：有界 JSON-lines、子进程、超时与取消。
- `runtime/agent/`：随包交付的 Python 核心和提示词。与上游核心仓库同目录开发时，构建会从 `../agent/` 或 `../agent/agent/` 重新复制；独立签出时直接使用此提交副本。
- `runtime/agent/harness_bridge.py`：Python 侧 provider 和运行桥接。

遵循官方文档：[第一个插件](https://deepseek-harness.github.io/deepseek-harness/develop/basic/)、[工具](https://deepseek-harness.github.io/deepseek-harness/develop/basic/tool)、[配置](https://deepseek-harness.github.io/deepseek-harness/develop/basic/config)、[打包安装](https://deepseek-harness.github.io/deepseek-harness/develop/basic/publish)、[工具编写契约](https://deepseek-harness.github.io/deepseek-harness/reference/cookbook/adding-a-tool)。共享 Harness 服务包使用 peerDependencies，并保留同版本 devDependencies 用于独立测试。
