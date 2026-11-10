"""I5 主动性校准机制 验收测试（先 RED 后 GREEN）。

覆盖三块契约：
- ConfidenceEstimator：多证据源加权合成确证度、缺源按中性 0.5 打折、空证据保守值、clamp。
- LabelStore：append-only JSONL 标注存储；非法 label 拒绝、speak_precision 计算、文件落盘可重读。
- calibrate：网格搜索满足 target_precision 的 (c_false, c_speak, lam)；
             全错标注集返回 calibrated False 并附最优 precision。

数值口径（calibrate 的网格，来自任务书默认值）：
  c_false ∈ {0.5, 1.0, 2.0}、c_speak ∈ {0.1, 0.2, 0.5}、lam ∈ {0.05, 0.1, 0.2}
  重放判定：p*(t) = (c_false + c_speak) / (u_eff(t) + c_false)，u_eff 由 ProactivityEngine 计算。
  固定时钟 now = max(created_at)：当所有记录 created_at 相同 → Δt=0 → 无衰减，u_eff = utility。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from agent.calibration import (  # noqa: E402
    ConfidenceEstimator,
    LabelStore,
    calibrate,
)


# ===================== ConfidenceEstimator =====================

def test_estimator_all_evidence_high():
    """三源全给满分 → 确证度应为满分 1.0，且落在 [0,1]。"""
    est = ConfidenceEstimator()
    score = est.estimate({
        "watchdog_pass": 1.0,
        "worker_verified": 1.0,
        "historical_precision": 1.0,
    })
    assert score == pytest.approx(1.0)
    assert 0.0 <= score <= 1.0


def test_estimator_missing_source_discounted():
    """缺一个证据源 → 该源按 0.5 中性值参与，并对最终结果乘 0.8 打折。"""
    est = ConfidenceEstimator()
    full = est.estimate({
        "watchdog_pass": 1.0,
        "worker_verified": 1.0,
        "historical_precision": 1.0,
    })
    partial = est.estimate({
        "watchdog_pass": 1.0,
        "worker_verified": 1.0,
    })
    # 打折后必须明显低于"全源齐备"
    assert partial < full
    # 手算：(1 + 1 + 0.5) / 3 = 0.8333...，再 ×0.8 = 0.6666...（精确值逐字对照）
    assert partial == pytest.approx((1.0 + 1.0 + 0.5) / 3.0 * 0.8)


def test_estimator_empty_evidence_conservative():
    """空 evidence → 保守值 0.4。"""
    est = ConfidenceEstimator()
    assert est.estimate({}) == pytest.approx(0.4)


def test_estimator_clamp_bounds():
    """即便证据值越界，输出也必须 clamp 到 [0,1]。"""
    est = ConfidenceEstimator(weights={"watchdog_pass": 1.0})
    assert est.estimate({"watchdog_pass": 1.0}) == pytest.approx(1.0)
    assert est.estimate({"watchdog_pass": 5.0}) <= 1.0
    assert est.estimate({"watchdog_pass": -5.0}) >= 0.0


# ===================== LabelStore =====================

def test_labelstore_rejects_bad_label():
    """非法 label 必须抛 ValueError。"""
    st = LabelStore()
    with pytest.raises(ValueError):
        st.append({"event_id": "e", "utility": 10.0, "confidence": 0.9,
                   "created_at": 1.0}, "maybe_speak")


def test_labelstore_precision():
    """speak_precision = speak_ok / (speak_ok + speak_bad)。"""
    st = LabelStore()
    for i in range(8):
        st.append({"event_id": f"ok{i}", "utility": 10.0, "confidence": 0.9,
                   "created_at": 1.0}, "speak_ok")
    for i in range(2):
        st.append({"event_id": f"bad{i}", "utility": 10.0, "confidence": 0.1,
                   "created_at": 1.0}, "speak_bad")
    s = st.stats()
    assert s["speak_ok"] == 8
    assert s["speak_bad"] == 2
    assert s["speak_precision"] == pytest.approx(0.8)


def test_labelstore_precision_none_when_no_speak():
    """没有开口标注时 speak_precision 返回 None（分母为 0）。"""
    st = LabelStore()
    st.append({"event_id": "w0", "utility": 1.0, "confidence": 0.3,
               "created_at": 1.0}, "wait_ok")
    s = st.stats()
    assert s["speak_precision"] is None


def test_labelstore_file_roundtrip(tmp_path):
    """文件模式：落盘为 JSONL，重开可完整读回，标签与字段保真。"""
    p = tmp_path / "labels.jsonl"
    st = LabelStore(str(p))
    st.append({"event_id": "e1", "utility": 5.0, "confidence": 0.7,
               "created_at": 2.0}, "speak_ok")
    st.append({"event_id": "e2", "utility": 5.0, "confidence": 0.2,
               "created_at": 2.0}, "wait_ok")
    assert p.exists()

    st2 = LabelStore(str(p))
    recs = st2.load()
    assert len(recs) == 2
    assert recs[0]["event_id"] == "e1"
    assert recs[0]["label"] == "speak_ok"
    assert recs[1]["label"] == "wait_ok"
    assert st2.stats()["speak_ok"] == 1


# ===================== calibrate =====================

def _labels_high_confirm() -> list[dict]:
    """高确证度才该开口：8 条高确证度 speak_ok + 2 条低确证度 speak_bad。"""
    recs: list[dict] = []
    for i in range(8):
        recs.append({"event_id": f"ok{i}", "utility": 10.0, "confidence": 0.9,
                     "created_at": 1000.0, "label": "speak_ok"})
    for i in range(2):
        recs.append({"event_id": f"bad{i}", "utility": 10.0, "confidence": 0.1,
                     "created_at": 1000.0, "label": "speak_bad"})
    return recs


def test_calibrate_reaches_target():
    """可校准数据集：必须找到一组参数使开口 precision ≥ 0.8。"""
    res = calibrate(_labels_high_confirm(), target_precision=0.8)
    assert res.get("calibrated") is not False
    assert res["achieved_precision"] >= 0.8
    assert res["n_speak"] > 0
    assert {"c_false", "c_speak", "lam"} <= set(res.keys())


def test_calibrate_all_wrong_returns_false():
    """全错标注集（高确证度却标 speak_bad、低确证度却标 speak_ok）无法达标 → False。"""
    recs: list[dict] = []
    for i in range(5):
        recs.append({"event_id": f"hb{i}", "utility": 10.0, "confidence": 0.95,
                     "created_at": 1000.0, "label": "speak_bad"})
    for i in range(5):
        recs.append({"event_id": f"lo{i}", "utility": 10.0, "confidence": 0.05,
                     "created_at": 1000.0, "label": "speak_ok"})
    res = calibrate(recs, target_precision=0.8)
    assert res.get("calibrated") is False
    # 必须附带最优组的 precision（不能只回一个 False）
    assert "best_precision" in res


def test_labelstore_feeds_calibrate(tmp_path):
    """闭环：LabelStore 落盘的标注 → load() → calibrate 直接可用。"""
    p = tmp_path / "labels.jsonl"
    st = LabelStore(str(p))
    for r in _labels_high_confirm():
        decision = {k: v for k, v in r.items() if k != "label"}
        st.append(decision, r["label"])
    res = calibrate(st.load(), target_precision=0.8)
    assert res.get("calibrated") is not False
    assert res["achieved_precision"] >= 0.8
