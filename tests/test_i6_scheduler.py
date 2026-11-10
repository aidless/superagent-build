"""I6 调度循环验收测试（先 RED 后 GREEN）。

契约（agent/scheduler.py）：
- SchedulerConfig(max_workers=3, idle_timeout=300.0)
- Scheduler(engine, cfg, clock=None)，tick(now) 一轮做三件事：
  a. 派发（active_workers() < max_workers 约束下逐条投递，投不进跳过该票）
  b. idle 回收（report 为空 + idle_at 超时 → report='idle_timeout' + 重开 session）
  c. 出站（engine.flush_outbox(now) 的 delivered 计数）
- active_workers()：未终结票上 "有 assignment 且 report 为空" 的数量
- run_until_idle(max_ticks, tick_interval)：循环到某轮 dispatched==0 且 reclaimed==0

全部用注入 clock（lambda: 0.0），无 sleep、无线程，纯确定性。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.admission import AdmissionConfig                 # noqa: E402
from agent.engine import Engine                            # noqa: E402
from agent.executor import EchoExecutor                    # noqa: E402
from agent.model import Input, Mandate                     # noqa: E402
from agent.store import Store                              # noqa: E402
from agent.scheduler import Scheduler, SchedulerConfig     # noqa: E402  （RED：此刻模块不存在）


# ---------- 测试脚手架 ----------

def _engine(max_workers=3, idle_timeout=300.0, executor=None):
    """造一个内存 Engine + Scheduler。clock 固定 0.0，tick 的 now 由测试显式传。"""
    store = Store(":memory:")
    ex = executor or EchoExecutor(on_report=None)   # 不回调：deliver 只返回 True
    eng = Engine(store, AdmissionConfig(whitelist=["u"]), ex,
                 deliver=lambda ch, body: True)
    cfg = SchedulerConfig(max_workers=max_workers, idle_timeout=idle_timeout)
    sched = Scheduler(eng, cfg, clock=lambda: 0.0)
    return eng, sched, ex


def _mk_ticket(eng, thread, input_ids, report="ready"):
    """直接造一张 OPEN 票：一个"已完成上轮任务、可接新活"的 assignment（report 非空
    → 不占 active 槽），加上若干待投递 input（undelivered 非空）。"""
    t = eng.tickets.create(f"d-{thread}", Mandate(task_text="x"),
                           [f"thread:{thread}"], [thread])
    a = eng.tickets.assign(t.id, f"sess-init-{thread}")
    a.report = report
    for iid in input_ids:
        eng.tickets.add_input(t.id, Input(id=iid, thread_key=thread, text=f"txt:{iid}"))
    return t, a


class _FlakyExecutor(EchoExecutor):
    """对指定 input id 的 deliver 返回 False，其余走 Echo 正常投递。"""

    def __init__(self, bad_ids):
        super().__init__(on_report=None)
        self.bad_ids = set(bad_ids)

    def deliver(self, assignment, inp):
        if inp.id in self.bad_ids:
            return False
        return super().deliver(assignment, inp)


# ---------- 1. 槽位约束：max_workers=1 时一轮只派一条 ----------

def test_max_workers_slot_limit():
    eng, sched, ex = _engine(max_workers=1)
    t1, _ = _mk_ticket(eng, "th1", ["i1"])
    t2, _ = _mk_ticket(eng, "th2", ["i2"])

    s1 = sched.tick(now=0.0)
    assert s1["dispatched"] == 1, s1                 # 槽位只有 1，只派一条
    assert t1.undelivered == [] and t2.undelivered == ["i2"], (t1.undelivered, t2.undelivered)

    s2 = sched.tick(now=0.0)
    assert s2["dispatched"] == 1, s2                 # 下一轮把第二条派出去
    assert t2.undelivered == [], t2.undelivered


# ---------- 2. 幂等空转：全派完后 dispatched==0 ----------

def test_tick_idempotent_when_all_delivered():
    eng, sched, ex = _engine(max_workers=3)
    t, _ = _mk_ticket(eng, "th", ["a", "b"])

    s = sched.tick(now=0.0)
    assert s["dispatched"] == 2, s
    assert t.undelivered == []

    s2 = sched.tick(now=0.0)
    assert s2["dispatched"] == 0, s2                 # 幂等空转


# ---------- 3. idle 超时回收：report='idle_timeout' + session 重开 ----------

def test_idle_timeout_reclaims_and_relaunches():
    eng, sched, ex = _engine(idle_timeout=300.0)
    t = eng.tickets.create("d", Mandate(task_text="x"), ["thread:th"], ["th"])
    a = eng.tickets.assign(t.id, "sess-old")
    # report 留空 = 占 active 槽；手工把 idle_at 拨到很久以前
    a.idle_at = -1000.0
    assert a.report is None

    before_seq = len(ex.seq)
    s = sched.tick(now=0.0)

    assert s["reclaimed"] == 1, s
    assert a.report == "idle_timeout", a.report
    assert a.session_id != "sess-old", a.session_id          # launch 重开回填
    assert len(ex.seq) == before_seq + 1, ex.seq             # EchoExecutor 的 seq 变长
    # 落盘：report 已被 _persist 写进 store
    stored = eng.store.get(f"ticket:{t.id}")
    assert stored["assignments"][-1]["report"] == "idle_timeout", stored


# ---------- 4. deliver 返回 False 的票被跳过、不阻塞其他票 ----------

def test_deliver_false_skips_ticket_not_blocking_others():
    ex = _FlakyExecutor(bad_ids={"bad"})
    eng, sched, ex = _engine(max_workers=5, executor=ex)
    tA, _ = _mk_ticket(eng, "A", ["bad"])
    tB, _ = _mk_ticket(eng, "B", ["good"])

    s = sched.tick(now=0.0)
    assert s["dispatched"] == 1, s                   # 只有 B 投成功
    assert tA.undelivered == ["bad"], tA.undelivered  # A 仍待投，不重试
    assert tB.undelivered == [], tB.undelivered


# ---------- 5. ENDED 票不被派发 ----------

def test_ended_ticket_not_dispatched():
    eng, sched, ex = _engine(max_workers=3)
    t, _ = _mk_ticket(eng, "E", ["x"])
    eng.tickets.end(t.id)

    s = sched.tick(now=0.0)
    assert s["dispatched"] == 0, s
    assert t.undelivered == ["x"], t.undelivered


# ---------- 6. run_until_idle 在 max_ticks 内收敛 ----------

def test_run_until_idle_converges():
    eng, sched, ex = _engine(max_workers=2)
    _mk_ticket(eng, "r1", ["x", "y"])

    stats = sched.run_until_idle(max_ticks=10, tick_interval=5.0)
    assert stats, stats
    assert len(stats) <= 10, len(stats)
    assert stats[-1]["dispatched"] == 0, stats       # 最后一轮空转收敛
