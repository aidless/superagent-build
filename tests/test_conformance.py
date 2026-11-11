"""GOVERNANCE.md 一致性测试：M1..M22 每条 MUST 一个测试函数
（另有 test_m21_review_fail_streak_fuses 为 M21 的补充交叉验证，共 23 个测试函数）。

纪律（AGENTS.md 第 21/27 条）：
- 每个测试内先 RED（注入明确违规，断言被拒/被拦/不落盘），再 GREEN（合规放行）；
- 断言真实输入→输出，不用 mock 替换被测行为；注入的 host_exec / deliver /
  transport 只是被测模块自己声明的可注入边界；
- 每个测试只用自己的 tmp_path，测试之间不共享临时文件；
- 纯 stdlib + pytest；数字/路径/标识符逐字复制自 docs/spec/GOVERNANCE.md 与实现。
"""
import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.admission import AdmissionConfig, decide                  # noqa: E402
from agent.calibration import LabelStore                             # noqa: E402
from agent.calibration_loop import apply_calibration, run_calibration  # noqa: E402
from agent.engine import Engine                                      # noqa: E402
from agent.executing_executor import (                               # noqa: E402
    ExecutingToolLoopExecutor, _default_host_exec)
from agent.governance import ScopeRegistry, _matches                 # noqa: E402
from agent.metrics import CallRecord, MetricsLedger                  # noqa: E402
from agent.model import Assessment, Assignment, Input, Mandate       # noqa: E402
from agent.outbox import (ChannelGone, backoff_next, pump)           # noqa: E402
from agent.proactivity import CandidateEvent, ProactivityConfig, ProactivityEngine  # noqa: E402
from agent.scheduler import Scheduler, SchedulerConfig               # noqa: E402
from agent.store import Store                                        # noqa: E402
from agent.watchdog import LLMJudge, Watchdog, WatchdogVerdict       # noqa: E402


# ---------- 公共夹具（真实行为，不替换被测逻辑） ----------

class _TokenExec:
    """可控执行器：每次 deliver 真实记 60 + 40 tokens（预算闸输入）。"""

    def __init__(self):
        self.metrics = MetricsLedger()
        self.delivered: list[str] = []

    def launch(self, assignment, ticket):
        return "s-" + assignment.ticket_id

    def deliver(self, assignment, inp):
        self.delivered.append(f"{assignment.ticket_id}:{inp.id}")
        self.metrics.record(CallRecord(
            session_id=assignment.session_id, model="fake",
            prompt_tokens=60, completion_tokens=40, latency_ms=1.0,
            ok=True, kind="worker", ticket_id=assignment.ticket_id))
        return True

    def resume(self, assignment):
        return True


class _EchoExec:
    """调度器用：只记录真实派发，不做报告回调。"""

    def __init__(self):
        self.delivered: list[str] = []

    def launch(self, assignment, ticket):
        return "s-" + assignment.ticket_id

    def deliver(self, assignment, inp):
        self.delivered.append(f"{assignment.ticket_id}:{inp.id}")
        return True

    def resume(self, assignment):
        return True


def _engine(db_path, executor, **kw):
    return Engine(Store(db_path), AdmissionConfig(whitelist=["t1"]),
                  executor=executor, deliver=lambda ch, body: True, **kw)


def _inp(iid, text, thread="t1"):
    return Input(id=iid, thread_key=thread, text=text, is_human=True)


def _event(eid, label, conf=0.9, utility=10.0, action="observe_speak"):
    return {"event_id": eid, "action": action, "confidence": conf,
            "utility": utility, "created_at": 1000.0,
            "watchdog_verdict": "OK", "label": label}


# ---------- M1 ----------

def test_m1_undeclared_scope_write_blocked():
    reg = ScopeRegistry()
    # RED：没有任何活动作用域声明 → 不许写
    r = reg.check("k1", "src/a.py")
    assert r.verdict == "BLOCK"
    assert r.reason == "ticket k1 无活动作用域声明——没有声明就不许写"
    # GREEN：claim 后命中路径放行；release 后再次 BLOCK（声明失效）
    reg.claim("k1", ["src/**"])
    assert reg.check("k1", "src/a.py").verdict == "ALLOW"
    reg.release("k1")
    assert reg.check("k1", "src/a.py").verdict == "BLOCK"


