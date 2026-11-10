"""J4：盈亏 sweep 对接桥——本仓 MetricsLedger ↔ 任务② framework。

设计约束：
- framework.py 通过 sys.path 引入 ../superagent-dig/02-cost-experiment/
  （相对本文件两层的仓库内路径），不复制代码——数学函数单一事实源。
- ledger_to_runrecord：逐调用 CallRecord(kind=...) → framework CallRecord
  (role=kind)；router 侧（非 worker 的 assessor/judge/router）的 prompt
  tokens 同时计入 router_overhead_tokens（Router 协调税，DESIGN.md 定义）。
- EchoExecutor 确定性 sweep（零网络）：
  single 臂 = 每 task 1 次 worker 调用 + 2 次人工介入（缺治理，出错靠人兜）；
  dual 臂   = 每 task 1 次 worker + 1 次 assessor + 1 次 judge 调用
              + 1 次人工介入（治理/复核拦下一次人为打断）。
  这是 Echo 模拟的确定性假设，真实臂要用 I3 执行器跑（对接面已留）。
- sweep_curve：配对差分（同 seed 同 task 排序后 single-dual）→ breakeven
  两个读数 p*（token 价格盈亏点）与 C*（注意力价值盈亏点）。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FW_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "superagent-dig",
                                       "02-cost-experiment"))
if _FW_DIR not in sys.path:
    sys.path.insert(0, _FW_DIR)

from framework import (CallRecord as FWCallRecord,       # noqa: E402
                       CostModel,                        # noqa: E402,F401
                       InterventionRecord,
                       RunRecord,
                       breakeven_attention,
                       breakeven_price,
                       paired_bootstrap_ci,
                       verdict)

from agent.metrics import CallRecord, MetricsLedger       # noqa: E402

WORKER_KINDS = {"worker"}
ROUTER_KINDS = {"assessor", "judge", "router"}


def ledger_to_runrecord(led: MetricsLedger, run_id: str, arm: str,
                        seed: int, interventions: int = 0,
                        wall_seconds: float = 0.0) -> RunRecord:
    """MetricsLedger → framework.RunRecord（role=kind 映射）。
    router 侧 prompt tokens 记入 router_overhead_tokens（协调税）。"""
    calls = []
    router_tax = 0
    for r in led.records:
        calls.append(FWCallRecord(role=r.kind if r.kind != "single-loop"
                                  else "single-loop",
                                prompt_tokens=r.prompt_tokens,
                                completion_tokens=r.completion_tokens,
                                wall_seconds=r.latency_ms / 1000.0))
        if r.kind in ROUTER_KINDS:
            router_tax += r.prompt_tokens
    return RunRecord(
        run_id=run_id, arm=arm, seed=seed, calls=calls,
        interventions=[InterventionRecord(task_id=f"i{k}", decision_kind="approval")
                       for k in range(interventions)],
        router_overhead_tokens=router_tax,
        wall_seconds=wall_seconds)


def paired_cost_deltas(costs_single: list[float],
                       costs_dual: list[float]) -> tuple[list[float], float, float]:
    """配对成本差 dual-single（framework.verdict 约定：delta<0 = dual 更省）
    → (deltas, ci_low, ci_high)（framework 原函数）。"""
    deltas = [d - s for s, d in zip(costs_single, costs_dual)]
    lo, hi = paired_bootstrap_ci(deltas, n_boot=2000, seed=0)
    return deltas, lo, hi


def decide_verdict(deltas: list[float], ci_low: float,
                   ci_high: float) -> str:
    """三档判决（framework.verdict 原样转发，禁止换指标）。"""
    return verdict(deltas, ci_low, ci_high)


# ---------------------------------------------------------------------------
# EchoExecutor 确定性 sweep（本仓零网络模拟；真实臂换 OpenAICompatExecutor）
# ---------------------------------------------------------------------------

def _echo_ledger(task_id: str, arm: str, seed: int,
                 worker_tokens: int) -> MetricsLedger:
    led = MetricsLedger()
    led.record(CallRecord(session_id=f"{task_id}-{arm}-{seed}-w", model="echo",
                          prompt_tokens=worker_tokens,
                          completion_tokens=50 + seed, latency_ms=1.0,
                          ok=True, kind="worker", ticket_id=task_id))
    if arm == "dual":
        # 治理研判 + watchdog 复审两个 router 侧调用（协调税）
        for kind, toks in (("assessor", 120), ("judge", 90)):
            led.record(CallRecord(session_id=f"{task_id}-{arm}-{seed}-{kind}",
                                  model="echo", prompt_tokens=toks,
                                  completion_tokens=20, latency_ms=1.0,
                                  ok=True, kind=kind, ticket_id=task_id))
    return led


def run_echo_sweep(tasks: list[dict], seeds: list[int],
                   worker_tokens: int = 300) -> list[RunRecord]:
    """确定性双臂 sweep：single=1 worker+2 介入；dual=1 worker+2 router+1 介入。
    dual token 严格更多、介入严格更少（治理存在的意义）。"""
    out: list[RunRecord] = []
    for t in tasks:
        for seed in seeds:
            for arm, n_int in (("single", 2), ("dual", 1)):
                led = _echo_ledger(t["id"], arm, seed, worker_tokens)
                rr = ledger_to_runrecord(led, run_id=f"{t['id']}-{arm}-{seed}",
                                         arm=arm, seed=seed,
                                         interventions=n_int)
                out.append(rr)
    return out


def sweep_curve(records: list[RunRecord],
                value_per_intervention: float = 5.0,
                price_per_token: float = 0.02) -> dict:
    """配对差分 → 盈亏曲线点。按 (seed, run 序) 对齐 single/dual。
    返回 delta_tokens / delta_interventions / p_star / c_star / verdict。"""
    singles = [r for r in records if r.arm == "single"]
    duals = [r for r in records if r.arm == "dual"]
    singles.sort(key=lambda r: (r.seed, r.run_id))
    duals.sort(key=lambda r: (r.seed, r.run_id))
    assert len(singles) == len(duals) and singles, "两臂 run 数必须配对相等"

    d_toks = [d.total_tokens() - s.total_tokens()
              for s, d in zip(singles, duals)]
    d_ints = [len(s.interventions) - len(d.interventions)
              for s, d in zip(singles, duals)]
    delta_tokens = sum(d_toks)
    delta_interventions = sum(d_ints)

    p_star = breakeven_price(delta_interventions, value_per_intervention,
                             delta_tokens)
    c_star = breakeven_attention(delta_tokens, price_per_token,
                                 delta_interventions)

    model = CostModel()
    costs_s = [model.cost(r)["total_cost"] for r in singles]
    costs_d = [model.cost(r)["total_cost"] for r in duals]
    deltas, lo, hi = paired_cost_deltas(costs_s, costs_d)
    return {
        "delta_tokens": delta_tokens,
        "delta_interventions": delta_interventions,
        "p_star": p_star,
        "c_star": c_star,
        "cost_deltas": deltas,
        "ci_low": lo,
        "ci_high": hi,
        "verdict": decide_verdict(deltas, lo, hi),
    }
