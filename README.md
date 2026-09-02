<div align="center">

# RIVET

**A demand-driven coding agent that verifies before it applies.**

没有真实 Demand，就不激活能力。没有独立 Evidence，就不允许 Apply。

<a href="pyproject.toml"><img alt="Python 3.13" src="https://img.shields.io/badge/Python-3.13-3776AB?style=flat-square&logo=python&logoColor=white"></a>
<a href="tui/package.json"><img alt="Bun 1.4" src="https://img.shields.io/badge/Bun-1.4-14151A?style=flat-square&logo=bun&logoColor=white"></a>
<a href="#demand-driven-kernel"><img alt="Demand-driven Kernel" src="https://img.shields.io/badge/Kernel-Demand--driven-65D1E6?style=flat-square"></a>
<a href="#evidence-gated-patching"><img alt="Evidence-gated Apply" src="https://img.shields.io/badge/Apply-Evidence--gated-79C99E?style=flat-square"></a>
<a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/License-MIT-2ea44f?style=flat-square"></a>

<p>
  <a href="#为什么是-rivet">设计动机</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#一次可靠修复">修复流程</a> ·
  <a href="#验证与审计">验证与审计</a>
</p>

</div>

<p align="center">
  <img src="docs/images/rivet-home.png" alt="Rivet 当前 OpenTUI 欢迎页" width="900" />
</p>

Rivet 是一个本地 Coding Agent，但它不追求堆叠功能。项目只保留两个能够形成完整证明链的能力：

1. **Demand-driven Lightweight Kernel**：只有用户或模型产生真实需求，相关能力才会被激活；
2. **Evidence-gated Reliable Patching**：模型只能生成候选补丁，独立验证通过并形成 Evidence 后，用户才能显式 Apply。

这两个约束贯穿 Kernel、工具、事务、验证、Trace、CLI 和 TUI。Rivet 的目标不是让“模型说修好了”，而是让一次代码修改能够回答：为什么启动了这个能力、修改边界是什么、验证了什么，以及最终应用的是否正是那份通过验证的补丁。

## 为什么是 Rivet

Coding Agent 同时面临两类常见问题：

| 问题 | Rivet 的约束 | 落地方式 |
| --- | --- | --- |
| 简单任务也启动完整工具链 | **No Demand, No Activation** | 静态 ToolCatalog、CapabilityDemand、惰性 ModuleRuntime |
| 可选能力缺失阻塞无关任务 | 前置条件在使用时检查 | Provider、Context、Git、Guard、Verify 分别按需激活 |
| 异常退出遗留进程或 Worktree | 每项资源都有明确所有者 | Lease、ResourceScope、逆序释放、失败后继续清理 |
| 模型自述被当成修复成功 | **Model cannot produce VERIFIED** | 模型最高只能返回 `READY_FOR_VERIFICATION` |
| 补丁越界或验证对象发生漂移 | 冻结并绑定验收边界 | AcceptanceSpec、Base、Patch、Evidence 哈希链 |
| 自动修改直接污染主工作区 | 候选与主工作区分离 | Git Worktree Transaction、显式 Apply |
| 崩溃后无法判断是否已应用 | 副作用事实必须可恢复 | Apply Intent、最小 Checkpoint、确定性恢复 |

Rivet 因此刻意不提供 Reader 平台、Tree-sitter/LSP 语义层、Session Resume、用户模块管理、Plan 模式、Benchmark/Doctor/Export 产品命令或动态工具插件。它们不是两条核心闭环的必要组成部分。

## Demand-driven Kernel

### 唯一合法的 Demand 来源

```text
USER_EXPLICIT
    └── MODEL_TOOL_CALL
            └── KERNEL_REQUIRED
                    └── module activation
```

- `USER_EXPLICIT` 是唯一合法根节点；
- `MODEL_TOOL_CALL` 必须绑定工具调用 ID 和已持久化父 Demand；
- `KERNEL_REQUIRED` 只能由 Kernel 为具体能力派生；
- 模块激活必须引用同一条 Demand 链中的 `demand_id`、来源和 Capability。

`demand.created` 会在激活前追加到 NDJSON Trace。记录失败时操作失败关闭，模块不会被导入或启动。CI 还会扫描内部 Permit 和 Runtime 私有入口，防止绕开 Demand 直接激活模块。

### 六状态 AgentLoop

Rivet 不把理解、规划或授权伪装成模型的固定认知阶段。AgentLoop 只保留真实控制流：

```text
MODEL_CALL
  ├── FINAL ───────────────────────────────→ COMPLETE
  └── TOOL_CALL → EXECUTE → OBSERVE ──────→ MODEL_CALL

终止状态：FAILED / CANCELLED
```

授权仍然存在，但属于工具执行边界：

```text
ToolCall → Schema Validate → Demand → Authorize → Acquire → Execute → Release
```

### 固定的五个模块

```text
provider.deepseek   context.lexical   transaction.git
guard.sandbox       verify.matrix
```

启动时只解析 Manifest，不导入模块 factory，也不会创建工作目录、网络连接、子进程或索引。`Lease` 保证使用中的能力不会被关闭；释放和异常路径会按确定性逆序回收完整依赖闭包。

