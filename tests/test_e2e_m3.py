"""M3 集成：Proactivity 挂进 engine（任务完成事件 → 开口判定）。
observe 模式（默认）下：判定留痕、永不真开口（RED），合规记录三数（GREEN）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.admission import AdmissionConfig  # noqa: E402
from agent.engine import Engine  # noqa: E402
from agent.executor import EchoExecutor  # noqa: E402
from agent.model import Input  # noqa: E402
from agent.proactivity import CandidateEvent, ProactivityConfig, ProactivityEngine  # noqa: E402
from agent.store import Store  # noqa: E402
from agent.watchdog import Watchdog  # noqa: E402


def main() -> int:
    failures = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL"), name)
        if not cond:
            failures.append(name)

    def build_engine():
        sent = []
        store = Store()
        eng = Engine(store, AdmissionConfig(whitelist=["alice"]), EchoExecutor(),
                     lambda ch, body: (sent.append((ch, body)), True)[1],
                     watchdog=Watchdog(),
                     proactivity=ProactivityEngine(ProactivityConfig(observe=True)))
        return eng, store, sent

    # 全链路：显式指令 → 报告 → proactivity 判定留痕
    eng, store, sent = build_engine()
    tid = eng.intake(Input("alice:t1", "t1", "/do task", is_human=True))["ticket_id"]
    t = eng.tickets.get(tid)
    eng.on_worker_report(t.assignments[-1], {
        "status": "done", "summary": "did it", "touched_paths": [], "scenario": "normal"})

    # RED1: observe 模式下无裸 speak
    actions = [d.action for d in eng.proactivity.log]
    check("M3-RED1 observe 无裸 speak", len(actions) >= 1 and
          all(a.startswith("observe_") for a in actions))

    # GREEN1: 判定留痕且 reason 可审计（含三数）
    d = eng.proactivity.log[-1]
    check("M3-GREEN1 判定留痕",
          d.event_id == f"done:{tid}" and d.p_star > 0 and d.reason.count("p*") >= 1)

    # GREEN2: 直接调用引擎的判定数学（对照手算 1.2/11）
    from agent.proactivity import ProactivityConfig as PC
    pe = ProactivityEngine(PC(observe=False, lam=0.0, c_false=1.0, c_speak=0.2))
    dec = pe.evaluate(CandidateEvent("e1", utility=10.0, confidence=0.95, created_at=0.0))
    import math
    check("M3-GREEN2 p* 手算一致", math.isclose(dec.p_star, 1.2 / 11.0, rel_tol=1e-12))
    check("M3-GREEN2b 高确证度开口", dec.action == "speak")

    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
