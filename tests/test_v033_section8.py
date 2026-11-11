"""v0.3.3 P0-A：GOVERNANCE.md §8 七条真口子的 RED→GREEN 验收。

逐条对应 §7 SHOULD / §8 discrepancy：
- S4 / §8-2：票级预算 + executor 无 metrics → fail-closed 拒绝派工（不旁路）
- S6 / §8-7：escalate 一次性 lift 可经 J2 写路径消费（content_hash 传通）
- §8-3：Watchdog(judge=None, rules_check=False) 构造级 fail-open 必须拒
- S5 / §8-5：outbox.pump 遇其它异常退避重试，不外泄
- S8 / §8-8：idle_timeout 回收产生 reason=idle_timeout 的 ruling
- §8-4：accept_on_broadcast 分支经 decide() 可达（且失败口径正确）
- §8-6：scopes.json 写前保留上一版快照（审计可回溯）

纪律（AGENTS.md 21/27）：每条先 RED（注入违规）再 GREEN（合规放行）；
纯 stdlib + pytest；只用自己的 tmp_path；断言真实输入→输出。
"""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.admission import AdmissionConfig, decide, gate, validate_assessment  # noqa: E402
from agent.engine import Engine                                                # noqa: E402
from agent.executing_executor import ExecutingToolLoopExecutor                  # noqa: E402
from agent.executor import EchoExecutor                                         # noqa: E402
from agent.governance import ScopeRegistry                                      # noqa: E402
from agent.model import (Assessment, Assignment, Input, Mandate,            # noqa: E402
                         Ticket)
from agent.outbox import pump                                                   # noqa: E402
from agent.scheduler import Scheduler, SchedulerConfig                           # noqa: E402
from agent.store import Store                                                   # noqa: E402
from agent.watchdog import ScriptedJudge, Watchdog, WatchdogVerdict             # noqa: E402


def _inp(iid, text, thread="t1"):
    return Input(id=iid, thread_key=thread, text=text, is_human=True)


def _engine(db_path, executor, **kw):
    return Engine(Store(db_path), AdmissionConfig(whitelist=["t1"]),
                  executor=executor, deliver=lambda ch, body: True, **kw)


# ---------- S4 / §8-2：无计量不给预算票 ----------

def test_s4_budget_without_metrics_fails_closed(tmp_path):
    eng = _engine(str(tmp_path / "e.db"), EchoExecutor(on_report=None))
    # RED：预算闸开着但 executor 没有 metrics → 必须拒，绝不静默旁路
    with pytest.raises(ValueError, match="budget_requires_metrics"):
        eng.intake(_inp("i1", "/do task"), budget_tokens=50)
    # 拒在副作用之前：同 id 重投仍可接单（receipt 没被吞）
    r = eng.intake(_inp("i1", "/do task"))
    assert r["disposition"] == "accept"
    # GREEN：带 metrics 的执行器 + 预算正常工作（不误杀）
    class _Metered:
        def __init__(self):
            from agent.metrics import MetricsLedger
            self.metrics = MetricsLedger()

        def launch(self, a, t):
            return "s1"

        def deliver(self, a, i):
            return True

        def resume(self, a):
            return True

    eng2 = _engine(str(tmp_path / "e2.db"), _Metered())
    assert eng2.intake(_inp("i2", "/do task"), budget_tokens=50)["disposition"] == "accept"


# ---------- S6 / §8-7：escalate lift 经 J2 写路径消费 ----------

def _resp(content, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"message": msg}]}


