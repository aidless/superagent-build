"""D1b：intake 输入侧开口候选事件（RED→GREEN，AGENTS.md 第 21/27 条）。

RED（先红）：observe=True + 闲聊输入 → observe_log 必须出现 action≠speak 的
            低效用条目；当前 engine 只在 on_worker_report 之后产生开口事件，
            wait/drop 侧零样本，断言必须先失败。
RED（API）：input_event_evidence 构造参数实现前 → TypeError。
GREEN（后绿）：observe=False 零新增、accept 路径不新增（保 I5 旧测试
              len(observe_log)==1 语义）、不写回复、不改 disposition；
              注入证据决定 utility/confidence；缺 estimator 时 confidence 合法。

非 observe 模式行为完全不变；零网络、纯 stdlib；每测试独立 Store(":memory:")。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.admission import AdmissionConfig  # noqa: E402
from agent.calibration import ConfidenceEstimator  # noqa: E402
from agent.engine import Engine  # noqa: E402
from agent.executor import EchoExecutor  # noqa: E402
from agent.model import Input  # noqa: E402
from agent.proactivity import ProactivityConfig, ProactivityEngine  # noqa: E402
from agent.store import Store  # noqa: E402

FIELDS = {"event_id", "utility", "confidence", "created_at",
          "p_star", "u_effective", "action", "watchdog_verdict"}


def _mk(observe, **kw):
    pro = ProactivityEngine(ProactivityConfig(observe=observe))
    return Engine(Store(":memory:"), AdmissionConfig(whitelist=["t:1"]),
                  EchoExecutor(on_report=None),
                  deliver=lambda ch, b: True,
                  proactivity=pro, confidence_estimator=ConfidenceEstimator(), **kw)


def _chitchat(msg_id="chat-1"):
    # 无显式 marker 的闲聊 → rejected_eval（非 accept / 非 follow_up）
    return Input(id=msg_id, thread_key="t:1", text="今天午饭吃什么", is_human=True)


def test_d1b_red_observe_mode_logs_input_side_candidate():
    eng = _mk(True)
    r = eng.intake(_chitchat())
    assert r["disposition"].startswith("rejected"), r
    assert len(eng.observe_log) == 1, eng.observe_log
    rec = eng.observe_log[0]
    assert FIELDS <= set(rec), rec
    assert rec["action"] != "speak", rec
    assert rec["action"] in ("observe_wait", "observe_drop"), rec
    assert rec["utility"] < 0.2, rec["utility"]     # 低效用
    assert 0.0 <= rec["confidence"] <= 1.0, rec
    assert rec["watchdog_verdict"] == "unassessable"
    assert rec["created_at"] > 0
    # 不写回复：outbox 零待发
    assert eng.store.pending_outbox_count() == 0


def test_d1b_green_observe_false_unchanged():
    eng = _mk(False)
    r = eng.intake(_chitchat())
    assert r["disposition"].startswith("rejected"), r
    assert eng.observe_log == [], eng.observe_log
    assert eng.store.pending_outbox_count() == 0
    assert eng.store.get(f"rejected:{_chitchat().id}") is not None


def test_d1b_green_accept_path_no_input_event():
    eng = _mk(True)
    r = eng.intake(Input(id="c1", thread_key="t:1", text="/do 任务", is_human=True))
    assert r["disposition"] == "accept"
    assert eng.observe_log == [], eng.observe_log


def test_d1b_green_injected_evidence_drives_utility_and_confidence():
    injected = {"utility": 0.05, "watchdog_verdict": "ok",
                "has_explicit_marker": True, "is_bot": False,
                "text_len": 300, "tool_rounds": 0, "repairs": 0}
    est = ConfidenceEstimator()
    eng = _mk(True, input_event_evidence=injected)
    eng.intake(_chitchat())
    assert len(eng.observe_log) == 1, eng.observe_log
    rec = eng.observe_log[0]
    assert rec["utility"] == 0.05
    assert abs(rec["confidence"] - est.estimate(injected)) < 1e-9


def test_d1b_green_confidence_without_estimator_is_legal():
    pro = ProactivityEngine(ProactivityConfig(observe=True))
    eng = Engine(Store(":memory:"), AdmissionConfig(whitelist=["t:1"]),
                 EchoExecutor(on_report=None), deliver=lambda ch, b: True,
                 proactivity=pro)
    eng.intake(_chitchat("chat-2"))
    assert len(eng.observe_log) == 1
    assert 0.0 <= eng.observe_log[0]["confidence"] <= 1.0
