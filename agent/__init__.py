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
