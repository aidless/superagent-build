"""调度循环（I6）：被外部循环驱动的一轮 tick。

纯 stdlib，无 sleep / 无线程：一轮 tick(now) 一次做一轮，clock 由调用方传入，
天然可测。

一轮三件事：
    a. 派发  —— 对 OPEN 且 undelivered 非空的票，在 active_workers() < max_workers
              约束下逐条投递；一条投不进去（deliver 返回 False）就跳过该票，
              继续下一票，不重试不阻塞。
    b. idle 回收 —— assignment.report 为空且 idle_at 超时的 OPEN 票：记
              report='idle_timeout' 落盘，再 launch 重开会话（回填 session_id），
              重投仍是 undelivered 的输入。
    c. 出站  —— engine.flush_outbox(now)，返回里 delivered 数计入 flushed。

写作约定：Scheduler 直接经 engine.tickets._persist(t) 落盘——这是任务契约
明示的"Engine 唯一写者豁免"，用于 idle 回收就地改 assignment 对象后的持久化。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .model import TicketStatus


@dataclass
class SchedulerConfig:
    max_workers: int = 3
    idle_timeout: float = 300.0


class Scheduler:
    def __init__(self, engine, cfg: SchedulerConfig,
                 clock: Optional[Callable[[], float]] = None,
                 pre_deliver_check: Optional[Callable[[str, str], bool]] = None):
        """pre_deliver_check(ticket_id, session_id) -> bool（AO sessionguard
        借鉴）：每次投递前重读票状态的租约检查，False=跳过该票（fail-closed，
        宁可不派发也不投给状态已变的会话）。None=不做检查（旧行为）。"""
        self.engine = engine
        self.cfg = cfg
        self.clock = clock or time.time
        self.pre_deliver_check = pre_deliver_check

    # ---------- 槽位占用口径 ----------
    def active_workers(self) -> int:
        """未终结票上"有 assignment 且该 assignment.report 为空"的数量。"""
        n = 0
        for t in self.engine.tickets._tickets.values():
            if t.status == TicketStatus.ENDED:
                continue
            for a in t.assignments:
                if not a.report:
                    n += 1
        return n

    # ---------- 一轮 ----------
    def tick(self, now: float) -> dict:
        dispatched = 0
        reclaimed = 0
        flushed = 0
        skipped_lease = 0

        # a. 派发：约束按"本轮实际派发前"的槽位计，每派发一条 active 加一
        active = self.active_workers()
        for tid, t in self.engine.tickets._tickets.items():
            if active >= self.cfg.max_workers:
                break
            if t.status != TicketStatus.OPEN:
                continue
            if not t.undelivered:
                continue
            if not t.assignments:
                continue
            a = t.assignments[-1]
            # 租约检查（AO sessionguard）：写前重读，检查不过=状态已变，跳过
            if self.pre_deliver_check is not None and \
                    not self.pre_deliver_check(tid, a.session_id):
                skipped_lease += 1
                continue
            for iid in list(t.undelivered):
                if active >= self.cfg.max_workers:
                    break
                inp = next((i for i in t.inputs if i.id == iid), None)
                if inp is None:
                    continue
                if self.engine.executor.deliver(a, inp):
                    self.engine.tickets.mark_delivered(tid, iid)
                    dispatched += 1
                    active += 1
                else:
                    # 投不进去 → 跳过该票，继续下一票，不重试不阻塞
                    break

        # b. idle 回收：report 为空 + idle_at 超时 → 记 idle_timeout + 重开会话
        for tid, t in list(self.engine.tickets._tickets.items()):
            if t.status != TicketStatus.OPEN:
                continue
            if not t.assignments:
                continue
            a = t.assignments[-1]
            if a.report:                       # 非空 = 已报告，不在 idle
                continue
            if a.idle_at is None:
                continue
            if now - a.idle_at <= self.cfg.idle_timeout:
                continue
            # idle 回收也要过租约（回开 session 前重读状态）
            if self.pre_deliver_check is not None and \
                    not self.pre_deliver_check(tid, a.session_id):
                skipped_lease += 1
                continue
            # 记 idle 超时并落盘（Engine 唯一写者豁免：直接改对象 + _persist）
            a.report = "idle_timeout"
            self.engine.tickets._persist(t)
            # S8 收口：idle 回收也是一条治理判决，reason 用 REASONS 词表内
            # 的 "idle_timeout"（此前这个词只存在于词表，判决流里零使用）。
            ruler = getattr(self.engine, "_ruling", None)
            if ruler is not None:
                ruler({"ticket_id": tid, "verdict": "IDLE_TIMEOUT",
                       "reason": "idle_timeout", "session_id": a.session_id,
                       "idle_at": a.idle_at, "now": now})
            # 重开一次派工：launch 拿新 session 回填，重投仍是 undelivered 的输入
            a.session_id = self.engine.executor.launch(a, t)
            for iid in list(t.undelivered):
                inp = next((i for i in t.inputs if i.id == iid), None)
                if inp is None:
                    continue
                if self.engine.executor.deliver(a, inp):
                    self.engine.tickets.mark_delivered(tid, iid)
            self.engine.tickets._persist(t)
            reclaimed += 1

        # c. 出站
        stat = self.engine.flush_outbox(now) or {}
        flushed = int(stat.get("delivered", stat.get("sent", 0)))

        return {"dispatched": dispatched, "reclaimed": reclaimed,
                "flushed": flushed, "skipped_lease": skipped_lease}

    # ---------- 便捷循环 ----------
    def run_until_idle(self, max_ticks: int = 100,
                       tick_interval: float = 5.0) -> list[dict]:
        """时钟步进 tick，直到某轮 dispatched==0 且 reclaimed==0，或达 max_ticks。"""
        stats: list[dict] = []
        now = self.clock()
        for _ in range(max_ticks):
            s = self.tick(now)
            stats.append(s)
            if s["dispatched"] == 0 and s["reclaimed"] == 0:
                break
            now += tick_interval
        return stats
