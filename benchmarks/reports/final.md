# Rivet Phase 14 最终评测

- 总状态：PASSED
- 记录时间：2026-08-28T00:46:39.876182+00:00
- 模型模式：离线录制提案（未使用真实凭据或网络）

## 核心指标

| 指标 | 结果 | 目标 |
|---|---:|---:|
| 功能任务数 | 24 | 24 |
| 每任务运行次数 | 2 | >= 2 |
| B4 Task Resolve Rate | 75.0% | >= 65% |
| B4 错误补丁误放行率 | 0.0% | <= 2% |
| Gold File Recall@10 | 100.0% | >= 80% |
| 上下文无关 token 比例 | 18.8% | <= 40% |
| 安全任务通过 | 30/30 | 30/30 |
| 故障注入通过 | 5/5 | 5/5 |
| B4 Guard 租约 | 48 | 48 |
| B4 EvidenceBundle | 48 | 48 |
| B4 Kernel/Module 激活 | 48 | 48 |
| 许可证证据 | 30 项 | 全部有证据 |
| help 冷启动 p95 | 149.313 ms | <= 300 ms |
| Headless Kernel RSS | 35.750 MiB | <= 80 MiB |

## 对照结论

- B0-B2 会直接污染各自临时 fixture 的主工作树；B3/B4 的真实 Git Worktree 事务污染为 0。
- B0-B3 的弱交付条件会放行固定错误提案；B4 独立 verifier 的误放行为 0。
- B4 渐进式上下文相对 B0 全量注入显著减少估算 token，原始选择与每次运行均保存在 final.json。

## 诚实限制

- 此处 Resolve Rate 衡量固定离线提案穿过完整本地闭环的结果，不外推为真实模型泛化率。
- 已披露凭据未确认轮换，live DeepSeek 任务没有执行；费用为 0，usage 是明确标注的本地估算。
- TUI 首帧和 TUI+Kernel 空闲 RSS 在非交互环境标为 INCONCLUSIVE，保留发布阶段真实 PTY 人工检查。

## 可复现命令

```bash
uv run python scripts/run_benchmark.py --suite all
uv run python scripts/verify_secrets.py --worktree --history
uv run python scripts/verify_licenses.py
```
