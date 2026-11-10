"""I3：调用指标台账——逐次 LLM 调用入账 + 汇总 + JSONL 落盘。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass


@dataclass
class CallRecord:
    """一次 LLM 调用的观测事实。"""
    session_id: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float
    ok: bool
    kind: str = "worker"        # 调用方角色：worker / assessor / judge ...
    ticket_id: str | None = None  # 关联票号（研判类调用可空）


class MetricsLedger:
    """内存台账；record 逐次入账，summary 出汇总，to_jsonl 落盘。"""

    def __init__(self):
        self.records: list[CallRecord] = []

    def record(self, rec: CallRecord) -> None:
        self.records.append(rec)

    def summary(self) -> dict:
        calls = len(self.records)
        ok_calls = sum(1 for r in self.records if r.ok)
        prompt_tokens = sum(r.prompt_tokens for r in self.records)
        completion_tokens = sum(r.completion_tokens for r in self.records)
        return {
            "calls": calls,
            "ok_calls": ok_calls,
            "failed_calls": calls - ok_calls,
            "success_rate": (ok_calls / calls) if calls else 0.0,
            "total_prompt_tokens": prompt_tokens,
            "calls_by_kind": {
                k: sum(1 for r in self.records if r.kind == k)
                for k in sorted({r.kind for r in self.records})},
            "total_completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def to_jsonl(self, path: str) -> None:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for r in self.records:
                f.write(json.dumps(asdict(r), ensure_ascii=False) + "\n")
