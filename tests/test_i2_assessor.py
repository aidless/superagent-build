"""I2：LLM 研判层验收测试（先 RED 后 GREEN）。

门 1（build_admission_context）：有界上下文——trigger 前 5 条 + trigger + 后 5 条，
    整体超 max_chars 从头部截断且 trigger 文本必须保留。
门 2（LLMAssessor）：严格 JSON 契约 + matched_rule 白名单（防幻觉）+ 任何异常 fail-closed。
HTTP 传输层可注入（transport: Callable[[dict], dict]），测试零真实网络。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.admission import AdmissionConfig, GateResult, gate, validate_assessment  # noqa: E402
from agent.assessor import LLMAssessor, build_admission_context                     # noqa: E402
from agent.model import Input                                                       # noqa: E402


def _mk_gate_result() -> GateResult:
    """mention 路由的 gate 结果（matched_rules=["whitelist_user"]）。"""
    cfg = AdmissionConfig(whitelist=["issue:1"])
    inp = Input(id="u1:m1", thread_key="issue:1", text="帮我看看这段日志", is_human=True)
    return gate(inp, cfg)


def _fake_response(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}


# ---------- 门 1：有界上下文 ----------

def test_build_admission_context_bounded_and_keeps_trigger():
    # 50 条 1000+ 字符长输入 → 窗口拼接必超 4000，须从头部截断且保住 trigger
    inputs = [Input(id=f"m{i}", thread_key="t", text=f"历史消息 {i} " + "x" * 1000)
              for i in range(50)]
    trigger = Input(id="m25", thread_key="t",
                    text="触发词 TARGET_TRIGGER_TEXT 请研判", is_human=True)
    inputs[25] = trigger
    result = build_admission_context(inputs, trigger, max_chars=4000)
    assert len(result) <= 4200, f"context 超界: {len(result)}"
    assert "TARGET_TRIGGER_TEXT" in result, "截断把 trigger 文本丢了"


def test_build_admission_context_window_shape():
    # 未超界时窗口形状：前 5 + trigger + 后 5 = 11 条，窗外内容不进
    inputs = [Input(id=f"m{i}", thread_key="t", text=f"msg {i}") for i in range(20)]
    result = build_admission_context(inputs, inputs[10])
    for i in range(5, 16):
        assert f"msg {i}" in result, f"窗口内 msg {i} 丢失"
    assert "msg 4 " not in result.replace("msg 4", "KEEP") or "msg 4" not in result
    assert "msg 16" not in result and "msg 17" not in result


def test_build_admission_context_trigger_not_in_list_still_bounded():
    # trigger 不在列表里也不炸，返回有界尾部
    inputs = [Input(id=f"m{i}", thread_key="t", text=f"历史 {i} " + "y" * 2000)
              for i in range(30)]
    trigger = Input(id="ghost", thread_key="t", text="GHOST_TRIGGER", is_human=True)
    result = build_admission_context(inputs, trigger, max_chars=4000)
    assert len(result) <= 4200, f"context 超界: {len(result)}"


# ---------- 门 2：LLMAssessor ----------

def test_assessor_accept_passes_hard_validation():
    calls = []

    def transport(payload: dict) -> dict:
        calls.append(payload)
        return _fake_response(json.dumps({
            "accepted": True,
            "matched_rule": "whitelist_user",
            "intent_evidence": "白名单用户明确请求日志分析",
            "scope_note": "仅分析日志，不改代码",
            "confidence": 0.9,
        }, ensure_ascii=False))

    a = LLMAssessor(transport=transport)
    g = _mk_gate_result()
    out = a(Input(id="u1:m1", thread_key="issue:1", text="帮我看看这段日志"), g)

    assert out.accepted is True
    assert out.matched_rule == "whitelist_user"
    ok, reason = validate_assessment(out, g)
    assert ok, reason

    # prompt 契约：gate 的 matched_rules 与 JSON 字段要求必须出现在请求里
    prompt_blob = json.dumps(calls[0], ensure_ascii=False)
    assert "whitelist_user" in prompt_blob, "matched_rules 没进 prompt"
    assert "matched_rule" in prompt_blob and "intent_evidence" in prompt_blob


def test_assessor_transport_exception_fail_closed():
    def transport(payload: dict) -> dict:
        raise ConnectionError("endpoint down")

    a = LLMAssessor(transport=transport)
    g = _mk_gate_result()
    out = a(Input(id="u1:m2", thread_key="issue:1", text="x"), g)

    assert out.accepted is False
    assert out.intent_evidence.startswith("assessor_unavailable:"), out.intent_evidence
    assert out.matched_rule == "whitelist_user", "fail-closed 必须回落 matched_rules[0]"
    # fail-closed 结果本身要能过硬校验
    ok, reason = validate_assessment(out, g)
    assert ok, reason


def test_assessor_malformed_json_fail_closed():
    a = LLMAssessor(transport=lambda p: _fake_response("这不是 JSON"))
    g = _mk_gate_result()
    out = a(Input(id="u1:m3", thread_key="issue:1", text="x"), g)
    assert out.accepted is False
    assert out.intent_evidence.startswith("assessor_unavailable:")
    assert out.matched_rule == "whitelist_user"


def test_assessor_hallucinated_rule_fail_closed():
    def transport(payload: dict) -> dict:
        return _fake_response(json.dumps({
            "accepted": True, "matched_rule": "made_up_rule",
            "intent_evidence": "幻觉规则", "confidence": 0.99}))

    a = LLMAssessor(transport=transport)
    g = _mk_gate_result()
    out = a(Input(id="u1:m4", thread_key="issue:1", text="x"), g)

    assert out.accepted is False, "幻觉规则名必须 fail-closed"
    assert "made_up_rule" in out.intent_evidence
    assert out.matched_rule == "whitelist_user"


def test_assessor_fenced_json_still_parses():
    content = "```json\n" + json.dumps({
        "accepted": True, "matched_rule": "whitelist_user",
        "intent_evidence": "明确请求", "confidence": 0.8}) + "\n```"
    a = LLMAssessor(transport=lambda p: _fake_response(content))
    g = _mk_gate_result()
    out = a(Input(id="u1:m5", thread_key="issue:1", text="x"), g)
    assert out.accepted is True
    assert out.matched_rule == "whitelist_user"


def test_assessor_fail_closed_empty_rules_falls_back_to_route():
    # broadcast 路由 matched_rules 为空 → fail-closed 时回落 route 本身
    a = LLMAssessor(transport=lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
    g = GateResult("broadcast", [])
    out = a(Input(id="x", thread_key="t", text="x"), g)
    assert out.accepted is False
    assert out.matched_rule == "broadcast"
    assert out.intent_evidence.startswith("assessor_unavailable:")
