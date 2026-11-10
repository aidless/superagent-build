"""① LLM judge 接进 watchdog 的测试（RED→GREEN）。

覆盖：
- LLMJudge 严格 JSON 契约 + fail-closed（解析失败/网络异常/幻觉判词 → UNASSESSABLE）；
- 有界证据上下文（mandate + report 拼接，超长截断）；
- Watchdog 二次接 LLMJudge 后行为不变（规则层正交、severity 取更重者）；
- LLMJudge 成本入 MetricsLedger（kind="judge"）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.metrics import MetricsLedger                     # noqa: E402
from agent.model import Mandate                             # noqa: E402
from agent.watchdog import LLMJudge, ScriptedJudge, Watchdog  # noqa: E402
from agent.watchdog import WatchdogVerdict                  # noqa: E402


def _mandate(**rules):
    base = {"allow_paths": ["/tmp/work/**"], "forbid": ["/etc/**"]}
    base.update(rules)
    return Mandate(task_text="在 /tmp/work 下整理笔记", rules=base)


def _fake_transport(responses):
    calls = []

    def transport(payload):
        calls.append(payload)
        return responses[min(len(calls) - 1, len(responses) - 1)]
    return transport, calls


# ---------- RED 1：LLMJudge 基本判定 ----------

def test_llm_judge_deviation_on_offtopic_report():
    resp = {"choices": [{"message": {"content": json.dumps({
        "verdict": "deviation",
        "reasons": ["报告声称整理笔记但 summary 在讨论天气"]})}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 8}}
    transport, _ = _fake_transport([resp])
    j = LLMJudge(transport=transport, model="m")
    verdict, reasons = j.judge(_mandate(),
                               {"summary": "今天天气不错", "touched_paths": []})
    assert verdict == WatchdogVerdict.DEVIATION
    assert reasons and "天气" in reasons[0]


def test_llm_judge_ok_on_in_scope_report():
    resp = {"choices": [{"message": {"content": json.dumps({
        "verdict": "ok", "reasons": []})}}]}
    transport, _ = _fake_transport([resp])
    j = LLMJudge(transport=transport)
    verdict, _ = j.judge(_mandate(),
                         {"summary": "已整理 /tmp/work/notes.md",
                          "touched_paths": ["/tmp/work/notes.md"]})
    assert verdict == WatchdogVerdict.OK


# ---------- RED 2：fail-closed 三连 ----------

def test_llm_judge_fail_closed_on_bad_json():
    resp = {"choices": [{"message": {"content": "我觉得没问题"}}]}
    transport, _ = _fake_transport([resp])
    j = LLMJudge(transport=transport)
    verdict, reasons = j.judge(_mandate(), {"summary": "x"})
    assert verdict == WatchdogVerdict.UNASSESSABLE
    assert reasons and "judge" in reasons[0].lower()


def test_llm_judge_fail_closed_on_transport_error():
    def boom(payload):
        raise ConnectionError("down")
    j = LLMJudge(transport=boom)
    verdict, _ = j.judge(_mandate(), {"summary": "x"})
    assert verdict == WatchdogVerdict.UNASSESSABLE


def test_llm_judge_fail_closed_on_hallucinated_verdict():
    resp = {"choices": [{"message": {"content": json.dumps({
        "verdict": "probably_fine", "reasons": []})}}]}
    transport, _ = _fake_transport([resp])
    j = LLMJudge(transport=transport)
    verdict, _ = j.judge(_mandate(), {"summary": "x"})
    assert verdict == WatchdogVerdict.UNASSESSABLE


# ---------- RED 3：有界证据上下文 ----------

def test_llm_judge_builds_bounded_evidence():
    resp = {"choices": [{"message": {"content": json.dumps({
        "verdict": "ok", "reasons": []})}}]}
    transport, calls = _fake_transport([resp])
    j = LLMJudge(transport=transport, max_chars=500)
    big_report = {"summary": "长" * 3000, "touched_paths": ["/tmp/work/a"]}
    j.judge(_mandate(), big_report)
    prompt = calls[0]["messages"][-1]["content"]
    assert len(prompt) <= 500 + 200  # 证据段截断在预算内（允许固定指令头）


# ---------- RED 4：成本入账 ----------

def test_llm_judge_records_cost():
    led = MetricsLedger()
    resp = {"choices": [{"message": {"content": json.dumps({
        "verdict": "ok", "reasons": []})}}],
        "usage": {"prompt_tokens": 15, "completion_tokens": 3}}
    transport, _ = _fake_transport([resp])
    j = LLMJudge(transport=transport, metrics=led)
    j.judge(_mandate(), {"summary": "x"})
    assert len(led.records) == 1
    assert led.records[0].kind == "judge"
    assert led.records[0].prompt_tokens == 15


def test_llm_judge_records_failed_transport():
    led = MetricsLedger()

    def boom(payload):
        raise RuntimeError("net")
    j = LLMJudge(transport=boom, metrics=led)
    j.judge(_mandate(), {"summary": "x"})
    assert led.records and led.records[0].ok is False
    assert led.records[0].kind == "judge"


# ---------- RED 5：Watchdog 接 LLMJudge 行为不变 ----------

def test_watchdog_with_llm_judge_severity_merge():
    # judge 说 ok，规则层抓到越界 → DEVIATION（取更重者）
    resp = {"choices": [{"message": {"content": json.dumps({
        "verdict": "ok", "reasons": []})}}]}
    transport, _ = _fake_transport([resp])
    wd = Watchdog(judge=LLMJudge(transport=transport))
    r = wd.review(_mandate(),
                  {"summary": "done", "touched_paths": ["/etc/passwd"]})
    assert r.verdict == WatchdogVerdict.DEVIATION


def test_watchdog_with_llm_judge_unassessable_not_ok():
    # judge fail-closed → UNASSESSABLE，绝不当 OK 放行
    transport, _ = _fake_transport([{"choices": [{"message": {"content": "…"}}]}])
    wd = Watchdog(judge=LLMJudge(transport=transport))
    r = wd.review(_mandate(),
                  {"summary": "done", "touched_paths": ["/tmp/work/a"]})
    assert r.verdict == WatchdogVerdict.UNASSESSABLE


def test_watchdog_scripted_judge_still_works():
    wd = Watchdog(judge=ScriptedJudge({"bad": (WatchdogVerdict.DEVIATION, ["r"])}))
    r = wd.review(_mandate(), {"summary": "done", "scenario": "bad",
                               "touched_paths": ["/tmp/work/a"]})
    assert r.verdict == WatchdogVerdict.DEVIATION


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
