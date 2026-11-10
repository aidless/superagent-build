"""领域模型：Thread / Ticket / Assignment / Input / Mandate / Assessment。

对齐 dsh-superagent-prototype 的 src/core/model.ts 聚合结构（见
superagent-dig/01-prototype-dissection.md），数据类只放事实，行为在对应模块。

约定：
- Thread 是一致性边界，持有订阅（订阅者是 ticket id，不是 Router——原型的教训）。
- Engine 是唯一写者，经由 store.py 落库。
- 所有 id 都是 str，时间戳统一 epoch 秒 float。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------- 输入（Trigger 的原始消息） ----------

@dataclass
class Input:
    """一条进入系统的消息。id 幂等：同一 id 不会被重复消费。"""
    id: str
    thread_key: str          # 所属线程（GitHub issue / 飞书会话 等）
    text: str
    is_human: bool = True
    is_bot: bool = False
    context_only: bool = False   # 订阅扇出的 context 消息，不授予 follow-up 强度
    delivered_to: Optional[str] = None  # 已投递给的 ticket id
    read_at: Optional[float] = None


# ---------- 线程 ----------

@dataclass
class Thread:
    """聚合根：consistency boundary of eval, holder of subscriptions。"""
    key: str
    ref: str = ""                       # 外部引用（issue url 等）
    subscriptions: list[str] = field(default_factory=list)  # 订阅本线程的 ticket id
    correlated: list[str] = field(default_factory=list)     # 同 trigger 关联的其他 thread key


# ---------- 任务委托 ----------

class TicketStatus(str, Enum):
    OPEN = "open"
    BLOCKED = "blocked"      # 等 attention
    ENDED = "ended"


@dataclass
class Mandate:
    """验收契约：worker 被授权做什么、不许做什么。Watchdog 以此为判据。"""
    task_text: str
    rules: dict = field(default_factory=dict)   # {"allow_paths": [...], "forbid": [...]}
    version: int = 1


@dataclass
class Assignment:
    """一次 worker 实例绑定。"""
    ticket_id: str
    session_id: str
    report: Optional[str] = None        # ready / blocked / dropped
    report_payload: dict = field(default_factory=dict)
    delivered_at: Optional[float] = None
    idle_at: Optional[float] = None


@dataclass
class Ticket:
    id: str
    decision_id: str
    mandate: Mandate
    channels: list[str] = field(default_factory=list)   # 回复去向
    inputs: list[Input] = field(default_factory=list)
    undelivered: list[str] = field(default_factory=list)  # 待投递 input id
    follows: list[str] = field(default_factory=list)      # 绑定的外部资源（PR 等）
    status: TicketStatus = TicketStatus.OPEN
    assignments: list[Assignment] = field(default_factory=list)
    attention: Optional[dict] = None    # 结构化 attention 请求
    created_at: float = 0.0
    ended_at: Optional[float] = None


# ---------- 两级准入的第二级：模型研判 ----------

@dataclass
class Assessment:
    """主 Session 内（或独立 judge）产出的研判，硬校验见 admission.py。"""
    accepted: bool
    matched_rule: Optional[str]     # 必须恰好命中一条规则（原型铁律）
    intent_evidence: str            # 判定依据，accept 时必填
    scope_note: str = ""            # 判定边界说明
    confidence: float = 0.0


@dataclass
class Decision:
    """准入完成后的决策记录：eval 的持久化结果。"""
    id: str
    trigger_input_id: str
    thread_keys: list[str]
    assessment: Assessment
    mandate: Optional[Mandate] = None
    created_at: float = 0.0
