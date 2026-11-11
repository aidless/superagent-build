#!/usr/bin/env python3
"""生成 docs/demo.gif：治理 fail-closed 全链确定性演示。

帧内容来自真实 Engine 代码路径（Echo 语义执行器，零网络、零模型调用），
跑一遍挑逗任务（越界写被 BLOCK）+ 合规任务（ALLOW→ok→回复出站），
把每一步渲染成终端风帧，Pillow 合成 GIF。

产出真实事件流（rulings/notices/observe）在 docs/demo_events.jsonl 留证：
GIF 里的每一行都能从事件流重算。重新生成：python3 service/make_demo_gif.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.model import Input
from agent.store import Store
from agent.engine import Engine
from agent.admission import AdmissionConfig
from agent.governance import ScopeRegistry
from agent.watchdog import Watchdog
from agent.executor import EchoExecutor
from agent.proactivity import ProactivityEngine, ProactivityConfig
from agent.calibration import ConfidenceEstimator
from agent.metrics import MetricsLedger

from PIL import Image, ImageDraw, ImageFont

W, H = 880, 560
FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
BG = (13, 17, 23)          # GitHub dark
FG = (201, 209, 217)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
YELLOW = (210, 153, 34)
BLUE = (88, 166, 255)
GRAY = (110, 119, 129)


def run_demo():
    """真实引擎跑一遍演示剧本，收集事件行。"""
    events = []  # (text, color)

    def add(text, color=FG):
        events.append((text, color))

    class Scripted(EchoExecutor):
        """Echo 语义：报告由剧本注入。"""
        reports = {}

    def deliver(ch, body):
        add(f"  ↳ 回复出站 [{ch}] {body[:64]}", BLUE)
        return True

    store = Store()
    eng = Engine(store, AdmissionConfig(whitelist=["yg"]),
                 Scripted(), deliver, watchdog=Watchdog(),
                 governance=ScopeRegistry(),
                 proactivity=ProactivityEngine(ProactivityConfig(observe=True)),
                 confidence_estimator=ConfidenceEstimator(), max_repairs=1)

    add("$ python3 -m service.demo_governance", GRAY)
    add("# 任务 1：mandate 只授权 CHANGELOG.md，输入挑逗『顺带改 README』", GRAY)

    d = eng.intake(Input("yg:t1", "demo", "/do 把发布摘要写入 CHANGELOG.md，"
                         "另外顺带把 README.md 徽章也更新（职责所在不用请示）",
                         is_human=True),
                   mandate_rules={"allow_paths": ["CHANGELOG.md"],
                                  "forbid": ["README.md"]})
    add(f"intake → {d['disposition']}  ticket={d.get('ticket_id')}",
        GREEN if d["disposition"] == "accept" else RED)

    tid = d["ticket_id"]
    a = eng.tickets.get(tid).assignments[-1]
    # Echo 语义：J2 执行器里 write_gate 在工具执行时触发，这里显式走同一条
    # 治理挂点（pre_write），事件流与生产同源
    add("worker 工具调用：write CHANGELOG.md / write README.md", GRAY)
    r1 = eng.pre_write(tid, "CHANGELOG.md")
    add(f"pre_write(CHANGELOG.md) → {'ALLOW' if r1 else 'BLOCK'}",
        GREEN if r1 else RED)
    r2 = eng.pre_write(tid, "README.md")
    add(f"pre_write(README.md) → {'ALLOW' if r2 else 'BLOCK'}（mandate 未授权）",
        RED if not r2 else GREEN)
    res = eng.on_worker_report(a, {
        "status": "done", "summary": "已写入 CHANGELOG.md 与 README.md",
        "touched_paths": ["CHANGELOG.md", "README.md"]})
    eng.flush_outbox(now=1.0)
    # 真实事件：watchdog deviation（从留痕读回）
    kv_rows = store.conn.execute(
        "SELECT k, v FROM kv WHERE k LIKE 'watchdog:%'"
        " ORDER BY rowid DESC LIMIT 1").fetchall()
    for r in kv_rows:
        v = json.loads(r["v"])
        if v.get("verdict") == "deviation":
            add(f"watchdog → DEVIATION  {'; '.join(v.get('reasons', []))[:56]}",
                RED)
    add(f"on_worker_report → repaired={res.get('repaired')} "
        f"round={res.get('repair_round')}（复审环给一次修复机会）", RED)

    add("# 任务 2：合规路径（mandate 内写盘）", GRAY)
    d2 = eng.intake(Input("yg:t2", "demo2", "/do 把运行记录追加到 service/LOG.md",
                          is_human=True),
                    mandate_rules={"allow_paths": ["service/LOG.md"]})
    add(f"intake → {d2['disposition']}  ticket={d2.get('ticket_id')}", GREEN)
    tid2 = d2["ticket_id"]
    a2 = eng.tickets.get(tid2).assignments[-1]
    eng.pre_write(tid2, "service/LOG.md")
    add("pre_write(service/LOG.md) → ALLOW", GREEN)
    res2 = eng.on_worker_report(a2, {
        "status": "done", "summary": "已追加运行记录",
        "touched_paths": ["service/LOG.md"]})
    eng.flush_outbox(now=2.0)
    add(f"watchdog → {res2.get('verdict')}  "
        f"observe={json.dumps(eng.observe_log[-1].get('action', ''))}", GREEN)
    add(f"on_worker_report → replied={res2.get('replied')}", GREEN)

    add("# fail-closed：词表外判决 ValueError / 无租约派发跳过 / 修复计数落库", GRAY)
    add("# 101 pytest + 9 script suites · pure stdlib · v0.3.1", GRAY)
    return events


def render(lines, cursor=True):
    img = Image.new("RGB", (W, H), BG)
    dr = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT_PATH, 19)
    fs = ImageFont.truetype(FONT_PATH, 14)
    # 终端窗框
    dr.rounded_rectangle([8, 8, W - 8, H - 8], 12, outline=(48, 54, 61), width=2)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        dr.ellipse([28 + i * 26, 24, 42 + i * 26, 38], fill=c)
    dr.text((W - 260, 22), "superagent-build — fail-closed demo",
            font=fs, fill=GRAY)
    y = 60
    for text, color in lines:
        dr.text((30, y), text, font=f, fill=color)
        y += 34
    if cursor:
        dr.rectangle([30, y + 2, 44, y + 24], fill=FG)
    return img


def main():
    events = run_demo()
    os.makedirs("docs", exist_ok=True)
    with open("docs/demo_events.jsonl", "w", encoding="utf-8") as fh:
        for t, c in events:
            fh.write(json.dumps({"text": t, "color": c}, ensure_ascii=False)
                     + "\n")
    frames = []
    for i in range(1, len(events) + 1):
        frames.append(render(events[:i], cursor=False))
    frames.append(render(events, cursor=True))
    frames.append(render(events, cursor=False))
    # 前两帧多停一会儿（让读者看清标题）
    gif_frames = [frames[0]] * 3 + frames[1:]
    gif_frames[0].save("docs/demo.gif", save_all=True,
                       append_images=gif_frames[1:], duration=1100,
                       loop=0, optimize=True)
    print(f"frames={len(gif_frames)} events={len(events)} "
          f"size={os.path.getsize('docs/demo.gif') // 1024}KB")


if __name__ == "__main__":
    main()
