"""D1：ConfidenceEstimator 证据源扩维（RED→GREEN，AGENTS.md 第 21/27 条）。

RED（先红）：两组不同的多源证据向量当前都返回 0.4（缺源中性值打折），
            断言「证据向量不同 → 置信度不同」必须先失败。
GREEN（后绿）：空证据 / 旧三源口径逐字保持；缺源仍返回合法 [0,1] 浮点；
              扩维下 watchdog ok > unassessable > deviation；
              tool_rounds / repairs / text_len 各维变化都改变分数。

语义变更（注明）：仅当 evidence 含扩展键（watchdog_verdict /
has_explicit_marker / is_bot / text_len / tool_rounds / repairs）时启用扩维
合成；无扩展键与空证据保持现行行为（空证据仍 0.4）。
零网络、纯 stdlib、测试间无共享状态。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.calibration import ConfidenceEstimator  # noqa: E402

WEAK = {
    "watchdog_verdict": "deviation",
    "has_explicit_marker": False,
    "is_bot": True,
    "text_len": 12,
    "tool_rounds": 3,
    "repairs": 2,
}
STRONG = {
    "watchdog_verdict": "ok",
    "has_explicit_marker": True,
    "is_bot": False,
    "text_len": 300,
    "tool_rounds": 0,
    "repairs": 0,
}


def test_d1_red_different_evidence_vectors_yield_different_confidence():
    est = ConfidenceEstimator()
    a = est.estimate(WEAK)
    b = est.estimate(STRONG)
    assert isinstance(a, float) and isinstance(b, float)
    assert a != b, f"证据向量不同却返回同一置信度（单证据源残缺）: weak={a} strong={b}"


def test_d1_green_empty_evidence_matches_current_behavior():
    est = ConfidenceEstimator()
    assert est.estimate({}) == pytest.approx(0.4)


def test_d1_green_legacy_sources_unchanged():
    est = ConfidenceEstimator()
    full = {"watchdog_pass": 1.0, "worker_verified": 1.0,
            "historical_precision": 1.0}
    assert est.estimate(full) == pytest.approx(1.0)
    partial = {"watchdog_pass": 1.0, "worker_verified": 1.0}
    assert est.estimate(partial) == pytest.approx((1.0 + 1.0 + 0.5) / 3.0 * 0.8)
    single = ConfidenceEstimator(weights={"watchdog_pass": 1.0})
    assert single.estimate({"watchdog_pass": 1.0}) == pytest.approx(1.0)
    assert single.estimate({"watchdog_pass": 5.0}) <= 1.0
    assert single.estimate({"watchdog_pass": -5.0}) >= 0.0


def test_d1_green_missing_sources_still_legal_float():
    est = ConfidenceEstimator()
    for ev in ({"watchdog_verdict": "deviation"},
               {"tool_rounds": 4},
               {"text_len": 0, "is_bot": True},
               {"watchdog_pass": 1.0, "repairs": 1},
               {"watchdog_verdict": "unassessable", "text_len": 4096}):
        score = est.estimate(ev)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0, (ev, score)


def test_d1_green_watchdog_verdict_ordering():
    est = ConfidenceEstimator()
    base = {"has_explicit_marker": True, "is_bot": False,
            "text_len": 300, "tool_rounds": 0, "repairs": 0}
    ok = est.estimate(dict(base, watchdog_verdict="ok"))
    un = est.estimate(dict(base, watchdog_verdict="unassessable"))
    dev = est.estimate(dict(base, watchdog_verdict="deviation"))
    assert ok > un > dev, (ok, un, dev)


def test_d1_green_signal_dimensions_change_score():
    est = ConfidenceEstimator()
    base = {"watchdog_verdict": "ok"}
    assert est.estimate(dict(base, tool_rounds=0)) != est.estimate(dict(base, tool_rounds=4))
    assert est.estimate(dict(base, repairs=0)) != est.estimate(dict(base, repairs=2))
    assert est.estimate(dict(base, text_len=8)) != est.estimate(dict(base, text_len=900))
    assert est.estimate(dict(base, has_explicit_marker=True)) != est.estimate(
        dict(base, has_explicit_marker=False))
    assert est.estimate(dict(base, is_bot=False)) != est.estimate(dict(base, is_bot=True))
