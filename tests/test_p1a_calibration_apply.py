"""P1-A 验收：校准参数真回写 + observe 解除 + 完成侧多维证据。

RED 项（before）：
1. close_loop 只出 applied_params 报告，传 cfg 也不回写、不解除 observe；
2. 含 speak_bad 的 INELIGIBLE 批次若被误回写 → 应拒绝解除；
3. Engine.on_worker_report 的完成侧 evidence 恒为单键，注入构造器不受支持；
4. observe 解除后（action 无 observe_ 前缀）既不记 observe_log 也不留主动开口痕。

设计约束：默认不注入构造器时逐字保持旧单键口径（tests/test_integration.py:52
的精确断言不改动）；多维证据与解除留痕都是显式开启的新能力。
"""
import json
import os
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.admission import AdmissionConfig          # noqa: E402
from agent.calibration import ConfidenceEstimator    # noqa: E402
from agent.calibration_loop import close_loop        # noqa: E402
from agent.engine import Engine                      # noqa: E402
from agent.executor import EchoExecutor              # noqa: E402
from agent.model import Input                        # noqa: E402
from agent.proactivity import ProactivityConfig      # noqa: E402
from agent.store import Store                        # noqa: E402


def _events(n_speak=4, n_wait=3, bad=False):
    # 全部事件钉在同一 1 秒窗口：calibrate_best 重放时 now=max(created_at)，
    # 若跨 1000 秒，e^(-lam*t) 会把 u_eff 衰减到 0 → 零开口、校准必然失败。
    out = []
    for i in range(n_speak):
        out.append({"event_id": f"done:s{i}", "utility": 10.0,
                    "confidence": 0.9, "created_at": 100.0 + i * 0.1,
                    "action": "observe_speak",
                    "watchdog_verdict": "DEVIATION" if bad and i == 0 else "OK"})
    for i in range(n_wait):
        out.append({"event_id": f"input:w{i}", "utility": 2.0,
                    "confidence": 0.2, "created_at": 100.4 + i * 0.1,
                    "action": "observe_drop", "watchdog_verdict": "OK"})
    return out


def _mk_engine(**kw):
    store = Store(":memory:")
    ex = EchoExecutor(on_report=None)
    eng = Engine(store, AdmissionConfig(whitelist=["t:1"]), ex,
                 deliver=lambda ch, b: True, **kw)
    return eng


def _report(eng, text="/do 任务", payload=None):
    r = eng.intake(Input(id="p1a:1", thread_key="t:1", text=text,
                         is_human=True))
    assert r["disposition"] == "accept", r
    a = eng.tickets.get(r["ticket_id"]).assignments[-1]
    return eng, a, eng.on_worker_report(a, payload or {
        "status": "done", "summary": "完工", "touched_paths": []})


# ---------- 1. 校准回写接线 ----------

def test_close_loop_applies_to_cfg_when_calibrated(tmp_path):
    from agent.proactivity import ProactivityEngine
    cfg = ProactivityConfig(observe=True)
    engine = ProactivityEngine(cfg)
    out = close_loop(_events(), state_dir=str(tmp_path), cfg=cfg, apply=True)
    assert out["calibration"]["calibrated"] is True
    assert out["applied"] is True, out
    assert cfg.observe is False, "达标后必须解除 observe"
    assert engine.cfg is cfg and engine.cfg.observe is False
    for k in ("c_false", "c_speak", "lam"):
        assert cfg.__dict__[k] == out["calibration"][k]
    # 解除后同一事件的重放动作不带 observe_ 前缀
    from agent.proactivity import CandidateEvent
    ev = CandidateEvent("x", 10.0, 0.9, 1000.0)
    assert engine.evaluate(ev).action in ("speak", "wait", "drop")


def test_close_loop_ineligible_never_applies(tmp_path):
    cfg = ProactivityConfig(observe=True)
    before = dict(cfg.__dict__)
    out = close_loop(_events(bad=True), state_dir=str(tmp_path),
                     cfg=cfg, apply=True)
    assert out["calibration"]["calibrated"] is False
    assert out["applied"] is False
    assert cfg.observe is True and cfg.__dict__ == before


def test_close_loop_without_apply_keeps_cfg(tmp_path):
    cfg = ProactivityConfig(observe=True)
    out = close_loop(_events(), state_dir=str(tmp_path), cfg=cfg)
    assert out["applied"] is False
    assert cfg.observe is True


