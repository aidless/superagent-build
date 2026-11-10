"""M0 端到端：一条显式指令 → 建票派工 → worker 报告 → 回复出站。
含崩溃恢复与幂等重放两个可靠性场景。暂不挂 watchdog/governance（后续里程碑集成）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.admission import AdmissionConfig  # noqa: E402
from agent.engine import Engine  # noqa: E402
from agent.executor import EchoExecutor  # noqa: E402
from agent.model import Input  # noqa: E402
from agent.store import Store  # noqa: E402


def main() -> int:
    failures: list[str] = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL"), name)
        if not cond:
            failures.append(name)

    sent: list[tuple[str, str]] = []

    def deliver(ch, body):
        sent.append((ch, body))
        return True

    store = Store()
    reports: list = []
    ex = EchoExecutor(on_report=lambda a, p: reports.append((a, p)))
    eng = Engine(store, AdmissionConfig(whitelist=["alice"]), ex, deliver)

    # E2E-1: 显式指令全链路
    r = eng.intake(Input("alice:issue-1", "issue-1", "/do fix the login bug", is_human=True))
    check("E2E1 accept 建票", r["disposition"] == "accept")
    tid = r["ticket_id"]
    # executor 的 Echo 回调在 deliver 时触发 → engine.on_worker_report
    for a, payload in reports:
        eng.on_worker_report(a, payload)
    stats = eng.flush_outbox(now=1.0)
    check("E2E1b 回复出站", stats["sent"] >= 1 and
          any(tid in body for _, body in sent))

    # E2E-2: 同消息重放幂等
    r2 = eng.intake(Input("alice:issue-1", "issue-1", "/do fix the login bug", is_human=True))
    check("E2E2 同消息幂等", r2["disposition"] == "duplicate")

    # E2E-3: follow-up 附加到既有票（注意用不同 input id——同 id 会被幂等判重）
    r3 = eng.intake(Input("alice:issue-1-fu", "issue-1", "also add a test", is_human=True))
    check("E2E3 follow_up 附加", r3["disposition"] == "follow_up"
          and r3["ticket_id"] == tid)

    # E2E-4: 崩溃恢复——重开库 pending 效果仍在
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "e2e.db")
        s1 = Store(p)
        eng1 = Engine(s1, AdmissionConfig(whitelist=["alice"]), EchoExecutor(), deliver)
        ex2 = EchoExecutor(on_report=lambda a, pay: eng1.on_worker_report(a, pay))
        eng1.executor = ex2  # 报告回调接上 engine，回复才会进出站队列
        rr = eng1.intake(Input("alice:issue-9", "issue-9", "/do task A", is_human=True))
        s1.conn.close()
        s2 = Store(p)
        eng2 = Engine(s2, AdmissionConfig(whitelist=["alice"]), ex2, deliver)
        check("E2E4 崩溃后 pending 可恢复", eng2.resume() >= 1)

    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