def _tc(name, args):
    return [{"id": "c1", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]


def test_s6_escalate_lift_reachable_via_write_path(tmp_path):
    reg = ScopeRegistry(state_dir=str(tmp_path / "gov"))
    reg.claim("T1", ["docs/**"])
    content = "approved body"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    reg.request_escalation("T1", "secret/outside.md", digest, approver="yg")

    eng = _engine(str(tmp_path / "e.db"), EchoExecutor(on_report=None),
                  governance=reg)
    writes = []

    def host_exec(op, path, body):
        writes.append((op, path, body))
        return True

    responses = [
        _resp("", tool_calls=_tc("write_file", {
            "path": "secret/outside.md", "content": content})),
        _resp(json.dumps({"summary": "done", "touched_paths": []})),
    ]
    calls = []

    def transport(payload):
        calls.append(payload)
        return responses[min(len(calls) - 1, len(responses) - 1)]

    ex = ExecutingToolLoopExecutor(transport=transport, host_exec=host_exec,
                                   write_gate=eng.pre_write,
                                   ticket_of=lambda sid: "T1")
    a = Assignment(ticket_id="T1", session_id="s1")
    ex.launch(a, Ticket(id="T1", decision_id="",
                        mandate=Mandate(task_text="write")))

    # RED（旧实现）：write_gate 只收两参，lift 永远消费不到 → 越界写 BLOCK
    assert ex.deliver(a, _inp("i1", "/do write")) is True
    assert writes == [("write", "secret/outside.md", content)]
    assert any(r.get("reason") == "escalation_lifted"
               for r in reg.read_rulings())
    # 一次性：同内容第二次写必须回到 BLOCK，且零落盘
    writes.clear()
    out = ex._exec_tool(a, "write_file",
                        {"path": "secret/outside.md", "content": content})
    assert "BLOCK" in out and writes == []
    # GREEN：普通作用域内写仍照常放行（2 参老 gate 兼容）
    ex2 = ExecutingToolLoopExecutor(transport=transport, host_exec=host_exec,
                                    write_gate=lambda tid, path: True)
    a2 = Assignment(ticket_id="T2", session_id="s2")
    ex2.launch(a2, Ticket(id="T2", decision_id="",
                          mandate=Mandate(task_text="write")))
    out2 = ex2._exec_tool(a2, "write_file",
                          {"path": "docs/a.md", "content": "plain"})
    assert "OK" in out2 and ("write", "docs/a.md", "plain") in writes


# ---------- §8-3：构造级 fail-open 拒收 ----------

def test_watchdog_no_checks_constructor_rejected():
    with pytest.raises(ValueError, match="no_checks"):
        Watchdog(judge=None, rules_check=False)
    mandate = Mandate(task_text="x", rules={"allow_paths": ["docs/**"]})
    # GREEN：默认规则层独立可用；judge 单独也允许关规则层
    ok = Watchdog().review(mandate, {"status": "done", "summary": "ok",
                                     "touched_paths": ["docs/a.md"]})
    assert ok.verdict is WatchdogVerdict.OK
    jd = Watchdog(judge=ScriptedJudge({"bad": (WatchdogVerdict.DEVIATION,
                                                ["r"])}),
                  rules_check=False)
    assert jd.review(mandate, {"scenario": "bad"}).verdict is WatchdogVerdict.DEVIATION


# ---------- S5 / §8-5：其它异常退避重试 ----------

def test_s5_outbox_other_exception_retries(tmp_path):
    store = Store(str(tmp_path / "o.db"))
    store.record_receipt("r1", "body")
    store.enqueue("m1", "r1", "thread:t1", "body")

    def boom(ch, body):
        raise RuntimeError("transient")

    # RED（旧实现）：RuntimeError 外泄出 pump
    stats = pump(store, 1000.0, boom)
    assert stats["retried"] == 1
    rows = store.due(1005.0)
    assert len(rows) == 1 and rows[0]["attempts"] == 1
    # GREEN：下一轮真送达
    assert pump(store, 1005.0, lambda ch, b: True)["sent"] == 1
    assert store.due(10000.0) == []


# ---------- S8 / §8-8：idle 回收产生 ruling ----------

def test_s8_idle_timeout_emits_ruling(tmp_path):
    eng = _engine(str(tmp_path / "e.db"), EchoExecutor(on_report=None),
                  governance=ScopeRegistry())
    r = eng.intake(_inp("i1", "/do task"))
    t = eng.tickets.get(r["ticket_id"])
    t.assignments[-1].idle_at = 0.0
    sched = Scheduler(eng, SchedulerConfig(idle_timeout=1.0))
    stats = sched.tick(1000.0)
    assert stats["reclaimed"] == 1
    # RED（旧实现）：词表里有 idle_timeout，但判决流里没有
    assert any(x.get("reason") == "idle_timeout"
               for x in eng.governance_rulings())
    # GREEN：未超时的票不误报
    eng2 = _engine(str(tmp_path / "e2.db"), EchoExecutor(on_report=None),
                   governance=ScopeRegistry())
    r2 = eng2.intake(_inp("i2", "/do task"))
    t2 = eng2.tickets.get(r2["ticket_id"])
    t2.assignments[-1].idle_at = 999.0
    assert Scheduler(eng2, SchedulerConfig(idle_timeout=300.0)).tick(1000.0)["reclaimed"] == 0
    assert eng2.governance_rulings() == []


# ---------- §8-4：accept_on_broadcast 经 decide() 可达 ----------

def test_accept_on_broadcast_branch_reachable_via_decide():
    cfg = AdmissionConfig(whitelist=["t1"])
    inp = Input(id="outsider:i1", thread_key="other",
                text="please do this", is_human=True)
    assert gate(inp, cfg).route == "broadcast"
    a = Assessment(True, "whitelist_user", intent_evidence="looks intentional",
                   confidence=1.0)
    # RED（旧实现）：membership 先触发 → rejected_eval:hallucinated_rule:...
    disp, got = decide(inp, cfg, None, assessor=lambda i, g: a)
    assert disp == "rejected_eval:accept_on_broadcast" and got is a
    # GREEN：broadcast + 明确 deny 也拒（gate 对 broadcast 输出空规则集，
    # 研判层引用任何规则名都是幻觉名，fail-closed 拒）；显式白名单指令正常接
    deny = Assessment(False, "whitelist_user", intent_evidence="", confidence=1.0)
    disp_deny, _ = decide(inp, cfg, None, assessor=lambda i, g: deny)
    assert disp_deny.startswith("rejected_eval")
    assert decide(_inp("t1:i2", "/do task"), cfg)[0] == "accept"


# ---------- §8-6：scopes.json 写前留上一版快照 ----------

def test_scopes_state_keeps_previous_snapshot(tmp_path):
    d = tmp_path / "gov"
    reg = ScopeRegistry(state_dir=str(d))
    reg.claim("T1", ["docs/**"])
    reg.claim("T1", ["src/**"])
    bak = d / "scopes.json.bak"
    # RED（旧实现）：整文件截断重写，旧状态无痕
    assert bak.exists()
    old = json.loads(bak.read_text(encoding="utf-8"))
    cur = json.loads((d / "scopes.json").read_text(encoding="utf-8"))
    assert old["T1"]["scopes"] == ["docs/**"]
    assert cur["T1"]["scopes"] == ["src/**"]
    # GREEN：新实例读回的是当前态，快照不参与判定
    assert ScopeRegistry(state_dir=str(d)).check("T1", "src/a.py").verdict == "ALLOW"
