"""K3：calibrate 择优修复（I5 缺口 C2）测试。

C2 旧行为：网格遍历返回第一个 (n_speak>0 且 precision≥target) 的组。
两个真实缺陷：
  a) 字典序第一组可能是脆弱点（自定义网格小样本组排前 → n_speak=1 precision 1.0）；
  b) 即使第一组是最大样本组，它的 precision 可能刚好压线，而后面有
     precision 更高的达标组——旧版永不考虑。

新行为（calibration_best.calibrate_best）：
  样本量门（n_speak≥min_speak，默认 3）→ (precision, n_speak, 保守度) 择优
  → 全部达标组留 alternatives 审计。无组过门 → calibrated=False（脆弱点
  不能当校准结论），best_params 留最优候选供调用方权衡。

RED→GREEN 断言：
 1 RED1a  自定义网格把脆弱组排前：旧版选中 n_speak<3 的组，新版 calibrated=False
 2 GREEN1  压线组在前高精度组在后：旧版 0.8，新版 1.0（同达标域内择优）
 3 GREEN2  同精度两级别组：新版选 n_speak 大的
 4 GREEN3  默认网格回归：J3 夹具（4好1坏3wait）达标且 n_speak≥3
 5 RED2    4 坏开口 precision 0.2 → calibrated=False 不变
 6 GREEN4  alternatives 含全部达标组且按择优键降序
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from agent.calibration import calibrate as calibrate_old    # noqa: E402
from agent.calibration_best import calibrate_best           # noqa: E402


def _labels(n_ok=4, n_bad=1):
    out = [{"event_id": f"ok{i}", "utility": 10.0, "confidence": 0.9,
            "created_at": 1000.0, "label": "speak_ok"} for i in range(n_ok)]
    out += [{"event_id": f"bad{i}", "utility": 2.0, "confidence": 0.55,
             "created_at": 1000.0, "label": "speak_bad"} for i in range(n_bad)]
    return out


def test_red1a_fragile_group_first():
    """脆弱组（n_speak=1, precision 1.0）排网格最前：旧版踩 C2，新版挡住。"""
    labels = _labels()
    # c_false=5 保守组：p*(u=10)=5.2/10.2≈0.51 → conf0.9 开口（4 次）；
    # p*(u=2)=5.2/2.5=2.08 → conf0.55 不开口 → n_speak=4。不够脆弱。
    # 更狠：c_false=50 → p*(u=10)=50.2/10.2≈4.9 → conf0.9 也不开口 → n_speak=0。
    # 脆弱组要 n_speak=1：只有 1 个事件能开口。用 c_false=1.2：
    # p*(u=2)=1.7/2.5=0.68 → conf0.55 不开；p*(u=10)=1.7/10.2≈0.167 → conf0.9 开
    # → 仍 4 次。n_speak=1 需要仅一个事件 u 大：改夹具 1 好 3 坏。
    fragile_labels = [{"event_id": "ok0", "utility": 10.0, "confidence": 0.9,
                       "created_at": 1000.0, "label": "speak_ok"},
                      {"event_id": "bad0", "utility": 2.0, "confidence": 0.3,
                       "created_at": 1000.0, "label": "speak_bad"}]
    # c_false=5: p*(u=10)≈0.51 → ok0 开（1 次全对 precision 1.0, n_speak=1 脆弱）
    #            p*(u=2)=2.08 → bad 不开。c_false=0.5: p*(u=2)=0.28 → bad 也开
    #            → 2 开 1 对 precision 0.5 < 0.8 不达标。
    grid = {"c_false": [5.0, 0.5], "c_speak": [0.2], "lam": [0.1]}
    old = calibrate_old(fragile_labels, target_precision=0.8, grid=grid)
    new = calibrate_best(fragile_labels, target_precision=0.8, grid=grid)
    assert old["calibrated"] is True and old["n_speak"] == 1, old   # 旧踩 C2
    assert new["calibrated"] is False                                # 新挡住
    assert new.get("note", "").startswith("all candidates")


def test_green1_higher_precision_group_wins():
    """压线组(0.8, n=5)在前，高精度组(1.0, n=4)在后：新版选 1.0。"""
    labels = _labels()
    grid = {"c_false": [0.5, 5.0], "c_speak": [0.2], "lam": [0.1]}
    old = calibrate_old(labels, target_precision=0.8, grid=grid)
    new = calibrate_best(labels, target_precision=0.8, grid=grid)
    assert old["achieved_precision"] == 0.8
    assert new["achieved_precision"] == 1.0
    assert new["n_speak"] == 4


def test_green2_same_precision_prefers_more_speaks():
    """同精度下开口更多的组优先（构造两级别组同 1.0）：宽严两组都全对，
    宽松组开口多。"""
    labels = [{"event_id": f"ok{i}", "utility": 10.0, "confidence": 0.9,
               "created_at": 1000.0, "label": "speak_ok"} for i in range(5)]
    # 保守组 c_false=5 只开 u=10 的 5 个（全对）；宽松组 c_false=0.5 同样
    # 只能开这 5 个（没有其他事件）→ 同精度 1.0，同 n_speak=5 → 择优键
    # 落到保守度（c_false 大者）。断言新版选 c_false=5.0。
    grid = {"c_false": [0.5, 5.0], "c_speak": [0.2], "lam": [0.1]}
    new = calibrate_best(labels, target_precision=0.8, grid=grid)
    assert new["calibrated"] is True
    assert new["c_false"] == 5.0      # 同精度同开口 → 更保守参数


def test_green3_default_grid_regression():
    """默认网格回归：J3 夹具达标且样本量过门。"""
    labels = _labels(n_ok=4, n_bad=1)
    labels += [{"event_id": f"w{i}", "utility": 1.0, "confidence": 0.3,
                "created_at": 1000.0, "label": "wait_ok"} for i in range(3)]
    new = calibrate_best(labels, target_precision=0.8)
    assert new["calibrated"] is True
    assert new["n_speak"] >= 3
    assert "alternatives" in new and len(new["alternatives"]) > 0


def test_red2_all_bad_never_calibrates():
    labels = _labels(n_ok=1, n_bad=4)
    new = calibrate_best(labels, target_precision=0.8)
    assert new["calibrated"] is False


def test_green4_alternatives_sorted():
    labels = _labels()
    grid = {"c_false": [0.5, 5.0], "c_speak": [0.2], "lam": [0.1]}
    new = calibrate_best(labels, target_precision=0.8, grid=grid)
    alts = new["alternatives"]
    keys = [(a["achieved_precision"], a["n_speak"]) for a in alts]
    assert keys == sorted(keys, reverse=True)
    assert alts[0]["achieved_precision"] == new["achieved_precision"]


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
