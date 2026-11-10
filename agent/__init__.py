"""自研 Super Agent——治理内嵌 + Watchdog + 主动阈值的任务执行层。

模块图：
    store.py        持久层（WAL+FULL、receipts、outbox）
    model.py        聚合根（Thread/Ticket/Mandate/Assessment）
    admission.py    两级准入（规则 gate + 研判硬校验）
    outbox.py       效果三角投递（先存后做 + 退避）
    tickets.py      Ticket 仓储与订阅扇出
    executor.py     worker 会话抽象（EchoExecutor 测试位）
    engine.py       编排（pass 循环、恢复）
    governance.py   写作用域治理（内嵌版）
    watchdog.py     跑偏检测（规则层 + judge）
    proactivity.py  主动性开口阈值（p*(t)，observe 模式）
"""

# 延迟导出核心 API（保持 import agent 零副作用、零依赖）
from __future__ import annotations


def __getattr__(name: str):
    _lazy = {
        "Engine": ("agent.engine", "Engine"),
        "EngineConfig": ("agent.engine", "EngineConfig"),
        "AdmissionConfig": ("agent.admission", "AdmissionConfig"),
        "Tickets": ("agent.tickets", "Tickets"),
        "Thread": ("agent.model", "Thread"),
        "Ticket": ("agent.model", "Ticket"),
        "Mandate": ("agent.model", "Mandate"),
        "Input": ("agent.model", "Input"),
        "ScopeRegistry": ("agent.governance", "ScopeRegistry"),
        "Watchdog": ("agent.watchdog", "Watchdog"),
        "WatchdogVerdict": ("agent.watchdog", "WatchdogVerdict"),
        "ProactivityEngine": ("agent.proactivity", "ProactivityEngine"),
        "ProactivityConfig": ("agent.proactivity", "ProactivityConfig"),
        "OpenAICompatExecutor": ("agent.openai_executor", "OpenAICompatExecutor"),
        "EchoExecutor": ("agent.executor", "EchoExecutor"),
        "LLMAssessor": ("agent.assessor", "LLMAssessor"),
        "MetricsLedger": ("agent.metrics", "MetricsLedger"),
        "CallRecord": ("agent.metrics", "CallRecord"),
        "Scheduler": ("agent.scheduler", "Scheduler"),
        "SchedulerConfig": ("agent.scheduler", "SchedulerConfig"),
        "Outbox": ("agent.outbox", "Outbox"),
        "Store": ("agent.store", "Store"),
    }
    if name in _lazy:
        import importlib
        mod = importlib.import_module(_lazy[name][0])
        return getattr(mod, _lazy[name][1])
    raise AttributeError(f"agent has no attribute {name!r}")