# ---------- M2 ----------

def test_m2_scope_pattern_boundaries():
    reg = ScopeRegistry()
    reg.claim("k1", ["docs"])
    # RED：路径字符串前缀相近但不是目录子路径 → 不许命中
    assert reg.check("k1", "docsx/a.md").verdict == "BLOCK"
    assert _matches("docsx/a.md", "docs") is False
    # GREEN：精确/目录前缀与 /** 前缀树按代码语义放行
    assert reg.check("k1", "docs").verdict == "ALLOW"
    assert reg.check("k1", "docs/a.md").verdict == "ALLOW"
    reg.claim("k2", ["src/**"])
    assert reg.check("k2", "src/pkg/deep/x.py").verdict == "ALLOW"
    assert _matches("srcx.py", "src/**") is False


# ---------- M3 ----------

def test_m3_lexical_traversal_normalized():
    reg = ScopeRegistry()
    reg.claim("k1", ["work/**"])
    # RED：文本穿越 work/../secret 不能靠 work/** 拿到 ALLOW
    assert reg.check("k1", "work/../secret").verdict == "BLOCK"
    assert _matches("work/../secret", "work/**") is False
    # GREEN：白的真实子路径放行
    assert reg.check("k1", "work/pkg/a.md").verdict == "ALLOW"


# ---------- M4 ----------

def test_m4_ruling_reason_vocabulary_closed(tmp_path):
    eng = _engine(str(tmp_path / "e.db"), _TokenExec())
    # RED：词表外 reason / 缺失 reason（setdefault 成 "unspecified"）都必须拒
    with pytest.raises(ValueError, match="不在词表内"):
        eng._ruling({"ticket_id": "k1", "verdict": "BLOCK",
                     "reason": "made_up"})
    with pytest.raises(ValueError, match="不在词表内"):
        eng._ruling({"ticket_id": "k1", "verdict": "BLOCK"})
    assert eng.governance_rulings() == []
    # GREEN：词表内 reason 落痕且可读回
    eng._ruling({"ticket_id": "k1", "verdict": "BLOCK",
                 "reason": "idle_timeout"})
    assert [r["reason"] for r in eng.governance_rulings()] == ["idle_timeout"]


# ---------- M5 ----------

def test_m5_pre_write_block_rules_and_denies(tmp_path):
    reg = ScopeRegistry()
    eng = _engine(str(tmp_path / "e.db"), _TokenExec(), governance=reg)
    tid = eng.intake(_inp("i1", "/do write"))["ticket_id"]
    reg.claim(tid, ["allowed/**"])
    # RED：越界写必须 False + 落 BLOCK/governance_path_block 判决
    assert eng.pre_write(tid, "secret/x.md") is False
    rulings = eng.governance_rulings()
    assert any(r.get("verdict") == "BLOCK"
               and r.get("reason") == "governance_path_block"
               and r.get("path") == "secret/x.md" for r in rulings)
    # GREEN：作用域内写放行，不留 BLOCK 判决
    assert eng.pre_write(tid, "allowed/x.md") is True
    assert all(r.get("path") != "allowed/x.md"
               for r in eng.governance_rulings())


# ---------- M6 ----------

