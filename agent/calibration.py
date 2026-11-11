"""主动性校准机制（迭代 I5）：给开口事件一个真证据源 + 校准闭环。

背景（任务⑤建模，见 superagent-dig/05-proactivity-modeling.md）：
- 开口判定 confidence ≥ p*(t) = (c_false + c_speak) / (u_eff(t) + c_false)。
- engine.py 里原先硬编码 confidence=0.95 —— 那是个假确证度，没有证据支撑。
- observe 模式攒下的日志就是"该不该说"的标注数据；用它反推能达标的参数
  (c_false, c_speak, lam)，校准目标是开口 precision ≥ target（默认 0.8）。

三个组件：
1) ConfidenceEstimator —— 多证据源加权合成确证度（替代硬编码 0.95）。
2) LabelStore         —— append-only JSONL 标注存储（observe 日志的人工标注）。
3) calibrate()        —— 网格搜索参数，重放判定，挑出开口 precision 达标的第一组。

设计约束：纯 stdlib、确定性、无网络、无 sleep。校准重放直接复用
proactivity.ProactivityEngine，把时钟钉在 max(created_at) 以剥离衰减干扰。
"""
from __future__ import annotations

import json
import os
from typing import Optional

# ---------- ConfidenceEstimator 常量 ----------

# 默认证据源键（缺省会参与加权平均）：
#   watchdog_pass        watchdog 复核通过率
#   worker_verified      worker 自证比例
#   historical_precision 历史开口准确率
DEFAULT_SOURCES = ("watchdog_pass", "worker_verified", "historical_precision")

_NEUTRAL = 0.5          # 缺证据源时的中性值
_MISSING_DISCOUNT = 0.8  # 有源缺失时对结果打的折
_EMPTY_CONSERVATIVE = 0.4  # 空 evidence 的保守值


class ConfidenceEstimator:
    """证据源 → 确证度 p ∈ [0,1] 的合成器。

    行为：
    - 配置的证据源逐个取 evidence 里的值；缺失的源按 0.5 中性值参与，并对
      最终加权平均乘 0.8 打折（缺证据要保守）。
    - 空 evidence 直接返回 0.4（最保守，连有多少源都不知道）。
    - 结果始终 clamp 到 [0,1]（宽容越界输入，避免脏数据撑爆阈值判定）。

    weights: 可选 {"源名": 权重}。None → 三个默认源各 1.0；显式给出则以其为
             唯一证据源集合（可用于单源/自定义源场景）。
    """

    def __init__(self, weights: Optional[dict] = None):
        if weights is None:
            self.weights: dict[str, float] = {k: 1.0 for k in DEFAULT_SOURCES}
        else:
            self.weights = dict(weights)

    def estimate(self, evidence: dict) -> float:
        if not evidence:
            return _EMPTY_CONSERVATIVE

        total = 0.0
        total_w = 0.0
        missing = False
        for src, w in self.weights.items():
            if w <= 0:
                continue
            if src in evidence:
                val = float(evidence[src])
            else:
                val = _NEUTRAL
                missing = True
            total += w * val
            total_w += w

        if total_w <= 0:
            return _EMPTY_CONSERVATIVE

        score = total / total_w
        if missing:
            score *= _MISSING_DISCOUNT
        # clamp 到 [0,1]
        if score < 0.0:
            return 0.0
        if score > 1.0:
            return 1.0
        return score


# ---------- LabelStore 常量 ----------

VALID_LABELS = ("speak_ok", "speak_bad", "wait_ok", "wait_bad")


