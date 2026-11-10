"""M1 集成：governance 内嵌进 engine（claim 预声明 + pre_write 拦截 + 判决流）。
RED：Mandate 未声明该路径 → pre_write BLOCK 且落判决流。
GREEN：allow_paths 内的写放行；越权 watchdog 报告走判决流。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.admission import AdmissionConfig  # noqa: E402
from agent.engine import Engine  # noqa: E402
from agent.executor import EchoExecutor  # noqa: E402
from agent.governance import ScopeRegistry  # noqa: E402
from agent.model import Input  # noqa: E402
from agent.store import Store  # noqa: E402
from agent.watchdog import Watchdog  # noqa: E402


def main() -> int:
    failures = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL"), name)
        if not cond:
            failures.append(name)

    sent: list[tuple[str, str]] = []
    store = Store()
    with tempfile.TemporaryDirectory() as td:
        gov = ScopeRegistry(state_dir=td)
        eng = Engine(store, AdmissionConfig(whitelist=["alice"]), EchoExecutor(),
                     lambda ch, body: (sent.append((ch, body)), True)[1],
                     watchdog=Watchdog(), governance=gov)

        # Mandate 带 allow_paths → intake 时治理预 claim
        tid = eng.intake(Input("alice:t1", "t1", "/do refactor src", is_human=True),
                         mandate_rules={"allow_paths": ["src/**"]})["ticket_id"]
        t = eng.tickets.get(tid)

        # RED1: 未在声明范围内的写被拦
        check("M1-RED1 范围外写 BLOCK", eng.pre_write(tid, "docs/leak.md") is False)
        # GREEN1: 范围内放行
        check("M1-GREEN1 范围内放行", eng.pre_write(tid, "src/main.py") is True)

        # RED2: BLOCK 落了判决流（读回验证）
        rulings = gov.read_rulings()
        check("M1-RED2 BLOCK 判决留痕",
              any(r.get("verdict") == "BLOCK" and r.get("path") == "docs/leak.md"
                  for r in rulings))

        # RED3: 双 worker 重叠作用域报警（conflicts 可见）
        gov.claim("k-other", ["src/**"])
        overlaps = [c for c in gov.conflicts() if tid in (c[0], c[1])]
        check("M1-RED3 重叠作用域报警", len(overlaps) >= 1)

        # 落盘文件确实存在
        check("M1-GREEN2 落盘态文件存在",
              os.path.exists(os.path.join(td, "scopes.json")) and
              os.path.exists(os.path.join(td, "rulings.jsonl")))

    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
