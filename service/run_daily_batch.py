#!/usr/bin/env python3
"""服役日批通用驱动：从 tasks JSON 读任务清单，幂等跑真网关批。

与 run_service.py（v0.3.0 首批，硬编码 3 任务）的区别：任务由 JSON 驱动、
input id 带日期戳（重跑不 duplicate）、observe/metrics 按日追加。

用法：python3 service/run_daily_batch.py --date 2026-11-11 \
        --tasks service/tasks_2026-11-11.json
"""
import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.model import Input
from agent.store import Store
from agent.engine import Engine
from agent.admission import AdmissionConfig
from agent.governance import ScopeRegistry
from agent.watchdog import Watchdog
from agent.proactivity import ProactivityEngine, ProactivityConfig
from agent.calibration import ConfidenceEstimator
from agent.executing_executor import ExecutingToolLoopExecutor
from agent.metrics import MetricsLedger

ENDPOINT = "http://127.0.0.1:3425/v1/chat/completions"
MODEL = "workbuddy/deepseek-v4.1-flash"  # opencode-go 配额耗尽(429)，改显式 provider


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--tasks", required=True)
    ap.add_argument("--model", default=MODEL,
                    help="网关模型 id（裸名走 routing group，可能落到配额耗尽 provider）")
    ap.add_argument("--completion-evidence", action="store_true",
                    help="完成侧注入多维证据构造器（P1-A：修 0.533 同质化）")
    ap.add_argument("--apply-calibration", default=None,
                    help="落盘 close_loop 报告路径；达标则回写参数并解除 observe")
    args = ap.parse_args()

    tasks = json.load(open(args.tasks, encoding="utf-8"))
    whitelist = [t["thread"] for t in tasks]
    from agent.calibration import build_completion_evidence
    from agent.calibration_loop import load_calibrated_cfg
    pcfg = ProactivityConfig(observe=True)
    if args.apply_calibration:
        report = json.load(open(args.apply_calibration, encoding="utf-8"))
        pcfg = load_calibrated_cfg(report)
        print(f"[calibration] observe={pcfg.observe} c_false={pcfg.c_false} "
              f"c_speak={pcfg.c_speak} lam={pcfg.lam}", flush=True)
    ce = build_completion_evidence if args.completion_evidence else None
    ledger = MetricsLedger()
    executor = ExecutingToolLoopExecutor(endpoint=ENDPOINT, model=args.model,
                                         metrics=ledger, timeout=60.0,
                                         max_tool_rounds=3)
    eng = Engine(store=Store(f"results/real/service-{args.date}.db"),
                 cfg=AdmissionConfig(whitelist=whitelist),
                 executor=executor, deliver=lambda ch, body: True,
                 watchdog=Watchdog(), governance=ScopeRegistry(),
                 proactivity=ProactivityEngine(pcfg),
                 confidence_estimator=ConfidenceEstimator(), max_repairs=1,
                 completion_evidence=ce)
    executor.write_gate = eng.pre_write

    def worker_final_text(sid):
        msgs = executor.sessions.get(sid, {}).get("messages", [])
        for m in reversed(msgs):
            if m.get("role") == "assistant" and m.get("content"):
                return m["content"]
        return ""

    def parse_report(raw):
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            try:
                d = json.loads(m.group(0))
                if isinstance(d, dict):
                    d.setdefault("summary", raw[:200])
                    return d
            except json.JSONDecodeError:
                pass
        return {"summary": raw[:200], "status": "done", "touched_paths": []}

    t0 = time.time()
    n_events_before = len(eng.observe_log)
    for i, t in enumerate(tasks, 1):
        inp_id = f"svc-{args.date}-t{i}"
        print(f"=== 任务 {i} [{inp_id}] ===", flush=True)
        d = eng.intake(Input(id=inp_id, thread_key=t["thread"], text=t["text"],
                             is_human=True, is_bot=False),
                       mandate_rules=t.get("rules"))
        disp = d.get("disposition")
        print(f"  admission: {disp}", flush=True)
        if disp != "accept":
            continue
        tid = d["ticket_id"]
        tick = eng.tickets.get(tid)
        if not tick.assignments:
            continue
        payload = parse_report(worker_final_text(tick.assignments[-1].session_id))
        verdict = eng.on_worker_report(tick.assignments[-1], payload)
        print(f"  verdict: {verdict}", flush=True)

    # observe 追加（K2 数据流）
    # 本臂独立留证（共享 observe_log 跨臂追加无法回切，报告要可复算）
    with open(f"results/real/observe_{args.date}.jsonl", "w",
              encoding="utf-8") as f:
        for ev in eng.observe_log[n_events_before:]:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    with open("service/observe_log.jsonl", "a", encoding="utf-8") as f:
        for ev in eng.observe_log[n_events_before:]:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    # P1-A：解除 observe 后主动开口走 notices + store kv，落盘留证
    speaks = [n for n in eng.notices if n.get("kind") == "proactive_speak"]
    if speaks:
        os.makedirs("results/real", exist_ok=True)
        with open(f"results/real/proactive_{args.date}.jsonl", "a",
                  encoding="utf-8") as f:
            for n in speaks:
                f.write(json.dumps(n, ensure_ascii=False) + "\n")
    ledger.to_jsonl(f"service/metrics_{args.date}.jsonl")
    tok = sum(r.prompt_tokens + r.completion_tokens for r in ledger.records)
    print(f"=== 批完成 {time.time() - t0:.0f}s calls={len(ledger.records)} "
          f"tokens={tok} observe_events={len(eng.observe_log) - n_events_before} "
          f"proactive_speaks={len(speaks)}", flush=True)
    confs = sorted({round(ev["confidence"], 4)
                    for ev in eng.observe_log[n_events_before:]})
    print(f"=== confidence 档位: {confs}", flush=True)


if __name__ == "__main__":
    main()
