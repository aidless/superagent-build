"""store.py 自检：每条断言都先注入违规（RED）再验证放行（GREEN）。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.store import Store  # noqa: E402


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool):
        print(("PASS" if cond else "FAIL"), name)
        if not cond:
            failures.append(name)

    # RED1: 先入队后登记 receipt —— 必须拒绝（先存后做被破坏要当场炸）
    s = Store()
    try:
        s.enqueue("m1", "no-such-receipt", "im", "hello")
        check("RED1 enqueue-before-receipt 拒绝", False)
    except ValueError:
        check("RED1 enqueue-before-receipt 拒绝", True)

    # GREEN1: 先 receipt 后 enqueue，正常入队
    first = s.record_receipt("r1", "p")
    ok = s.enqueue("m1", "r1", "im", "hello")
    check("GREEN1 receipt→enqueue", first and ok)

    # RED2: 重复 receipt 幂等（返回 False，不炸也不重复副作用）
    dup = s.record_receipt("r1", "p")
    check("RED2 重复 receipt 幂等返回 False", dup is False)

    # RED3: 非法放弃原因必须拒绝
    try:
        s.abandon("m1", "meh")
        check("RED3 非法 abandon 原因拒绝", False)
    except ValueError:
        check("RED3 非法 abandon 原因拒绝", True)

    # GREEN2: 合法放弃 channelGone 放行
    s.abandon("m1", "channelGone")
    check("GREEN2 channelGone 放弃放行",
          s.pending_outbox_count() == 0)

    # GREEN3: 退避重试后 due 才可见
    s.record_receipt("r2")
    s.enqueue("m2", "r2", "im", "x")
    check("GREEN3a next_try_at=0 可见", len(s.due(now=100)) == 1)
    s.mark_retry("m2", next_try_at=1000)
    check("GREEN3b 未到期不可见", s.due(now=100) == [])
    check("GREEN3c 到期可见", len(s.due(now=1000)) == 1)

    # GREEN4: 落盘库重开，pending 仍在（崩溃恢复语义）
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "a.db")
        s2 = Store(p)
        s2.record_receipt("r3")
        s2.enqueue("m3", "r3", "im", "y")
        s2.conn.close()
        s3 = Store(p)
        check("GREEN4 重开库 pending 仍在", s3.pending_outbox_count() == 1)

    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
