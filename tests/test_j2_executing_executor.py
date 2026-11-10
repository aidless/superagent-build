"""J2：真执行器闭环——治理在执行路径内强制。

设计：
- ExecutingToolLoopExecutor：在 tool-loop 里真执行工具（write_file/append_file），
  pre_write 工具调用 = 治理裁决点：BLOCK 时不落盘、向 worker 回填 BLOCK 事实；
- 宿主钩子 host_exec 可注入测试（零真实文件系统）；默认实现直写本地文件；
- 治理挂点通过 write_gate: Callable[[ticket_id, path], bool] 注入（Engine.pre_write）；
- fail-closed：无 write_gate 时工具执行一律拒绝（write_gate_required）。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.metrics import MetricsLedger                       # noqa: E402
from agent.model import Assignment, Input, Mandate, Ticket    # noqa: E402


def _mk_assignment(sid="s1", tid="k1"):
    return Assignment(ticket_id=tid, session_id=sid, report="", report_payload={})


def _mk_ticket(rules=None):
    return Ticket(id="k1", decision_id="",
                  mandate=Mandate(task_text="整理笔记",
                                  rules=rules if rules is not None
                                  else {"allow_paths": ["/tmp/work/**"]}))


def _resp(content, usage=None, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    r = {"choices": [{"message": msg}]}
    if usage is not None:
        r["usage"] = usage
    return r


def _fake_transport(responses):
    calls = []

    def transport(payload):
        calls.append(payload)
        return responses[min(len(calls) - 1, len(responses) - 1)]
    return transport, calls


def _tc(name, args):
    return [{"id": "c1", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}]


# ---------- RED 1：pre_write BLOCK 时不落盘 ----------

def test_block_prevents_write():
    from agent.executing_executor import ExecutingToolLoopExecutor
    writes = []

    def host_exec(op, path, content):
        writes.append((op, path, content))
        return True

    # 治理恒 BLOCK：任何写都不得落盘
    def gate(tid, path):
        return False
    responses = [
        _resp("", tool_calls=_tc("append_file",
                                 {"path": "/tmp/work/a.md", "content": "x"})),
        _resp(json.dumps({"summary": "done", "touched_paths": ["/tmp/work/a.md"]})),
    ]
    transport, _ = _fake_transport(responses)
    ex = ExecutingToolLoopExecutor(transport=transport, host_exec=host_exec,
                                   write_gate=gate)
    a = _mk_assignment()
    ex.launch(a, _mk_ticket())
    assert ex.deliver(a, Input(id="i1", thread_key="t", text="go")) is True
    assert writes == []              # BLOCK 之下零落盘
    # tool 回填里能看到 BLOCK 事实
    tool_msgs = [m for m in ex.sessions["s1"]["messages"] if m["role"] == "tool"]
    assert any("BLOCK" in m["content"] for m in tool_msgs)


# ---------- RED 2：ALLOW 时真执行且回填 OK ----------

def test_allow_executes_and_reports_ok():
    from agent.executing_executor import ExecutingToolLoopExecutor
    writes = []

    def host_exec(op, path, content):
        writes.append((op, path, content))
        return True
    responses = [
        _resp("", tool_calls=_tc("write_file",
                                 {"path": "/tmp/work/b.md", "content": "hi"})),
        _resp(json.dumps({"summary": "done", "touched_paths": []})),
    ]
    transport, calls = _fake_transport(responses)
    ex = ExecutingToolLoopExecutor(transport=transport, host_exec=host_exec,
                                   write_gate=lambda t, p: True)
    a = _mk_assignment()
    ex.launch(a, _mk_ticket())
    assert ex.deliver(a, Input(id="i1", thread_key="t", text="go")) is True
    assert writes == [("write", "/tmp/work/b.md", "hi")]
    tool_msgs = [m for m in ex.sessions["s1"]["messages"] if m["role"] == "tool"]
    assert any("OK" in m["content"] for m in tool_msgs)


# ---------- RED 3：fail-closed——无 write_gate 一律拒写 ----------

def test_no_gate_fail_closed():
    from agent.executing_executor import ExecutingToolLoopExecutor
    writes = []

    def host_exec(op, path, content):
        writes.append((op, path, content))
        return True
    responses = [
        _resp("", tool_calls=_tc("write_file",
                                 {"path": "/tmp/work/c.md", "content": "x"})),
        _resp(json.dumps({"summary": "done", "touched_paths": []})),
    ]
    transport, _ = _fake_transport(responses)
    ex = ExecutingToolLoopExecutor(transport=transport, host_exec=host_exec,
                                   write_gate=None)
    a = _mk_assignment()
    ex.launch(a, _mk_ticket())
    assert ex.deliver(a, Input(id="i1", thread_key="t", text="go")) is True
    assert writes == []              # 未过治理绝不落盘


# ---------- RED 4：未知工具 → 拒绝且不抛 ----------

def test_unknown_tool_rejected():
    from agent.executing_executor import ExecutingToolLoopExecutor
    responses = [
        _resp("", tool_calls=_tc("rm_rf", {"path": "/"})),
        _resp(json.dumps({"summary": "done", "touched_paths": []})),
    ]
    transport, _ = _fake_transport(responses)
    ex = ExecutingToolLoopExecutor(transport=transport,
                                   host_exec=lambda *a: True,
                                   write_gate=lambda t, p: True)
    a = _mk_assignment()
    ex.launch(a, _mk_ticket())
    assert ex.deliver(a, Input(id="i1", thread_key="t", text="go")) is True
    tool_msgs = [m for m in ex.sessions["s1"]["messages"] if m["role"] == "tool"]
    assert any("unknown_tool" in m["content"] for m in tool_msgs)


# ---------- RED 5：默认 host_exec 真写文件（本地模式） ----------

def test_default_host_exec_writes_real_file():
    from agent.executing_executor import ExecutingToolLoopExecutor
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "real.md")
        responses = [
            _resp("", tool_calls=_tc("append_file",
                                     {"path": target, "content": "real"})),
            _resp(json.dumps({"summary": "done", "touched_paths": [target]})),
        ]
        transport, _ = _fake_transport(responses)
        ex = ExecutingToolLoopExecutor(transport=transport,
                                       write_gate=lambda t, p: True)
        a = _mk_assignment()
        ex.launch(a, _mk_ticket())
        assert ex.deliver(a, Input(id="i1", thread_key="t", text="go")) is True
        assert os.path.exists(target)
        with open(target) as f:
            assert f.read() == "real"


# ---------- RED 6：与 Engine 集成——Engine.pre_write 就是 write_gate ----------

def test_engine_pre_write_as_gate():
    from agent.admission import AdmissionConfig
    from agent.engine import Engine
    from agent.executing_executor import ExecutingToolLoopExecutor
    from agent.governance import ScopeRegistry
    from agent.store import Store

    with tempfile.TemporaryDirectory() as d:
        store = Store(os.path.join(d, "t.db"))
        gov = ScopeRegistry(state_dir=os.path.join(d, "gov"))
        gov.claim("k1", ["/tmp/work/**"])
        writes = []

        def host_exec(op, path, content):
            writes.append((op, path, content))
            return True
        eng = Engine(store, AdmissionConfig(whitelist=["t"]), executor=None,
                     deliver=lambda ch, b: True, governance=gov)
        responses = [
            _resp("", tool_calls=_tc("write_file",
                                     {"path": "/etc/evil", "content": "x"})),
            _resp(json.dumps({"summary": "blocked", "touched_paths": []})),
        ]
        transport, _ = _fake_transport(responses)
        ex = ExecutingToolLoopExecutor(transport=transport, host_exec=host_exec,
                                       write_gate=eng.pre_write,
                                       ticket_of=lambda sid: "k1")
        eng.executor = ex
        ex.launch(_mk_assignment(), _mk_ticket())
        assert ex.deliver(_mk_assignment(),
                          Input(id="i1", thread_key="t", text="go")) is True
        assert writes == []          # 越界写被治理拦在执行路径内
        rulings = gov.read_rulings()
        assert any(r.get("verdict") == "BLOCK" for r in rulings)


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
