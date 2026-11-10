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


def run_real_sweep(tasks: list[dict], seeds: list[int],
                   endpoint: str = "http://127.0.0.1:3425/v1/chat/completions",
                   model: str = "deepseek-v4.1-flash",
                   work_root: str = "/tmp/superagent-sweep",
                   timeout: float = 45.0,
                   max_tool_rounds: int = 2) -> list:
    """K1 真实臂 sweep：同一批任务×seeds 在两臂下真跑网关。

    single 臂：裸 worker（OpenAICompatExecutor 直调一次 tool-loop），
      无 assessor / watchdog / 治理 —— 介入记账 2 次/任务（人自己研判+复核）。
    dual 臂：Engine 全链（LLMAssessor 研判 + worker + Watchdog 复核 + 治理
      挂点）—— 介入记账 1 次/任务（治理确定性拦截已由 J2/演示 ruling 证实）。
    成本数字全部取自真 usage（MetricsLedger），返回 RunRecord 列表可直喂
    sweep_curve()。
    """
    import time as _time
    import os as _os
    from agent.model import Input
    from agent.store import Store
    from agent.engine import Engine
    from agent.admission import AdmissionConfig
    from agent.governance import ScopeRegistry
    from agent.watchdog import Watchdog
    from agent.assessor import LLMAssessor
    from agent.openai_executor import OpenAICompatExecutor
    from agent.metrics import MetricsLedger

    out = []
    _os.makedirs(work_root, exist_ok=True)
    for t in tasks:
        for seed in seeds:
            for arm in ("single", "dual"):
                led = MetricsLedger()
                run_dir = _os.path.join(work_root, f"{t['id']}-{arm}-{seed}")
                _os.makedirs(run_dir, exist_ok=True)
                target = _os.path.join(run_dir, "notes.md")
                if arm == "single":
                    ex = OpenAICompatExecutor(
                        endpoint=endpoint, model=model, metrics=led,
                        timeout=timeout, max_tool_rounds=max_tool_rounds)
                    sid = f"single-{t['id']}-{seed}"
                    ex.sessions[sid] = {"ticket_id": t["id"],
                                        "mandate": None,
                                        "messages": [{"role": "system",
                                                      "content": ex.system_prompt}]}
                    # 直接以 Input 形式投给 worker（绕过 admission：single 臂无治理）
                    inp = Input(id=f"{t['id']}-single-{seed}",
                                thread_key="direct", text=t["text"],
                                is_human=True)
                    ex.deliver(_fake_assignment(sid, t["id"]), inp)
                    n_int = 2
                else:
                    engine, ex = _real_engine(endpoint, model, led, timeout,
                                              max_tool_rounds, run_dir)
                    inp = Input(id=f"{t['id']}-dual-{seed}",
                                thread_key="inbox", text=t["text"],
                                is_human=True)
                    res = engine.intake(inp, mandate_rules=t["rules"])
                    if res.get("disposition") == "accept":
                        tid = res["ticket_id"]
                        ticket = engine.tickets.get(tid)
                        a = ticket.assignments[-1]
                        _finish(engine, ex, a, tid, target)
                    n_int = 1
                rr = ledger_to_runrecord(
                    led, run_id=f"{t['id']}-{arm}-{seed}", arm=arm,
                    seed=seed, interventions=n_int)
                out.append(rr)
                print(f"  [{t['id']} {arm} seed{seed}] "
                      f"tokens={rr.router_overhead_tokens + _worker_tokens(led)} "
                      f"calls={len(led.records)}")
    return out


def _fake_assignment(session_id: str, ticket_id: str):
    from agent.model import Assignment
    return Assignment(ticket_id=ticket_id, session_id=session_id)


def _worker_tokens(led) -> int:
    return sum(c.prompt_tokens + c.completion_tokens for c in led.records
               if c.kind == "worker")


def _real_engine(endpoint, model, led, timeout, rounds, run_dir):
    from agent.engine import Engine
    from agent.store import Store
    from agent.admission import AdmissionConfig
    from agent.governance import ScopeRegistry
    from agent.watchdog import Watchdog
    from agent.assessor import LLMAssessor
    from agent.openai_executor import OpenAICompatExecutor
    from agent.proactivity import ProactivityEngine, ProactivityConfig
    from agent.calibration import ConfidenceEstimator
    import os as _os

    ex = OpenAICompatExecutor(endpoint=endpoint, model=model, metrics=led,
                              timeout=timeout, max_tool_rounds=rounds)
    engine = Engine(
        store=Store(_os.path.join(run_dir, "sweep.db")),
        cfg=AdmissionConfig(whitelist=["inbox"]), executor=ex,
        deliver=lambda ch, body: True, watchdog=Watchdog(),
        governance=ScopeRegistry(),
        proactivity=ProactivityEngine(ProactivityConfig(observe=True)),
        assessor=LLMAssessor(model=model, endpoint=endpoint, timeout=30.0),
        max_repairs=1, confidence_estimator=ConfidenceEstimator())
    return engine, ex


def _finish(engine, ex, a, tid, target):
    """收报告：宿主代执行写动作（过治理），再喂 watchdog。"""
    import json as _json, re as _re, os as _os
    msgs = ex.sessions.get(a.session_id, {}).get("messages", [])
    raw = ""
    for m in reversed(msgs):
        if m.get("role") == "assistant" and m.get("content"):
            raw = m["content"]
            break
    report = {"summary": raw[:200], "touched_paths": []}
    m = _re.search(r"\{.*\}", raw, _re.S)
    if m:
        try:
            d = _json.loads(m.group(0))
            if isinstance(d, dict):
                d.setdefault("summary", raw[:200])
                report = d
        except _json.JSONDecodeError:
            pass
    for path in report.get("touched_paths") or []:
        engine.pre_write(tid, path)
    engine.on_worker_report(a, report)


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
