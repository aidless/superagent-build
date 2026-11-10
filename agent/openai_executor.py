"""I3：OpenAI 兼容 chat worker 执行器（对话式 worker，治理挂点在 prompt 层）。

实现 WorkerExecutor 三方法：
- launch：内存登记 session（system_prompt + Mandate 边界）；
- deliver：一次 tool-loop——按 OpenAI chat 格式发 messages，解析
  choices[0].message；usage 逐调用记入 MetricsLedger（缺失记 0）；
  transport 异常 / HTTP 非 200 / 错误响应体 → 返回 False（fail-closed，不抛）；
- resume：session 还在登记表里。

本轮为对话式 worker：不真执行工具。模型若发起 tool_calls（如 pre_write），
回填 tool 角色占位结果继续对话，直至给出文本回复或轮数用尽。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from .metrics import CallRecord, MetricsLedger
from .model import Assignment, Input, Mandate, Ticket

Transport = Callable[[dict], dict]

# Mandate 边界 + 写文件前先调 pre_write 挂点（prompt 层文字，本轮不真执行工具）
DEFAULT_SYSTEM_PROMPT = (
    "你是 superagent-build 治理体系下的受控 worker。\n"
    "Mandate 边界（硬约束）：\n"
    "1. 只执行 Mandate.task_text 授权的任务，严格遵守 Mandate.rules"
    "（allow_paths 白名单 / forbid 禁区）；任何越界动作立即停止并上报 blocked，"
    "不得自行扩大范围。\n"
    "2. 写文件前必须先调用 pre_write 挂点，把目标路径与内容摘要上报治理层，"
    "得到放行后才允许落盘；未过挂点的写操作一律不发。\n"
    "3. 完成后报告结果，包含 summary 与 touched_paths 列表。\n"
    "当前为对话式 worker：工具调用（含 pre_write）由宿主治理层裁决，"
    "你只负责按上述边界陈述意图与结果。"
)


class OpenAICompatExecutor:
    """对接 OpenAI 兼容 chat completions 端点的 worker 执行器。"""

    def __init__(self,
                 endpoint: str = "http://127.0.0.1:3425/v1/chat/completions",
                 model: str = "deepseek-v4.1-flash",
                 api_key: str = "",
                 system_prompt: str = DEFAULT_SYSTEM_PROMPT,
                 transport: Optional[Transport] = None,
                 metrics: Optional[MetricsLedger] = None,
                 max_tool_rounds: int = 6,
                 timeout: float = 60.0):
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.system_prompt = system_prompt
        self.metrics = metrics if metrics is not None else MetricsLedger()
        self.max_tool_rounds = max_tool_rounds
        self.timeout = timeout
        self._transport: Transport = transport if transport is not None else self._http_transport
        # session_id -> {"ticket_id", "messages", "mandate"}（内存登记）
        self.sessions: dict[str, dict] = {}
        self._seq = 0

    # ---------- 默认 HTTP 传输（真实环境用；测试注入 fake 不走这里） ----------

    def _http_transport(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"worker_endpoint_http_{resp.status}")
            return json.loads(resp.read().decode("utf-8"))

    # ---------- WorkerExecutor ----------

    def launch(self, assignment: Assignment, ticket: Ticket) -> str:
        sid = assignment.session_id or self._next_session_id()
        self.sessions[sid] = {
            "ticket_id": assignment.ticket_id,
            "mandate": ticket.mandate,
            "messages": [{"role": "system", "content": self.system_prompt}],
        }
        return sid

    def deliver(self, assignment: Assignment, inp: Input) -> bool:
        sess = self.sessions.get(assignment.session_id)
        if sess is None:
            # 懒登记（对齐 EchoExecutor：deliver 不强依赖先 launch）
            sid = self.launch(assignment, Ticket(id=assignment.ticket_id,
                                                 decision_id="",
                                                 mandate=Mandate(task_text="")))
            sess = self.sessions[sid]
        sess["messages"].append(
            {"role": "user", "content": f"[input {inp.id}] {inp.text}"})

        rounds = 0
        while True:
            t0 = time.monotonic()
            payload = {"model": self.model, "messages": sess["messages"],
                       "stream": False}
            try:
                resp = self._transport(payload)
            except Exception:
                self._record(assignment, None, t0, ok=False)
                return False  # transport 异常 / HTTP 非 200：不抛

            choices = resp.get("choices")
            msg = (choices[0].get("message") if choices else None) or {}
            if not choices or not isinstance(msg, dict):
                # 错误响应体（缺 choices）→ fail-closed
                self._record(assignment, resp, t0, ok=False)
                return False
            self._record(assignment, resp, t0, ok=True)

            tool_calls = msg.get("tool_calls") or []
            if tool_calls and rounds < self.max_tool_rounds:
                # 回填 assistant 消息 + tool 占位结果（本轮不真执行工具）
                sess["messages"].append({"role": "assistant", "content": msg.get("content", ""),
                                         "tool_calls": tool_calls})
                for tc in tool_calls:
                    sess["messages"].append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": "[governance] pre_write 已登记，由宿主治理层裁决；"
                                   "对话式 worker 本轮不直接执行工具。",
                    })
                rounds += 1
                continue
            # 收尾：文本回复入会话
            sess["messages"].append({"role": "assistant",
                                     "content": msg.get("content", "")})
            return True

    def resume(self, assignment: Assignment) -> bool:
        return assignment.session_id in self.sessions

    # ---------- 内部 ----------

    def _record(self, assignment: Assignment, resp: Optional[dict],
                t0: float, ok: bool) -> None:
        usage = (resp or {}).get("usage") or {}
        self.metrics.record(CallRecord(
            session_id=assignment.session_id,
            model=self.model,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            latency_ms=(time.monotonic() - t0) * 1000.0,
            ok=ok,
        ))

    def _next_session_id(self) -> str:
        self._seq += 1
        return f"sess-{self._seq}"
