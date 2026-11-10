"""I2：两级准入的第二级——LLM 研判层（严格 JSON 契约 + fail-closed）。

对齐原型 admission-context.ts 的有界上下文语义：
- 窗口 = trigger 前 5 条 + trigger 本身 + 后 5 条；
- 整体拼接超 max_chars 时从头部逐条丢弃（最老的先丢），trigger 及其后文不丢；
- 任何解析失败 / 网络异常 / 超时 / 幻觉规则名 → fail-closed（accepted=False），
  matched_rule 回落 gate_result.matched_rules[0]，空则回落 route 本身。

铁律（与 admission.validate_assessment 对齐）：
- matched_rule 只能从 gate_result.matched_rules 里选（防幻觉）；
- assessor 缺位或失联时绝不 accept（研判层缺位 fail-closed）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

from .admission import GateResult
from .metrics import CallRecord
from .model import Assessment, Input

# transport: OpenAI chat completions 请求体 -> 响应体（可注入 fake 做零网络测试）
Transport = Callable[[dict], dict]

SYSTEM_PROMPT = (
    "你是 superagent-build 的准入研判层（admission assessor）。"
    "你只输出一个严格 JSON 对象，不输出任何其他文字、注释或 markdown 代码栅栏。"
    "JSON 字段：accepted(bool)、matched_rule(str)、intent_evidence(str)、"
    "scope_note(str)、confidence(float, 0~1)。"
    "matched_rule 必须且只能从用户消息给出的候选规则列表中选择，禁止编造规则名。"
    "拒绝时 matched_rule 仍须从候选列表中选，intent_evidence 可为空。"
)


def build_admission_context(inputs: list[Input], trigger: Input,
                            max_chars: int = 4000,
                            before: int = 5, after: int = 5) -> str:
    """有界线程上下文：trigger 前 before 条 + trigger + 后 after 条。

    超过 max_chars 时从头部逐条丢弃（最老的先丢），trigger 附近内容保留。
    trigger 不在 inputs 里时，退化为保留最后 before+after+1 条的尾部窗口。
    """
    idx = next((i for i, m in enumerate(inputs) if m.id == trigger.id), -1)
    if idx < 0:
        window = list(inputs[-(before + after + 1):])
        trigger_idx_in_window = None
    else:
        lo = max(0, idx - before)
        hi = min(len(inputs), idx + after + 1)
        window = inputs[lo:hi]
        trigger_idx_in_window = idx - lo

    # 行号用原始列表位置（从 1 起），截断后仍可读出被丢弃的范围
    base = max(0, idx) if idx >= 0 else max(0, len(inputs) - len(window))
    lines = [(f"[{base + i + 1}] {m.text}", i) for i, m in enumerate(window)]

    def joined(items) -> str:
        return "\n".join(t for t, _ in items)

    # 从头部逐条丢弃非 trigger 行，直到塞进预算（保 trigger 附近内容）
    kept = list(lines)
    while len(joined(kept)) > max_chars and kept:
        drop_i = next((k for k, (_, w) in enumerate(kept)
                       if trigger_idx_in_window is None or w != trigger_idx_in_window),
                      None)
        if drop_i is None:
            break
        kept.pop(drop_i)
        if trigger_idx_in_window is not None:
            # trigger 已是窗口内唯一不可丢行；其后内容随行保留
            pass
    text = joined(kept)
    if len(text) > max_chars:
        # 兜底：全部行都不可丢仍超界（单条超长），硬截尾部保 trigger 一侧
        text = text[-max_chars:]
    return text


def _extract_content(resp: dict) -> str:
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content")
    if not isinstance(content, str):
        raise ValueError("response missing choices[0].message.content")
    return content


def _parse_strict_json(content: str) -> dict:
    """容忍模型套 markdown 代码栅栏，但内容必须是合法 JSON 对象。"""
    text = content.strip()
    if text.startswith("```"):
        # 去掉 ```json / ``` 围栏
        body = text.strip("`").lstrip()
        if body.lower().startswith("json"):
            body = body[4:]
        text = body.strip().rstrip("`").strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("assessor output is not a JSON object")
    return data


def _fail_closed(gate_result: GateResult, reason: str) -> Assessment:
    fallback = (gate_result.matched_rules[0]
                if gate_result.matched_rules else gate_result.route)
    return Assessment(accepted=False, matched_rule=fallback,
                      intent_evidence=f"assessor_unavailable: {reason}",
                      scope_note="fail-closed: 研判层不可用，默认拒绝",
                      confidence=0.0)


class LLMAssessor:
    """Callable(Input, GateResult) -> Assessment。

    研判 prompt 只允许输出严格 JSON；解析失败 / 网络异常 / 超时 / 幻觉规则名
    一律 fail-closed，绝不 accept。
    """

    def __init__(self, model: str = "deepseek-v4.1-flash",
                 endpoint: str = "http://127.0.0.1:3425/v1/chat/completions",
                 api_key: str = "",
                 timeout: float = 20.0,
                 transport: Optional[Transport] = None,
                 metrics: Optional["object"] = None):
        self.model = model
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.metrics = metrics  # MetricsLedger：研判调用逐次入账（成本全口径）
        self._transport: Transport = transport if transport is not None else self._http_transport

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
                raise RuntimeError(f"assessor_endpoint_http_{resp.status}")
            return json.loads(resp.read().decode("utf-8"))

    # ---------- 研判 ----------

    def _ledger_record(self, inp: Input, resp, t0: float, ok: bool) -> None:
        """研判调用逐次入成本账（kind=assessor，无票据归属留空）。"""
        if self.metrics is None:
            return
        usage = (resp or {}).get("usage") or {}
        self.metrics.record(CallRecord(
            session_id=f"assessor:{inp.thread_key}",
            model=self.model,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            latency_ms=(time.monotonic() - t0) * 1000.0,
            ok=ok,
            kind="assessor",
            ticket_id=None,
        ))

    def __call__(self, inp: Input, gate_result: GateResult) -> Assessment:
        prompt = (
            f"候选规则列表（matched_rule 只能从中选择）："
            f"{json.dumps(gate_result.matched_rules, ensure_ascii=False)}\n"
            f"gate 路由：{gate_result.route}\n"
            f"触发消息（id={inp.id}, thread={inp.thread_key}）：\n{inp.text}\n\n"
            "只输出严格 JSON：{\"accepted\": bool, \"matched_rule\": str(从候选规则列表选), "
            "\"intent_evidence\": str(accept 时必填非空), \"scope_note\": str, "
            "\"confidence\": float}。"
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "stream": False,
        }
        t0 = time.monotonic()
        try:
            resp = self._transport(payload)
            data = _parse_strict_json(_extract_content(resp))
            self._ledger_record(inp, resp, t0, ok=True)
        except Exception as e:  # 解析失败 / 网络异常 / 超时 → fail-closed
            self._ledger_record(inp, None, t0, ok=False)
            return _fail_closed(gate_result, f"{type(e).__name__}: {e}")

        matched_rule = data.get("matched_rule")
        # 防幻觉铁律：模型给的规则名必须真实存在于 gate 输出
        if matched_rule not in gate_result.matched_rules:
            return _fail_closed(gate_result, f"hallucinated_rule:{matched_rule}")

        try:
            accepted = bool(data["accepted"])
            intent_evidence = str(data.get("intent_evidence", "") or "")
            scope_note = str(data.get("scope_note", "") or "")
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except (KeyError, TypeError, ValueError) as e:
            return _fail_closed(gate_result, f"malformed_assessment: {e}")

        return Assessment(accepted=accepted, matched_rule=matched_rule,
                          intent_evidence=intent_evidence,
                          scope_note=scope_note, confidence=confidence)
