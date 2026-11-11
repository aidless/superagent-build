"""J3 校准闭环测试：RED→GREEN。

RED（写实现前跑）：ModuleNotFoundError calibration_loop 不存在。
GREEN 断言（先失败后通过）：
 1 RED1  ingest 空列表 → 0 条，stats 全零
 2 GREEN1 watchdog_pass 开口 → speak_ok；deviation 开口 → speak_bad；wait → wait_ok
 3 GREEN2 close_loop 攒够"好开口"→ calibrated=True 且 applied_params 带三参数
 4 RED2  混入大量坏开口（precision<0.8）→ calibrated=False 且无 applied_params
 5 GREEN3 apply_calibration 达标回写三参数并解除 observe；不达标不动参数
 6 RED3  标注数据不足（n_speak=0）→ 不达标（防沉默刷分）
 7 GREEN4 落盘 labels.jsonl 可复读（append-only）
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from agent.proactivity import ProactivityConfig   # noqa: E402


def _ev(eid, action="observe_speak", conf=0.9, wd="OK", utility=10.0, t=1000.0):
    return {"event_id": eid, "action": action, "confidence": conf,
            "watchdog_verdict": wd, "utility": utility,
            "created_at": t, "p_star": 0.109091, "u_effective": 9.0}


def test_ingest_empty_is_zero():
    from agent.calibration_loop import LabelStore, ingest_observe_log
    s = LabelStore()
    n = ingest_observe_log([], s)
    assert n == 0
    st = s.stats()
    assert st["speak_ok"] == 0 and st["speak_bad"] == 0


def test_heuristic_labels():
    from agent.calibration_loop import LabelStore, ingest_observe_log
    s = LabelStore()
    ingest_observe_log([
        _ev("e1", action="observe_speak", wd="OK"),
        _ev("e2", action="observe_speak", wd="DEVIATION"),
        _ev("e3", action="observe_wait", wd="OK"),
    ], s)
    st = s.stats()
    assert st["speak_ok"] == 1 and st["speak_bad"] == 1 and st["wait_ok"] == 1


def test_close_loop_calibrated():
    from agent.calibration_loop import close_loop
    with tempfile.TemporaryDirectory() as d:
        # 好开口=高utility高置信；wait=低utility低置信。precision≥0.8 才达标。
        # v0.3.0 信用门语义变更：夹具改为干净数据（无 speak_bad）——含坏开口
        # 的批次现在判 INELIGIBLE（官方校准拒绝），坏开口路径由
        # test_v03_upgrades.py::test_4_credit_gate_ineligible 覆盖。
        events = ([_ev(f"ok{i}", wd="OK", conf=0.9, utility=10.0)
                   for i in range(4)]
                  + [_ev(f"w{i}", action="observe_wait", wd="OK",
                         utility=1.0, conf=0.3) for i in range(3)])
        out = close_loop(events, d, target_precision=0.8)
        assert out["n_labeled"] == 7
        assert out["calibration"]["calibrated"] is True
        assert "applied_params" in out
        assert set(out["applied_params"]) == {"c_false", "c_speak", "lam"}


def test_close_loop_not_calibrated_on_bad_precision():
    from agent.calibration_loop import close_loop
    with tempfile.TemporaryDirectory() as d:
        # 1 好 4 坏开口 → precision=0.2 远低于 0.8
        events = ([_ev("ok0", wd="OK")]
                  + [_ev(f"bad{i}", wd="DEVIATION") for i in range(4)])
        out = close_loop(events, d, target_precision=0.8)
        assert out["calibration"]["calibrated"] is False
        assert "applied_params" not in out


def test_apply_calibration_writeback():
    from agent.calibration_loop import apply_calibration
    cfg = ProactivityConfig()
    ok = apply_calibration(cfg, {"calibrated": True, "c_false": 0.5,
                                 "c_speak": 0.1, "lam": 0.05,
                                 "achieved_precision": 0.9})
    assert ok is True
    assert (cfg.c_false, cfg.c_speak, cfg.lam) == (0.5, 0.1, 0.05)
    assert cfg.observe is False          # 达标才解除 observe

    cfg2 = ProactivityConfig()
    ok2 = apply_calibration(cfg2, {"calibrated": False, "best_params": None})
    assert ok2 is False
    assert cfg2.observe is True          # 不达标绝不改参数


def test_zero_speak_never_calibrates():
    from agent.calibration_loop import close_loop
    with tempfile.TemporaryDirectory() as d:
        events = [_ev(f"w{i}", action="observe_wait", wd="OK") for i in range(6)]
        out = close_loop(events, d, target_precision=0.8)
        assert out["calibration"]["calibrated"] is False   # 沉默不能刷分


def test_labels_persisted_and_reloadable():
    from agent.calibration_loop import LabelStore, ingest_observe_log, run_calibration
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "labels.jsonl")
        s = LabelStore(path=path)
        ingest_observe_log([_ev("e1", wd="OK"), _ev("e2", wd="OK")], s)
        assert os.path.exists(path)
        s2 = LabelStore(path=path)
        assert len(s2.load()) == 2
        rec = json.loads(open(path).readline())
        assert rec["label"] == "speak_ok" and rec["event_id"] == "e1"
        r = run_calibration(s2, target_precision=0.8)
        assert r["calibrated"] is True or r["calibrated"] is False  # 可跑


def main():
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            fails += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    if fails:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