def test_m6_escalation_lift_one_shot_hash_bound():
    reg = ScopeRegistry()
    # RED：无活动作用域的 ticket 不能登记放行
    with pytest.raises(ValueError, match="无活动作用域，无法登记放行"):
        reg.request_escalation("NOPE", "outside/a.md", "a" * 64, approver="YG")
    reg.claim("k1", ["src/**"])
    h = "a" * 64
    lift = reg.request_escalation("k1", "outside/a.md", h, approver="YG")
    assert lift["consumed"] is False
    # RED：无哈希 / 错哈希 / 错路径都不许消费 lift
    assert reg.check("k1", "outside/a.md").verdict == "BLOCK"
    assert reg.check("k1", "outside/a.md", content_hash="b" * 64).verdict == "BLOCK"
    assert reg.check("k1", "outside/b.md", content_hash=h).verdict == "BLOCK"
    # GREEN：精确 (path, content_hash) 命中 → ALLOW 一次；第二次同参 BLOCK
    assert reg.check("k1", "outside/a.md", content_hash=h).verdict == "ALLOW"
    assert reg.check("k1", "outside/a.md", content_hash=h).verdict == "BLOCK"
    reasons = [r.get("reason") for r in reg.read_rulings()]
    assert "escalation_requested" in reasons
    assert "escalation_lifted" in reasons


# ---------- M7 ----------

def test_m7_rulings_stream_append_only(tmp_path):
    bad = tmp_path / "bad"
    reg_bad = ScopeRegistry(state_dir=str(bad))
    reg_bad.claim("k1", ["src/**"])
    reg_bad.ruling({"ticket_id": "k1", "reason": "escalation_requested"})
    # RED：坏行不许被静默吞掉
    with (bad / "rulings.jsonl").open("a", encoding="utf-8") as f:
        f.write("{not-json}\n")
    with pytest.raises(json.JSONDecodeError):
        ScopeRegistry(state_dir=str(bad)).read_rulings()

    good = tmp_path / "good"
    reg = ScopeRegistry(state_dir=str(good))
    reg.claim("k1", ["src/**"])
    reg.ruling({"ticket_id": "k1", "reason": "escalation_requested"})
    reg.ruling({"ticket_id": "k1", "reason": "escalation_lifted"})
    before = (good / "rulings.jsonl").read_bytes()
    # GREEN：跨实例追加，旧行字节级原样保留，行数增长
    reg2 = ScopeRegistry(state_dir=str(good))
    reg2.ruling({"ticket_id": "k2", "reason": "governance_path_block"})
    after = (good / "rulings.jsonl").read_bytes()
    assert after.startswith(before)
    assert len(after.splitlines()) == len(before.splitlines()) + 1
    assert [r["reason"] for r in reg2.read_rulings()] == [
        "escalation_requested", "escalation_lifted", "governance_path_block"]


# ---------- M8 ----------

