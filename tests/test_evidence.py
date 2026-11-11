"""B：把"方向性证据"做成结论——CI 是否跨 0 + V 敏感性翻面点。

RED 纪律：先写断言（agent/evidence.py 不存在 → 收集期必红），再写实现。
数学全部解析可复算（构造用例的期望值手算给出，见注释）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.evidence import (ci_summary, flip_point, v_sensitivity,
                            cost_model_for_value)
from agent.sweep_bridge import (FWCallRecord, InterventionRecord, RunRecord)


def _rr(arm, seed, ptok, ctok, n_int):
    return RunRecord(
        run_id=f"r-{arm}-{seed}", arm=arm, seed=seed,
        calls=[FWCallRecord(role="worker", prompt_tokens=ptok,
                            completion_tokens=ctok, wall_seconds=1.0)],
        interventions=[InterventionRecord(task_id=f"i{k}",
                                          decision_kind="approval")
                       for k in range(n_int)],
        router_overhead_tokens=0, wall_seconds=1.0)


# single: 200 tokens / 2 介入；dual: 1600 tokens / 1 介入
# → delta_tokens=1400, delta_interventions=1
# cost delta = 1400/1e6*price_per_1m - V = 0.0056 - V（price_per_1m=4.0）
# 翻面点 V* = 0.0056（解析解）
SINGLES = [_rr("single", s, 100, 100, 2) for s in (0, 1, 2, 3)]
DUALS = [_rr("dual", s, 1200, 400, 1) for s in (0, 1, 2, 3)]


def test_ci_summary_crosses_zero():
    c = ci_summary([0.1, -0.1, 0.05, -0.02], -0.08, 0.07)
    assert c["crosses_zero"] is True
    assert c["verdict"] == "INCONCLUSIVE"
    c2 = ci_summary([-1.0, -0.9, -1.1, -1.05], -1.2, -0.8)
    assert c2["crosses_zero"] is False
    assert c2["verdict"] == "DUAL_WINS"


def test_cost_model_for_value_matches_definition():
    m = cost_model_for_value(5.0, minutes=5.0)
    # V = minutes/60 * hourly → hourly = V*60/minutes = 60
    assert abs(m.value_per_intervention() - 5.0) < 1e-9


def test_v_sensitivity_rows_and_flip():
    rows = v_sensitivity(SINGLES, DUALS, values=[0.001, 0.005, 0.006, 0.01, 5.0])
    assert [r["value_per_intervention"] for r in rows] == \
        [0.001, 0.005, 0.006, 0.01, 5.0]
    # V=0.001 → delta=0.0056-0.001=+0.0046 > 0 → SINGLE_WINS
    assert rows[0]["verdict"] == "SINGLE_WINS"
    assert abs(rows[0]["mean_delta_cost"] - 0.0046) < 1e-9
    # V=0.005 → +0.0006（仍 SINGLE）；V=0.006 → -0.0004（跨到 DUAL）
    assert rows[1]["verdict"] == "SINGLE_WINS"
    assert rows[2]["verdict"] == "DUAL_WINS"
    assert abs(rows[2]["mean_delta_cost"] + 0.0004) < 1e-9
    # p* = ΔN*V/ΔT = 1*V/1400
    assert abs(rows[4]["p_star"] - 5.0 / 1400) < 1e-12
    # 翻面点 = 网格里第一个 DUAL_WINS 的 V
    assert flip_point(rows) == 0.006


def test_sensitivity_no_flip_is_none():
    rows = v_sensitivity(SINGLES, DUALS, values=[0.001, 0.005])
    assert flip_point(rows) is None


def test_close_loop_accepts_custom_labeler(tmp_path):
    """close_loop 必须能把人工覆写标签带进 LabelStore（D4 真标注入口）。"""
    from agent.calibration_loop import close_loop
    events = [{"event_id": "input:t6", "action": "observe_drop",
               "confidence": 0.48, "utility": 0.01, "created_at": 1000.0,
               "p_star": 1.18, "u_effective": 0.01,
               "watchdog_verdict": "unassessable"}]
    res = close_loop(events, state_dir=str(tmp_path),
                     labeler=lambda ev: "wait_bad")
    assert res["stats"]["wait_bad"] == 1
    assert res["n_labeled"] == 1
