"""B：把"方向性证据"做成可判决的结论。

两件此前缺的东西：
1. **CI 是否跨 0**——`verdict()` 只看 ci_high<0 / ci_low>0，但旧 sweep_curve 没把
   "跨 0 就说明结论不成立"这件事写成可读字段，读者得自己比。
2. **V 敏感性**——介入价值 V（元/次）在旧脚本里是硬编码假设（5.0），而
   `CostModel` 默认又给 8.33（=5/60*100），两处口径不一致。本模块让**同一个 V
   同时驱动成本模型与 p***：`cost_model_for_value(V)` 把 V 换成
   attention_value_per_hour = V*60/minutes，于是
       delta_cost(V) = ΔT/1e6 * price_per_1m - ΔN * V
   是 V 的一次函数，翻面点可解析求出，网格扫描只是把它摆出来给人看。

数学单一事实源仍是 `vendor/framework.py`（breakeven_price / paired_bootstrap_ci /
verdict / CostModel），本模块不重写公式。
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.abspath(os.path.join(_HERE, "..", "vendor"))
_DIGIT = os.path.abspath(os.path.join(_HERE, "..", "..", "superagent-dig",
                                      "02-cost-experiment"))
_FW_DIR = _VENDOR if os.path.exists(os.path.join(_VENDOR, "framework.py")) else _DIGIT
if _FW_DIR not in sys.path:
    sys.path.insert(0, _FW_DIR)

from framework import (CostModel,                # noqa: E402
                       breakeven_price,
                       paired_bootstrap_ci,
                       verdict)


def cost_model_for_value(value_per_intervention: float,
                         minutes: float = 5.0,
                         price_per_1m_tokens: float = 4.0) -> CostModel:
    """构造让 value_per_intervention() 恰等于给定 V 的成本模型。

    CostModel 的定义：V = minutes/60 * hourly → hourly = V*60/minutes。
    """
    hourly = value_per_intervention * 60.0 / minutes
    return CostModel(price_per_1m_tokens=price_per_1m_tokens,
                     attention_value_per_hour=hourly,
                     intervention_minutes=minutes)


def ci_summary(deltas: list[float], ci_low: float, ci_high: float) -> dict:
    """配对差分 + CI 的可读判决块。crosses_zero=True 表示结论不成立。"""
    n = len(deltas)
    mean = sum(deltas) / n if n else 0.0
    return {
        "n_pairs": n,
        "mean_delta_cost": round(mean, 6),
        "ci_low": round(ci_low, 6),
        "ci_high": round(ci_high, 6),
        # 区间含 0 = 在 95% 置信度下无法区分两臂
        "crosses_zero": ci_low <= 0.0 <= ci_high,
        "verdict": verdict(deltas, ci_low, ci_high),
    }


def _pair(singles: list, duals: list) -> list[tuple]:
    """按 seed 配对（同 seed 同 task 才算配对差分，禁止混配）。"""
    by_seed = {d.seed: d for d in duals}
    pairs = [(s, by_seed[s.seed]) for s in singles if s.seed in by_seed]
    return pairs


def v_sensitivity(singles: list, duals: list,
                  values: list[float] | None = None,
                  price_per_1m_tokens: float = 4.0,
                  n_boot: int = 2000, seed: int = 0) -> list[dict]:
    """对每个 V 重算成本差分、CI、判决与 p*（V 一次扫过）。"""
    values = values if values is not None else [0.5, 1.0, 2.0, 5.0, 10.0, 20.0]
    pairs = _pair(singles, duals)
    rows: list[dict] = []
    for v in values:
        model = cost_model_for_value(v, price_per_1m_tokens=price_per_1m_tokens)
        deltas: list[float] = []
        d_tokens = 0
        d_ints = 0
        for s, d in pairs:
            deltas.append(model.cost(d)["total_cost"] - model.cost(s)["total_cost"])
            d_tokens += d.total_tokens() - s.total_tokens()
            d_ints += len(s.interventions) - len(d.interventions)
        if not deltas:
            continue
        lo, hi = paired_bootstrap_ci(deltas, n_boot=n_boot, seed=seed)
        block = ci_summary(deltas, lo, hi)
        block.update({
            "value_per_intervention": v,
            "delta_tokens": d_tokens,
            "delta_interventions": d_ints,
            "p_star": breakeven_price(d_ints, v, d_tokens),
        })
        rows.append(block)
    return rows


def flip_point(rows: list[dict]) -> float | None:
    """网格里第一个判 DUAL_WINS 的 V；付不起这个价就不该上双层。

    网格需按 V 升序传入（调用方负责），本函数不排序——排序会掩盖输入错误。
    """
    for r in rows:
        if r.get("verdict") == "DUAL_WINS":
            return r["value_per_intervention"]
    return None