def test_load_calibrated_cfg_reads_real_report():
    from agent.calibration_loop import load_calibrated_cfg
    # 真实落盘报告的入库副本（evidence bundle 由 make_evidence_bundle.py 生成并 sha256 验真）
    path = os.path.join("evidence", "calibration", "d4_calibration.json")
    report = json.load(open(path, encoding="utf-8"))
    cfg = load_calibrated_cfg(report)
    # 逐字复算：落盘 applied_params 的三个参数必须进 cfg，且解除 observe
    assert cfg.c_false == report["applied_params"]["c_false"]
    assert cfg.c_speak == report["applied_params"]["c_speak"]
    assert cfg.lam == report["applied_params"]["lam"]
    assert cfg.observe is False
    # 伪造未达标报告必须 fail-closed 保持 observe
    bogus = {"calibrated": False, "applied_params": {"c_false": 99.0,
             "c_speak": 99.0, "lam": 99.0}}
    safe = load_calibrated_cfg(bogus)
    assert safe.observe is True and safe.c_false != 99.0


# ---------- 2. 完成侧多维证据 ----------

def test_completion_evidence_hook_changes_confidence():
    from agent.calibration import build_completion_evidence
    eng = _mk_engine(completion_evidence=build_completion_evidence)
    from agent.proactivity import ProactivityEngine
    eng.proactivity = ProactivityEngine(ProactivityConfig(observe=True))
    eng.confidence_estimator = ConfidenceEstimator()
    eng, a, _ = _report(eng)
    rec = eng.observe_log[-1]
    assert isinstance(rec.get("evidence"), dict)
    for key in ("watchdog_verdict", "has_explicit_marker", "is_bot",
                "text_len", "tool_rounds", "repairs"):
        assert key in rec["evidence"], (key, rec["evidence"])
    single = ConfidenceEstimator().estimate({"watchdog_pass": 1.0})
    assert abs(rec["confidence"] - single) > 1e-6, "完成侧仍是单键同质 0.533"


def test_completion_evidence_varies_with_text_and_rounds():
    from agent.calibration import build_completion_evidence
    seen = []
    for text, payload in (
        ("/do " + "x" * 300, {"status": "done", "summary": "a",
                              "touched_paths": [], "tool_rounds": 3,
                              "repairs": 1}),
        ("/do 短", {"status": "done", "summary": "b",
                    "touched_paths": [], "tool_rounds": 0, "repairs": 0}),
    ):
        eng = _mk_engine(completion_evidence=build_completion_evidence)
        from agent.proactivity import ProactivityEngine
        eng.proactivity = ProactivityEngine(ProactivityConfig(observe=True))
        eng.confidence_estimator = ConfidenceEstimator()
        _report(eng, text=text, payload=payload)
        seen.append(eng.observe_log[-1]["confidence"])
    assert len(set(seen)) == 2, f"多维证据没产生区分度: {seen}"


def test_default_single_key_contract_unchanged():
    est = ConfidenceEstimator()
    eng = _mk_engine()
    from agent.proactivity import ProactivityEngine
    eng.proactivity = ProactivityEngine(ProactivityConfig(observe=True))
    eng.confidence_estimator = est
    eng, a, _ = _report(eng)
    rec = eng.observe_log[-1]
    assert "evidence" not in rec
    assert abs(rec["confidence"] - est.estimate({"watchdog_pass": 1.0})) < 1e-9


# ---------- 3. observe 解除后真开口留痕 ----------

def test_unobserved_speak_leaves_trace():
    from agent.proactivity import ProactivityEngine
    eng = _mk_engine(proactivity=ProactivityEngine(
        ProactivityConfig(observe=False)),
        confidence_estimator=ConfidenceEstimator())
    eng, a, verdict = _report(eng)
    assert verdict["replied"] is True
    assert eng.observe_log == [], "解除 observe 后不该再记标注旁路"
    speaks = [n for n in eng.notices if n.get("kind") == "proactive_speak"]
    assert len(speaks) == 1, eng.notices
    assert speaks[0]["action"] in ("speak", "wait", "drop")
    stored = eng.store.get(f"proactive:{speaks[0]['event_id']}")
    assert stored is not None and stored["action"] == speaks[0]["action"]
