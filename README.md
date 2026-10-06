# AI Native Agent for DeepSeek Harness

将现有 Python AI Native Agent 作为 Harness 的工具插件运行。Constructor、Critic、Guardian、学习提案和知识审查共用 Harness 的 `llm` 服务；原有 Git 副本、文件范围、真实验证、审计和 System Record 逻辑继续由 Python 核心执行。

适配并固定到 **Harness 0.2.0-rc.2 / Cordis 4.0.4**。需要 Node.js 22+（开发构建）、Python 3.11+、Git。预构建包内已包含 Python 核心，不需要 pip 安装，也不依赖开发目录的绝对路径。

## 思路

写代码的能力可以交给模型，但**改动一个真实仓库的权限不能**。这个插件的全部设计都服务于这一个区分：让模型只负责"提出与评判"，让确定性代码负责"改文件、跑命令、落盘知识"，并在两者之间设置显式闸门。角色提示词可以随便换模型——纪律不随之改变。

三条分工线划得很清楚：

| 层面 | 谁负责 | 具体是什么 |
| --- | --- | --- |
| 智力 | Harness（可替换） | 角色提示词、`llm` 路由与适配器、模型密钥 |
| 纪律 | Python 核心（固定） | Git 副本隔离、路径白名单、真实验证、审计、System Record |
| 裁决 | 人 | 补丁是否应用到原仓库 |

因此模型不持有 API 密钥（密钥由 Harness 适配器管理，只把路由与模型 ID 交给 Python），也无法自己发明验证命令（命令来自可信配置文件，不能由工具参数或模型提案追加）。

### 关键设计取舍

**一次完整候选，而不是增量补丁。** 每一轮提案都被当作"相对 base_commit 的完整候选"来执行：先 `reset --hard`、清掉上一轮遗留文件，再按本轮内容整体写入。修订时模型必须重述全部意图，而不是在上次结果上打补丁。这样避免了增量编辑在修订循环里错位累积，也让"第 N 轮候选"始终可独立复核。

**知识必须挂靠真实验证证据。** 学习提案声称的每一条 `evidence_ids`，都必须命中本轮真实验证产生、且 `returncode == 0`、未超时、去重后的证据 ID；否则直接判为伪造、拒绝保存。模型无法"声称已验证"：一条声称"通过边界测试"的知识如果引用了失败命令的 evidence_id、或引用一个不存在的 ID，整个知识提案都会被拒。

**Guardian 在动手前后各拦一次。** 提示词阶段和候选成型后各做一次风险评估；YELLOW/RED 或显式要求人工介入即停止。前者让高风险意图在写文件之前就被挡下，后者防止候选在验证之后被改动。

**度量差异，而不是相信陈述。** 候选目录在每次操作前后都做 SHA-256 快照，逐文件比对；验证结束后还要复核候选 diff 与验证时是否一致。验证过程本身若改动了候选，其证据即判失效；验证报告也不能"声称成功"——聚合标志无法覆盖失败命令或缺失证据。

**`ACCEPTED` 不等于"已应用"。** 通过验证与审查后，导出 `accepted.patch`，**原仓库一个字节都不改**。应用与否是人的决定。

**代码通过 ≠ 知识通过。** 补丁验收与知识入库是两件独立的事：知识要再过一次 Critic 审查，被拒时补丁照样交付，但知识不写入，并说明原因。

### 一次运行是怎么走的

```text
ai_native_run(project, goal)
  └─ 取得 System Record 文件锁；读取仓库上下文与 System Record
      └─ 轮次循环（上限 max_rounds）
          ① Constructor 提案（完整候选 + side_effects）
          ② Guardian 预检 ── 非 GREEN 或要求人工 → HUMAN_REQUIRED（不写任何文件）
          ③ 写入独立 clone（先 reset --hard 到 base_commit，再整体落盘）
          ④ 运行可信验证命令 ── 失败 → 带证据回到 ①
          ⑤ Critic 审查 ── BLOCK → BLOCKED；REVISE → 回到 ①
          ⑥ Guardian 终检 ── 非 GREEN → HUMAN_REQUIRED
          ⑦ 导出 accepted.patch
          ⑧ 学习提案 → 证据校验 → 知识审查
      └─ 返回 ACCEPTED + 补丁路径 + 验证证据 + 审计路径（原仓库未改动）
```

两个细节值得注意：③ 的 `reset --hard` 意味着**每一轮都从 base_commit 重新开始**，所以第 2 轮不是在第 1 轮的改动之上继续，而是重做整个候选；⑥ 之后还会再核对一次 diff，确保审查过的候选就是导出的候选。

## 功能

**候选补丁，不直接落地。** 每次 `ai_native_run` 产出一份完整补丁，附带验证证据、审查结论与审计轨迹；原仓库保持干净（测试会断言 `git status` 为空）。核对补丁后按你既有的流程应用。

**领域状态而非模糊失败。** 补丁就绪为 `ACCEPTED`（待应用），Critic 阻断为 `BLOCKED`，Guardian 非 GREEN 为 `HUMAN_REQUIRED`，修订预算用尽为 `EXHAUSTED`，取消为 `CANCELLED`，核心捕获的模型或验证故障为 `ERROR`。这些都是可检查的领域结果，与"工具调用出错"区分开。

**硬边界。** `editable_paths` 是白名单；`protected_paths` 之外还有一批**永远不可改**的路径（测试、`agent.json`、`system_record*`、`.git`、凭据与密钥文件等），不因模型声称而放行。单文件改动上限 500KB，单轮提案上限 50 个文件，工作区上下文上限 120KB，命令输出上限 120KB。

**执行隔离与可取消。** 候选在独立 clone 中准备（不共享索引、对象与引用），`origin` 被移除；验证命令在收敛的环境变量下运行，HOME 被重定向到运行目录，Windows 上还以 Job Object 约束后代进程，超时或收尾时整树终止。取消工具调用或卸载插件时，插件向 Python 发送取消、中止 LLM 调用；Python 停止验证子进程、释放记录锁并记录 `CANCELLED`。

**并发保护。** 同一份配置同一时刻只允许一次运行，跨实例由 System Record 文件锁保证；结束或中止即释放。强制杀进程或主机崩溃留下的锁不会被自动窃取，需确认进程已结束后人工处理——这是刻意的选择，避免两个运行同时写同一份记录。

**审计与知识。** 每次运行的完整事件流写入 `audit.jsonl`，最终报告写入 `result.json`；被审查通过的知识以候选作用域写入 System Record（标注 `applicability: verified_candidate_only`、`applied_to_source: false`），并支持 `supersedes` 取代关系。

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
