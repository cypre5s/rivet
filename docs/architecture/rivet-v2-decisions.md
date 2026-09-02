# Rivet Core Architecture Decision

- 状态：Accepted
- 决策日期：2026-09-02
- 适用范围：Python Kernel、Headless CLI、IPC Worker、OpenTUI、Transaction、Verify、Evidence、Trace、依赖与 CI
- 实施原则：本文定义可测试的目标契约；任何完成声明都必须由代码和自动化测试共同证明

本文是 Rivet 收缩后的规范性架构记录。“必须”“不得”“应该”分别对应 MUST、MUST NOT、SHOULD。实现与本文冲突时，必须修改实现或通过新的明确决策修订本文。

## 1. 决策摘要

Rivet 只保留两个不可替代的产品闭环：

1. **Demand-driven Lightweight Kernel**

   > No traceable demand, no capability activation.

   能力只有在用户或模型产生真实需求后才进入运行路径。每次激活必须有耐久、可追溯的 Demand 因果链。

2. **Evidence-gated Reliable Patching**

   > No verified evidence, no apply.

   模型只能生成候选补丁。只有冻结的 AcceptanceSpec、隔离 Worktree、独立 Verify 与哈希绑定 Evidence 全部成立后，用户才能显式 Apply。

代码、CLI、TUI、持久状态、依赖和测试都必须直接服务于这两条不变量。Rivet 不以功能数量、格式覆盖或可扩展平台作为产品目标。

## 2. 系统总览

```text
User
 │
 ▼
AgentLoop ── USER_EXPLICIT Demand
 │
 ▼
Provider
 │
 ├─ FINAL ─────────────────────────────────────┐
 │                                             │
 └─ TOOL_CALL ── MODEL_TOOL_CALL Demand        │
       │                                       │
       ▼                                       │
   ToolExecutor                                │
       │                                       │
       ├─ schema validate                      │
       ├─ KERNEL_REQUIRED capability demands   │
       ├─ authorize                            │
       ├─ execute                              │
       └─ observation ─────────────────→ Provider
                                               │
                         candidate complete ───┘
                                  │
                                  ▼
                         frozen AcceptanceSpec
                                  │
                            Git Worktree
                                  │
                                Patch
                                  │
                         independent Verify
                                  │
                               Evidence
                                  │
                              VERIFIED
                                  │
                         explicit user Apply
```

模型循环与可靠事务是两个清晰边界：前者调查并生成候选；后者根据本地事实决定候选能否交付。模型文字不能越过这条边界。

## 3. Demand-driven Lightweight Kernel

### 3.1 合法 Demand 来源

`CapabilityDemandSource` 只有三个值：

| Source | 创建者 | 父节点规则 | 典型目标 |
| --- | --- | --- | --- |
| `USER_EXPLICIT` | CLI 或 IPC 命令入口 | 唯一允许的根节点 | 一次 ask、fix、verify 或 apply 操作 |
| `MODEL_TOOL_CALL` | ToolExecutor | 必须引用当前运行中已落盘的父 Demand，并绑定 tool call ID | `context_search`、`file_write` |
| `KERNEL_REQUIRED` | Kernel 内部受限 API | 必须引用当前运行中已落盘的父 Demand | Provider、Guard、Worktree、Verifier |

额外约束：

1. `KERNEL_REQUIRED` 永远不能成为根节点。
2. 任意非根 Demand 的祖先链最终必须到达 `USER_EXPLICIT`。
3. 父子 Demand 必须属于相同的 `run_id` 和操作上下文；不得跨运行借用父节点。
4. 未知、重复、尚未落盘或循环父链必须失败关闭。
5. `MODEL_TOOL_CALL` 必须绑定稳定的 `operation_id`，使工具调用、Capability 和副作用事实可以关联。

### 3.2 激活前耐久化

固定顺序是：

```text
construct Demand
  → validate causality
  → append demand.created
  → fsync
  → return sealed DemandHandle
  → resolve capability
  → activate minimum module closure
  → return Lease
```

`DemandHandle` 是 Journal 完成耐久写后的不可伪造收据。Runtime 只接受当前 Kernel 签发的 Handle；业务代码不能用 capability 字符串直接绕过 Demand。