### 固定的九个工具

```text
workspace_info   context_search   file_read
file_write       file_replace     file_create
file_delete      process_run      git_diff
```

ToolCatalog 只保存名称、说明、JSON Schema、副作用级别、所需 Capability 和静态 executor key。它不导入执行实现。Context 只使用 `git ls-files`、ripgrep、路径/关键词排序和有界文本片段，不预建语义索引。

## Evidence-gated Patching

一次 FIX 必须穿过下面的完整状态链：

```text
只读调查
  → Acceptance Proposal
  → 用户确认 Proposal 哈希与 Git Base
  → ACCEPTANCE_FROZEN
  → 隔离 Git Worktree
  → Candidate Patch
  → READY_FOR_VERIFICATION
  → Independent Verify
  → Evidence Manifest
  → VERIFIED
  → Explicit Apply
```

### 冻结 AcceptanceSpec

AcceptanceSpec 在第一个候选 Worktree 和第一项写操作之前冻结，包含：

- 用户目标；
- 允许读取的现有路径；
- 允许修改的现有路径；
- 允许新建的路径；
- 禁止路径；
- 预期行为与需要保留的行为；
- 独立 Acceptance、Regression 和 Static argv；
- 文件、轮次、Token、费用和进程预算。

确认后的任务、范围、验证命令、Acceptance 哈希或 Git Base 发生任何漂移，事务都不会创建。

### 独立 Verify

验证不复用模型结论，也不允许 Provider、工具或事务管理器写出 `VERIFIED`。Verify Matrix 使用七类有明确含义的检查：

| 检查 | 证明什么 |
| --- | --- |
| `Baseline` | 修改前确实能够复现目标问题 |
| `Behavior` | 候选补丁满足独立行为验收 |
| `Regression` | 已冻结的回归和静态命令继续通过 |
| `Scope` | 修改仅发生在获准范围内 |
| `Secret` | 补丁没有引入凭据或危险内容 |
| `Binding` | 每条验收声明都有对应执行证据 |
| `Resource` | Worktree、Patch 与资源状态一致且无泄漏 |

Evidence 原子发布，并绑定：

```text
base_commit
acceptance_sha256
patch_sha256
manifest_sha256
changed_files
verification_results
```

Apply 前会再次验证整条哈希链和主仓库漂移。持久化 Apply Intent 用于处理“Git 已应用，但最终状态记录尚未落盘”的崩溃窗口；此时 `abort` 会拒绝破坏现场，重试 `apply` 会恢复确定事实。

## 交互界面

不带子命令会启动 OpenTUI。普通输入就是 ASK，`/fix` 进入可靠修复流程，`@` 只选择只读上下文。

```bash
uv run rivet
```

Slash 菜单固定为七项，不存在隐藏的 Plan、Session、Module 或配置中心：

<p align="center">
  <img src="docs/images/rivet-slash-commands.png" alt="Rivet 当前七项 Slash 命令" width="900" />
</p>

```text
/help  /model  /fix  /diff  /verify  /apply  /abort
```

TUI 只保留 Welcome、Timeline、Composer、Slash Menu、`@` 文件选择、Diff、Evidence、权限确认、模型选择和 Transaction 选择。权限面板会分别展示读范围、写范围、新建范围、禁止路径、预期行为、验证命令和预算。

## 快速开始

### 环境要求

