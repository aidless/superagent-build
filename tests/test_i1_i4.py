"""I1 状态回灌 + I4 复审环 的验收测试（先 RED 后 GREEN）。

I1 门：建票 → 关库 → 重开 → resume() 后票/线程全部回来，
       同线程 follow_up 仍附加原票；新票 id 不与旧票撞号。
I4 门：deviation 第一次给修复机会（票不终止、修复指令重投 worker、复核转绿）；
       修复次数用尽才终止票 + 落 governance ruling。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.admission import AdmissionConfig          # noqa: E402
from agent.engine import Engine                       # noqa: E402
from agent.executor import EchoExecutor               # noqa: E402
from agent.governance import ScopeRegistry            # noqa: E402
from agent.model import Input, TicketStatus           # noqa: E402
from agent.store import Store                         # noqa: E402
from agent.watchdog import ScriptedJudge, Watchdog, WatchdogVerdict  # noqa: E402


# ---------- I1：状态回灌 ----------

def _build_first_phase(path: str):
    """阶段一：建票、留状态、关库。返回 (ticket_id, ended_ticket_id, thread_key)。"""
    store = Store(path)
    ex = EchoExecutor(on_report=None)   # 不回调，只测建票与持久化
    eng = Engine(store, AdmissionConfig(whitelist=["issue:1", "issue:2", "issue:3"]),
                 ex, deliver=lambda ch, b: True)

    r1 = eng.intake(Input(id="in1", thread_key="issue:1", text="/do 补测试",
                          is_human=True),
                    mandate_rules={"allow_paths": ["src/**"]})
    assert r1["disposition"] == "accept", r1
    tid_open = r1["ticket_id"]

    r2 = eng.intake(Input(id="in2", thread_key="issue:2", text="/do 修文档",
                          is_human=True))
    assert r2["disposition"] == "accept", r2
    tid_end = r2["ticket_id"]
    eng.tickets.end(tid_end)

    # 持久化应该已发生（先验证落盘，回灌才有意义）
    assert store.get(f"ticket:{tid_open}") is not None
    assert store.get(f"ticket:{tid_end}") is not None
    assert store.get(f"thread:issue:1") is not None, "thread 必须持久化，否则回灌没有源头"

    store.conn.close()
    return tid_open, tid_end


def test_i1_hydration_follow_up_survives_restart():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "s.db")
        tid_open, tid_end = _build_first_phase(path)

        # 阶段二：重开库，全新 Engine 实例
        store = Store(path)
        eng = Engine(store, AdmissionConfig(whitelist=["issue:1", "issue:3"]),
                     EchoExecutor(on_report=None), deliver=lambda ch, b: True)
        eng.resume()   # 回灌

        # 票回来了，状态保真
        t_open = eng.tickets.get(tid_open)
        assert t_open is not None, "open ticket 没有从 store 回灌"
        assert t_open.status == TicketStatus.OPEN
        t_end = eng.tickets.get(tid_end)
        assert t_end is not None, "ended ticket 没有从 store 回灌"
        assert t_end.status == TicketStatus.ENDED

        # 订阅保真：线程上还能找到原票
        subs = eng.tickets.tickets_on_thread("issue:1")
        assert any(t.id == tid_open for t in subs), "thread 订阅没有回灌"

        # 关键语义：同线程 follow_up 仍附加原票（修复前这里必红）
        r = eng.intake(Input(id="fu1", thread_key="issue:1", text="再补一个边界",
                             is_human=True))
        assert r["disposition"] == "follow_up", r
        assert r["ticket_id"] == tid_open, r
        assert any(i.id == "fu1" for i in t_open.inputs), "follow_up 没进原票"

        # 序号恢复：新票不撞旧 id
        r3 = eng.intake(Input(id="in3", thread_key="issue:3", text="/do 新任务",
                              is_human=True))
        assert r3["disposition"] == "accept"
        assert r3["ticket_id"] not in (tid_open, tid_end)


# ---------- I4：复审环 ----------

def _mk_engine(max_repairs: int):
    store = Store(":memory:")
    gov = ScopeRegistry(state_dir=None)   # 内存模式
    wd = Watchdog(judge=ScriptedJudge({
        "drift": (WatchdogVerdict.DEVIATION, ["实现与任务目标不一致"]),
    }))
    eng = Engine(store, AdmissionConfig(whitelist=["t:1"]), EchoExecutor(on_report=None),
                 deliver=lambda ch, b: True, watchdog=wd, governance=gov,
                 max_repairs=max_repairs)
    # EchoExecutor 的报告直接回灌 engine（闭环）
    eng.executor.on_report = lambda a, p: eng.on_worker_report(a, p)
    r = eng.intake(Input(id="i4-1", thread_key="t:1", text="/do 改代码",
                         is_human=True))
    assert r["disposition"] == "accept"
    return eng, r["ticket_id"]


def test_i4_first_deviation_gets_repair_round():
    eng, tid = _mk_engine(max_repairs=1)

    res1 = eng.on_worker_report(eng.tickets.get(tid).assignments[-1],
                                {"status": "done", "summary": "改了",
                                 "touched_paths": [], "scenario": "drift"})
    # 第一次跑偏：给修复机会，不终止票
    assert res1.get("repaired") is True, res1
    assert res1.get("repair_round") == 1, res1
    assert eng.tickets.get(tid).status == TicketStatus.OPEN

    # 修复指令已重投 worker；Echo 回的 normal 报告应复核通过并回复
    assert res1.get("reply_after_repair") is True, res1


def test_i4_second_deviation_terminates():
    eng, tid = _mk_engine(max_repairs=1)
    a = eng.tickets.get(tid).assignments[-1]

    eng.on_worker_report(a, {"status": "done", "summary": "x",
                             "touched_paths": [], "scenario": "drift"})
    # 修复轮之后再跑偏：次数用尽 → 终止 + ruling
    res2 = eng.on_worker_report(a, {"status": "done", "summary": "还是歪的",
                                    "touched_paths": [], "scenario": "drift"})
    assert res2.get("repaired") is not True, res2
    assert res2.get("escalated") is True, res2
    assert eng.tickets.get(tid).status == TicketStatus.ENDED
    rulings = eng.governance.read_rulings()
    assert any(rr.get("verdict") == "DEVIATION" for rr in rulings), rulings


def test_i4_zero_repairs_escalates_immediately():
    eng, tid = _mk_engine(max_repairs=0)
    res = eng.on_worker_report(eng.tickets.get(tid).assignments[-1],
                               {"status": "done", "summary": "x",
                                "touched_paths": [], "scenario": "drift"})
    assert res.get("escalated") is True, res
    assert eng.tickets.get(tid).status == TicketStatus.ENDED
