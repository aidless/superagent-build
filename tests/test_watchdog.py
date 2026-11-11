"""watchdog.py 自检：每条断言都先注入违规（RED）再验证放行（GREEN）。

覆盖任务硬纪律要求的 7 项：
- 越界路径必红、forbid 命中必红、无 summary 自称 done 必红（UNASSESSABLE 不是 OK）、
  自述额外工作必红、合规产出全绿、judge 判 deviation 时规则层全过也红（一票否决）、
  judge 缺位时规则层独立可用。
"""
import os
import sys

# 把 superagent-build 放进 sys.path，才能 import agent.*（与 test_store.py 一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.model import Mandate  # noqa: E402
from agent.watchdog import (  # noqa: E402
    ScriptedJudge,
    Watchdog,
    WatchdogVerdict,
)


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(("PASS" if cond else "FAIL"), name)
        if not cond:
            failures.append(name)

    # 契约：allow src 全树 + docs 树；forbid 密钥文件
    mandate = Mandate(
        task_text="给 src 补测试，禁止碰密钥",
        rules={"allow_paths": ["src/**", "docs"], "forbid": ["src/secret.py"]},
    )
    wd = Watchdog()  # judge=None：规则层独立可用

    # ---- 硬纪律 1：越界路径必红 ----
    r = wd.review(mandate, {"touched_paths": ["lib/evil.py"], "status": "done",
                            "summary": "done"})
    check("RED1 越界路径必红 → DEVIATION", r.verdict is WatchdogVerdict.DEVIATION)
    check("RED1b 越界写进 rule_findings", any("lib/evil.py" in x for x in r.rule_findings))

    # ---- 硬纪律 2：forbid 命中必红（即便在 allow 树里）----
    r = wd.review(mandate, {"touched_paths": ["src/secret.py"], "status": "done",
                            "summary": "done"})
    check("RED2 forbid 命中必红 → DEVIATION", r.verdict is WatchdogVerdict.DEVIATION)

    # ---- 硬纪律 3：无 summary 自称 done 必红（UNASSESSABLE 不是 OK）----
    r = wd.review(mandate, {"touched_paths": ["src/a.py"], "status": "done"})
    check("RED3 无 summary 自称 done → UNASSESSABLE", r.verdict is WatchdogVerdict.UNASSESSABLE)
    check("RED3b UNASSESSABLE 不是 OK", r.verdict is not WatchdogVerdict.OK)
    # summary 空串同样算无证据
    r = wd.review(mandate, {"touched_paths": ["src/a.py"], "status": "done", "summary": "   "})
    check("RED3c 空 summary → UNASSESSABLE", r.verdict is WatchdogVerdict.UNASSESSABLE)

    # ---- 硬纪律 4：自述额外工作必红，理由取 extra_work ----
    r = wd.review(mandate, {"touched_paths": ["src/a.py"], "status": "done", "summary": "ok",
                            "declares_extra_work": True, "extra_work": "顺手改了部署脚本"})
    check("RED4 自述额外工作必红 → DEVIATION", r.verdict is WatchdogVerdict.DEVIATION)
    check("RED4b 理由取 extra_work", any("顺手改了部署脚本" in x for x in r.reasons))

    # ---- 硬纪律 5：合规产出全绿 ----
    r = wd.review(mandate, {"touched_paths": ["src/a.py", "src/sub/b.py", "docs/guide.md"],
                            "status": "done", "summary": "全部补齐"})
    check("GREEN1 合规产出全绿 → OK", r.verdict is WatchdogVerdict.OK)
    check("GREEN1b 合规无 rule_findings", r.rule_findings == [])

    # ---- 硬纪律 6：judge 判 deviation，规则层全过也红（一票否决）----
    judge = ScriptedJudge({"drift": (WatchdogVerdict.DEVIATION, ["产出偏离任务目标"])})
    r = Watchdog(judge=judge).review(
        mandate, {"touched_paths": ["src/a.py"], "status": "done", "summary": "ok",
                  "scenario": "drift"})
    check("RED5 judge 一票否决 → DEVIATION", r.verdict is WatchdogVerdict.DEVIATION)
    check("RED5b judge 理由进入 reasons", any("产出偏离任务目标" in x for x in r.reasons))

    # ---- 硬纪律 7：judge 缺位时规则层独立可用 ----
    r = Watchdog(judge=None).review(
        mandate, {"touched_paths": ["lib/x.py"], "status": "done", "summary": "ok"})
    check("GREEN2 judge 缺位规则层独立判红", r.verdict is WatchdogVerdict.DEVIATION)

    # ---- 附加：更多正交边界 ----
    # judge 判 OK 不改变规则层放行
    r = Watchdog(judge=ScriptedJudge({"fine": (WatchdogVerdict.OK, [])})).review(
        mandate, {"touched_paths": ["src/a.py"], "status": "done", "summary": "ok",
                  "scenario": "fine"})
    check("GREEN3 judge OK 保持放行", r.verdict is WatchdogVerdict.OK)

    # judge 与规则层同判 deviation：取更严重（仍 deviation），且两层理由都在
    judge2 = ScriptedJudge({"both": (WatchdogVerdict.DEVIATION, ["judge 层补充理由"])})
    r = Watchdog(judge=judge2).review(
        mandate, {"touched_paths": ["lib/x.py"], "status": "done", "summary": "ok",
                  "scenario": "both"})
    check("RED6 双层都红 → DEVIATION", r.verdict is WatchdogVerdict.DEVIATION)
    check("RED6b 规则层与 judge 理由合并", any("lib/x.py" in x for x in r.reasons)
          and any("judge 层补充理由" in x for x in r.reasons))

    # 语义变更（v0.3.3 §8-3 收口，详见 docs/spec/GOVERNANCE.md）：
    # 旧 GREEN4 断言「judge=None + rules_check=False → 任意 payload 判 OK」，
    # 这正是构造级 fail-open。契约升级后该构造 MUST 被拒；跳过规则层的
    # 合法用法是「带 judge + rules_check=False」，下面两条分别验红/验绿。
    rejected = False
    try:
        Watchdog(judge=None, rules_check=False)
    except ValueError as e:
        rejected = "no_checks" in str(e)
    check("RED8 无判据构造被拒（fail-closed）", rejected)
    r = Watchdog(judge=ScriptedJudge({"fine": (WatchdogVerdict.OK, [])}),
                 rules_check=False).review(
        mandate, {"touched_paths": ["lib/x.py"], "status": "done",
                  "scenario": "fine"})
    check("GREEN4 带 judge 时 rules_check=False 跳过规则层",
          r.verdict is WatchdogVerdict.OK and r.rule_findings == [])

    # 前缀树精确性：docs 只匹配 docs 树，不误伤 docs2
    r = wd.review(mandate, {"touched_paths": ["docs2/x.py"], "status": "done", "summary": "ok"})
    check("RED7 docs 不误放 docs2 → DEVIATION", r.verdict is WatchdogVerdict.DEVIATION)

    # 自带 selftest 也要全绿
    check("GREEN5 内置 selftest 返回 0", Watchdog().selftest() == 0)

    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