- Python `3.13.x`；
- [uv](https://docs.astral.sh/uv/)；
- Git 与 ripgrep；
- Bubblewrap：受管写入和本地进程执行时需要；
- Bun `1.4.x`：只在使用 TUI 时需要。

### 安装

```bash
git clone https://github.com/cypre5s/rivet.git
cd rivet

uv sync --frozen
bun install --cwd tui --frozen-lockfile
```

只在真正调用 DeepSeek Provider 时才需要凭据：

```bash
export DEEPSEEK_API_KEY="your-api-key"
```

### 初始化独立验收

`init` 先只读检测候选命令；只有显式确认才会写入最小模板：

```bash
uv run rivet init
uv run rivet init --yes
```

仓库内唯一的 Rivet 文件是 `.rivet/project.toml`：

```toml
schema_version = 1

[rivet]
model = "deepseek-v4-flash"

[verification]
acceptance = [["uv", "run", "pytest", "tests/test_bug.py", "-q"]]
regression = [["uv", "run", "pytest", "-q"]]
static = []
```

所有验证命令都必须是 argv 数组，不接受 shell 字符串。`acceptance` 必须由用户审查并直接判断目标行为；Rivet 不会自动采信模型生成的测试作为独立 oracle。

## 一次可靠修复

### 1. 获取只读 Proposal

第一次调用只调查代码并返回冻结候选，不创建事务或 Worktree：

```bash
uv run rivet fix \
  --allow-read tests/test_port.py \
  --allow-write src/port.py \
  --allow-new tests/test_port_regression.py \
  "拒绝 1..65535 之外的端口"
```

输出会包含 Proposal、`acceptance_sha256` 和 `base_commit`。

### 2. 显式确认同一份 Proposal

```bash
uv run rivet fix \
  --allow-read tests/test_port.py \
  --allow-write src/port.py \
  --allow-new tests/test_port_regression.py \
  --yes \
  --acceptance-sha256 'sha256:<proposal digest>' \
  --base-commit '<proposal base commit>' \
  "拒绝 1..65535 之外的端口"
```

TUI 会自动完成这次绑定重放，但仍会展示完整权限确认。

### 3. 审查、验证、应用

```bash
uv run rivet diff <transaction_id>
uv run rivet verify <transaction_id>
uv run rivet apply <transaction_id>
```

`fix` 已自动执行一次独立验证；`verify` 用于显式重验。只有当前事务为 `VERIFIED`、Evidence 完整且主仓库没有漂移时，`apply` 才会成功。

不再需要候选时：

```bash
uv run rivet abort <transaction_id>
```

## Headless CLI

公开命令精确为七个：

| 命令 | 作用 | 是否需要 Provider |
| --- | --- | --- |
| `rivet init` | 检测并确认最小验证配置 | 否 |
| `rivet ask` | 只读询问仓库 | 是 |
| `rivet fix` | 提案、隔离修改和独立验证 | 是 |
| `rivet diff [TX]` | 查看持久化候选 Patch | 否 |
| `rivet verify [TX]` | 对候选重新执行独立验证 | 否 |
| `rivet apply TX` | 显式应用 VERIFIED Patch | 否 |
| `rivet abort TX` | 终止并清理未应用事务 | 否 |

全局 `--json` 适合脚本集成；`--model`、`--base-url`、`--max-rounds`、`--max-total-tokens` 和 `--max-cost-usd` 提供有界运行时覆盖。

## 状态、Trace 与恢复

Rivet 不在仓库中写 Session 数据。状态与缓存遵循 XDG：

```text
$XDG_STATE_HOME/rivet/<repo-id>/
├── trace/events.ndjson
├── transactions/
└── evidence/

$XDG_CACHE_HOME/rivet/worktrees/
```

- Trace 是 append-only NDJSON；
- Transaction、Patch、Evidence 和 Apply Intent 是跨进程恢复事实；
- Worktree 是可由 Base + 持久 Patch 重建的缓存；
- 普通搜索和读取不创建持久 Checkpoint；
- 写入、进程和 Apply 只记录最小副作用状态。

离线审计可以重新计算 Demand 可追溯率和孤儿激活数：

```bash
uv run python scripts/audit_trace.py \
  "$XDG_STATE_HOME/rivet/<repo-id>/trace/events.ndjson"
```

通过条件固定为：

```text
Demand Traceability = 100%
Orphan Activation = 0
```

## 验证与审计

当前实现的发布门禁包括：

```bash
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv run basedpyright

bun test --cwd tui
bun run --cwd tui typecheck

uv run python scripts/verify_architecture.py
uv run python scripts/verify_dependencies.py
uv run python scripts/verify_ipc_contracts.py
uv run python scripts/verify_secrets.py
uv run python scripts/verify_licenses.py
```

开发期离线矩阵和固定 Release Demo 不进入产品 CLI：

```bash
uv run python scripts/run_benchmark.py --suite all --output /tmp/rivet-benchmark.json
uv run python scripts/run_release_demo.py \
  --bwrap-path /usr/bin/bwrap \
  --result /tmp/rivet-release-demo.json
```

本次重构的最终本地验收结果：

| 门禁 | 结果 |
| --- | --- |
| Python | `493 passed`, 仅真实 API smoke 按设计跳过 |
| TUI | `63 passed`，TypeScript typecheck 通过 |
| 离线功能矩阵 | 48 次运行，误放行 0、主工作区污染 0、资源泄漏 0 |
| 故障注入 | 5/5 通过 |
| Release Demo | Patch → Verify → Evidence → Apply 全链路通过 |
| Trace 审计 | Demand 可追溯率 100%，孤儿激活 0 |

离线录制 Provider 用于可复现架构验收，不代表真实模型的泛化质量；真实 DeepSeek smoke 必须显式启用并使用有效凭据。

## 项目结构

```text
src/rivet/
├── cli/           # 七个公开命令与最小配置
├── kernel/        # AgentLoop、Demand、Runtime、Lease、ResourceScope
├── context/       # 仅词法检索
├── tools/         # 静态 Catalog、执行器与九个工具处理器
├── providers/     # DeepSeek Provider
├── transaction/   # Acceptance、Git Worktree、Patch 与 Apply 恢复
├── guard/         # Workspace 边界与 Bubblewrap
├── verify/        # 七类验证和 Evidence
├── trace/         # NDJSON、因果审计与恢复适配器
└── ipc/           # TUI Worker 协议

tui/               # OpenTUI 前端
scripts/           # 架构门禁、离线矩阵和 Release Demo
tests/             # unit / integration / security / e2e / performance
```

更完整的架构不变量和取舍见 [Rivet v2 架构决策](docs/architecture/rivet-v2-decisions.md)。

## License

[MIT](LICENSE)
