"""J4：盈亏 sweep 对接——metrics.CallRecord ↔ 任务② framework.CallRecord。

桥接语义（DESIGN.md 第 1 节成本模型 Cost = T×p + N_int×V_int）：
- MetricsLedger（本仓）逐调用记录 → 按 kind 聚合映射到 framework.RunRecord
  的 calls（role=kind）与 router_overhead_tokens（assessor/judge 等
  router 侧调用的 prompt tokens 记账）。
- 判决用 framework 原函数：breakeven_price / breakeven_attention /
  paired_bootstrap_ci / verdict（预注册三档，禁止换指标）。
- EchoExecutor 确定性模拟：dual 臂有治理拦一次+watchdog 复审（介入少），
  single 臂无治理（介入多、token 少）。零网络，usage 取自响应。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ledger_records(cost_per_call_tokens, kinds):
    """构造 MetricsLedger：每调用 tokens 按 cost_per_call_tokens。"""
    from agent.metrics import CallRecord, MetricsLedger
    led = MetricsLedger()
    for i, (toks, kind) in enumerate(zip(cost_per_call_tokens, kinds)):
        led.record(CallRecord(session_id=f"s{i}", model="m",
                              prompt_tokens=toks, completion_tokens=10,
                              latency_ms=1.0, ok=True, kind=kind,
                              ticket_id=f"t{i}"))
    return led


def test_red_bridge_module_exists():
    import agent.sweep_bridge as sb   # noqa: F401  RED: 不存在则收集失败


def test_ledger_to_runrecord():
    from agent.sweep_bridge import ledger_to_runrecord
    led = _ledger_records([100, 200, 50], ["worker", "worker", "assessor"])
    rr = ledger_to_runrecord(led, run_id="r1", arm="dual", seed=0)
    assert rr.run_id == "r1" and rr.arm == "dual"
    assert len(rr.calls) == 3
    # router 侧（assessor/judge）prompt tokens 进 router_overhead_tokens
    assert rr.router_overhead_tokens == 50
    assert rr.total_tokens() == 100 + 200 + 50 + 30  # 每个 completion 10


def test_breakeven_math_uses_framework():
    """盈亏数学直接调用任务②的原函数（现算双向验证）。"""
    from agent.sweep_bridge import breakeven_price, breakeven_attention
    # p* = ΔN × V / ΔT：200 tokens 更贵、省 2 次介入、每次值 5 元
    p_star = breakeven_price(delta_interventions=2, value_per_intervention=5.0,
                             delta_tokens=200)
    assert abs(p_star - 2 * 5.0 / 200) < 1e-9        # 0.05 元/token
    c_star = breakeven_attention(delta_tokens=200, price_per_token=0.02,
                                 delta_interventions=2)
    assert abs(c_star - 200 * 0.02 / 2) < 1e-9       # 2.0 元/次
    # 边界：无 delta → None（平衡点不存在）
    assert breakeven_price(0, 5.0, 200) is None
    assert breakeven_attention(200, 0.02, 0) is None


def test_single_vs_dual_verdict_three_ways():
    """三档判决各触发一次（预注册规则，禁止换指标）。"""
    from agent.sweep_bridge import paired_cost_deltas, decide_verdict
    # dual 总成本 < single → DUAL_WINS
    single = [10.0, 10.0, 10.0, 10.0]
    dual = [4.0, 4.0, 4.0, 4.0]
    deltas, lo, hi = paired_cost_deltas(single, dual)
    assert all(d < 0 for d in deltas)
    assert decide_verdict(deltas, lo, hi) == "DUAL_WINS"
    # dual 总成本 > single → SINGLE_WINS
    dual2 = [20.0, 20.0, 20.0, 20.0]
    deltas2, lo2, hi2 = paired_cost_deltas(single, dual2)
    assert decide_verdict(deltas2, lo2, hi2) == "SINGLE_WINS"
    # 混合 → INCONCLUSIVE
    dual3 = [9.0, 11.0, 9.0, 11.0]
    deltas3, lo3, hi3 = paired_cost_deltas(single, dual3)
    assert decide_verdict(deltas3, lo3, hi3) == "INCONCLUSIVE"


def test_echo_sweep_end_to_end():
    """EchoExecutor 确定性 sweep：N 任务 × 双臂 → RunRecord ×2 → breakeven 曲线点。
    dual 臂治理拦 1 次（介入-1）但多 assessor/judge 调用（token+）。"""
    from agent.sweep_bridge import run_echo_sweep, sweep_curve
    recs = run_echo_sweep(tasks=[{"id": f"k{i}", "prompt": f"do {i}",
                                  "complexity": "L1"} for i in range(4)],
                          seeds=[1, 2])
    assert len(recs) == 16                     # 4 任务 × 2 seeds × 2 臂
    assert {r.arm for r in recs} == {"single", "dual"}
    # dual 臂 token 严格更多（多了研判/judge 调用），介入严格更少（治理拦下）
    for seed in (1, 2):
        s = next(r for r in recs if r.arm == "single" and r.seed == seed)
        d = next(r for r in recs if r.arm == "dual" and r.seed == seed)
        assert d.total_tokens() > s.total_tokens()
        assert len(d.interventions) < len(s.interventions)
    curve = sweep_curve(recs, value_per_intervention=5.0)
    assert curve["delta_tokens"] > 0 and curve["delta_interventions"] > 0
    assert abs(curve["p_star"] - curve["delta_interventions"] * 5.0
               / curve["delta_tokens"]) < 1e-9


def main():
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            fails += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    if fails:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
