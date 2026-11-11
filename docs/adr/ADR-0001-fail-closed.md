# ADR-0001 — 治理闸一律 fail-closed

- Status: Accepted
- Date: 2026-11-10
- Supersedes: —
- Related: [`docs/spec/GOVERNANCE.md`](../spec/GOVERNANCE.md) §4（本 ADR 的可测判据落在那里）

## Context

治理闸（admission gate / 研判层 / 写前治理 / watchdog judge / 计量台账 / 预算闸）
都是"依赖外部组件才能做判断"的位置。这类位置只有两种失败姿态：

1. **fail-open**：依赖缺失时默认放行。表现是系统"更顺"，代价是**治理在没人管的时候
   悄悄消失**——而这恰好是它唯一被需要的时刻。
2. **fail-closed**：依赖缺失时默认拒绝。表现是"更容易被拦"，代价是误拦与噪声。

实测证据（本仓 v0.3.1 之前的接线）：

- `agent/engine.py` 的 `pre_write()` 在 `self.governance is None` 时 `return True`
  ——治理挂点缺位即放行（GOVERNANCE.md 的 S3 曾记录为偏差）。
- `agent/watchdog.py` 的 `Watchdog(judge=None, rules_check=False)` 对任何报告返回 `OK`
  ——复核层可被构造参数关成 fail-open。
- 上游同类实现也踩过同一坑：`agent-governance-toolkit` 把这条固化成 ADR（"无配置开关
  可关"），因为"顺手改回 fail-open"是持续发生的诱惑。

## Decision

治理闸一律 **fail-closed**。实现 **MUST NOT** 提供把闸开成 fail-open 的配置开关：

| 依赖缺位 | 必须的行为 |
|---|---|
| 研判层（`assessor is None`） | `mention`/`broadcast` 路由 → `rejected_eval`（不放行） |
| 写前治理（`governance is None`） | `pre_write()` → `False` + 一条 `reason: "governance_path_block"` 的 BLOCK 判决 |
| 执行器写闸（`write_gate is None`） | 拒绝一切写操作（`write_gate_required`） |
| watchdog judge 异常 / 判词幻觉 | `UNASSESSABLE`（不是 `OK`） |
| 计量台账缺位 | 预算闸显式记录为旁路，且在文档里写明"没有计量就没有硬闸" |

放宽能力只能来自**显式的人为动作**（写作用域 `claim`、一次性 escalate lift 绑定
`(path, content_hash)`、`bot_allowlist` 白名单），不能来自"某个参数忘了传"。

## Consequences

- 新增治理闸的默认分支必须是拒绝，且该分支要有 conformance 测试（RED 断言拒绝 +
  GREEN 断言接入后放行）。当前落点：M5 / M13 / M15 / M22（GOVERNANCE.md §6）。
- 误拦的处理方式是**配置显式授权**，不是给闸加 bypass 开关。
- 已知代价：本地脚本/测试夹具必须先 `claim` 作用域或注入挂点，否则会被拦。这是设计
  意图——拦在写之前比事后审计便宜。
- 本 ADR 落地时收口的偏差：S3（`pre_write` 的 `return True`）已修为 BLOCK +
  判决留痕，由 `tests/test_conformance.py::test_m22_pre_write_without_governance_is_fail_closed`
  守住。

## Rejected alternatives

- **配置开关 `--allow-degraded`**：拒绝。它把"治理是否生效"变成部署细节，而审查者
  无法从代码判断线上跑的是哪种姿态。
- **只在启动时告警、运行时放行**：拒绝。告警没人看，等于 fail-open 加日志噪声。
- **按调用点各自决定**：拒绝。同一仓里两种姿态并存时，默认行为变成"看谁写的这一行"。
