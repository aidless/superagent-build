"""J2：真执行器——tool-loop 内真执行工具，治理在执行路径内强制。

与 OpenAICompatExecutor（对话式，工具占位回填）的区别：
- pre_write 语义落地为真实裁决：write_gate(ticket_id, path[, content_hash]) 返回
  False 时不落盘，tool 回填 BLOCK 事实（worker 可见），且 host_exec 永不触发；
- write_file / append_file 在 ALLOW 后经 host_exec 真执行；写路径会把
  sha256(content) 传进 write_gate，让 escalate 一次性 lift 能被真实消费（S6）；
- 未知工具一律拒绝（unknown_tool），不抛异常（fail-closed，回填错误事实）；
- 无 write_gate 时所有写工具拒绝（write_gate_required）——治理缺位宁可不动手。

host_exec(op, path, content) -> bool：宿主执行钩子，测试注入替代真实文件系统；
默认实现 _default_host_exec 直写本地文件（append/write 两语义）。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import re
import time
import urllib.request
from typing import Callable, Optional

from .metrics import CallRecord, MetricsLedger
from .model import Assignment, Input, Mandate, Ticket

Transport = Callable[[dict], dict]
HostExec = Callable[[str, str, str], bool]   # (op, path, content) -> bool
# (ticket_id, path) -> bool 旧协议；(ticket_id, path, content_hash) -> bool 新协议。
# 构造时按签名探测，老的两参 gate（大量既有测试/服务脚本）零改动继续可用。
WriteGate = Callable[..., bool]


def _gate_takes_hash(gate: Optional[WriteGate]) -> bool:
    """gate 是否接受第三参 content_hash：按签名数位置参数判断。

    Engine.pre_write(self, ticket_id, path, content_hash=None) 绑定后是 3 参；
    老的两参 lambda 是 2 参。带 *args 的 gate 按 2 参处理（兼容旧调用）。
    """
    if gate is None:
        return False
    try:
        sig = inspect.signature(gate)
    except (TypeError, ValueError):
        return False
    positional = [p for p in sig.parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    return len(positional) >= 3


def _content_hash(content: str) -> str:
    """与 ScopeRegistry.request_escalation 的 content_hash 口径逐字一致。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _default_host_exec(op: str, path: str, content: str) -> bool:
    """真执行：write=覆盖写，append=追加。目录不存在则建。

    symlink 加固（AF 借鉴）：POSIX 下 open 带 O_NOFOLLOW——叶子在
    "策略放行后、内核打开前"被换成 symlink 时内核拒绝（TOCTOU 收口到
    内核层）。Windows 无 O_NOFOLLOW，退回普通 open（能力边界诚实声明：
    本执行器是单租户协作设计，不是对抗敌对进程的沙箱）。
    """
    import os
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT
    if op == "append":
        flags |= os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if op in ("write", "append"):
        try:
            fd = os.open(path, flags, 0o644)
            try:
                os.write(fd, content.encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except OSError:
            return False
    return False


class ExecutingToolLoopExecutor:
    """真执行版 worker 执行器（J2）。接口与 OpenAICompatExecutor 一致。"""

    DEFAULT_SYSTEM_PROMPT = (
        "你是 superagent-build 治理体系下的受控 worker。\n"
        "可用工具：\n"
        "- write_file(path, content)：覆盖写（治理放行后真实执行）\n"
        "- append_file(path, content)：追加写（治理放行后真实执行）\n"
        "硬约束：只执行 Mandate 授权的任务；写文件必须先经工具调用（治理在执行路径"
        "内裁决，BLOCK 则不得换路径绕过）；完成后只输出最终报告 JSON 对象"
        "（含 summary 与 touched_paths）。"
    )

    def __init__(self,
                 endpoint: str = "http://127.0.0.1:3425/v1/chat/completions",
                 model: str = "deepseek-v4.1-flash",
                 api_key: str = "",
                 system_prompt: str = DEFAULT_SYSTEM_PROMPT,
                 transport: Optional[Transport] = None,
                 metrics: Optional[MetricsLedger] = None,
                 host_exec: Optional[HostExec] = None,
                 write_gate: Optional[WriteGate] = None,
                 ticket_of: Optional[Callable[[str], str]] = None,
                 max_tool_rounds: int = 8,
                 timeout: float = 60.0):
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        self.system_prompt = system_prompt
        self.metrics = metrics if metrics is not None else MetricsLedger()
        self.host_exec = host_exec if host_exec is not None else _default_host_exec
        self.write_gate = write_gate
        # S6：按真实签名探测 gate 是否收 content_hash（不能用 try/except
        # TypeError 兜底——gate 内部抛的 TypeError 会被误判成"老协议"）。
        self._gate_takes_hash = _gate_takes_hash(write_gate)
        # session_id -> ticket_id（治理挂点按票查）；默认从 launch 时的登记取
        self._ticket_of = ticket_of
        self.max_tool_rounds = max_tool_rounds
        self.timeout = timeout
        self._transport: Transport = (transport if transport is not None
                                      else self._http_transport)
        self.sessions: dict[str, dict] = {}
        self._seq = 0

    # ---------- 默认 HTTP 传输 ----------

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

    def _ticket_id_for(self, assignment: Assignment) -> str:
        if self._ticket_of is not None:
            return self._ticket_of(assignment.session_id)
        sess = self.sessions.get(assignment.session_id)
        return (sess or {}).get("ticket_id", assignment.ticket_id)

    def _exec_tool(self, assignment: Assignment, name: str,
                   args: dict) -> str:
        """执行一个工具调用，返回回填给 worker 的事实文本。治理在路径内强制。"""
        tid = self._ticket_id_for(assignment)
        if name == "pre_write":
            # 兼容旧协议：单独的 pre_write 询问（不落盘，只裁决）
            path = str(args.get("path", ""))
            allowed = (bool(self.write_gate)
                       and self._gate_allows(tid, path, None))
            return f"[governance] pre_write {path} -> {'OK' if allowed else 'BLOCK'}"
        if name in ("write_file", "append_file"):
            path = str(args.get("path", ""))
            content = str(args.get("content", ""))
            if self.write_gate is None:
                return ("[governance] BLOCK: write_gate_required "
                        "(治理挂点缺位，fail-closed 拒绝一切写操作)")
            # S6：把内容哈希传进治理挂点，escalate 的一次性 lift 才消费得到。
            if not self._gate_allows(tid, path, _content_hash(content)):
                return f"[governance] BLOCK: {path} (未过治理，禁止落盘)"
            op = "write" if name == "write_file" else "append"
            ok = self.host_exec(op, path, content)
            return (f"[governance] OK: {path} {'written' if ok else 'host_exec_failed'}"
                    if ok else f"[governance] OK granted but host_exec failed: {path}")
        return f"[governance] error: unknown_tool:{name}"

    def _gate_allows(self, tid: str, path: str,
                     content_hash: Optional[str]) -> bool:
        """按构造时探测的协议调用 write_gate（两参/三参都支持）。"""
        if self.write_gate is None:
            return False
        if self._gate_takes_hash:
            return bool(self.write_gate(tid, path, content_hash))
        return bool(self.write_gate(tid, path))

    def deliver(self, assignment: Assignment, inp: Input) -> bool:
        sess = self.sessions.get(assignment.session_id)
        if sess is None:
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
                return False
            choices = resp.get("choices")
            msg = (choices[0].get("message") if choices else None) or {}
            if not choices or not isinstance(msg, dict):
                self._record(assignment, resp, t0, ok=False)
                return False
            self._record(assignment, resp, t0, ok=True)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                # 兼容只输出文本工具调用的端点（如 deepseek-v4.1-flash 走
                # <tool_call>{...}</tool_call> 文本协议，不填原生字段）。
                tool_calls = self._parse_text_tool_calls(
                    msg.get("content") or "")
            if tool_calls and rounds < self.max_tool_rounds:
                sess["messages"].append({"role": "assistant",
                                         "content": msg.get("content", ""),
                                         "tool_calls": tool_calls})
                for tc in tool_calls:
                    fn = (tc.get("function") or {})
                    name = str(fn.get("name", ""))
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                        if not isinstance(args, dict):
                            args = {}
                    except (ValueError, TypeError):
                        args = {}
                    result = self._exec_tool(assignment, name, args)
                    sess["messages"].append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result,
                    })
                rounds += 1
                continue
            final_text = msg.get("content", "")
            if not self._is_report(final_text):
                sess["messages"].append({"role": "assistant",
                                         "content": final_text})
                sess["messages"].append({
                    "role": "user",
                    "content": "请只输出最终报告 JSON 对象（含 summary 与 "
                               "touched_paths 字段），不要输出其他文字。"})
                t0 = time.monotonic()
                try:
                    resp = self._transport(payload)
                except Exception:
                    self._record(assignment, None, t0, ok=False)
                    return False
                choices2 = resp.get("choices")
                msg2 = (choices2[0].get("message") if choices2 else None) or {}
                if not choices2 or not isinstance(msg2, dict):
                    self._record(assignment, resp, t0, ok=False)
                    return False
                self._record(assignment, resp, t0, ok=True)
                final_text = msg2.get("content", "") or final_text
            sess["messages"].append({"role": "assistant",
                                     "content": final_text})
            return True

    def resume(self, assignment: Assignment) -> bool:
        return assignment.session_id in self.sessions

    # ---------- 内部 ----------

    @staticmethod
    def _parse_text_tool_calls(text: str) -> list:
        """从文本里解析工具调用块（文本工具协议兼容）。

        支持两种格式：
        1. <tool_call>{json}</tool_call>
        2. DeepSeek DSML 标记（网关未剥离内部协议时泄漏到 content）：
           <｜DSML｜ invoke name="X">
           <｜DSML｜ parameter name="p" string="true">v</｜DSML｜ parameter>
           </｜DSML｜ invoke>
        返回与原生 tool_calls 同构的列表；无匹配返回 []。坏块静默跳过。
        """
        out = []
        if not text:
            return out
        # 格式 1：JSON 块
        for m in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
                             text, re.S):
            try:
                obj = json.loads(m.group(1))
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            fn = obj.get("function") if isinstance(obj.get("function"),
                                                   dict) else obj
            name = str(fn.get("name", ""))
            args = fn.get("arguments", "{}")
            if not isinstance(args, str):
                args = json.dumps(args, ensure_ascii=False)
            out.append({"id": f"text-{len(out)}", "type": "function",
                        "function": {"name": name, "arguments": args}})
        if out:
            return out
        # 格式 2：DSML invoke 块
        for m in re.finditer(
                r"<｜DSML｜\s*invoke\s+name=\"([^\"]+)\">(.*?)"
                r"</｜DSML｜\s*invoke>", text, re.S):
            name = m.group(1)
            args = {}
            for p in re.finditer(
                    r"<｜DSML｜\s*parameter\s+name=\"([^\"]+)\"[^>]*>"
                    r"(.*?)</｜DSML｜\s*parameter>", m.group(2), re.S):
                args[p.group(1)] = p.group(2)
            out.append({"id": f"dsml-{len(out)}", "type": "function",
                        "function": {"name": name,
                                     "arguments": json.dumps(
                                         args, ensure_ascii=False)}})
        return out

    @staticmethod
    def _is_report(text: str) -> bool:
        t = (text or "").strip()
        if t.startswith("```"):
            body = t.strip("`").lstrip()
            if body.lower().startswith("json"):
                body = body[4:]
            t = body.strip().rstrip("`").strip()
        try:
            data = json.loads(t)
        except (ValueError, TypeError):
            return False
        return isinstance(data, dict) and "summary" in data

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
            kind="worker",
            ticket_id=assignment.ticket_id,
        ))

    def _next_session_id(self) -> str:
        self._seq += 1
        return f"esess-{self._seq}"
