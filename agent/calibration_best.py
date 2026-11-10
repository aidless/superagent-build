"""K3：修 I5 缺口 C2——calibrate 达标组选择从"字典序第一组"改为显式择优。

旧行为：网格遍历顺序里第一个 (n_speak>0 且 precision≥target) 的组立即返回，
可能选中 n_speak=1 的脆弱点（一个开口恰好对 = precision 1.0，样本量撑不起结论）。

新行为（不达标分支语义不变，达标分支改为全网格择优）：
- 达标候选集 = 所有 (n_speak≥min_speak 且 precision≥target) 的组
  （min_speak 默认 3：n_speak=1 的脆弱点——一个开口恰好对=precision 1.0——
  样本量撑不起结论，先过样本量门再谈择优）；
- 择优键 = (achieved_precision, n_speak, -c_false, -c_speak) 字典序最大：
  ① 精度优先；② 同精度选开口更多（样本量更大、结论更稳）；
  ③ 同精度同开口选更保守参数（开口更难发生 → precision 更可信）；
  若无组过样本量门，回落到 n_speak>0 的旧门（诚实报达标组，调用方自行
  权衡），但 alternatives 里标出 trusted=False；
- 返回里加 "alternatives"（全部达标组，按择优键降序）供审计。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Optional                                    # noqa: E402

from agent.calibration import DEFAULT_GRID, _replay_precision   # noqa: E402


def calibrate_best(labels: list[dict], target_precision: float = 0.8,
                   grid: Optional[dict] = None,
                   min_speak: int = 3) -> dict:
    """全网格择优版 calibrate（C2 修复）。返回结构与 agent.calibration.calibrate
    兼容：达标 {"calibrated": True, "c_false", "c_speak", "lam",
    "achieved_precision", "n_speak", "alternatives"}；
    不达标 {"calibrated": False, "best_precision", "best_params", "n_speak"}。"""
    g = grid or DEFAULT_GRID
    if not labels:
        return {"calibrated": False, "best_precision": None,
                "best_params": None, "n_speak": 0}

    now = max(float(r["created_at"]) for r in labels)

    passing: list[dict] = []
    best_precision = -1.0
    best_params: Optional[dict] = None
    best_n_speak = 0

    for c_false in g["c_false"]:
        for c_speak in g["c_speak"]:
            for lam in g["lam"]:
                precision, n_speak = _replay_precision(
                    labels, c_false, c_speak, lam, now)
                if (precision > best_precision
                        or (precision == best_precision
                            and n_speak > best_n_speak)):
                    best_precision = precision
                    best_params = {"c_false": c_false, "c_speak": c_speak,
                                   "lam": lam}
                    best_n_speak = n_speak
                if n_speak > 0 and precision >= target_precision:
                    passing.append({"c_false": c_false, "c_speak": c_speak,
                                    "lam": lam,
                                    "achieved_precision": precision,
                                    "n_speak": n_speak,
                                    "trusted": n_speak >= min_speak})

    trusted = [p for p in passing if p["trusted"]]
    if not trusted:
        # 全部候选样本量不足：不宣布达标（脆弱点不能当校准结论），
        # 但把最优候选留在 best_params 里供调用方判断。
        if passing:
            passing.sort(key=lambda p: (p["achieved_precision"], p["n_speak"]),
                         reverse=True)
            top = passing[0]
            return {"calibrated": False,
                    "best_precision": top["achieved_precision"],
                    "best_params": {"c_false": top["c_false"],
                                    "c_speak": top["c_speak"],
                                    "lam": top["lam"]},
                    "n_speak": top["n_speak"],
                    "note": f"all candidates n_speak<{min_speak}"}
        return {"calibrated": False, "best_precision": best_precision,
                "best_params": best_params, "n_speak": best_n_speak}

    # 择优键：精度 ↓、开口数 ↓、c_false ↓（更保守）、c_speak ↓（开口更难）
    # 保守度键方向：同精度同开口 → c_false/c_speak 大者优先（开口更难发生，
    # precision 更可信）。直接用原值 + reverse=True。
    keyfn = lambda p: (p["achieved_precision"], p["n_speak"],
                       p["c_false"], p["c_speak"])
    passing.sort(key=keyfn, reverse=True)
    trusted.sort(key=keyfn, reverse=True)
    top = trusted[0]
    return {"calibrated": True, "c_false": top["c_false"],
            "c_speak": top["c_speak"], "lam": top["lam"],
            "achieved_precision": top["achieved_precision"],
            "n_speak": top["n_speak"],
            "alternatives": passing}


def _t(*, eid="e", action="observe_speak", conf=0.9, utility=10.0, t=1000.0,
       label=None):
    d = {"event_id": eid, "action": action, "confidence": conf,
         "utility": utility, "created_at": t,
         "watchdog_verdict": "OK"}
    if label is not None:
        d["label"] = label
    return d


def _fixture():
    """好开口(u=10,conf0.9,speak_ok)×4 + 坏开口(u=2,conf0.55,speak_bad)×1
    + wait(u=1,conf0.3,wait_ok)×3 —— J3 同款分层语义。"""
    labels = [_t(eid=f"ok{i}", conf=0.9, utility=10.0, label="speak_ok")
              for i in range(4)]
    labels.append(_t(eid="bad0", conf=0.55, utility=2.0, label="speak_bad"))
    labels += [_t(eid=f"w{i}", action="observe_wait", conf=0.3, utility=1.0,
                  label="wait_ok") for i in range(3)]
    return labels


def selftest() -> int:
    """RED→GREEN 双向：旧行为会选 n_speak=1 脆弱点；新行为必须选大样本组。"""
    labels = _fixture()
    r = calibrate_best(labels, target_precision=0.8)
    # GREEN3：达标时选中的组必须有足够样本量
    assert r["calibrated"] is True, r
    assert r["n_speak"] >= 3, f"脆弱点未排除：n_speak={r['n_speak']}"
    # 同精度下开口更多的组优先：alternatives 里没有 n_speak 更大且同精度的组
    top = (r["achieved_precision"], r["n_speak"])
    for alt in r["alternatives"]:
        assert (alt["achieved_precision"], alt["n_speak"]) <= top, \
            f"择优失败：{alt} 优于 {top}"
    # RED1 复现旧行为（对照）：旧 calibrate 在同一夹具上可能选 n_speak=1
    from agent.calibration import calibrate
    old = calibrate(labels, target_precision=0.8)
    assert old["calibrated"] is True
    print(f"[对照] 旧=第一组 n_speak={old['n_speak']}  "
          f"新=择优 n_speak={r['n_speak']} (alternatives={len(r['alternatives'])})")

    # 不达标语义不变
    bad = [_t(eid=f"bad{i}", conf=0.55, utility=2.0, label="speak_bad")
           for i in range(4)]
    r2 = calibrate_best(bad, target_precision=0.8)
    assert r2["calibrated"] is False and "applied" not in r2

    # 空输入
    r3 = calibrate_best([], target_precision=0.8)
    assert r3["calibrated"] is False and r3["best_params"] is None
    print("selftest OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
