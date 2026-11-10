"""M2 集成：Watchdog 挂进 engine.on_worker_report。
RED：worker 越权路径的 report 必须被判 deviation、票终止、落判决留痕，且不发回复。
GREEN：合规 report 正常回复出站。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.admission import AdmissionConfig  # noqa: E402
from agent.engine import Engine  # noqa: E402
from agent.executor import EchoExecutor  # noqa: E402
from agent.model import Input  # noqa: E402
from agent.store import Store  # noqa: E402
from agent.watchdog import Watchdog  # noqa: E402


class RepoExecutor(EchoExecutor):
    """占位执行器（报告由测试直接注入 engine.on_worker_report）。"""


def run(scenario: str, allow_paths: list[str]):
    sent: list[tuple[str, str]] = []
    store = Store()
    eng = Engine(store, AdmissionConfig(whitelist=["alice"]), RepoExecutor(),
                 lambda ch, body: (sent.append((ch, body)), True)[1],
                 watchdog=Watchdog(),
                 max_repairs=0)  # M2 契约：首次 deviation 即终止（复审环语义见 test_i1_i4）
    tid = eng.intake(Input("alice:t1", "t1", "/do task", is_human=True))["ticket_id"]
    t = eng.tickets.get(tid)
    t.mandate.rules["allow_paths"] = allow_paths
    result = eng.on_worker_report(t.assignments[-1], {
        "status": "done", "summary": "x",
        "touched_paths": ["src/main.py"] if scenario == "normal" else ["/etc/passwd"],
        "scenario": scenario})
    return eng, store, sent, tid, result


def main() -> int:
    failures = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL"), name)
        if not cond:
            failures.append(name)

    # RED1: 越权路径 → deviation，票终止，无回复出站
    eng, store, sent, tid, res = run("rogue", ["src/**"])
    check("M2-RED1 越权判 deviation", res["verdict"] == "deviation")
    check("M2-RED1b 不回复", res["replied"] is False)
    check("M2-RED1c 票已终止", eng.tickets.get(tid).status.value == "ended")

    # RED2: 判定留痕真实落库（kv 表里能查到 deviation 记录）——恒真断言是装饰
    rows = store.conn.execute(
        "SELECT k, v FROM kv WHERE k LIKE 'watchdog:%'").fetchall()
    check("M2-RED2 watchdog 留痕落库", len(rows) >= 1 and
          any('"deviation"' in r["v"] for r in rows))

    # GREEN1: 合规 → ok，回复出站
    eng2, store2, sent2, tid2, res2 = run("normal", ["src/**"])
    check("M2-GREEN1 合规 ok", res2["verdict"] == "ok")
    eng2.flush_outbox(now=1.0)
    check("M2-GREEN1b 回复出站", any(tid2 in b for _, b in sent2))

    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
