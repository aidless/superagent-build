"""执行层抽象：worker 会话生命周期 + 治理内嵌挂点。

真实环境对接位：SesExecutor 的 launch/deliver 换成 dsh/CLI/远程 harness 适配器。
测试用 EchoExecutor 确定性回放。
"""
from __future__ import annotations

from typing import Optional, Protocol

from .model import Assignment, Input, Ticket


class WorkerExecutor(Protocol):
    def launch(self, assignment: Assignment, ticket: Ticket) -> str:
        """启动 worker 会话，返回 session 标识。"""
    def deliver(self, assignment: Assignment, inp: Input) -> bool:
        """投递输入到 worker。True=已送达。"""
    def resume(self, assignment: Assignment) -> bool:
        """崩溃恢复：会话还在吗？丢了返回 False（调用方重派）。"""


class EchoExecutor:
    """确定性测试执行器：launch 造会话号，deliver 直接生成 ready 报告回调。"""

    def __init__(self, on_report=None, seq: list[int] | None = None):
        self.on_report = on_report
        self.seq = seq if seq is not None else []
        self.sessions: dict[str, str] = {}

    def launch(self, assignment: Assignment, ticket: Ticket) -> str:
        self.seq.append(len(self.seq) + 1)
        sid = f"sess-{self.seq[-1]}"
        self.sessions[sid] = assignment.ticket_id
        return sid

    def deliver(self, assignment: Assignment, inp: Input) -> bool:
        if self.on_report:
            self.on_report(assignment, {
                "status": "done",
                "summary": f"echo:{inp.text}",
                "touched_paths": [],
                "scenario": "normal",
            })
        return True

    def resume(self, assignment: Assignment) -> bool:
        return assignment.session_id in self.sessions
