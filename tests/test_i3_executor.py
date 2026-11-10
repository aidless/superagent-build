"""I3：OpenAI 兼容 worker 执行器 + 指标台账验收测试（先 RED 后 GREEN）。

门 1（MetricsLedger）：record / summary（总 tokens、调用数、成功率）/ to_jsonl。
门 2（OpenAICompatExecutor）：内存登记 launch/resume；deliver 一次 tool-loop，
    usage 逐调用入账（缺失记 0），transport 异常或错误响应 → False 不抛；
    system_prompt 声明 Mandate 边界与 pre_write 挂点。
传输层可注入（transport: Callable[[dict], dict]），测试零真实网络。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.metrics import CallRecord, MetricsLedger                    # noqa: E402
from agent.model import Assignment, Input, Mandate, Ticket             # noqa: E402
from agent.openai_executor import OpenAICompatExecutor                 # noqa: E402


def _mk_ticket() -> Ticket:
    return Ticket(id="t-1", decision_id="d-1",
                  mandate=Mandate(task_text="修复测试",
                                  rules={"allow_paths": ["src/**"]}))


def _mk_assignment(session_id: str) -> Assignment:
    return Assignment(ticket_id="t-1", session_id=session_id)


def _resp(content="done", usage=None, tool_calls=None) -> dict:
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    r = {"choices": [{"message": msg}]}
    if usage is not None:
        r["usage"] = usage
    return r


# ---------- 门 1：MetricsLedger ----------

def test_metrics_summary_and_jsonl(tmp_path):
    led = MetricsLedger()
    led.record(CallRecord("s1", "deepseek-v4.1-flash", 10, 5, 12.0, True))
    led.record(CallRecord("s1", "deepseek-v4.1-flash", 7, 3, 9.0, False))

    s = led.summary()
    assert s["calls"] == 2, s
    assert s["total_prompt_tokens"] == 17, s
    assert s["total_completion_tokens"] == 8, s
    assert s["total_tokens"] == 25, s
    assert abs(s["success_rate"] - 0.5) < 1e-9, s

    path = tmp_path / "calls.jsonl"
    led.to_jsonl(str(path))
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 2, lines
    assert lines[0]["session_id"] == "s1"
    assert lines[0]["prompt_tokens"] == 10
    assert lines[1]["ok"] is False


def test_metrics_empty_summary():
    s = MetricsLedger().summary()
    assert s["calls"] == 0
    assert s["total_tokens"] == 0
    assert s["success_rate"] == 0.0


# ---------- 门 2：OpenAICompatExecutor ----------

def test_executor_launch_registers_and_resume():
    ex = OpenAICompatExecutor(transport=lambda p: _resp("ok"))
    sid = ex.launch(_mk_assignment("sess-9"), _mk_ticket())
    assert sid == "sess-9", sid
    assert ex.resume(_mk_assignment("sess-9")) is True
    assert ex.resume(_mk_assignment("sess-unknown")) is False, "未知 session 必须 False"


def test_executor_deliver_records_usage():
    led = MetricsLedger()
    ex = OpenAICompatExecutor(
        transport=lambda p: _resp('{"summary": "done", "touched_paths": []}',
                                  usage={"prompt_tokens": 10, "completion_tokens": 5}),
        metrics=led)
    ok = ex.deliver(_mk_assignment("sess-9"),
                    Input(id="in1", thread_key="t", text="干活"))
    assert ok is True
    assert len(led.records) == 1, led.records
    rec = led.records[0]
    assert rec.session_id == "sess-9"
    assert rec.prompt_tokens == 10 and rec.completion_tokens == 5
    assert rec.ok is True


def test_executor_transport_exception_returns_false():
    led = MetricsLedger()

    def transport(payload: dict) -> dict:
        raise ConnectionError("endpoint down")

    ex = OpenAICompatExecutor(transport=transport, metrics=led)
    ok = ex.deliver(_mk_assignment("sess-9"), Input(id="in2", thread_key="t", text="x"))
    assert ok is False, "transport 异常必须返回 False，不抛"
    assert led.records and led.records[-1].ok is False, "失败调用也要入账"


def test_executor_error_response_returns_false():
    # 错误响应体（等价 HTTP 非 200 / 缺 choices）→ fail-closed False
    ex = OpenAICompatExecutor(transport=lambda p: {"error": {"message": "rate_limited"}})
    ok = ex.deliver(_mk_assignment("sess-9"), Input(id="in3", thread_key="t", text="x"))
    assert ok is False


def test_executor_deliver_without_launch_lazy_registers():
    # 对齐参考实现 EchoExecutor：deliver 不强依赖先 launch（懒登记）
    ex = OpenAICompatExecutor(transport=lambda p: _resp("ok"))
    a = _mk_assignment("sess-lazy")
    assert ex.deliver(a, Input(id="in5", thread_key="t", text="x")) is True
    assert ex.resume(a) is True, "deliver 后 session 应已登记"


def test_executor_tool_loop_and_missing_usage_zero():
    led = MetricsLedger()
    responses = [
        _resp("", tool_calls=[{"id": "call1", "type": "function",
                               "function": {"name": "pre_write",
                                            "arguments": "{\"path\":\"src/x.py\"}"}}]),
        _resp('{"summary": "finished", "touched_paths": []}',
              usage={"prompt_tokens": 3, "completion_tokens": 2}),
    ]
    payloads = []

    def transport(payload: dict) -> dict:
        payloads.append(payload)
        return responses.pop(0)

    ex = OpenAICompatExecutor(transport=transport, metrics=led, max_tool_rounds=6)
    ok = ex.deliver(_mk_assignment("sess-9"),
                    Input(id="in4", thread_key="t", text="写文件"))
    assert ok is True
    assert len(payloads) == 2, f"应有一次 tool 回环 + 一次收尾: {len(payloads)}"
    assert len(led.records) == 2, led.records
    # 第一轮缺 usage → 记 0；第二轮对得上
    assert led.records[0].prompt_tokens == 0 and led.records[0].completion_tokens == 0
    assert led.records[1].prompt_tokens == 3 and led.records[1].completion_tokens == 2
    # tool 结果消息以 tool 角色回填（OpenAI chat 格式）
    sent_msgs = payloads[1]["messages"]
    assert any(m.get("role") == "tool" for m in sent_msgs), sent_msgs


def test_executor_system_prompt_declares_mandate_and_pre_write():
    ex = OpenAICompatExecutor(transport=lambda p: _resp("ok"))
    assert "Mandate" in ex.system_prompt, "system_prompt 必须声明 Mandate 边界"
    assert "pre_write" in ex.system_prompt, "system_prompt 必须声明 pre_write 挂点"
