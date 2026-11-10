"""Ticket 仓储与订阅扇出。对齐原型 tickets.ts 语义：
- addInput 幂等（input id 去重）；
- 订阅者是 ticket id；Trigger 进来按 thread 的 subscriptions 扇出；
- ended ticket 只被人消息唤醒（wake），bot/CI 不唤醒；
- markUnrelated：worker 判无关 → 退订并交还（handback 语义在 engine 层处理）。
"""
from __future__ import annotations

import time
from typing import Optional

from .model import Assignment, Input, Mandate, Ticket, TicketStatus, Thread


class Tickets:
    def __init__(self, store):
        self.store = store
        self._threads: dict[str, Thread] = {}
        self._tickets: dict[str, Ticket] = {}
        self._seq = 0

    # ---------- 线程 ----------
    def thread(self, key: str) -> Thread:
        if key not in self._threads:
            self._threads[key] = Thread(key=key)
            self._persist_thread(self._threads[key])
        return self._threads[key]

    def tickets_on_thread(self, key: str) -> list[Ticket]:
        return [self._tickets[t] for t in self.thread(key).subscriptions if t in self._tickets]

    def open_ticket_on(self, key: str) -> Optional[str]:
        """该线程上的开放 ticket（follow-up 判定用）。"""
        for t in self.tickets_on_thread(key):
            if t.status in (TicketStatus.OPEN, TicketStatus.BLOCKED):
                return t.id
        return None

    # ---------- ticket 生命周期 ----------
    def create(self, decision_id: str, mandate: Mandate,
               channels: list[str], thread_keys: list[str]) -> Ticket:
        self._seq += 1
        t = Ticket(id=f"k{self._seq}", decision_id=decision_id, mandate=mandate,
                   channels=channels, created_at=time.time())
        self._tickets[t.id] = t
        for k in thread_keys:
            th = self.thread(k)
            th.subscriptions.append(t.id)
            self._persist_thread(th)   # 订阅追加后再落库（否则落的是空订阅）
        self._persist(t)
        return t

    def get(self, ticket_id: str) -> Optional[Ticket]:
        return self._tickets.get(ticket_id)

    def assign(self, ticket_id: str, session_id: str) -> Assignment:
        t = self._tickets[ticket_id]
        a = Assignment(ticket_id=ticket_id, session_id=session_id)
        t.assignments.append(a)
        self._persist(t)
        return a

    def add_input(self, ticket_id: str, inp: Input) -> bool:
        """幂等入队：同 id 重复输入静默跳过（原型 tickets.ts:234-245）。"""
        t = self._tickets[ticket_id]
        if any(i.id == inp.id for i in t.inputs):
            return False
        t.inputs.append(inp)
        t.undelivered.append(inp.id)
        self._persist(t)
        return True

    def mark_delivered(self, ticket_id: str, input_id: str) -> None:
        t = self._tickets[ticket_id]
        if input_id in t.undelivered:
            t.undelivered.remove(input_id)
        for i in t.inputs:
            if i.id == input_id:
                i.delivered_to = ticket_id
        self._persist(t)

    def end(self, ticket_id: str) -> None:
        t = self._tickets[ticket_id]
        t.status = TicketStatus.ENDED
        t.ended_at = time.time()
        self._persist(t)

    def wake(self, ticket_id: str, inp: Input) -> bool:
        """ended ticket 只被人消息唤醒（原型 tickets.ts:252-270）。"""
        t = self._tickets[ticket_id]
        if t.status != TicketStatus.ENDED or not inp.is_human:
            return False
        t.status = TicketStatus.OPEN
        t.ended_at = None
        return self.add_input(ticket_id, inp)

    def unsubscribe(self, ticket_id: str) -> None:
        t = self._tickets[ticket_id]
        for th in self._threads.values():
            if ticket_id in th.subscriptions:
                th.subscriptions.remove(ticket_id)
                self._persist_thread(th)
        self._persist(t)

    # ---------- 持久化（Engine 唯一写者约定） ----------
    def _persist(self, t: Ticket) -> None:
        from dataclasses import asdict
        self.store.put(f"ticket:{t.id}", asdict(t))

    def _persist_thread(self, th: Thread) -> None:
        from dataclasses import asdict
        self.store.put(f"thread:{th.key}", asdict(th))

    # ---------- 崩溃回灌（outbox 之外的另一半恢复） ----------
    def load_all(self) -> int:
        """从 store 重载全部 threads + tickets。

        返回恢复的票数。幂等：重复调用只覆盖同名条目，无副作用。
        Thread 必须先于 ticket 回灌吗？不需要——订阅在 Thread 里，票自洽；
        但 thread 里订阅引用的票必须能 get 到，故两个循环都先跑完再对外。
        """
        for k in self.store.keys("thread:"):
            d = self.store.get(k) or {}
            th = Thread(key=d.get("key", k[len("thread:"):]),
                        ref=d.get("ref", ""),
                        subscriptions=list(d.get("subscriptions", [])),
                        correlated=list(d.get("correlated", [])))
            self._threads[th.key] = th

        n = 0
        for k in self.store.keys("ticket:"):
            d = self.store.get(k)
            if not isinstance(d, dict) or "id" not in d:
                continue
            t = self._ticket_from_dict(d)
            self._tickets[t.id] = t
            n += 1

        # 序号恢复：新票 id 不得与旧票撞号
        for tid in self._tickets:
            if tid.startswith("k") and tid[1:].isdigit():
                self._seq = max(self._seq, int(tid[1:]))
        return n

    @staticmethod
    def _ticket_from_dict(d: dict) -> Ticket:
        """把 json round-trip 后的 dict 重建为强类型 Ticket（Enum/嵌套 dataclass 复原）。"""
        md = d.get("mandate") or {}
        mandate = Mandate(task_text=md.get("task_text", ""),
                          rules=dict(md.get("rules") or {}),
                          version=int(md.get("version", 1)))
        inputs = [Input(**{k: v for k, v in i.items()
                           if k in Input.__dataclass_fields__})
                  for i in (d.get("inputs") or [])]
        assignments = [Assignment(**{k: v for k, v in a.items()
                                     if k in Assignment.__dataclass_fields__})
                       for a in (d.get("assignments") or [])]
        return Ticket(
            id=d["id"],
            decision_id=d.get("decision_id", ""),
            mandate=mandate,
            channels=list(d.get("channels") or []),
            inputs=inputs,
            undelivered=list(d.get("undelivered") or []),
            follows=list(d.get("follows") or []),
            status=TicketStatus(d.get("status", "open")),
            assignments=assignments,
            attention=d.get("attention"),
            created_at=float(d.get("created_at", 0.0)),
            ended_at=d.get("ended_at"))
