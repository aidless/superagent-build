"""admission + outbox 自检：每条断言先注入违规（RED）再验证放行（GREEN）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.model import Assessment, Input  # noqa: E402
from agent.admission import (AdmissionConfig, decide, gate,  # noqa: E402
                             validate_assessment)
from agent.store import Store  # noqa: E402
from agent.outbox import ChannelGone, pump  # noqa: E402

CFG = AdmissionConfig(whitelist=["alice"], explicit_markers=("/do", "@agent"))


def main() -> int:
    failures: list[str] = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL"), name)
        if not cond:
            failures.append(name)

    # ---- 准入 ----
    # RED1: bot 消息进不来
    d, _ = decide(Input("bot:x1", "t1", "hi", is_human=False, is_bot=True), CFG)
    check("RED1 bot 门拒绝", d == "rejected_bot")

    # RED2: 白名单外 mention 不接（broadcast → fail-closed）
    d, _ = decide(Input("eve:t1", "t1", "please fix", is_human=True), CFG)
    check("RED2 白名单外 fail-closed", d == "rejected_eval")

    # RED3: 无研判层时 mention 不接
    d, _ = decide(Input("alice:t1", "t1", "please fix the bug", is_human=True), CFG)
    check("RED3 mention 无研判层 fail-closed", d == "rejected_eval")

    # GREEN1: 显式指令直通
    d, a = decide(Input("alice:t1", "t1", "/do fix the bug", is_human=True), CFG)
    check("GREEN1 explicit 直通", d == "accept" and a is not None and a.accepted)

    # GREEN2: follow_up 优先于一切（同线程开放 ticket 的人消息不开新 eval）
    d, _ = decide(Input("alice:t1", "t1", "/do more", is_human=True), CFG,
                  thread_open_tickets={"t1": "k1"})
    check("GREEN2 follow_up 不开新 eval", d == "follow_up")

    # GREEN3: 研判层放行 mention（证据齐全）
    assessor = lambda inp, g: Assessment(True, "whitelist_user", intent_evidence="user asked fix")
    d, a = decide(Input("alice:t1", "t1", "please fix the bug", is_human=True),
                  CFG, assessor=assessor)
    check("GREEN3 mention 经研判放行", d == "accept")

    # RED4: 研判幻觉规则名必须被硬校验打掉
    bad = Assessment(True, "made_up_rule", intent_evidence="x")
    ok, reason = validate_assessment(bad, gate(Input("alice:t1", "t1", "help", True), CFG))
    check("RED4 幻觉规则名拒绝", not ok and reason.startswith("hallucinated_rule"))

    # RED5: accept 无证据拒绝
    bad2 = Assessment(True, "whitelist_user", intent_evidence="")
    ok, reason = validate_assessment(bad2, gate(Input("alice:t1", "t1", "help", True), CFG))
    check("RED5 accept 无证据拒绝", not ok and reason == "accept_without_evidence")

    # RED6: broadcast 上 accept 拒绝
    ok, reason = validate_assessment(
        Assessment(True, "whitelist_user", intent_evidence="x"),
        gate(Input("eve:t1", "t1", "help", True), CFG))
    check("RED6 broadcast 上 accept 拒绝", not ok)

    # ---- outbox 三角 ----
    s = Store()
    delivered: list[tuple[str, str]] = []

    def deliver_ok(ch, body):
        delivered.append((ch, body))
        return True

    # RED7: 重复 messageId 幂等（第二次 enqueue 返回 False）
    s.record_receipt("r1")
    first = s.enqueue("m1", "r1", "im", "a")
    second = s.enqueue("m1", "r1", "im", "a")
    check("RED7 重复 messageId 幂等", first and not second)

    # GREEN4: pump 投递并 mark_sent
    stats = pump(s, now=10, deliver=deliver_ok)
    check("GREEN4 投递成功", stats["sent"] == 1 and s.due(10) == [])

    # GREEN5: 投递失败退避，5s 基线
    s.record_receipt("r2")
    s.enqueue("m2", "r2", "im", "b")
    stats = pump(s, now=10, deliver=lambda ch, body: False)
    check("GREEN5 失败退避", stats["retried"] == 1)
    check("GREEN5b 5 秒内不可见", s.due(now=14) == [] and len(s.due(now=15)) == 1)

    # GREEN6: channelGone 合法放弃
    s.record_receipt("r3")
    s.enqueue("m3", "r3", "gone-ch", "c")

    def deliver_gone(ch, body):
        if ch == "gone-ch":
            raise ChannelGone()
        return False  # 其他通道继续退避

    stats = pump(s, now=100, deliver=deliver_gone)
    due_ids = [m["message_id"] for m in s.due(100)]
    check("GREEN6 channelGone 放弃",
          stats["abandoned"] == 1 and "m3" not in due_ids)  # m2 仍在合法退避，属预期

    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