若 Trace 打开、写入、序列分配或 `fsync` 失败：

- 当前请求失败；
- 不导入 factory；
- 不构造 capability；
- 不发布 activation 事件；
- 不留下部分激活资源。

因此 CI 可直接检查：

```text
Orphan Activation = 0
Demand Traceability = 100%
```

### 3.3 AgentLoop

AgentLoop 只公开真实 I/O 和终止边界：

```text
MODEL_CALL
EXECUTE
OBSERVE
COMPLETE
FAILED
CANCELLED
```

合法主循环：

```text
MODEL_CALL
  ├─ final answer ───────────────→ COMPLETE
  └─ tool calls → EXECUTE → OBSERVE ─┐
                                     └→ MODEL_CALL
```

Schema 解析、权限判断与预算判断是执行路径中的规则，不伪装成模型的认知阶段。任何阶段遇到用户取消、预算耗尽、Provider 失败、非法工具调用或工具失败，都确定性终止为 `CANCELLED` 或 `FAILED`。

ASK 成功结果是 `ANSWERED`；FIX 的模型阶段成功结果最多是 `READY_FOR_VERIFICATION`。AgentLoop 没有产生 `VERIFIED` 的 API。

### 3.4 静态 ToolCatalog

ToolCatalog 是纯数据，加载目录时不得导入重型实现、创建 `ResourceScope`、获得 Guard、建立 Worktree、启动进程或连接网络。

每项工具定义至少包含：

```python
ToolSpec(
    name,
    description,
    input_schema,
    side_effect,
    permission,
    required_capabilities,
    executor,
)
```

模型可见名称与内部名称相同，统一使用 `snake_case`。固定目录如下：

| Tool | 模式 | 副作用 | 按调用请求的 Capability |
| --- | --- | --- | --- |
| `workspace_info` | ASK/FIX | 只读 | 无 |
| `context_search` | ASK/FIX | 只读 | `context.search.lexical` |
| `file_read` | ASK/FIX | 只读 | 无 |
| `file_write` | FIX | 事务写 | `transaction.worktree`、`guard.local_execution` |
| `file_replace` | FIX | 事务写 | `transaction.worktree`、`guard.local_execution` |
| `file_create` | FIX | 事务写 | `transaction.worktree`、`guard.local_execution` |
| `file_delete` | FIX | 事务写 | `transaction.worktree`、`guard.local_execution` |
| `process_run` | FIX | 本地进程 | `transaction.worktree`、`guard.local_execution` |
| `git_diff` | FIX | 只读 | `transaction.worktree` |

ToolExecutor 的固定顺序是：

```text
ToolCall
  → strict schema validation
  → durable MODEL_TOOL_CALL Demand
  → authorization
  → durable KERNEL_REQUIRED Demand(s) and acquire minimum capabilities
  → side-effect checkpoint when required
  → executor
  → Observation
  → reverse-order Lease release
```

非法参数在 Demand 之前被拒绝，未获授权的调用不会激活内部 Capability。只读工具不产生副作用 checkpoint。

### 3.5 词法 Context

`context_search` 只提供：

- `git ls-files` 获取受版本控制文件；
- ripgrep 关键词检索；
- 简单且稳定的路径/匹配排序；
- 有界 UTF-8 片段读取；
- 敏感路径过滤；
- 明确的 `MATCH` 或 `NO_MATCH` 观察。

Kernel 启动和第一次模型调用前不自动收集仓库上下文。只有模型明确调用 `context_search` 时，才创建 Demand 并激活 `context.lexical`。

### 3.6 五个生产模块

模块目录固定为：

| Module | Provides | Requires | 激活时机 |
| --- | --- | --- | --- |
| `provider.deepseek` | `provider.chat.completions` | 无 | 真实模型调用 |
| `context.lexical` | `context.search.lexical` | 无 | 真实词法检索 |
| `transaction.git` | `transaction.worktree` | 无 | Proposal 只读绑定 Git 基线；确认后创建 Worktree |
| `guard.sandbox` | `guard.local_execution` | 无 | 真实写入或进程执行 |
| `verify.matrix` | `verify.deterministic` | `transaction.git`、`guard.sandbox` | 独立验证 |

