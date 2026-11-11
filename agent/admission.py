"""两级准入：第一级确定性规则 gate，第二级模型研判（带硬校验）。

对齐原型 policy.ts / admission.ts 的语义（见 superagent-dig/01）：
- explicit：白名单用户 + 明确 @/指令 → 直接显式接单；
- mention：白名单用户提到但未显式指令 → 需研判；
- broadcast：非定向 → 默认忽略（可被 observe 模式记录）；
- bot 门：bot/CI 消息一律不进 eval（机器门 machineGate）；
- follow-up：开放 ticket 所在线程上、同一个人的后续消息 → 不开新 eval，
  直接作为该 ticket 的输入（原型 policy.ts:206-207）。

第二级研判产 Assessment，硬校验（原型 policy.ts:40-61 的铁律）：
1. matched_rule 必须恰好命中一条规则（0 条或 2 条都拒）；
2. accept 必须 intent_evidence 非空；
3. 研判声明的规则必须真实存在于 gate 输出里（防幻觉规则名）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .model import Assessment, Input


# ---------- 配置 ----------

@dataclass
class AdmissionConfig:
    whitelist: list[str] = field(default_factory=list)   # 用户 id 白名单
    explicit_markers: tuple[str, ...] = ("/do", "/task", "@agent")  # 显式指令前缀
    bot_allowlist: list[str] = field(default_factory=list)  # 允许进来的 bot（默认全拒）


# ---------- 第一级：规则 gate ----------

@dataclass
class GateResult:
    route: str          # explicit / mention / broadcast / bot / follow_up
    matched_rules: list[str]
    open_ticket_id: Optional[str] = None  # follow_up 命中的开放 ticket


def gate(inp: Input, cfg: AdmissionConfig,
         thread_open_tickets: Optional[dict[str, str]] = None) -> GateResult:
    """thread_open_tickets: thread_key -> 开放 ticket id（无则为空 dict）。"""
    thread_open_tickets = thread_open_tickets or {}

    # bot 门：机器消息不进 eval，除非显式白名单
    if inp.is_bot and inp.thread_key not in cfg.bot_allowlist:
        return GateResult("bot", [])

    # follow-up：同线程已有开放 ticket 的人消息 → 附加到既有 ticket
    if inp.is_human and inp.thread_key in thread_open_tickets:
        return GateResult("follow_up", ["follow_up"],
                          open_ticket_id=thread_open_tickets[inp.thread_key])

    if not inp.is_human:
        return GateResult("bot", [])

    whitelisted = inp.id.split(":", 1)[0] in cfg.whitelist or inp.thread_key in cfg.whitelist
    if not whitelisted:
        return GateResult("broadcast", [])

    is_explicit = any(inp.text.startswith(m) or f" {m}" in inp.text
                      for m in cfg.explicit_markers)
    if is_explicit:
        return GateResult("explicit", ["whitelist_user", "explicit_marker"])
    return GateResult("mention", ["whitelist_user"])


# ---------- 第二级：研判硬校验 ----------

def validate_assessment(a: Assessment, gate_result: GateResult) -> tuple[bool, str]:
    """返回 (ok, reason)。ok=False 时调用方必须拒绝 accept（fail-closed）。"""
    # 铁律1：matched_rule 必须是 gate 实际输出规则中的恰好一条
    if a.matched_rule is None or not isinstance(a.matched_rule, str):
        return False, "missing_rule"
    # 铁律2：accept 必须 intent_evidence 非空
    if a.accepted and not a.intent_evidence.strip():
        return False, "accept_without_evidence"
    # 铁律4（§8-4 收口）：broadcast 无授权，accept 一律拒。必须排在
    # 幻觉规则名检查之前——gate 对 broadcast 返回空 matched_rules，
    # 旧顺序会先命中 hallucinated_rule 使这条分支永远不可达。
    if a.accepted and gate_result.route == "broadcast":
        return False, "accept_on_broadcast"
    # 铁律3：研判引用的规则必须恰好命中 gate 实际输出的一条（防幻觉规则名）
    hits = [r for r in gate_result.matched_rules if r == a.matched_rule]
    if len(hits) != 1:
        return False, f"hallucinated_rule:{a.matched_rule}"
    return True, "ok"


def decide(inp: Input, cfg: AdmissionConfig,
           thread_open_tickets: Optional[dict[str, str]] = None,
           assessor=None) -> tuple[str, Optional[Assessment]]:
    """完整准入决策。返回 (disposition, assessment)。

    assessor: Callable(Input, GateResult) -> Assessment
      — None 时用默认保守策略（explicit 即接，mention/broadcast 拒），
        即"研判层缺位时 fail-closed"。
    disposition: accept / follow_up / rejected_bot / rejected_broadcast / rejected_eval / observe
    """
    g = gate(inp, cfg, thread_open_tickets)
    if g.route == "bot":
        return "rejected_bot", None
    if g.route == "follow_up":
        return "follow_up", None
    if g.route == "explicit":
        return "accept", Assessment(True, "explicit_marker",
                                    intent_evidence="whitelisted explicit marker",
                                    confidence=1.0)

    # mention / broadcast：需要研判层
    if assessor is None:
        return "rejected_eval", None  # 无研判层 → fail-closed
    a = assessor(inp, g)
    ok, reason = validate_assessment(a, g)
    if not ok:
        return f"rejected_eval:{reason}", a
    return ("accept", a) if a.accepted else ("rejected_eval", a)
