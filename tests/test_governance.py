"""governance 内嵌库自检：每条断言先注入违规（RED）再验证放行（GREEN）。

运行：python3 tests/test_governance.py  退出码 0 = 全过。
精确值纪律：断言里的 pattern / 路径 / ticket id / 判决 id 全部逐字复制自模块契约，
禁止凭记忆重打或近似。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.governance import CheckResult, ScopeRegistry  # noqa: E402


def main() -> int:
    failures: list[str] = []

    def check(name, cond):
        print(("PASS" if cond else "FAIL"), name)
        if not cond:
            failures.append(name)

    # ========== 内存态（state_dir=None） ==========

    reg = ScopeRegistry()
    reg.claim("T1", ["src/a/**", "docs"])

    # RED1: 未声明作用的 ticket 直接写 → 必须 BLOCK
    r = reg.check("T2", "src/a/foo.ts")
    check("RED1 未声明 ticket 写被拦",
          r.verdict == "BLOCK" and "无活动作用域声明" in r.reason)

    # RED2: 已声明 ticket 越界写 → 必须 BLOCK
    r = reg.check("T1", "src/b/leak.ts")
    check("RED2 越界写被拦", r.verdict == "BLOCK")

    # RED3: 目录前缀边界——pattern "docs" 不得命中 "docsx/a.md" → 必须 BLOCK
    r = reg.check("T1", "docsx/a.md")
    check("RED3 目录前缀边界被拦", r.verdict == "BLOCK")

    # GREEN1: 声明范围内精确路径 → ALLOW
    r = reg.check("T1", "src/a/foo.ts")
    check("GREEN1 范围内精确路径放行", r.verdict == "ALLOW")

    # GREEN2: pattern "src/a/**" 覆盖子树 → ALLOW
    r = reg.check("T1", "src/a/sub/x.ts")
    check("GREEN2 ** 前缀树放行", r.verdict == "ALLOW")

    # GREEN3: pattern "docs" 目录前缀命中 "docs/a.md" → ALLOW
    r = reg.check("T1", "docs/a.md")
    check("GREEN3 目录前缀放行", r.verdict == "ALLOW")

    # RED4: 两 ticket 模式重叠 → conflicts 报警（精确对 + 精确 pattern）
    reg.claim("T2", ["src/a/sub/**"])
    conf = reg.conflicts()
    check("RED5 重叠报警",
          ("T1", "T2", ["src/a/**", "src/a/sub/**"]) in [(a, b, p) for a, b, p in conf])

    # GREEN4: 无重叠不报警
    reg.release("T2")
    reg.claim("T2", ["src/c/**"])
    check("GREEN4 无重叠不报警", reg.conflicts() == [])

    # RED5: release 后写 → 必须 BLOCK（无活动声明）
    reg.release("T2")
    r = reg.check("T2", "src/c/x.ts")
    check("RED4(续) release 后写被拦", r.verdict == "BLOCK")

    # GREEN5: 同 ticket 重复 claim = 覆盖（旧作用域失效、新作用域生效）
    reg.claim("T1", ["src/c/**"])
    old_ok = reg.check("T1", "src/a/foo.ts").verdict == "BLOCK"
    new_ok = reg.check("T1", "src/c/new.ts").verdict == "ALLOW"
    check("GREEN5 重复 claim 覆盖", old_ok and new_ok)

    # RED6: 覆盖后旧作用域写 → 必须 BLOCK
    check("RED6 覆盖后旧作用域被拦",
          reg.check("T1", "src/a/foo.ts").verdict == "BLOCK")

    # ========== 判决流（内存态读回 + append-only） ==========

    reg.ruling({"id": "R1", "verdict": "supported", "why": "证据齐全"})
    reg.ruling({"id": "R2", "verdict": "needs-work", "why": "缺 file:line 引用"})
    rulings = reg.read_rulings()
    check("GREEN6 判决流可读回且保序",
          len(rulings) == 2 and rulings[0]["id"] == "R1"
          and rulings[0]["verdict"] == "supported"
          and rulings[1]["id"] == "R2"
          and rulings[1]["verdict"] == "needs-work")

    # ========== 落盘态（tempfile state_dir） ==========

    tmp = tempfile.mkdtemp()
    try:
        reg_a = ScopeRegistry(state_dir=tmp)
        reg_a.claim("T1", ["src/a/**", "docs"])
        reg_a.ruling({"id": "R1", "verdict": "supported", "why": "证据齐全"})

        # 落盘文件确实存在
        check("GREEN7 scopes.json + rulings.jsonl 落盘",
              os.path.exists(os.path.join(tmp, "scopes.json"))
              and os.path.exists(os.path.join(tmp, "rulings.jsonl")))

        # RED7: 新实例加载同一 state_dir，未声明 ticket 写 → BLOCK
        reg_b = ScopeRegistry(state_dir=tmp)
        check("RED7 持久化后未声明 ticket 被拦",
              reg_b.check("T9", "src/a/foo.ts").verdict == "BLOCK")

        # GREEN8: 新实例加载同一 state_dir，声明范围内写 → ALLOW
        check("GREEN8 持久化后范围内写放行",
              reg_b.check("T1", "src/a/foo.ts").verdict == "ALLOW"
              and reg_b.check("T1", "docs/a.md").verdict == "ALLOW")

        # GREEN9: 判决流 append-only——追加后旧字节前缀逐字不变
        path_r = os.path.join(tmp, "rulings.jsonl")
        before = open(path_r, "rb").read()
        first = json.loads(before.decode("utf-8").splitlines()[0])
        reg_b.ruling({"id": "R2", "verdict": "needs-work", "why": "缺 file:line 引用"})
        after = open(path_r, "rb").read()
        first_after = json.loads(after.decode("utf-8").splitlines()[0])
        check("GREEN9 append-only 旧条目不可变",
              after.startswith(before) and len(after) > len(before)
              and first == first_after
              and first["id"] == "R1" and first["verdict"] == "supported")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    # ========== 模块自带 selftest 独立跑一遍 ==========

    check("GREEN10 内嵌 selftest() 返回 0", ScopeRegistry().selftest() == 0)

    # 类型契约冒烟：CheckResult 字段名与字面值
    cr = CheckResult("ALLOW", "x")
    check("GREEN11 CheckResult 契约字段", cr.verdict == "ALLOW" and cr.reason == "x")

    print(f"\n共 {len(failures)} 条失败" if failures else "\nALL PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