`ModuleManifest` 只表达 `module_id`、`factory`、`provides` 与 `requires`。所有模块都是内部按需生命周期，用户不控制启停策略。

factory 必须无副作用；激活必须返回 Manifest 声明的真实 capability；声明与实际能力不一致时失败关闭。并发请求由依赖激活锁合并，且每次调用的 Demand 归属不能被另一个并发调用覆盖。

### 3.7 Lease 与 ResourceScope

每个长期资源必须有唯一 Scope owner。Lease 活跃期间，模块及其依赖不得关闭。释放遵循依赖逆序，并满足：

- 正常结束、失败和取消都执行清理；
- 子进程有界终止并最终 `wait()`；
- 客户端、文件句柄、Task、临时目录和 Worktree 都登记在 Scope；
- 关闭失败被聚合报告，其他资源仍继续清理；
- 任务完成后的资源计数归零。

## 4. Evidence-gated Reliable Patching

### 4.1 FIX 前置条件

FIX 先允许一次受限的模型只读调查，随后必须在 Worktree、事务持久化或写操作之前完成确认。进入确认边界前必须满足：

1. 目标是有效 Git 工作树；
2. 主工作树及索引干净，包括 tracked、untracked、rename、conflict 与 submodule 状态；
3. `.rivet/project.toml` 可以安全解析；
4. `[verification].acceptance` 是用户提供的非空 argv 命令组；
5. 用户明确给出最小写范围。

调查模型只获得 `workspace_info`、`context_search` 与 `file_read`，且必须真实调用至少一个读取工具。调查完成后 Rivet 在内存中形成 Proposal，展示 Goal、Scope、Acceptance、Regression、调查结论、`acceptance_sha256` 和 `base_commit`；用户确认时必须回传这两个绑定值。规范或 Git HEAD 在等待期间发生变化都会在创建事务前失败关闭。

Rivet 不猜测如何混合用户已有修改。工作树不干净时稳定拒绝，并要求用户先 commit 或 stash。

通用环境检查不是固定启动阶段。每项外部前置条件在对应 Demand 到达时检查：模型调用才检查凭据，词法搜索才检查 ripgrep，受管执行才检查 Bubblewrap。事务查询命令不得因为缺少模型凭据而失败。

### 4.2 AcceptanceSpec 冻结边界

用户在冻结前必须看到并接受：

- `Goal`：要改变的行为；
- `Read Scope`：允许调查的范围；
- `Write Scope`：允许修改的现有路径；
- `Allowed New Paths`：允许创建的路径；
- `Forbidden Paths`：验证器和明确禁止触碰的路径；
- `Expected Behaviors`：修复后必须成立的行为；
- `Regression`：必须保持的行为和可选质量命令；
- 时间、工具调用、Token 与费用预算。

用户确认且 Proposal 绑定复核通过后，Rivet 原子写入 AcceptanceSpec 和 `acceptance_sha256`。第一个持久事务状态是 `ACCEPTANCE_FROZEN`；确认前不存在可恢复的候选事务。基线 Git commit 与仓库身份作为同一冻结事实记录。

以下顺序不可交换：

```text
display + explicit confirmation
  → persist ACCEPTANCE_FROZEN
  → create candidate Worktree
  → allow first write
```

Verify 只消费冻结副本。用户之后修改 `.rivet/project.toml` 不会改变既有事务的验收含义。

### 4.3 候选 Patch

所有自动写入发生在 `$XDG_CACHE_HOME` 下的独立 Git Worktree。主工作区在 Apply 前保持不变。

候选完成后生成 `PatchSet`，至少包含：

- `transaction_id`；
- `base_commit`；
- `acceptance_sha256`；
- `patch_sha256`；
- changed files 与 created files；
- 二进制补丁标记；
- 创建时间。

Patch 内容或哈希在 Verify 后发生变化，会使原 Evidence 失效。

### 4.4 七类独立 Verify

Verify 阶段不调用模型，不采信模型文字，也不允许写工具参与 verdict。检查类别固定为：