def test_m8_enqueue_requires_receipt(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    # RED：receipt 未落就 enqueue → 拒绝且不落 outbox 行
    with pytest.raises(ValueError, match="enqueue before receipt: m1 needs receipt r1"):
        store.enqueue("m1", "r1", "thread:t1", "hello")
    assert store.due(0.0) == []
    # GREEN：先存 receipt 再入队；同 message_id 重复入队幂等跳过
    assert store.record_receipt("r1", "hello") is True
    assert store.enqueue("m1", "r1", "thread:t1", "hello") is True
    rows = store.due(0.0)
    assert len(rows) == 1 and rows[0]["message_id"] == "m1"
    assert store.enqueue("m1", "r1", "thread:t1", "hello") is False


# ---------- M9 ----------

def test_m9_abandon_reason_closed_set(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.record_receipt("r1", "hello")
    store.enqueue("m1", "r1", "thread:t1", "hello")
    # RED：闭合集之外的原因拒绝且不得改状态
    with pytest.raises(ValueError, match="illegal abandon reason: because_i_said"):
        store.abandon("m1", "because_i_said")
    assert len(store.due(0.0)) == 1
    # GREEN：合法原因放弃后消息不再是 pending
    store.abandon("m1", "channelGone")
    assert store.due(0.0) == []


# ---------- M10 ----------

def test_m10_failed_delivery_backoff_not_dropped(tmp_path):
    store = Store(str(tmp_path / "o.db"))
    store.record_receipt("r1", "hello")
    store.enqueue("m1", "r1", "thread:t1", "hello")
    assert backoff_next(0) == 5.0
    assert backoff_next(20) == 300.0
    # RED：投递 False 不许丢消息，按 backoff_next 重试
    stat = pump(store, 1000.0, lambda ch, body: False)
    assert stat["retried"] == 1 and stat["sent"] == 0
    assert store.due(1000.0) == []
    rows = store.due(1005.0)
    assert len(rows) == 1 and rows[0]["attempts"] == 1
    assert rows[0]["next_try_at"] == 1005.0
    # GREEN：deliver 返回 True → mark_sent，pending 清空
    stat2 = pump(store, 1005.0, lambda ch, body: True)
    assert stat2["sent"] == 1
    assert store.due(10000.0) == []
    # 合法放弃原因也走 abandon 分支（不丢消息、状态明确）
    store.record_receipt("r2", "bye")
    store.enqueue("m2", "r2", "thread:t1", "bye")

    def _gone(ch, body):
        raise ChannelGone()

    assert pump(store, 10000.0, _gone)["abandoned"] == 1
    assert store.due(10000.0) == []


# ---------- M11 ----------

def test_m11_budget_gate_terminates_ticket(tmp_path):
    # RED：50 tokens 预算，一次 deliver 用 60 + 40 → 终止票、不回复
    red, _ = _engine(str(tmp_path / "red.db"), _TokenExec()), None
    tid = red.intake(_inp("i1", "/do task"), budget_tokens=50)["ticket_id"]
    res = red.on_worker_report(red.tickets.get(tid).assignments[-1],
                               {"summary": "done", "touched_paths": []})
    assert res == {"verdict": "budget_exceeded", "replied": False}
    assert any(r.get("verdict") == "BUDGET_EXCEEDED"
               and r.get("reason") == "budget_tokens_exceeded"
               for r in red.governance_rulings())
    assert red.tickets.get(tid).status.value.lower() == "ended"
    assert red.store.get(f"budget:{tid}") == {
        "tokens": 50, "set_at": red.store.get(f"budget:{tid}")["set_at"]}
    # GREEN：1000 tokens 预算下同一报告正常回复
    green = _engine(str(tmp_path / "green.db"), _TokenExec())
    tid2 = green.intake(_inp("i1", "/do task"), budget_tokens=1000)["ticket_id"]
    res2 = green.on_worker_report(green.tickets.get(tid2).assignments[-1],
                                  {"summary": "done", "touched_paths": []})
    assert res2["replied"] is True
    assert green.tickets.get(tid2).status.value.lower() == "open"


# ---------- M12 ----------

def test_m12_deviation_repair_circuit_breaker(tmp_path):
    # RED：连续 deviation 用尽 max_repairs=1 → 落闸、终票、不回复
    red = _engine(str(tmp_path / "red.db"), _TokenExec(), watchdog=Watchdog())
    tid = red.intake(_inp("i1", "/do task"),
                     mandate_rules={"forbid": ["secret/**"]})["ticket_id"]
    a = red.tickets.get(tid).assignments[-1]
    first = red.on_worker_report(
        a, {"summary": "done", "touched_paths": ["secret/a.md"]})
    assert first["repaired"] is True and first["replied"] is False
    second = red.on_worker_report(
        red.tickets.get(tid).assignments[-1],
        {"summary": "done", "touched_paths": ["secret/a.md"]})
    assert second["replied"] is False and second["escalated"] is True
    assert any(r.get("reason") == "deviation_repair_limit"
               for r in red.governance_rulings())
    assert red.tickets.get(tid).status.value.lower() == "ended"
    # GREEN：合规报告 → 正常回复、不触碰修复闸
    green = _engine(str(tmp_path / "green.db"), _TokenExec(), watchdog=Watchdog())
    tid2 = green.intake(_inp("i1", "/do task"),
                        mandate_rules={"allow_paths": ["work/**"]})["ticket_id"]
    ok = green.on_worker_report(
        green.tickets.get(tid2).assignments[-1],
        {"summary": "完成", "touched_paths": ["work/a.md"]})
    assert ok["replied"] is True
    assert not any(r.get("reason") == "deviation_repair_limit"
                   for r in green.governance_rulings())


# ---------- M13 ----------

def test_m13_admission_fail_closed():
    cfg = AdmissionConfig(whitelist=["t1"])
    # RED：bot 门 / 无 assessor / 幻觉规则名 / 空证据，一律 fail-closed
    assert decide(Input(id="u1:i1", thread_key="t1", text="/do x",
                        is_bot=True), cfg, {}, None)[0] == "rejected_bot"
    mention = Input(id="u1:i2", thread_key="t1", text="please look")
    assert decide(mention, cfg, {}, None)[0] == "rejected_eval"
    ghost = lambda inp, g: Assessment(True, "ghost_rule", "evidence")
    assert decide(mention, cfg, {}, ghost)[0] == "rejected_eval:hallucinated_rule:ghost_rule"
    no_ev = lambda inp, g: Assessment(True, "whitelist_user", "")
    assert decide(mention, cfg, {}, no_ev)[0] == "rejected_eval:accept_without_evidence"
    # GREEN：白名单显式指令 / 有证据的研判 → accept
    disp, a = decide(Input(id="u1:i3", thread_key="t1", text="/do ship it"),
                     cfg, {}, None)
    assert disp == "accept" and a.matched_rule == "explicit_marker"
    good = lambda inp, g: Assessment(True, "whitelist_user", "user asked to ship")
    assert decide(mention, cfg, {}, good)[0] == "accept"


# ---------- M14 ----------

def test_m14_watchdog_unassessable_not_ok():
    m = Mandate(task_text="x")
    wd = Watchdog()
    # RED：自称 done 无 summary → UNASSESSABLE，不许说成 OK
    r = wd.review(m, {"status": "done", "summary": "", "touched_paths": []})
    assert r.verdict is WatchdogVerdict.UNASSESSABLE
    # RED：judge 异常 → judge_unavailable
    def _boom(payload):
        raise RuntimeError("boom")

    jv, jr = LLMJudge(transport=_boom).judge(m, {"summary": "done"})
    assert jv is WatchdogVerdict.UNASSESSABLE
    assert "judge_unavailable: RuntimeError: boom" in jr
    # RED：幻觉判词 → UNASSESSABLE + 点名
    jv2, jr2 = LLMJudge(
        transport=lambda p: {"choices": [{"message": {"content": json.dumps(
            {"verdict": "totally_ok", "reasons": []})}}]}).judge(
        m, {"summary": "done"})
    assert jv2 is WatchdogVerdict.UNASSESSABLE
    assert "hallucinated_verdict:totally_ok" in jr2[0]
    # RED：触碰 forbid → DEVIATION
    r3 = wd.review(Mandate(task_text="x", rules={"forbid": ["secret/**"]}),
                   {"summary": "done", "touched_paths": ["secret/a.md"]})
    assert r3.verdict is WatchdogVerdict.DEVIATION
    # GREEN：有完成证据且未越界 → OK
    assert wd.review(m, {"status": "done", "summary": "完成",
                         "touched_paths": []}).verdict is WatchdogVerdict.OK


# ---------- M15 ----------

def test_m15_write_gate_required_and_unknown_tool(tmp_path):
    a = Assignment(ticket_id="k1", session_id="s1")
    calls = []

    def _host_exec(op, path, content):
        calls.append((op, path, content))
        return True

    # RED：write_gate 缺位 → 拒绝一切写，host_exec 一次都不许调
    no_gate = ExecutingToolLoopExecutor(write_gate=None, host_exec=_host_exec)
    out = no_gate._exec_tool(a, "write_file",
                             {"path": "a.md", "content": "x"})
    assert "write_gate_required" in out and calls == []
    # RED：gate 返回 False → 禁止落盘
    deny = ExecutingToolLoopExecutor(write_gate=lambda tid, p: False,
                                    host_exec=_host_exec)
    out2 = deny._exec_tool(a, "write_file", {"path": "a.md", "content": "x"})
    assert "未过治理，禁止落盘" in out2 and calls == []
    # RED：未知工具名 → 明确报错
    assert "unknown_tool:rm_rf" in deny._exec_tool(a, "rm_rf", {})
    # GREEN：gate 放行 → 真实调用 host_exec 并回报 written
    allow = ExecutingToolLoopExecutor(write_gate=lambda tid, p: True,
                                     host_exec=_host_exec)
    out3 = allow._exec_tool(a, "write_file", {"path": "a.md", "content": "x"})
    assert "written" in out3
    assert calls == [("write", "a.md", "x")]


# ---------- M16 ----------

def test_m16_host_exec_rejects_symlink_leaf(tmp_path):
    target = tmp_path / "target.txt"
    assert _default_host_exec("write", str(target), "original") is True
    link = tmp_path / "link.txt"
    os.symlink(str(target), str(link))
    # RED：symlink 叶子必须被 O_NOFOLLOW 拒绝，且不许改写 symlink 目标
    assert _default_host_exec("write", str(link), "pwned") is False
    assert target.read_text(encoding="utf-8") == "original"
    # GREEN：普通文件真实写成 0o644 内容
    plain = tmp_path / "plain.txt"
    assert _default_host_exec("write", str(plain), "hello") is True
    assert plain.read_text(encoding="utf-8") == "hello"
    assert oct(plain.stat().st_mode & 0o777) == "0o644"


# ---------- M17 ----------

def test_m17_credit_gate_blocks_calibration(tmp_path):
    # RED：含 speak_bad 的标注库 → 官方校准拒绝，参数不许回写
    dirty = LabelStore(path=str(tmp_path / "dirty.jsonl"))
    for i in range(4):
        dirty.append(_event(f"ok{i}", "speak_ok"), "speak_ok")
    dirty.append(_event("bad0", "speak_bad", conf=0.55, utility=2.0),
                 "speak_bad")
    assert dirty.stats()["ineligible"] is True
    result = run_calibration(dirty)
    assert result["calibrated"] is False
    assert "ineligible_run" in result["note"]
    cfg_bad = ProactivityConfig()
    before = (cfg_bad.c_false, cfg_bad.c_speak, cfg_bad.lam, cfg_bad.observe)
    assert apply_calibration(cfg_bad, result) is False
    assert (cfg_bad.c_false, cfg_bad.c_speak, cfg_bad.lam,
            cfg_bad.observe) == before
    # GREEN：干净标注达标 → 允许回写参数并解除 observe
    clean = LabelStore(path=str(tmp_path / "clean.jsonl"))
    for i in range(4):
        clean.append(_event(f"c{i}", "speak_ok"), "speak_ok")
    assert clean.stats()["ineligible"] is False
    good = run_calibration(clean)
    assert good["calibrated"] is True and good["n_speak"] >= 3
    cfg = ProactivityConfig()
    assert apply_calibration(cfg, good) is True
    assert cfg.c_false == float(good["c_false"])
    assert cfg.c_speak == float(good["c_speak"])
    assert cfg.lam == float(good["lam"])
    assert cfg.observe is False


# ---------- M18 ----------

def test_m18_scheduler_lease_check_skips_dispatch(tmp_path):
    def _setup(db_name):
        ex = _EchoExec()
        eng = _engine(str(tmp_path / db_name), ex)
        t = eng.tickets.create(f"d-{db_name}", Mandate(task_text="x"),
                               ["thread:t1"], ["t1"])
        a = eng.tickets.assign(t.id, "s1")
        a.report = "ready"
        eng.tickets.add_input(
            t.id, Input(id="i1", thread_key="t1", text="go", is_human=True))
        return eng, ex, t

    # RED：租约检查不过 → 跳过该票，executor.deliver 一次都不许调
    eng, ex, t = _setup("red.db")
    sch = Scheduler(eng, SchedulerConfig(), clock=lambda: 0.0,
                    pre_deliver_check=lambda tid, sid: False)
    stat = sch.tick(0.0)
    assert stat["skipped_lease"] == 1 and stat["dispatched"] == 0
    assert ex.delivered == [] and t.undelivered == ["i1"]
    # GREEN：租约通过 → 真实派发并 mark_delivered
    eng2, ex2, t2 = _setup("green.db")
    sch2 = Scheduler(eng2, SchedulerConfig(), clock=lambda: 0.0,
                     pre_deliver_check=lambda tid, sid: True)
    stat2 = sch2.tick(0.0)
    assert stat2["dispatched"] == 1 and stat2["skipped_lease"] == 0
    assert ex2.delivered == [f"{t2.id}:i1"] and t2.undelivered == []


# ---------- M19 ----------

def test_m19_proactivity_input_validation_and_observe():
    eng = ProactivityEngine(ProactivityConfig(), clock=lambda: 1000.0)
    # RED：负效用 / 越界确证度 → ValueError 且不记入 log
    with pytest.raises(ValueError, match="utility 必须 ≥ 0"):
        eng.evaluate(CandidateEvent("bad-u", -1.0, 0.9, 1000.0))
    with pytest.raises(ValueError, match=r"confidence 必须落在 \[0,1\]"):
        eng.evaluate(CandidateEvent("bad-c", 10.0, 1.5, 1000.0))
    assert eng.log == []
    # RED：低效用事件先判 drop（observe 模式下前缀 observe_）
    dropped = eng.evaluate(CandidateEvent("low", 0.1, 0.9, 1000.0))
    assert dropped.action == "observe_drop"
    assert dropped.u_effective < eng.cfg.c_speak
    # GREEN：合规事件记入 log，动作带 observe_ 前缀，p_star 可复算
    d = eng.evaluate(CandidateEvent("ok", 10.0, 0.9, 1000.0))
    assert d.action.startswith("observe_")
    assert d.p_star == (eng.cfg.c_false + eng.cfg.c_speak) / (
        d.u_effective + eng.cfg.c_false)
    assert [x.event_id for x in eng.log] == ["low", "ok"]


# ---------- M20 ----------

def test_m20_intake_receipt_idempotent(tmp_path):
    # 两个线程都在白名单，避免 follow_up 路由掩盖"新 id 应接单"这一断言
    eng = Engine(Store(str(tmp_path / "e.db")),
                 AdmissionConfig(whitelist=["t1", "t2"]),
                 executor=_TokenExec(), deliver=lambda ch, body: True)
    # RED：同 id 重投必须先存后做地判重，不许新建第二张 ticket
    first = eng.intake(_inp("i1", "/do task"))
    dup = eng.intake(_inp("i1", "/do task"))
    assert first["disposition"] == "accept"
    assert dup == {"disposition": "duplicate", "input_id": "i1"}
    assert len(eng.tickets._tickets) == 1
    assert eng.store.has_receipt("input:i1") is True
    # GREEN：新 id 正常接单
    second = eng.intake(_inp("i2", "/do another", thread="t2"))
    assert second["disposition"] == "accept"
    assert second["ticket_id"] == "k2"


# ---------- M21 ----------

def test_m21_consecutive_review_failure_fuse(tmp_path):
    # RED：连续 max_review_failures+1 次非 OK 报告 → D2 熔断，不回复、终票
    red = _engine(str(tmp_path / "red.db"), _TokenExec(), watchdog=Watchdog(),
                  max_repairs=10, max_review_failures=3)
    tid = red.intake(_inp("i1", "/do task"),
                     mandate_rules={"forbid": ["secret/**"]})["ticket_id"]
    bad = {"summary": "done", "touched_paths": ["secret/a.md"]}
    for n in range(3):
        out = red.on_worker_report(red.tickets.get(tid).assignments[-1], bad)
        assert out["repaired"] is True
        assert red.store.get(f"review_fail:{tid}")["count"] == n + 1
    fused = red.on_worker_report(red.tickets.get(tid).assignments[-1], bad)
    assert fused["replied"] is False and fused["escalated"] is True
    assert red.store.get(f"review_fail:{tid}")["count"] == 4
    assert any(r.get("reason") == "failed_same_sha_retry_limit"
               and r.get("consecutive_failures") == 4
               for r in red.governance_rulings())
    assert red.tickets.get(tid).status.value.lower() == "ended"
    # GREEN：OK 报告把连续失败计数清零，回复正常出站
    green = _engine(str(tmp_path / "green.db"), _TokenExec(), watchdog=Watchdog())
    tid2 = green.intake(_inp("i1", "/do task"))["ticket_id"]
    ok = green.on_worker_report(green.tickets.get(tid2).assignments[-1],
                                {"summary": "完成", "touched_paths": []})
    assert ok["replied"] is True
    assert green.store.get(f"review_fail:{tid2}")["count"] == 0


# ---------- M21 ----------


class _DevWd:
    """可切 verdict 的 watchdog 桩：真实复核逻辑在 agent/watchdog.py，这里只
    控制「连续失败」这一维（被测对象是 engine 的计数与熔断，不是复核规则）。"""

    def __init__(self, value="deviation"):
        self.value = value

    def review(self, mandate, payload):
        return SimpleNamespace(verdict=SimpleNamespace(value=self.value),
                               reasons=["越界写"])


def test_m21_review_fail_streak_fuses(tmp_path):
    # RED：连发 max_repairs+1 份 deviation，第 4 份 MUST 熔断且 reason 逐字
    #      （不是 deviation_repair_limit）；连续计数 MUST 落 kv 可恢复。
    wd = _DevWd("deviation")
    eng = _engine(str(tmp_path / "e.db"), _EchoExec(), watchdog=wd,
                  max_repairs=3, max_review_failures=3)
    tid = eng.intake(_inp("i1", "/do task"))["ticket_id"]
    reports = []
    for n in range(1, 5):
        a = eng.tickets.get(tid).assignments[-1]
        reports.append(eng.on_worker_report(
            a, {"status": "done", "summary": f"r{n}", "touched_paths": []}))
    assert [r.get("repaired") for r in reports[:3]] == [True, True, True], reports
    assert reports[3].get("replied") is False and reports[3].get("repaired") is not True
    assert eng.tickets.get(tid).status.value.lower() == "ended"
    assert any(x.get("reason") == "failed_same_sha_retry_limit"
               for x in eng.governance_rulings())
    assert (eng.store.get(f"review_fail:{tid}") or {}).get("count") == 4
    # GREEN：夹杂一份 OK 报告 MUST 清零连续计数，之后重新计数（不熔断）
    wd.value = "ok"
    a = eng.tickets.get(tid).assignments[-1]
    ok = eng.on_worker_report(a, {"status": "done", "summary": "good",
                                  "touched_paths": []})
    assert ok["replied"] is True
    assert (eng.store.get(f"review_fail:{tid}") or {}).get("count") == 0


# ---------- M22 ----------


def test_m22_pre_write_without_governance_is_fail_closed(tmp_path):
    # RED：治理挂点缺位（Engine(governance=None)）时 MUST 拒绝写，
    #      且 MUST 落一条 reason 在词表内的 BLOCK 判决（不许静默放行）。
    eng = _engine(str(tmp_path / "e.db"), _EchoExec())
    assert eng.governance is None
    assert eng.pre_write("k1", "docs/x.md") is False
    r = [x for x in eng.governance_rulings()
         if x.get("reason") == "governance_path_block"]
    assert len(r) == 1 and r[0]["verdict"] == "BLOCK"
    assert "governance_absent" in r[0]["detail"]
    # GREEN：接上治理挂点且已 claim 的路径 → 放行
    from agent.governance import ScopeRegistry
    eng2 = _engine(str(tmp_path / "e2.db"), _EchoExec(),
                   governance=ScopeRegistry())
    tid = eng2.intake(_inp("i1", "/do task"))["ticket_id"]
    eng2.governance.claim(tid, ["docs/**"])
    assert eng2.pre_write(tid, "docs/x.md") is True
