"""D3：LLMAssessor 调用入成本台账（RED→GREEN，AGENTS.md 第 21/27 条）。

RED（先红）：解析失败 / 网络异常当前仍记一条 ok=False 的 assessor 记录，
            断言「失败零记账」必须先失败；同时 fail-closed 不得抛。
GREEN（后绿）：成功调用把 usage 的 prompt_tokens/completion_tokens 逐字记成
              kind="assessor" 的 CallRecord；不传 metrics 行为完全不变；
              JSONL 落盘数字可复算。
零真实网络（transport 桩）、纯 stdlib、tmp_path 隔离落盘。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.admission import AdmissionConfig, gate  # noqa: E402
from agent.assessor import LLMAssessor  # noqa: E402
from agent.metrics import MetricsLedger  # noqa: E402
from agent.model import Input  # noqa: E402

USAGE = {"prompt_tokens": 137, "completion_tokens": 42}
ACCEPT_JSON = json.dumps({
    "accepted": True, "matched_rule": "whitelist_user",
    "intent_evidence": "白名单用户明确请求", "scope_note": "仅分析",
    "confidence": 0.9}, ensure_ascii=False)


def _ok_resp(content=ACCEPT_JSON):
    return {"choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": dict(USAGE)}


def _gate_result():
    return gate(Input(id="u1:m1", thread_key="issue:1", text="帮我看看这段日志",
                      is_human=True), AdmissionConfig(whitelist=["issue:1"]))


def _inp(msg_id="u1:m1"):
    return Input(id=msg_id, thread_key="issue:1", text="帮我看看这段日志",
                 is_human=True)


def test_d3_red_network_failure_does_not_record():
    ledger = MetricsLedger()

    def transport(payload):
        raise ConnectionError("endpoint down")

    a = LLMAssessor(transport=transport, metrics=ledger)
    out = a(_inp(), _gate_result())
    assert out.accepted is False
    assert out.intent_evidence.startswith("assessor_unavailable:")
    # 失败尝试必须留痕（ok=False、零 token）：可观测但不污染 token 成本。
    # 这是基线契约 tests/test_polish.py:125-138 的口径，本测试与之一致。
    recs = [r for r in ledger.records if r.kind == "assessor"]
    assert len(recs) == 1, ledger.records
    assert recs[0].ok is False
    assert recs[0].prompt_tokens == 0 and recs[0].completion_tokens == 0


def test_d3_red_parse_failure_does_not_record():
    ledger = MetricsLedger()
    a = LLMAssessor(transport=lambda p: _ok_resp("这不是 JSON"), metrics=ledger)
    out = a(_inp("u1:m2"), _gate_result())
    assert out.accepted is False
    assert out.intent_evidence.startswith("assessor_unavailable:")
    recs = [r for r in ledger.records if r.kind == "assessor"]
    assert len(recs) == 1, ledger.records
    assert recs[0].ok is False
    assert recs[0].prompt_tokens == 0 and recs[0].completion_tokens == 0


def test_d3_green_success_records_exact_usage():
    ledger = MetricsLedger()
    a = LLMAssessor(transport=lambda p: _ok_resp(), metrics=ledger)
    out = a(_inp("u1:m3"), _gate_result())
    assert out.accepted is True
    recs = [r for r in ledger.records if r.kind == "assessor"]
    assert len(recs) == 1, recs
    assert recs[0].prompt_tokens == 137
    assert recs[0].completion_tokens == 42
    assert recs[0].ok is True
    assert recs[0].model == "deepseek-v4.1-flash"
    assert ledger.summary()["calls_by_kind"]["assessor"] == 1


def test_d3_green_no_metrics_behavior_unchanged():
    calls = []

    def transport(payload):
        calls.append(payload)
        return _ok_resp()

    a = LLMAssessor(transport=transport)
    out = a(_inp("u1:m4"), _gate_result())
    assert out.accepted is True
    assert len(calls) == 1


def test_d3_green_ledger_jsonl_exact_numbers(tmp_path):
    ledger = MetricsLedger()
    a = LLMAssessor(transport=lambda p: _ok_resp(), metrics=ledger)
    out = a(_inp("u1:m5"), _gate_result())
    assert out.accepted is True
    path = tmp_path / "assessor-metrics.jsonl"
    ledger.to_jsonl(str(path))
    rows = [json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "assessor"
    assert row["prompt_tokens"] == 137
    assert row["completion_tokens"] == 42