| Kind | 证明的问题 |
| --- | --- |
| `Baseline` | 修改前问题是否可复现，基线输出和退出状态是什么 |
| `Behavior` | 候选补丁是否满足独立的行为验收 |
| `Regression` | 冻结的回归与静态质量命令是否保持通过；配置可为空 |
| `Scope` | changed/created paths 是否完全位于冻结范围内 |
| `Secret` | 补丁是否新增秘密、危险内容或不允许的二进制变化 |
| `Binding` | 执行事实是否覆盖冻结的验收条目并绑定同一补丁 |
| `Resource` | Worktree、进程、超时、输出和清理事实是否完整 |

Baseline 和 Behavior 必须分开执行、分开记录。项目 `[verification].acceptance` 驱动独立 Behavior；`regression` 与 `static` 合并为可选 Regression 检查。工具与依赖可用性属于执行前置事实，不增加第八类验证。

程序按以下固定优先级从 required 结果计算状态：

```text
CANCELLED → FAILED → BLOCKED → INCONCLUSIVE → PASSED
```

只有所有 required 结果为 `PASSED`，事务才进入 `VERIFIED`。模型没有构造 Verdict 或修改事务状态的入口。

### 4.5 Evidence

Evidence Bundle 原子发布，必须包括可复核的输入、结果和日志。`EvidenceManifest` 顶层显式包含：

```text
evidence_id
transaction_id
base_commit
acceptance_sha256
patch_sha256
files[] = {path, size_bytes, sha256}
created_at
```

Manifest 不把自身包含在 `files[]` 中。完整 Manifest 字节的 SHA-256 由事务记录单独保存，形成四层校验：

1. 冻结 AcceptanceSpec 的内容哈希；
2. 候选 Patch 的字节哈希；
3. Evidence 内每个文件的大小和哈希；
4. 外部保存的 Manifest 哈希。

任一文件缺失、路径逃逸、符号链接、大小不符、哈希不符、三重绑定不符或 Manifest 哈希不符，Evidence 都无效。

### 4.6 显式 Apply 与恢复

Apply 必须由用户选择确切 Transaction 并显式确认。执行前重新验证：

- 事务状态为 `VERIFIED`；
- AcceptanceSpec、Patch、Evidence 和 Manifest 哈希全部匹配；
- 主仓库身份、HEAD、索引和工作树没有漂移；
- 当前候选 Worktree 的补丁字节仍匹配 `patch_sha256`；
- Patch 仍可干净应用。

Apply 使用耐久 `ApplyIntent` 表达开始事实。崩溃恢复根据仓库和补丁事实判断 `APPLIED`、可安全重试或 `UNKNOWN`，不得猜测成功。

Rivet 只恢复可靠性所需的 Transaction、AcceptanceSpec、Patch、Evidence、ApplyIntent 和副作用事实。模型对话中间轮次不作为跨进程恢复契约。

### 4.7 最小副作用 Checkpoint

只有真实写入、`process_run` 和 Apply 需要持久副作用事实：

```text
STARTED
SUCCEEDED
FAILED
```

`UNKNOWN` 不是写入状态，而是恢复时从“存在 STARTED 且没有终态”推导出的结论。遇到未知写入或进程结果时失败关闭；Apply 只有在 `ApplyIntent` 与仓库事实可以确定性恢复时才续行并补写终态。只读搜索、读取和仓库概览不写 checkpoint。

## 5. Trace 与本地状态

### 5.1 NDJSON Trace

Trace 唯一事实文件是：

```text
trace/events.ndjson
```

它必须保留：

- 单 writer 和进程锁；
- 有界 writer queue；
- 单事件大小上限；
- 写前秘密脱敏；
- 单调 sequence；
- parent-before-child 校验；
- append 后 `fsync`；
- 启动扫描与截断尾记录恢复；
- 对 Demand、Activation、工具副作用、Verify 和 Apply 的因果事件。

Trace 是 append-only 审计事实，不承担可变业务状态。TUI 在当前运行中消费 IPC event；离线审计顺序读取 NDJSON。

`scripts/audit_trace.py <events.ndjson>` 对完整事实流重新计算 `Orphan Activation` 与 `Demand Traceability`。CI 和固定 Release Demo 都要求前者为 `0`、后者为 `100%`，不能只依赖运行时意图或局部单元测试。

