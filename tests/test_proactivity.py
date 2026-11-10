"""test_proactivity.py 自检：每条断言都先覆盖违规/边界（RED）再验证放行（GREEN）。

约定沿用本仓其他测试：
- REDx   = 必须被拦 / 必须不发生的负向断言；
- GREENx = 合规输入必须给出的正向断言。
全部通过 → `python3 tests/test_proactivity.py` 退出码 0。

数值口径说明：
  开口判据不等式为 p·u_eff - (1-p)·c_false ≥ c_speak，
  解得 p* = (c_false + c_speak) / (u_eff + c_false)。
  代入 c_false=1.0, c_speak=0.2, u=10（t=0，无衰减）：
      p* = 1.2 / 11 = 0.10909090909090909。
  （注：建模任务书括注写 "→ p*=0.12"，那是 1.2/10 的口径，分母漏了 +c_false，
   与所述不等式解析解不一致；本测试以上述不等式解析解为准。）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.proactivity import (CandidateEvent, ProactivityConfig,  # noqa: E402
                               ProactivityEngine)


def main() -> int:
    failures: list[str] = []
    NOW = 1000.0

    def check(name: str, cond: bool) -> None:
        print(("PASS" if cond else "FAIL"), name)
        if not cond:
            failures.append(name)

    def expect_value_error(name: str, fn) -> None:
        """RED 辅助：调用必须抛 ValueError，否则判失败。"""
        try:
            fn()
            check(name, False)   # 没抛 = 门没拦住 = 失败
        except ValueError:
            check(name, True)

    def fixed_engine(cfg: ProactivityConfig, now: float = NOW) -> ProactivityEngine:
        return ProactivityEngine(cfg, clock=lambda: now)

    cfg = ProactivityConfig(lam=0.1, c_false=1.0, c_speak=0.2, observe=False)

    # ================= RED：负向 / 边界必须被拦 =================

    # RED1 确证度低于 p* 必不开口（wait）
    #   u=10、t=0 → p*=1.2/11≈0.109091；confidence=0.05 < p* → wait
    d = fixed_engine(cfg).evaluate(CandidateEvent("e_lowp", 10.0, 0.05, NOW))
    check("RED1 confidence<p* → wait（不开口）", d.action == "wait")

    # RED2 效用衰减到低于 c_speak → drop（无论确证度多高，drop 优先）
    #   u=0.1、t=0 → u_eff=0.1 < c_speak=0.2 → drop，即便 confidence=1.0
    d = fixed_engine(cfg).evaluate(CandidateEvent("e_drop", 0.1, 1.0, NOW))
    check("RED2 u_eff<c_speak 高确证度仍 drop", d.action == "drop")

    # RED3 observe 模式下永远不产生裸 "speak"
    cfg_obs = ProactivityConfig(lam=0.1, c_false=1.0, c_speak=0.2, observe=True)
    eng_obs = fixed_engine(cfg_obs)
    obs_actions = [
        eng_obs.evaluate(CandidateEvent("o1", 10.0, 0.9, NOW)).action,   # 本该 speak
        eng_obs.evaluate(CandidateEvent("o2", 10.0, 0.01, NOW)).action,  # 本该 wait
        eng_obs.evaluate(CandidateEvent("o3", 0.1, 1.0, NOW)).action,    # 本该 drop
    ]
    check("RED3 observe 模式所有 action 以 observe_ 开头",
          all(a.startswith("observe_") for a in obs_actions))
    check("RED3b observe 模式无裸 speak/wait/drop",
          all(a not in ("speak", "wait", "drop") for a in obs_actions))

    # RED4 效用为负必须抛 ValueError
    expect_value_error(
        "RED4 utility<0 抛 ValueError",
        lambda: fixed_engine(cfg).evaluate(CandidateEvent("bad", -1.0, 0.5, NOW)))

    # RED5 确证度越界（<0 或 >1）必须抛 ValueError
    expect_value_error(
        "RED5a confidence<0 抛 ValueError",
        lambda: fixed_engine(cfg).evaluate(CandidateEvent("bad", 10.0, -0.01, NOW)))
    expect_value_error(
        "RED5b confidence>1 抛 ValueError",
        lambda: fixed_engine(cfg).evaluate(CandidateEvent("bad", 10.0, 1.01, NOW)))

    # ================= GREEN：合规输入必须给出正确结果 =================

    # GREEN1 确证度高于 p* 且效用足 → speak
    d = fixed_engine(cfg).evaluate(CandidateEvent("e_highp", 10.0, 0.5, NOW))
    check("GREEN1 confidence≥p* 且效用足 → speak", d.action == "speak")

    # GREEN1b 恰好等于 p* 也算开口（判据是 ≥）
    ps_eq = (1.0 + 0.2) / (10.0 + 1.0)
    d = fixed_engine(cfg).evaluate(CandidateEvent("e_eq", 10.0, ps_eq, NOW))
    check("GREEN1b confidence==p* 边界 → speak", d.action == "speak")

    # GREEN2 同一事件时间越晚 u_eff 越小（单调性）
    holder = {"now": NOW}
    eng_mono = ProactivityEngine(cfg, clock=lambda: holder["now"])
    ev = CandidateEvent("e_mono", 10.0, 0.9, NOW)
    d_early = eng_mono.evaluate(ev)
    holder["now"] = NOW + 30.0
    d_late = eng_mono.evaluate(ev)
    check("GREEN2 时间越晚 u_eff 越小",
          d_late.u_effective < d_early.u_effective)

    # GREEN3 p*(t) 手算值与公式一致（精确值逐字对照）
    #   c_false=1.0, c_speak=0.2, u=10, t=0 → p* = 1.2/11 = 0.10909090909090909
    eng_ps = fixed_engine(cfg)
    d = eng_ps.evaluate(CandidateEvent("e_ps", 10.0, 0.5, NOW))
    hand = 1.2 / 11.0
    check("GREEN3 p* 精确值 == 1.2/11", abs(d.p_star - hand) < 1e-12)
    check("GREEN3b 决策 p_star 与引擎公式一致",
          abs(d.p_star - eng_ps.p_star(d.u_effective)) < 1e-12)

    # GREEN4 Decision.reason 必须含 p*、u_eff、confidence 三个数（可审计）
    reason_ok = (f"p*={d.p_star:.6f}" in d.reason
                 and f"u_eff={d.u_effective:.6f}" in d.reason
                 and f"confidence={0.5:.6f}" in d.reason)
    check("GREEN4 speak 分支 reason 含 p*/u_eff/confidence", reason_ok)

    #   drop 分支的 reason 也必须含三数
    d_drop = fixed_engine(cfg).evaluate(CandidateEvent("e_pd", 0.1, 1.0, NOW))
    drop_reason_ok = (f"p*={d_drop.p_star:.6f}" in d_drop.reason
                      and f"u_eff={d_drop.u_effective:.6f}" in d_drop.reason
                      and f"confidence={1.0:.6f}" in d_drop.reason)
    check("GREEN4b drop 分支 reason 也含三数", drop_reason_ok)

    # GREEN5 observe 模式把三种真实动作映射为 observe_ 前缀
    check("GREEN5 observe 映射 speak/wait/drop 为 observe_*",
          obs_actions == ["observe_speak", "observe_wait", "observe_drop"])

    # GREEN6 log 记录全部判定（含 observe 期间）
    n_before = len(eng_obs.log)
    eng_obs.evaluate(CandidateEvent("o4", 10.0, 0.9, NOW))
    check("GREEN6 log 追加记录", len(eng_obs.log) == n_before + 1)

    # GREEN7 内置 selftest 通过（返回 0）
    check("GREEN7 engine.selftest()==0",
          ProactivityEngine(ProactivityConfig()).selftest() == 0)

    print("FAILURES:", failures if failures else "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
