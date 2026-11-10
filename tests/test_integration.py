"""I5↔engine / I6↔engine 集成验收：confidence 证据源、observe 标注旁路、idle_at 写入。

RED 项：
- Engine 无 confidence_estimator 参数 → TypeError；
- observe 判定不留原始 CandidateEvent（utility/confidence/created_at）→ 标注闭环断不成立；
- 交付后 assignment.idle_at 无人写 → I6 idle 回收永不触发。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.admission import AdmissionConfig          # noqa: E402
from agent.calibration import ConfidenceEstimator    # noqa: E402
from agent.engine import Engine                      # noqa: E402
from agent.executor import EchoExecutor              # noqa: E402
from agent.model import Input                        # noqa: E402
from agent.proactivity import ProactivityConfig, ProactivityEngine  # noqa: E402
from agent.store import Store                        # noqa: E402


def _mk_engine(**kw):
    store = Store(":memory:")
    ex = EchoExecutor(on_report=None)
    eng = Engine(store, AdmissionConfig(whitelist=["t:1"]), ex,
                 deliver=lambda ch, b: True, **kw)
    return eng


def test_engine_accepts_confidence_estimator():
    est = ConfidenceEstimator()
    eng = _mk_engine(confidence_estimator=est)   # RED: TypeError 无此参数
    assert eng.confidence_estimator is est


def test_observe_log_carries_raw_event():
    est = ConfidenceEstimator()
    pro = ProactivityEngine(ProactivityConfig(observe=True))
    eng = _mk_engine(proactivity=pro, confidence_estimator=est)
    r = eng.intake(Input(id="c1", thread_key="t:1", text="/do 任务", is_human=True))
    assert r["disposition"] == "accept"
    a = eng.tickets.get(r["ticket_id"]).assignments[-1]
    eng.on_worker_report(a, {"status": "done", "summary": "完工",
                             "touched_paths": []})
    # observe 判定旁路：标注记录必须带原始事件三数 + 判定依据
    assert len(eng.observe_log) == 1, eng.observe_log
    rec = eng.observe_log[0]
    assert rec["utility"] == 10.0
    assert rec["created_at"] > 0
    # confidence 来自 estimator 而非硬编码 0.95：
    # evidence 只给 watchdog_pass=1.0，其余缺源 0.5 → (1+0.5+0.5)/3 ×0.8 ≈ 0.5333
    assert abs(rec["confidence"] - est.estimate({"watchdog_pass": 1.0})) < 1e-9
    assert abs(rec["confidence"] - 0.95) > 1e-6, "还在用硬编码 0.95"
    assert rec["action"].startswith("observe_")
    assert rec["p_star"] > 0


def test_idle_at_written_on_delivery():
    eng = _mk_engine()
    r = eng.intake(Input(id="c2", thread_key="t:1", text="/do 任务", is_human=True))
    a = eng.tickets.get(r["ticket_id"]).assignments[-1]
    assert a.idle_at is not None, "交付后 idle_at 仍为 None，I6 idle 回收永不触发"