### 5.2 XDG 布局

仓库内唯一 Rivet 文件是：

```text
.rivet/project.toml
```

持久运行事实按仓库身份隔离：

```text
$XDG_STATE_HOME/rivet/<repo-id>/
├── transactions/
├── evidence/
└── trace/
    └── events.ndjson
```

缺省 `XDG_STATE_HOME` 时使用平台约定的用户状态目录。`repo-id` 由规范仓库路径和 Git common-dir 稳定派生，不包含秘密。

可重建 Worktree 位于：

```text
$XDG_CACHE_HOME/rivet/worktrees/<repo-id>/...
```

状态目录和缓存目录不得位于目标仓库内部，不得经过符号链接，创建权限必须限制为当前用户。

## 6. 配置、CLI 与 TUI

### 6.1 项目配置

配置格式固定为：

```toml
schema_version = 1

[rivet]
model = "deepseek-v4-flash"

[verification]
acceptance = [["program", "arg1", "arg2"]]
regression = []
static = []
```

规则：

1. `[rivet]` 只允许 `model`。
2. `acceptance` 在 FIX 前必须非空，且必须由用户提供。
3. `regression` 与 `static` 可选并允许为空。
4. 命令只接受有界 argv 数组，不接受 shell 字符串。
5. 项目检测只产生未执行建议；`rivet init --yes` 可以写模板，但不会自动填充 acceptance。
6. API Key 只来自进程环境，不写项目配置、Trace 或 Evidence。

### 6.2 公开 CLI

不带子命令的 `rivet` 启动 TUI。公开 Headless 命令固定为：

```text
rivet init
rivet ask "..."
rivet fix "..."
rivet diff [TX]
rivet verify [TX]
rivet apply TX
rivet abort TX
```

只有 `fix` 接受明确范围和冻结确认：`--allow-read` 只授权现有路径的调查，
`--allow-write` 只授权现有路径的修改，`--allow-new` 只授权尚不存在的路径。
只读 Context 不得隐式升级为写权限。`diff` 与 `verify` 可以使用当前唯一活跃事务；
`apply` 与 `abort` 必须指定 Transaction ID。

### 6.3 TUI 与 IPC

TUI 只保留：

```text
Welcome
Timeline
Composer
Slash Menu
@ File Picker
Diff
Evidence
Permission Confirm
Model Selector
Transactions
```

普通输入执行 ASK。Slash 命令固定为：

```text
/help
/fix
/diff
/verify
/apply
/abort
/model
```

TUI 的 `@ File Picker` 只产生 `context_paths/read_scope`。FIX 的写入范围必须通过
`/fix --write PATH --new PATH -- TASK` 显式给出，并在权限确认中逐项展示
`read_scope`、`write_scope`、`allowed_new_paths`、`forbidden_paths`、
`expected_behaviors` 与预算。

IPC 只保留完成这些流程所需的方法与事件：

```text
handshake
shutdown
command.ask
command.fix
command.diff
command.verify
command.apply
command.abort
permission.resolve
cancel
event
workspace.files
transactions.list
evidence.get
evidence.log
```

IPC Worker 是内部版本化入口，不属于公开 CLI。每个响应必须有稳定 request ID、结构化错误和脱敏载荷；取消必须最终等待子进程退出。

## 7. 依赖与按需前置条件

Python 运行时第三方依赖只保留：

```text
httpx
pydantic
```

其余能力依赖标准库和按需调用的系统程序。系统前置条件不得在无关命令启动时统一检查：

| 操作 | 到达此操作时才检查 |
| --- | --- |
| Provider 请求 | `DEEPSEEK_API_KEY`、网络与 Provider 可达性 |
| `context_search` | ripgrep |
| 事务创建、Diff、Verify、Apply | Git 和仓库事实 |
| 受管写入 | Guard 激活时检查 Bubblewrap 可用；文本变更仍由 Worktree 路径边界与原子文件原语完成 |
| `process_run`、Verify 命令 | Guard 注入的 Bubblewrap 沙箱与对应 argv 程序 |
| TUI 启动 | Bun 与已安装前端依赖 |

