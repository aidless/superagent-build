"""outbox 投递器：效果三角（receipt → enqueue → deliver）+ 退避重试。

对齐原型 outbox.ts:27-51 / delivery.ts 语义：
- 先存后做：enqueue 前必须有 receipt（store.py 已强制）；
- 投递后 flush 才算送达（deliver 返回 True 才 mark_sent）；
- 退避 5s→300s（×2 封顶），唯二合法放弃 channelGone/channelIgnored（store 强制）；
- messageId 幂等（store 主键保证重复 enqueue 静默跳过）。
"""
from __future__ import annotations

from typing import Callable, Optional

from .store import Store

BACKOFF_BASE = 5.0
BACKOFF_CAP = 300.0

# Deliverer: channel, body -> bool（True=真送达并已 flush）
Deliverer = Callable[[str, str], bool]


def backoff_next(attempts: int) -> float:
    return min(BACKOFF_BASE * (2 ** attempts), BACKOFF_CAP)


def pump(store: Store, now: float, deliver: Deliverer,
         max_attempts: int = 6) -> dict:
    """跑一轮投递。返回统计 {"sent": n, "retried": n, "abandoned": n}。

    deliver 抛 channelGone/channelIgnored 时合法放弃；
    抛其他异常或返回 False → 退避重试；超过 max_attempts 仍失败 → 继续退避等下轮
    （不静默丢弃——那是原型的可靠性底线）。
    """
    stats = {"sent": 0, "retried": 0, "abandoned": 0}
    for msg in store.due(now):
        try:
            ok = deliver(msg["channel"], msg["body"])
        except ChannelGone:
            store.abandon(msg["message_id"], "channelGone")
            stats["abandoned"] += 1
            continue
        except ChannelIgnored:
            store.abandon(msg["message_id"], "channelIgnored")
            stats["abandoned"] += 1
            continue
        except Exception:
            # S5 收口：docstring 早就承诺"抛其他异常 → 退避重试"，此前只有
            # 两个合法放弃被捕获、其它异常直接外泄出 pump。现在按同一退避规则
            # 重试（不静默丢弃，也不放弃），ChannelGone/Ignored 仍是唯二放弃。
            store.mark_retry(msg["message_id"], now + backoff_next(msg["attempts"]))
            stats["retried"] += 1
            continue
        if ok:
            store.mark_sent(msg["message_id"])
            stats["sent"] += 1
        else:
            store.mark_retry(msg["message_id"], now + backoff_next(msg["attempts"]))
            stats["retried"] += 1
    return stats


class ChannelGone(Exception):
    """通道已不存在（issue 关闭/会话失效）——唯二合法放弃之一。"""


class ChannelIgnored(Exception):
    """通道明确拒收（目标明确表示不要）——唯二合法放弃之一。"""
