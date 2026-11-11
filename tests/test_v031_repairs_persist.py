"""v0.3.1 修复：复审环 _repairs 计数只存内存 dict——崩溃恢复后修复次数清零，
max_repairs 形同虚设（Linus 过堂抓出的真 bug）。
RED：恢复后第二份 deviation 报告必须走 repair-limit 终止，而不是再修一轮。
GREEN：store kv "repairs:{tid}" 是唯一事实源，恢复语义正确。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.admission import AdmissionConfig  # noqa: E402
from agent.engine import Engine  # noqa: E402
from agent.executor import EchoExecutor  # noqa: E402
from agent.model import Input  # noqa: E402
from agent.store import Store  # noqa: E402
from agent.watchdog import Watchdog  # noqa: E402


class RepoExecutor(EchoExecutor):
    """占位执行器：报告由测试手动注入，deliver 不触发报告闭环。"""


def _engine(store):
    return Engine(store, AdmissionConfig(whitelist=["alice"]), RepoExecutor(),
                  lambda ch, body: True,
                  watchdog=Watchdog(), max_repairs=1)


def _open(eng):
    tid = eng.intake(Input("alice:t1", "t1", "/do task", is_human=True),
                     mandate_rules={"allow_paths": ["src/**"]})["ticket_id"]
    return tid


def _rogue(eng, tid):
    t = eng.tickets.get(tid)
    return eng.on_worker_report(t.assignments[-1], {
        "status": "done", "summary": "x",
        "touched_paths": ["/etc/passwd"], "scenario": "rogue"})


def test_repair_count_survives_restart(tmp_path):
    store = Store(os.path.join(str(tmp_path), "e.db"))
    eng = _engine(store)
    tid = _open(eng)
    r1 = _rogue(eng, tid)
    assert r1.get("repaired") is True and r1.get("repair_round") == 1

    # 崩溃重启：同 store 的新 Engine，票从 SQLite 回灌
    eng2 = _engine(store)
    eng2.resume()
    assert eng2.tickets.get(tid) is not None
    r2 = _rogue(eng2, tid)
    # 修复次数已用完（第 1 轮在崩溃前），必须 repair-limit 终止
    assert r2.get("repaired") is not True
    assert r2.get("escalated") is True


def test_repair_count_persisted_in_kv(tmp_path):
    store = Store(os.path.join(str(tmp_path), "e.db"))
    eng = _engine(store)
    tid = _open(eng)
    _rogue(eng, tid)
    v = store.get(f"repairs:{tid}")
    assert v is not None and int(v.get("count", 0)) == 1