缺少某项前置条件只能阻止真实需要它的 Demand，不能阻止无关命令。

## 8. 自动化验收

完成审计至少覆盖以下层级。

### 8.1 Kernel 单元测试

- 三种合法 Demand 来源和所有非法父链；
- Journal 写入失败时零 import、零 activation；
- 并发调用各自保持正确 Demand 归属；
- dependency closure、Lease 计数、逆序清理和失败聚合；
- 六状态 AgentLoop 与预算、取消、重复动作终止；
- 静态目录恰好九个工具、名称与 required capabilities 完全匹配；
- strict schema validation 发生在 Demand 之前。

### 8.2 事务与 Evidence 测试

- tracked、untracked、staged、rename、conflict、submodule 脏状态全部拒绝；
- 用户确认前没有持久事务或 Worktree；
- `ACCEPTANCE_FROZEN` 先于 Worktree 和首个写入；
- Verify 使用冻结配置，不读取后改的项目配置；
- Baseline 与 Behavior 分开执行并记录；
- 七类结果与 deterministic verdict 优先级；
- base commit、acceptance、patch、Evidence 文件和 Manifest 任一篡改均拒绝；
- repo drift、patch drift、错误 Transaction 与非 `VERIFIED` Apply 均拒绝；
- ApplyIntent 和 UNKNOWN 副作用恢复失败关闭。

### 8.3 Trace、安全与资源测试

- append、序列、父子顺序、锁、队列上限、事件大小和 `fsync` 失败；
- 尾部半条记录可安全截断，中间损坏拒绝启动；
- API Key、环境秘密、命令输出和异常信息脱敏；
- 路径逃逸、符号链接、二进制读取与越权写入拒绝；
- 取消、超时、异常后的进程、Task、Worktree 和 Scope 资源归零；
- XDG 状态位于仓库外且不同仓库身份隔离。

### 8.4 CLI、IPC 与 TUI 测试

- 公开帮助只暴露七个 Headless 命令；
- 普通输入 ASK 与七个 Slash 命令；
- 最小 IPC 方法、request/response 关联、取消与错误脱敏；
- Transactions、Diff、Evidence 与权限确认的键盘流程；
- 缺少 API Key 不影响 `diff`、`verify`、`apply` 和 `abort`；
- ASK 不搜索时不会激活 Context，FIX 写入前不会建立 Worktree。

### 8.5 架构边界检查

CI 必须用静态导入测试、目录白名单和公开协议快照防止旧复杂度重新进入：

- 模块 Manifest 数量固定为五；
- 模型工具数量固定为九；
- AgentLoop 状态固定为六；
- VerificationKind 固定为七；
- 公开 CLI 和 Slash 命令保持最小集合；
- 仓库运行配置只允许 `.rivet/project.toml`；
- 所有 activation 都能关联已落盘 Demand；
- 所有 Apply 都能关联有效 `VERIFIED` Evidence。

## 9. 决策后果

### 9.1 获得的性质

- 简单 ASK 不承担未使用能力的导入、进程和内存成本；
- 激活原因能够从用户意图追溯到具体 capability；
- 配置或依赖缺失只影响真实需要它的操作；
- 主工作区不会被未经验证的模型输出直接修改；
- `VERIFIED` 是独立程序结论，而不是模型自述；
- Evidence 与确切基线、验收条件和补丁字节绑定；
- 崩溃恢复只处理可观察副作用事实，边界更小且更可信；
- 产品界面可以完整围绕两条闭环解释。

### 9.2 明确接受的限制

- 只支持干净 Git 仓库上的自动修补；
- Context 是词法、有界、显式调用的，搜索不足时模型必须调整查询；
- 二进制文件不进入普通文本工具；
- Provider 只有 DeepSeek 生产模块；
- 自动修改必须有用户提供的独立 acceptance；
- 没有绕过 Verify 的可 Apply 候选路径；
- 不以通用模块平台、通用配置中心或多用途文件处理为目标。

这些限制不是暂时缺项，而是为了使两条核心不变量可证明、可测试、可答辩而做出的产品边界。
