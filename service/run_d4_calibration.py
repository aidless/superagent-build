#!/usr/bin/env python3
"""D4 校准闭环：把服役批的 observe 事件标注入库 → 择优校准 → 输出结论。

与 heuristic_label 的差别只有一处**人工覆写**：`svc-...-t6`（"紧急：线上服务
像是挂了"）被判为 `observe_drop`——静默是错的，人应该被告知。这是本批唯一的
wait_bad 金标（其余 wait/drop 无反证，按 wait_ok 计）。

诚实口径：13 条事件里 6 条 done-side speak（watchdog 全 OK）与 4 条 input-side
drop，标注是启发式 + 1 条人工覆写，不是双人独立标注；结论按"方向性验证"报，
不冒充充分统计。

用法：python3 service/run_d4_calibration.py [--observe service/observe_log.jsonl]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.calibration_loop import close_loop, heuristic_label  # noqa: E402

# 人工覆写：静默错误的样本（金标来自人的判断，不是启发式）
OVERRIDES = {
    # 紧急求助被 drop —— 该开口却没开口
    "input:svc-*-t6": "wait_bad",
}


def _match(event_id: str, pattern: str) -> bool:
    """支持 `input:svc-*-t6` 这种前缀 + 尾部通配（日期戳在中间变）。"""
    if "*" not in pattern:
        return event_id == pattern
    head, tail = pattern.split("*", 1)
    return event_id.startswith(head) and event_id.endswith(tail)


def labeler(ev: dict) -> str:
    eid = ev.get("event_id", "")
    for pat, label in OVERRIDES.items():
        if _match(eid, pat):
            return label
    return heuristic_label(ev)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--observe", default="service/observe_log.jsonl")
    ap.add_argument("--out-dir", default="results/real/d4_calibration")
    args = ap.parse_args()

    events = [json.loads(l) for l in open(args.observe, encoding="utf-8")
              if l.strip()]
    os.makedirs(args.out_dir, exist_ok=True)
    result = close_loop(events, state_dir=args.out_dir, labeler=labeler)
    result["n_observe_events"] = len(events)
    result["human_overrides"] = dict(OVERRIDES)
    out = os.path.join(args.out_dir, "calibration.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    cal = result["calibration"]
    print(f"n_observe_events={len(events)} n_labeled={result['n_labeled']}")
    print(f"stats={json.dumps(result['stats'], ensure_ascii=False)}")
    print(f"calibrated={cal.get('calibrated')} note={cal.get('note')}")
    if cal.get("calibrated"):
        print(f"applied_params={result.get('applied_params')}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
