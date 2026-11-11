"""D2：复审连续失败熔断上限 3（RED→GREEN，AGENTS.md 第 21/27 条）。

对齐 agent-orchestrator autoreview 的 autoReviewFailedRetryLimit=3。
RED（先红）：max_repairs=3 连发 4 份 deviation，第 4 次票虽被 end，
            当前落的是 deviation_repair_limit；断言 reason 逐字等于
            "failed_same_sha_retry_limit" 必须先失败。
RED（API）：max_review_failures 构造参数实现前 → TypeError。
GREEN（后绿）：OK 报告清零连续计数、注入上限=1 时第 2 次即熔断、
              连续失败计数落 kv "review_fail:{tid}" 且重启可恢复。
零网络（watchdog 桩）、纯 stdlib、每测试独立 tmp_path/state.db。
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.admission import AdmissionConfig  # noqa: E402
from agent.engine import REASONS, Engine  # noqa: E402
from agent.executor import EchoExecutor  # noqa: E402
from agent.model import Input, TicketStatus  # noqa: E402
from agent.store import Store  # noqa: E402

REASON = "failed_same_sha_retry_limit"


class _Wd:
    """可切 verdict 的 watchdog 桩（零网络、零真实复核）。"""

    def __init__(self, value="deviation"):
        self.value = value

    def review(self, mandate, payload):
        return SimpleNamespace(
            verdict=SimpleNamespace(value=self.value),
            reasons=["越界写"])


def _mk(tmp_path, max_repairs=3, wd=None, **kw):
    store = Store(str(tmp_path / "state.db"))
    eng = Engine(store, AdmissionConfig(whitelist=["t:1"]),
                 EchoExecutor(on_report=None),
                 deliver=lambda ch, b: True,
                 watchdog=wd or _Wd(), max_repairs=max_repairs, **kw)
    return eng, store


def _open(eng):
    r = eng.intake(Input(id="i1", thread_key="t:1", text="/do task", is_human=True))
    assert r["disposition"] == "accept"
    return r["ticket_id"]


def _report(eng, tid, n):
    a = eng.tickets.get(tid).assignments[-1]
    return eng.on_worker_report(a, {
        "status": "done", "summary": f"report-{n}", "touched_paths": []})


def test_d2_red_fourth_consecutive_failure_fuses(tmp_path):
    eng, store = _mk(tmp_path, max_repairs=3)
    tid = _open(eng)
    r1 = _report(eng, tid, 1)
    r2 = _report(eng, tid, 2)
    r3 = _report(eng, tid, 3)
    assert r1.get("repaired") is True, r1
    assert r2.get("repaired") is True, r2
    assert r3.get("repaired") is True, r3
    r4 = _report(eng, tid, 4)
    assert r4.get("repaired") is not True, r4
    assert r4.get("replied") is not True, r4
    assert eng.tickets.get(tid).status == TicketStatus.ENDED
    rulings = eng.governance_rulings()
    assert any(x.get("reason") == REASON for x in rulings), rulings
    assert REASON in REASONS
    rec = store.get(f"review_fail:{tid}") or {}
    assert rec.get("count") == 4, rec


def test_d2_green_ok_report_resets_streak(tmp_path):
    wd = _Wd("deviation")
    eng, store = _mk(tmp_path, max_repairs=3, wd=wd)
    tid = _open(eng)
    assert _report(eng, tid, 1).get("repaired") is True
    assert _report(eng, tid, 2).get("repaired") is True
    assert (store.get(f"review_fail:{tid}") or {}).get("count") == 2
    wd.value = "ok"
    ok = _report(eng, tid, 3)
    assert ok["replied"] is True
    assert (store.get(f"review_fail:{tid}") or {}).get("count") == 0
    wd.value = "deviation"
    r = _report(eng, tid, 4)
    assert r.get("repaired") is True, r     # 连续计数从 1 重来，未熔断
    assert not any(x.get("reason") == REASON for x in eng.governance_rulings())


def test_d2_green_injected_limit_one_fuses_on_second_failure(tmp_path):
    eng, store = _mk(tmp_path, max_repairs=3, max_review_failures=1)
    tid = _open(eng)
    assert _report(eng, tid, 1).get("repaired") is True
    r = _report(eng, tid, 2)
    assert r.get("repaired") is not True
    assert eng.tickets.get(tid).status == TicketStatus.ENDED
    assert any(x.get("reason") == REASON for x in eng.governance_rulings())


def test_d2_green_failure_count_survives_restart(tmp_path):
    eng, store = _mk(tmp_path, max_repairs=3)
    tid = _open(eng)
    _report(eng, tid, 1)
    _report(eng, tid, 2)
    assert (store.get(f"review_fail:{tid}") or {}).get("count") == 2

    eng2, _ = _mk(tmp_path, max_repairs=3)
    assert eng2.tickets.load_all() >= 1
    a = eng2.tickets.get(tid).assignments[-1]
    r3 = eng2.on_worker_report(
        a, {"status": "done", "summary": "r3", "touched_paths": []})
    assert r3.get("repaired") is True
    assert (store.get(f"review_fail:{tid}") or {}).get("count") == 3
    r4 = eng2.on_worker_report(
        a, {"status": "done", "summary": "r4", "touched_paths": []})
    assert r4.get("repaired") is not True
    assert eng2.tickets.get(tid).status == TicketStatus.ENDED
    assert any(x.get("reason") == REASON for x in eng2.governance_rulings())
