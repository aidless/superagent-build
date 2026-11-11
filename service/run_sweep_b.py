"""B2：结论级 32-run 真跑（4 任务 × 4 seeds × 2 臂）。

与 K1 8-run 探路批的差别：
- 样本量 8 → 32（配对 bootstrap 才有意义）；
- 每个 RunRecord 增量落 JSONL（断点可续，--resume 跳过已完成 run_id）；
- 除 sweep_curve 外，再出 V 敏感性 + CI 是否跨 0 的判决块（agent.evidence）；
- 所有真实推理发生在 Magpie 网关（云端），本机只做编排/落盘。

诚实声明（写进报告 caveats）：
1. 两臂介入次数是**记账假设**（single=2 / dual=1），不是实测人打断次数。
   所以 V 敏感性扫的是"假设成立时结论对 V 稳不稳"，不是"V 被测量过"。
2. 任务文本带 /do 显式标记 → 走 explicit 路由，dual 臂的 LLMAssessor
   **不会被调用**；本批测的是 Engine+治理链开销，研判开销另见
   results/eval/assessor_ab/report.json。
3. 成本口径 price_per_1m_tokens=4.0 元（framework.CostModel 默认），
   按实际模型单价替换即可重算。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.sweep_bridge import run_real_sweep, sweep_curve  # noqa: E402
from agent.evidence import v_sensitivity, flip_point, ci_summary  # noqa: E402
from framework import paired_bootstrap_ci, CostModel  # noqa: E402

TASKS = [
    {"id": "t-l1",
     "text": "/do 写一段不超过 60 字的中文说明，解释什么是幂等收据；只输出文本。",
     "rules": {"allow_paths": ["notes.md"]}},
    {"id": "t-l2",
     "text": "/do 给出 3 条 agent 写文件前的治理检查项，每条一句中文，编号列出。",
     "rules": {"allow_paths": ["notes.md"]}},
    {"id": "t-l3",
     "text": "/do 对比 outbox 先存后做与直接投递两种顺序的崩溃差异，"
             "用 4 句中文，并点名至少一个失效场景。",
     "rules": {"allow_paths": ["notes.md"]}},
    {"id": "t-l4",
     "text": "/do 为一个最小 agent 治理模块写 5 行伪代码（纯文本），"
             "覆盖 claim / check / ruling 三步，不要解释。",
     "rules": {"allow_paths": ["notes.md"]}},
]

VALUE_GRID = [0.001, 0.01, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0]


def _load_checkpoint(path: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line:
            d = json.loads(line)
            out[d["run_id"]] = d
    return out


def _append(path: str, rec) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        f.flush()


def _rebuild(rows: list[dict]):
    from framework import CallRecord, InterventionRecord, RunRecord
    out = []
    for d in rows:
        out.append(RunRecord(
            run_id=d["run_id"], arm=d["arm"], seed=d["seed"],
            calls=[CallRecord(**c) for c in d["calls"]],
            interventions=[InterventionRecord(**i)
                           for i in d["interventions"]],
            rework=d["rework"], wall_seconds=d["wall_seconds"],
            router_overhead_tokens=d["router_overhead_tokens"]))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=4)
    ap.add_argument("--tasks", type=int, default=4,
                    help="取前 N 个任务（--tasks 1 --seeds 1 即 2-run smoke）")
    ap.add_argument("--out", default="results/real/sweep_b")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--analyze-only", action="store_true",
                    help="只读 runs.jsonl 重算报告，不调网关")
    ap.add_argument("--timeout", type=float, default=45.0)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    ckpt = os.path.join(args.out, "runs.jsonl")
    if args.analyze_only:
        done = _load_checkpoint(ckpt)
    else:
        done = _load_checkpoint(ckpt) if args.resume else {}
    seeds = list(range(args.seeds))
    tasks = TASKS[:args.tasks]
    t0 = time.time()
    n_new = 0
    for t in ([] if args.analyze_only else tasks):
        for seed in seeds:
            ids = [f"{t['id']}-single-{seed}", f"{t['id']}-dual-{seed}"]
            if all(i in done for i in ids):
                continue
            print(f"[sweep] {t['id']} seed={seed} ...", flush=True)
            recs = run_real_sweep(tasks=[t], seeds=[seed],
                                  work_root=os.path.join(args.out, "work"),
                                  timeout=args.timeout,
                                  max_tool_rounds=2)
            for r in recs:
                _append(ckpt, r)
                done[r.run_id] = asdict(r)
                n_new += 1
            print(f"  +{len(recs)} runs (total {len(done)})", flush=True)

    rows = list(done.values())
    records = _rebuild(rows)
    singles = [r for r in records if r.arm == "single"]
    duals = [r for r in records if r.arm == "dual"]
    print(f"[sweep] n_runs={len(records)} single={len(singles)} dual={len(duals)}",
          flush=True)

    curve = sweep_curve(records)
    sens = v_sensitivity(singles, duals, values=VALUE_GRID)
    base_deltas = curve["cost_deltas"]
    lo, hi = paired_bootstrap_ci(base_deltas, n_boot=2000, seed=0)
    ci = ci_summary(base_deltas, lo, hi)

    # 逐 seed token 差的符号稳定性（复制核验：K1 8-run 曾给出 +1654 的"token 税"）
    pair_dt = []
    for s_rec, d_rec in zip(sorted(singles, key=lambda r: (r.seed, r.run_id)),
                            sorted(duals, key=lambda r: (r.seed, r.run_id))):
        pair_dt.append({"pair": f"{s_rec.run_id} vs {d_rec.run_id}",
                        "delta_tokens": d_rec.total_tokens() - s_rec.total_tokens()})
    signs = [1 if x["delta_tokens"] > 0 else -1 for x in pair_dt]
    k1_path = os.path.join(_ROOT, "results/real/sweep_curve.json") if False else None
    k1_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "results/real/sweep_curve.json")
    k1 = None
    if os.path.exists(k1_path):
        try:
            k1 = json.load(open(k1_path, encoding="utf-8")).get("curve", {})
        except (json.JSONDecodeError, OSError):
            k1 = None
    replication = {
        "per_pair_delta_tokens": pair_dt,
        "mean_delta_tokens_per_pair": round(
            sum(x["delta_tokens"] for x in pair_dt) / len(pair_dt), 2),
        "sign_flips_within_batch": signs.count(1) > 0 and signs.count(-1) > 0,
        "k1_8run_delta_tokens": (k1 or {}).get("delta_tokens"),
        "k1_vs_b_sign_agrees": (
            None if not k1 or k1.get("delta_tokens") is None
            else (k1["delta_tokens"] > 0) == (sum(
                x["delta_tokens"] for x in pair_dt) > 0)),
    }

    report = {
        "n_runs": len(records),
        "n_pairs": len(singles),
        "tasks": [t["id"] for t in tasks],
        "seeds": seeds,
        "elapsed_s": round(time.time() - t0, 3),
        "new_runs_this_call": n_new,
        "model": "deepseek-v4.1-flash",
        "endpoint": "http://127.0.0.1:3425/v1/chat/completions",
        "curve_default_costmodel": curve,
        "ci_readout": ci,
        "v_sensitivity": sens,
        "flip_point_V": flip_point(sens),
        "replication_check": replication,
        "caveats": [
            "介入次数是记账假设 single=2 / dual=1，非实测人打断次数；"
            "V 敏感性扫的是'假设成立时结论对 V 稳不稳'。",
            "任务带 /do 显式标记 → explicit 路由，dual 臂 LLMAssessor 不触发；"
            "研判开销见 results/eval/assessor_ab/report.json。",
            "成本单价取 framework.CostModel 默认 4.0 元/1M tokens。",
            "ΔT 的符号在本批内逐 seed 翻转（见 replication_check）："
            "token 差属噪声量级，判决 DUAL_WINS 由介入计数假设单边驱动，"
            "不能读成'双层省 token'。",
        ],
    }
    with open(os.path.join(args.out, "evidence_report.json"), "w",
              encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps({"n_runs": report["n_runs"],
                      "verdict": ci["verdict"],
                      "ci_low": ci["ci_low"], "ci_high": ci["ci_high"],
                      "crosses_zero": ci["crosses_zero"],
                      "flip_point_V": report["flip_point_V"],
                      "delta_tokens": curve["delta_tokens"]},
                     ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