class LabelStore:
    """append-only 标注存储。

    - path=None → 纯内存（self._mem）。
    - path=str  → 追加写 JSONL（每行一条记录；不覆写、不删除）。
    - 每条记录 = decision_dict 的浅拷贝 + 覆盖写入的 "label" 字段，
      这样 load() 读回的记录可直接喂给 calibrate()（含 utility/confidence/
      created_at/label）。
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path
        self._mem: list[dict] = []
        if path is not None:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)

    def append(self, decision_dict: dict, label: str) -> None:
        if label not in VALID_LABELS:
            raise ValueError(
                f"非法 label: {label!r}，仅允许 {VALID_LABELS}")
        record = dict(decision_dict)
        record["label"] = label
        if self.path is None:
            self._mem.append(record)
            return
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def load(self) -> list[dict]:
        if self.path is None:
            return list(self._mem)
        if not os.path.exists(self.path):
            return []
        out: list[dict] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                out.append(json.loads(line))
        return out

    def stats(self) -> dict:
        recs = self.load()
        counts = {lb: 0 for lb in VALID_LABELS}
        for r in recs:
            lb = r.get("label")
            if lb in counts:
                counts[lb] += 1
        so = counts["speak_ok"]
        sb = counts["speak_bad"]
        denom = so + sb
        precision = (so / denom) if denom > 0 else None
        result: dict = dict(counts)
        result["speak_precision"] = precision
        # 信用门（APx 借鉴）：库里存在 speak_bad = watchdog 抓到过真实坏开口
        # → 本批数据只能做诊断，官方校准置 INELIGIBLE（prevent 假 precision
        # 从冲突数据上刷出来）。
        result["ineligible"] = sb > 0
        return result


# ---------- calibrate ----------

DEFAULT_GRID = {
    "c_false": [0.5, 1.0, 2.0],
    "c_speak": [0.1, 0.2, 0.5],
    "lam": [0.05, 0.1, 0.2],
}


def _replay_precision(labels: list[dict], c_false: float, c_speak: float,
                      lam: float, now: float) -> tuple[float, int]:
    """用一组参数重放全部标注，返回 (开口 precision, n_speak)。

    判定直接复用 ProactivityEngine（observe=False，时钟钉在 now）：
      - action == "speak" 记一次开口；
      - 其中人工 label 为 "speak_ok" 的算命中；
      - precision = 命中数 / n_speak；n_speak==0 时记 0.0（不开口没有可证明的精度）。
    """
    from .proactivity import (CandidateEvent, ProactivityConfig,
                              ProactivityEngine)

    cfg = ProactivityConfig(lam=lam, c_false=c_false, c_speak=c_speak,
                            observe=False)
    engine = ProactivityEngine(cfg, clock=lambda: now)

    n_speak = 0
    tp = 0
    for r in labels:
        ev = CandidateEvent(
            event_id=str(r.get("event_id", "?")),
            utility=float(r["utility"]),
            confidence=float(r["confidence"]),
            created_at=float(r["created_at"]),
        )
        decision = engine.evaluate(ev)
        if decision.action == "speak":
            n_speak += 1
            if r.get("label") == "speak_ok":
                tp += 1

    precision = (tp / n_speak) if n_speak > 0 else 0.0
    return precision, n_speak


def calibrate(labels: list[dict], target_precision: float = 0.8,
              grid: Optional[dict] = None) -> dict:
    """网格搜索 (c_false, c_speak, lam)，找出开口 precision 达标的第一组。

    返回：
      达标 → {"calibrated": True, "c_false", "c_speak", "lam",
              "achieved_precision", "n_speak"}
      不达标 → {"calibrated": False, "best_precision", "best_params", "n_speak"}

    要求 n_speak > 0 才算达标（"从不开口"没有可证明的精度，不能靠沉默刷分）。
    """
    g = grid or DEFAULT_GRID
    cf_list = list(g["c_false"])
    cs_list = list(g["c_speak"])
    lam_list = list(g["lam"])

    if not labels:
        return {"calibrated": False, "best_precision": None,
                "best_params": None, "n_speak": 0}

    # 固定时钟：now = max(created_at)，剥离"事件存放时间"造成的衰减干扰。
    now = max(float(r["created_at"]) for r in labels)

    best_precision = -1.0
    best_params: Optional[dict] = None
    best_n_speak = 0

    for c_false in cf_list:
        for c_speak in cs_list:
            for lam in lam_list:
                precision, n_speak = _replay_precision(
                    labels, c_false, c_speak, lam, now)
                # 记录最优组（同精度优先更多开口，避免"零开口"占位）
                if (precision > best_precision
                        or (precision == best_precision
                            and n_speak > best_n_speak)):
                    best_precision = precision
                    best_params = {"c_false": c_false, "c_speak": c_speak,
                                   "lam": lam}
                    best_n_speak = n_speak
                # 达标（必须有开口且 precision ≥ target）→ 返回第一组
                if n_speak > 0 and precision >= target_precision:
                    return {
                        "calibrated": True,
                        "c_false": c_false,
                        "c_speak": c_speak,
                        "lam": lam,
                        "achieved_precision": precision,
                        "n_speak": n_speak,
                    }

    return {
        "calibrated": False,
        "best_precision": best_precision,
        "best_params": best_params,
        "n_speak": best_n_speak,
    }
