"""v0.3.0 六条升级验收（先 RED 后 GREEN）。

六条升级（源自 superagent-dig/06-ecosystem-learning/NOTES.md）：
1. escalate 档（AGT）：request_escalation 登记 + check 带 content_hash 消费
   一次性放行；错哈希不消费。
2. 预算进治理（AGT budgets.rego）：intake(budget_tokens=...) 落 kv；
   on_worker_report 累计超限 → ruling reason=budget_tokens_exceeded + end 票。
3. 派发租约（AO sessionguard）：Scheduler(pre_deliver_check=...) 写前重读；
   check 不过 → 跳过该票不投递。
4. 信用门（APx）：LabelStore.stats() 增 ineligible；run_calibration 在
   speak_bad>0 时强制 calibrated=False + note 含 "ineligible"。
5. reason 词表（AO autoreview）：agent.engine.REASONS 常量；engine 产的
   ruling 一律带 reason∈词表。
6. symlink 加固（AF）：governance.check realpath 归一（work/../secret →
   BLOCK）；_default_host_exec O_NOFOLLOW 拒 symlink 叶子。

全部纯 stdlib / 无网络 / 无 sleep。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.admission import AdmissionConfig                 # noqa: E402
from agent.calibration import LabelStore                    # noqa: E402
from agent.engine import Engine                             # noqa: E402
from agent.executing_executor import (                      # noqa: E402
    ExecutingToolLoopExecutor, _default_host_exec)
from agent.governance import ScopeRegistry                  # noqa: E402
from agent.metrics import CallRecord, MetricsLedger         # noqa: E402
from agent.model import Assignment, Input, Mandate, Ticket   # noqa: E402
from agent.scheduler import Scheduler, SchedulerConfig      # noqa: E402
from agent.store import Store                               # noqa: E402
from agent.tickets import Tickets                           # noqa: E402


# ---------- 公共夹具 ----------

class _EchoExec:
    """可控 echo 执行器：deliver 记录调用；带 metrics 记账。"""

    def __init__(self, on_deliver=None):
        self.metrics = MetricsLedger()
        self.delivered: list[str] = []
        self._on_deliver = on_deliver

    def launch(self, assignment, ticket):
        return f"s-{assignment.ticket_id}"

    def deliver(self, assignment, inp):
        self.delivered.append(f"{assignment.ticket_id}:{inp.id}")
        self.metrics.record(CallRecord(
            session_id=assignment.session_id, model="fake",
            prompt_tokens=60, completion_tokens=40, latency_ms=1.0,
            ok=True, kind="worker", ticket_id=assignment.ticket_id))
        if self._on_deliver is not None:
            self._on_deliver(assignment, inp)
        return True

    def resume(self, assignment):
        return True


def _mk_engine(tmpdir, executor, governance=None, max_repairs=1):
    store = Store(os.path.join(tmpdir, "e.db"))
    eng = Engine(store, AdmissionConfig(whitelist=["t1", "t2"]),
                 executor=executor,
                 deliver=lambda ch, body: True,
                 governance=governance, max_repairs=max_repairs)
    return eng, store


def _inp(iid, text, thread="t1"):
    return Input(id=iid, thread_key=thread, text=text, is_human=True)


def _report(assignment, payload):
    """模拟 worker 交报告（Engine.on_worker_report 直接驱动）。"""
    return payload


# ---------- 1. escalate 档 ----------

def test_1_escalate_wrong_hash_stays_blocked():
    reg = ScopeRegistry()
    reg.claim("T1", ["src/**"])
    h = "a" * 64
    # 放行登记针对**越界路径**（escalate 的本义：解封一条 BLOCK）
    reg.request_escalation("T1", "outside/secret.md", h, approver="YG")
    # 错哈希 → 仍 BLOCK，lift 不被消费
    r = reg.check("T1", "outside/secret.md", content_hash="b" * 64)
    assert r.verdict == "BLOCK"
    # 正哈希 → ALLOW 一次
    r2 = reg.check("T1", "outside/secret.md", content_hash=h)
    assert r2.verdict == "ALLOW"
    # 消费后第二次 → BLOCK（一次性）
    r3 = reg.check("T1", "outside/secret.md", content_hash=h)
    assert r3.verdict == "BLOCK"


def test_1_escalate_ruling_trail():
    reg = ScopeRegistry()
    reg.claim("T1", ["src/**"])
    h = "c" * 64
    reg.request_escalation("T1", "outside/x.md", h, approver="YG")
    reg.check("T1", "outside/x.md", content_hash=h)
    reasons = [r.get("reason") for r in reg.read_rulings()]
    assert "escalation_requested" in reasons
    assert "escalation_lifted" in reasons


# ---------- 2. 预算进治理 ----------

def test_2_budget_exceeded_ends_ticket(tmp_path):
    ex = _EchoExec()
    eng, store = _mk_engine(str(tmp_path), ex)
    tid = eng.intake(_inp("i1", "/do task"), mandate_rules=None,
                     budget_tokens=50)["ticket_id"]
    # 一次 deliver 记 100 tokens > 50 预算
    res = eng.on_worker_report(
        eng.tickets.get(tid).assignments[-1],
        {"summary": "done", "touched_paths": []})
    assert res["verdict"] == "budget_exceeded"
    assert res["replied"] is False
    reasons = [r.get("reason") for r in eng.governance_rulings()]
    assert "budget_tokens_exceeded" in reasons
    assert eng.tickets.get(tid).status.value.lower() == "ended"


def test_2_budget_under_keeps_reply(tmp_path):
    ex = _EchoExec()
    eng, _ = _mk_engine(str(tmp_path), ex)
    tid = eng.intake(_inp("i1", "/do task"), budget_tokens=10000)["ticket_id"]
    res = eng.on_worker_report(
        eng.tickets.get(tid).assignments[-1],
        {"summary": "done", "touched_paths": []})
    assert res["replied"] is True


# ---------- 3. 派发租约 ----------

def _undelivered_ticket(eng):
    """建票后再追加一条 follow_up（open 时已投递 i1，i2 留在 undelivered）。"""
    tid = eng.intake(_inp("i1", "/do task"))["ticket_id"]
    eng.intake(_inp("i2", "补充一句"), )  # 同线程 → follow_up，不投递
    return tid


def test_3_scheduler_pre_deliver_check_skips(tmp_path):
    ex = _EchoExec()
    eng, _ = _mk_engine(str(tmp_path), ex)
    _undelivered_ticket(eng)
    # 租约检查永远拒绝 → tick 阶段零派发（i1 在 intake 时已投递，与调度无关）
    sched = Scheduler(eng, SchedulerConfig(max_workers=3),
                      clock=lambda: 0.0,
                      pre_deliver_check=lambda tid, sid: False)
    n_before = len(ex.delivered)
    stat = sched.tick(0.0)
    assert stat["dispatched"] == 0
    assert stat.get("skipped_lease", 0) >= 1
    assert len(ex.delivered) == n_before


def test_3_scheduler_lease_pass_dispatches(tmp_path):
    ex = _EchoExec()
    eng, _ = _mk_engine(str(tmp_path), ex)
    _undelivered_ticket(eng)
    sched = Scheduler(eng, SchedulerConfig(max_workers=3),
                      clock=lambda: 0.0, pre_deliver_check=None)
    stat = sched.tick(0.0)
    assert stat["dispatched"] >= 1


# ---------- 4. 信用门 ----------

def test_4_credit_gate_ineligible():
    st = LabelStore()  # 内存态
    base = {"event_id": "e1", "utility": 10.0, "confidence": 0.9,
            "created_at": 100.0}
    st.append(dict(base), "speak_ok")
    st.append(dict(base, event_id="e2"), "speak_bad")
    s = st.stats()
    assert s.get("ineligible") is True
    from agent.calibration_loop import run_calibration
    res = run_calibration(st)
    assert res.get("calibrated") is False
    assert "ineligible" in str(res.get("note", ""))


def test_4_credit_gate_clean_data_eligible():
    st = LabelStore()
    base = {"event_id": "e1", "utility": 10.0, "confidence": 0.9,
            "created_at": 100.0}
    st.append(dict(base), "speak_ok")
    s = st.stats()
    assert s.get("ineligible") is False


# ---------- 5. reason 词表 ----------

def test_5_reason_vocabulary_exists():
    import agent.engine as em
    vocab = em.REASONS
    assert isinstance(vocab, (dict, frozenset, set))
    for key in ("deviation_repair_limit", "budget_tokens_exceeded",
                "governance_path_block"):
        assert key in vocab


def test_5_deviation_limit_ruling_has_vocabulary_reason(tmp_path):
    ex = _EchoExec()
    eng, _ = _mk_engine(str(tmp_path), ex, max_repairs=0)
    tid = eng.intake(_inp("i1", "/do task"))["ticket_id"]

    class _RV:
        value = "deviation"

    class _Rev:
        verdict = _RV()
        reasons = ["越界写"]

    class _Wd:
        @staticmethod
        def review(mandate, payload):
            return _Rev()

    eng.watchdog = _Wd()
    eng.on_worker_report(eng.tickets.get(tid).assignments[-1],
                         {"summary": "bad", "touched_paths": []})
    rulings = [r for r in eng.governance_rulings()
               if r.get("verdict") == "DEVIATION"]
    assert rulings and rulings[-1].get("reason") == "deviation_repair_limit"


# ---------- 6. symlink 加固 ----------

def test_6_governance_normpath_traversal_blocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)          # 相对路径语义下测
    (tmp_path / "work").mkdir()
    reg = ScopeRegistry()
    reg.claim("T1", ["work/**"])
    # 字面路径前缀命中 work/**，但 normpath 归一后逃逸出 work/
    assert reg.check("T1", "work/../secret").verdict == "BLOCK"
    assert reg.check("T1", "work/sub/../../secret").verdict == "BLOCK"
    # 正常路径仍放行
    assert reg.check("T1", "work/a.md").verdict == "ALLOW"


def test_6_host_exec_nofollow_rejects_symlink_leaf(tmp_path):
    target = tmp_path / "outside.txt"
    target.write_text("orig")
    link = tmp_path / "leaf"
    os.symlink(str(target), str(link))
    # O_NOFOLLOW：叶子是 symlink → 拒绝，返回 False 且不写入
    ok = _default_host_exec("write", str(link), "evil")
    assert ok is False
    assert target.read_text() == "orig"


def test_6_host_exec_normal_write_still_works(tmp_path):
    p = tmp_path / "plain.txt"
    assert _default_host_exec("write", str(p), "hello") is True
    assert p.read_text() == "hello"
    assert _default_host_exec("append", str(p), "!") is True
    assert p.read_text() == "hello!"
