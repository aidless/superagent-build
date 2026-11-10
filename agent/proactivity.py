"""主动性调度：开口阈值引擎（任务⑤）。

模型（见 superagent-dig/05-proactivity-modeling.md）：
- 每个候选事件 e 有信息价值 u(e) ≥ 0、确证度 p ∈ [0,1]、产生时刻 created_at。
- 价值随时间指数衰减：u_eff(t) = u(e) · exp(-λ·(t - created_at))。
- 三个动作：Speak（现在开口）/ Wait（再观察）/ Drop（永不开口）。

开口判据（Speak 优于 Wait 的临界确证度）：
    p·u_eff - (1-p)·c_false ≥ c_speak
  移项解得
    p*(t) = (c_false + c_speak) / (u_eff(t) + c_false)
  confidence ≥ p*(t) → Speak，否则 Wait。

Drop 判据（连"说"都不值得，优先于 Speak/Wait 判定）：
    u_eff(t) < c_speak  → Drop（无论确证度多高）。

observe 模式：把真实动作改记为带 observe_ 前缀
（observe_speak / observe_wait / observe_drop），只记录不真开口，
用来攒"该不该说"的标注数据（对齐任务⑤的验证路径）。

设计约束：纯 stdlib、无网络、无 sleep；时钟可注入（clock 参数）以便
测试与回放；所有判定写入 self.log 供审计。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class ProactivityConfig:
    """引擎参数。默认值即建模文档的基准参数。"""
    lam: float = 0.1          # 衰减率 /秒
    c_false: float = 1.0      # 误报代价
    c_speak: float = 0.2      # 开口基础代价
    observe: bool = True      # observe 模式：只记录不真开口


@dataclass
class CandidateEvent:
    """一个待报事件。"""
    event_id: str
    utility: float            # u(e) ≥ 0
    confidence: float         # p ∈ [0,1]（确证度，由上游 judge/证据给）
    created_at: float         # epoch 秒


@dataclass
class Decision:
    """一次判定结果（可审计：p_star / u_effective / reason 三件套）。"""
    event_id: str
    action: str               # speak|wait|drop|observe_speak|observe_wait|observe_drop
    p_star: float             # 临界确证度（判定依据）
    u_effective: float        # 衰减后效用
    reason: str               # 一句话，含本次 p* / u_eff / confidence 三个数


class ProactivityEngine:
    """主动性调度引擎。clock 可注入，便于测试与回放（本模块不 sleep）。"""

    def __init__(self, cfg: ProactivityConfig,
                 clock: Optional[Callable[[], float]] = None):
        self.cfg = cfg
        self.clock: Callable[[], float] = clock or time.time
        self.log: list[Decision] = []   # 全部判定记录（审计 + observe 标注数据）

    # ---------- 数值核心 ----------

    def u_effective(self, ev: CandidateEvent, now: float) -> float:
        """衰减后效用 u(e)·exp(-λ·Δt)；Δt = now - created_at，负延迟按 0 处理。"""
        dt = now - ev.created_at
        if dt < 0.0:
            dt = 0.0
        return ev.utility * math.exp(-self.cfg.lam * dt)

    def p_star(self, u_effective: float) -> float:
        """临界确证度 p* = (c_false + c_speak) / (u_eff + c_false)。"""
        denom = u_effective + self.cfg.c_false
        if denom <= 0.0:
            # 仅当 c_false=0 且 u_eff=0 的退化情形：任何有限确证度都不值得开口
            return float("inf")
        return (self.cfg.c_false + self.cfg.c_speak) / denom

    # ---------- 判定 ----------

    def _validate(self, ev: CandidateEvent) -> None:
        """入参硬校验：效用非负、确证度落在 [0,1]，越界即抛 ValueError。"""
        if ev.utility < 0.0:
            raise ValueError(
                f"utility 必须 ≥ 0，收到 {ev.utility!r}（event {ev.event_id}）")
        if not (0.0 <= ev.confidence <= 1.0):
            raise ValueError(
                f"confidence 必须落在 [0,1]，收到 {ev.confidence!r}"
                f"（event {ev.event_id}）")

    def evaluate(self, ev: CandidateEvent) -> Decision:
        """判定序：先 Drop（u_eff < c_speak）→ 再 Speak vs Wait（confidence ≥ p*）。"""
        self._validate(ev)
        now = self.clock()
        u_eff = self.u_effective(ev, now)
        ps = self.p_star(u_eff)

        # 审计头：无论走哪条分支都先拼上 p* / u_eff / confidence 三个数
        head = (f"p*={ps:.6f} u_eff={u_eff:.6f} "
                f"confidence={ev.confidence:.6f}")

        if u_eff < self.cfg.c_speak:
            raw = "drop"
            reason = (f"{head} | 判据: u_eff<{self.cfg.c_speak:.6f} → drop"
                      f"（连说都不值得，drop 优先）")
        elif ev.confidence >= ps:
            raw = "speak"
            reason = f"{head} | 判据: confidence≥p* → speak"
        else:
            raw = "wait"
            reason = f"{head} | 判据: confidence<p* → wait（证据不足，继续观察）"

        action = ("observe_" + raw) if self.cfg.observe else raw
        decision = Decision(event_id=ev.event_id, action=action, p_star=ps,
                            u_effective=u_eff, reason=reason)
        self.log.append(decision)
        return decision

    # ---------- 内置自检 ----------

    def selftest(self) -> int:
        """引擎内置自检：固定时钟跑几条基准判定。返回 0=全过 / 1=有失败。"""
        base = ProactivityConfig(lam=0.1, c_false=1.0, c_speak=0.2, observe=False)
        t0 = 1000.0
        eng = ProactivityEngine(base, clock=lambda: t0)

        ok = True

        def expect(cond: bool) -> None:
            nonlocal ok
            if not cond:
                ok = False

        # u=10、t=0（无衰减）→ p* = 1.2/11 ≈ 0.109091
        d_speak = eng.evaluate(CandidateEvent("s", 10.0, 0.5, t0))   # ≥p* → speak
        d_wait = eng.evaluate(CandidateEvent("w", 10.0, 0.05, t0))   # <p* → wait
        d_drop = eng.evaluate(CandidateEvent("d", 0.1, 1.0, t0))     # u_eff<c_speak → drop

        expect(d_speak.action == "speak")
        expect(d_wait.action == "wait")
        expect(d_drop.action == "drop")
        expect(abs(d_speak.p_star - (1.0 + 0.2) / (10.0 + 1.0)) < 1e-12)
        return 0 if ok else 1
