"""打磨轮测试：成本账全口径（kind/ticket_id）、报告契约硬化、__init__ 导出。

RED→GREEN 纪律：先跑本文件确认收集失败，实现后全绿。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.metrics import CallRecord, MetricsLedger          # noqa: E402
from agent.model import Assignment, Input, Mandate, Ticket  # noqa: E402


def _mk_assignment(sid="s1", tid="k1"):
    return Assignment(ticket_id=tid, session_id=sid, report="", report_payload={})


# ---------- RED 1：CallRecord 带 kind 与 ticket_id（默认 worker，兼容旧构造） ----------

def test_callrecord_defaults_kind_worker_ticket_none():
    rec = CallRecord(session_id="s", model="m", prompt_tokens=1,
                     completion_tokens=2, latency_ms=1.0, ok=True)
    assert rec.kind == "worker"
    assert rec.ticket_id is None


def test_ledger_summary_breaks_down_by_kind():
    led = MetricsLedger()
    led.record(CallRecord(session_id="s", model="m", prompt_tokens=10,
                          completion_tokens=5, latency_ms=1.0, ok=True,
                          kind="worker", ticket_id="k1"))
    led.record(CallRecord(session_id="s2", model="m", prompt_tokens=3,
                          completion_tokens=2, latency_ms=1.0, ok=True,
                          kind="assessor", ticket_id=None))
    s = led.summary()
    assert s["calls_by_kind"] == {"worker": 1, "assessor": 1}


# ---------- RED 2：执行器报告契约硬化（收尾非报告 JSON → 追问一轮） ----------

def _fake_transport(responses):
    """按调用次序吐响应；每次调用记录 payload。"""
    calls = []

    def transport(payload):
        calls.append(payload)
        return responses[min(len(calls) - 1, len(responses) - 1)]
    return transport, calls


def test_executor_finalize_round_when_last_content_not_report():
    from agent.openai_executor import OpenAICompatExecutor
    bad = {"choices": [{"message": {"content": "我先去看看文件。"}}],
           "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    good = {"choices": [{"message": {
        "content": json.dumps({"summary": "done", "touched_paths": []})}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    transport, calls = _fake_transport([bad, good])
    ex = OpenAICompatExecutor(transport=transport, metrics=MetricsLedger())
    a = _mk_assignment()
    ex.launch(a, Ticket(id="k1", decision_id="",
                        mandate=Mandate(task_text="t")))
    inp = Input(id="i1", thread_key="th", text="do it")
    assert ex.deliver(a, inp) is True
    # 第二次调用必须是追问报告的 finalize 轮
    assert len(calls) == 2
    last_user = [m for m in calls[1]["messages"] if m["role"] == "user"][-1]
    assert "JSON" in last_user["content"]
    assert ex.sessions["s1"]["messages"][-1]["content"].startswith("{\"summary\"")


def test_executor_no_finalize_when_already_report():
    from agent.openai_executor import OpenAICompatExecutor
    good = {"choices": [{"message": {
        "content": json.dumps({"summary": "done", "touched_paths": ["/a"]})}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    transport, calls = _fake_transport([good])
    ex = OpenAICompatExecutor(transport=transport, metrics=MetricsLedger())
    a = _mk_assignment()
    ex.launch(a, Ticket(id="k1", decision_id="",
                        mandate=Mandate(task_text="t")))
    assert ex.deliver(a, Input(id="i1", thread_key="th", text="do it")) is True
    assert len(calls) == 1


def test_executor_records_kind_and_ticket():
    from agent.openai_executor import OpenAICompatExecutor
    led = MetricsLedger()
    good = {"choices": [{"message": {
        "content": json.dumps({"summary": "done", "touched_paths": []})}}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3}}
    transport, _ = _fake_transport([good])
    ex = OpenAICompatExecutor(transport=transport, metrics=led)
    a = _mk_assignment()
    ex.launch(a, Ticket(id="k1", decision_id="",
                        mandate=Mandate(task_text="t")))
    assert ex.deliver(a, Input(id="i1", thread_key="th", text="do it")) is True
    assert led.records[0].kind == "worker"
    assert led.records[0].ticket_id == "k1"


# ---------- RED 3：assessor 调用入成本账 ----------

def test_assessor_records_cost():
    from agent.assessor import LLMAssessor
    from agent.admission import GateResult
    led = MetricsLedger()
    resp = {"choices": [{"message": {"content": json.dumps({
        "accepted": True, "matched_rule": "r1", "intent_evidence": "ev",
        "scope_note": "", "confidence": 0.9})}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 4}}
    transport, _ = _fake_transport([resp])
    ass = LLMAssessor(transport=transport, metrics=led)
    inp = Input(id="i1", thread_key="th", text="x")
    gate = GateResult(route="mention", matched_rules=["r1"])
    a = ass(inp, gate)
    assert a.accepted is True
    assert len(led.records) == 1
    assert led.records[0].kind == "assessor"
    assert led.records[0].prompt_tokens == 11


def test_assessor_records_failed_transport():
    from agent.assessor import LLMAssessor
    from agent.admission import GateResult
    led = MetricsLedger()

    def boom(payload):
        raise RuntimeError("net down")
    ass = LLMAssessor(transport=boom, metrics=led)
    a = ass(Input(id="i1", thread_key="th", text="x"),
            GateResult(route="mention", matched_rules=["r1"]))
    assert a.accepted is False          # fail-closed 不变
    assert len(led.records) == 1
    assert led.records[0].ok is False
    assert led.records[0].kind == "assessor"


# ---------- RED 4：__init__ 导出 ----------

def test_package_exports_core_api():
    import agent
    for name in ("Engine", "AdmissionConfig", "Tickets", "ScopeRegistry",
                 "Watchdog", "ProactivityEngine", "OpenAICompatExecutor",
                 "LLMAssessor", "MetricsLedger", "Scheduler"):
        assert hasattr(agent, name), f"agent.{name} 未导出"


# ---------- RED 5：Protocol 文档契约（deliver 可独立于 launch） ----------

def test_worker_executor_protocol_docstring_states_deliver_dependency():
    import inspect
    from agent.executor import WorkerExecutor
    doc = inspect.getdoc(WorkerExecutor) or ""
    assert "deliver" in doc and ("launch" in doc)


def main():
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    fails = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            fails += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - fails}/{len(fns)} passed")
    if fails:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
