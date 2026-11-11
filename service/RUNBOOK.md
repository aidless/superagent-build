# 服役月 RUNBOOK — superagent-build 真实服役计划

> 目标（PG 过堂裁决）：功能冻结一个月，让 agent 干真活攒数据——observe 事件
> 既是 K2 校准输入，又是文章素材。**所有功能迭代暂停，bug 修复例外。**

## 每日一键

```bash
cd superagent-build
bash service/run_daily.sh          # 跑当日服役批 + 追加 changelog 行
```

`run_daily.sh` 幂等：input id 带日期戳，重跑不会 duplicate（receipt 幂等）。
任务集每天由 `service/tasks_$(date +%F).json` 驱动；没有当天任务文件时
自动回落到内置的三件套（CHANGELOG 维护 / 越界挑逗 / 运行日志）。

## 数据流向

| 产物 | 路径 | 用途 |
|---|---|---|
| observe 事件 | `service/observe_log.jsonl`（追加） | K2 校准（目标 30-50 条含坏开口） |
| 成本台账 | `service/metrics.jsonl`（按日） | 成本账 + DUAL_WINS 曲线数据 |
| 判决流 | 各日 `results/real/service-<date>.db` | 审计/复盘 |
| 运行日志 | `service/LOG.md` | worker 自己写的（治理内写盘） |

## 每周动作（人工，10 分钟）

1. `python3 -c "from agent.calibration import LabelStore; ..."` 统计本周
   speak_precision（或直接看 observe_log 的 verdict 分布）。
2. 攒满 30 条且含 ≥3 个坏开口（watchdog 非 ok）→ 跑
   `close_loop`（calibration_loop.py）做一次真校准。
3. 有意思的对话（自主拒绝/治理拦截）截出来 → 文章素材库。

## 纪律（AGENTS.md 17 条的落地）

- **不装 cron**：本机是控制台不是算力节点，服役批由人手动触发（约 1 分钟/批）。
- 真网关批跑一律 `run_in_background` + `job_output(wait)`，不挂后台悄悄跑。
- 30-50 条攒够前不烧配额做重复校准（同质数据零信息量）。

## 冻结清单（一个月内不动）

- engine/admission/governance/watchdog/proactivity/scheduler 的接口签名。
- 例外：修真 bug（先 RED 复现）；estimator 证据源扩维**在攒数据前做**（否则
  又是一批同质 conf=0.533）。
