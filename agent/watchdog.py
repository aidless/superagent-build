"""Watchdog：任务跑偏检测（以 Mandate 契约对照 worker 的 report payload）。

对齐 superagent-dig/01-prototype-dissection.md 的差距清单：
原型全库 0 命中 "watchdog"，"跑偏"只靠 worker 自觉上报（未兑现）。
本模块把"跑偏检测"做成真的：

- 契约 = Mandate（task_text + rules={"allow_paths": [...], "forbid": [...]}）；
- 证据 = worker 的 report_payload（touched_paths / status / summary /
  declares_extra_work / extra_work ...）；
- 先跑确定性规则层（纯 stdlib、不依赖 LLM），规则层之后再交可插拔 Judge
  （真实 LLM judge 只留 Protocol，测试用 ScriptedJudge 注入）；
- 规则层与 judge 正交，两者取更严重者；
- 证据不足一律 fail-closed（UNASSESSABLE，绝不当 OK 放行）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol

Transport = "callable-dict-to-dict"  # 类型别名占位：(dict)->dict

from .model import Mandate


class WatchdogVerdict(str, Enum):
    OK = "ok"                       # 在 Mandate 内
    DEVIATION = "deviation"         # 跑偏：做了没让做的事 / 没做让做的事
    UNASSESSABLE = "unassessable"   # 证据不足以判（fail-closed，不算 OK）


# 严重度排序：DEVIATION > UNASSESSABLE > OK。
# 合并规则层与 judge 时取更严重者；UNASSESSABLE 虽非 OK，但 DEVIATION 是确定的违规，更重。
_SEVERITY: dict[WatchdogVerdict, int] = {
    WatchdogVerdict.OK: 0,
    WatchdogVerdict.UNASSESSABLE: 1,
    WatchdogVerdict.DEVIATION: 2,
}


@dataclass
class ReviewResult:
    verdict: WatchdogVerdict
    reasons: list[str] = field(default_factory=list)          # 每条违规/依据一句话
    rule_findings: list[str] = field(default_factory=list)    # 确定性规则层 findings


class Judge(Protocol):
    """判官接口。真实 LLM judge 只留接口，不在此实现网络调用。"""

    def judge(self, mandate: Mandate,
              report_payload: dict) -> tuple[WatchdogVerdict, list[str]]:
        ...


class ScriptedJudge:
    """测试注入用判官：按 report_payload["scenario"] 到预设表里取 (verdict, reasons)。"""

    def __init__(self, table: dict[str, tuple[WatchdogVerdict, list[str]]]):
        self.table = table

    def judge(self, mandate: Mandate,
              report_payload: dict) -> tuple[WatchdogVerdict, list[str]]:
        key = report_payload.get("scenario")
        if key in self.table:
            return self.table[key]
        # 未命中的 scenario 视为不反对（规则层仍然独立生效）
        return WatchdogVerdict.OK, []


def _parse_judge_json(content: str) -> dict:
    """容忍 markdown 代码栅栏，内容必须是合法 JSON 对象。"""
    text = content.strip()
    if text.startswith("```"):
        body = text.strip("`").lstrip()
        if body.lower().startswith("json"):
            body = body[4:]
        text = body.strip().rstrip("`").strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("judge output is not a JSON object")
    return data


class LLMJudge:
    """J1：真实语义层判官——LLM 对照 Mandate 检查"答非所问/跑偏"。

    契约：
    - 严格 JSON 输出 {"verdict": "ok"|"deviation"|"unassessable", "reasons": [...]}，
      容忍 markdown 代码栅栏；判词必须 ∈ 三值，幻觉判词 → UNASSESSABLE（fail-closed）；
    - 解析失败 / 网络异常 / 超时 → UNASSESSABLE（证据不足以判，绝不当 OK 放行）；
    - 有界证据：mandate + report 拼接超 max_chars 时截断证据段（保头部）；
    - 每次调用入 MetricsLedger（kind="judge"，与 worker/assessor 同口径全成本账）。
    """

    SYSTEM_PROMPT = (
        "你是 agent 系统的 watchdog 语义层判官。给定任务授权（Mandate）与 worker 报告，"
        "判断报告内容是否在授权范围内、是否答非所问。只输出严格 JSON 对象："
        '{"verdict": "ok"|"deviation"|"unassessable", "reasons": ["..."]}。'
        "deviation=报告显示做了授权外的事或没做授权内的事；"
        "unassessable=证据不足以判断；不确定时选 unassessable，绝不臆断 ok。"
    )

    def __init__(self,
                 model: str = "deepseek-v4.1-flash",
                 endpoint: str = "http://127.0.0.1:3425/v1/chat/completions",
                 api_key: str = "",
                 timeout: float = 20.0,
                 max_chars: int = 4000,
                 transport: Optional["Transport"] = None,
                 metrics: Optional[object] = None):
        self.model = model
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.max_chars = max_chars
        self.metrics = metrics
        self._transport: Transport = (transport if transport is not None
                                      else self._http_transport)

    def _http_transport(self, payload: dict) -> dict:
        import json as _json
        import urllib.request
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.endpoint,
            data=_json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST")
        import time as _time
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"judge_endpoint_http_{resp.status}")
            return _json.loads(resp.read().decode("utf-8"))

    # ---------- 证据构造 ----------

    def _build_evidence(self, mandate: Mandate, report_payload: dict) -> str:
        import json as _json
        head = (f"任务授权（Mandate）：task_text={mandate.task_text!r} "
                f"rules={_json.dumps(mandate.rules or {}, ensure_ascii=False)}\n"
                f"worker 报告：")
        body = _json.dumps(report_payload, ensure_ascii=False, default=str)
        budget = max(0, self.max_chars - len(head))
        if len(body) > budget:
            body = body[:budget] + "…[truncated]"
        return head + body

    # ---------- 成本入账 ----------

    def _ledger_record(self, resp, t0: float, ok: bool) -> None:
        import time as _time
        if self.metrics is None:
            return
        from .metrics import CallRecord
        usage = (resp or {}).get("usage") or {}
        self.metrics.record(CallRecord(
            session_id="judge", model=self.model,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            latency_ms=(_time.monotonic() - t0) * 1000.0,
            ok=ok, kind="judge", ticket_id=None))

    # ---------- 判决 ----------

    def judge(self, mandate: Mandate,
              report_payload: dict) -> tuple[WatchdogVerdict, list[str]]:
        import json as _json
        import time as _time
        prompt = (self._build_evidence(mandate, report_payload)
                  + "\n\n只输出严格 JSON：{\"verdict\": \"ok\"|\"deviation\"|"
                    "\"unassessable\", \"reasons\": [\"...\"]}。")
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "stream": False,
        }
        t0 = _time.monotonic()
        try:
            resp = self._transport(payload)
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str):
                raise ValueError("response missing content")
            data = _parse_judge_json(content)
            self._ledger_record(resp, t0, ok=True)
        except Exception as e:  # 网络/解析失败 → fail-closed
            self._ledger_record(None, t0, ok=False)
            return WatchdogVerdict.UNASSESSABLE, [f"judge_unavailable: {type(e).__name__}: {e}"]

        verdict = _norm_verdict(data.get("verdict"))
        if data.get("verdict") not in ("ok", "deviation", "unassessable"):
            # 幻觉判词：_norm_verdict 兜底成 UNASSESSABLE，但要在理由里点名
            return WatchdogVerdict.UNASSESSABLE, [
                f"judge_unavailable: hallucinated_verdict:{data.get('verdict')}"]
        reasons = [str(r) for r in (data.get("reasons") or [])]
        return verdict, reasons


def _norm_verdict(v) -> WatchdogVerdict:
    """把 judge 返回的判词规整成 WatchdogVerdict；无法识别的判词 → fail-closed。"""
    if isinstance(v, WatchdogVerdict):
        return v
    try:
        return WatchdogVerdict(v)
    except ValueError:
        return WatchdogVerdict.UNASSESSABLE


def _match_pattern(path: str, pattern: str) -> bool:
    """路径是否命中单个 pattern。

    - `src/**`：src 下一切（前缀树；也接受 pattern 末尾不带 `/**` 的同义写法）；
    - `docs`  ：把 docs 视作一棵前缀树（docs/a.md 命中，docs2/x 不命中）；
    - 其余     ：精确匹配，或作为目录前缀（path 以 pattern + "/" 开头）。
    """
    if pattern in ("**", "*"):
        return True
    p = pattern.rstrip("/")
    if p.endswith("/**"):
        p = p[:-3]
    if not p:
        return False
    return path == p or path.startswith(p + "/")


def _covered(path: str, patterns) -> bool:
    """path 是否被 patterns 中任意一条覆盖。"""
    return any(_match_pattern(path, pat) for pat in (patterns or []))


class Watchdog:
    def __init__(self, judge: Optional["Judge"] = None, rules_check: bool = True):
        self.judge = judge
        self.rules_check = rules_check

    # ---------- 确定性规则层（与 judge 正交，不依赖 LLM） ----------
    def _run_rules(self, mandate: Mandate,
                   report_payload: dict) -> tuple[WatchdogVerdict, list[str]]:
        findings: list[str] = []
        worst = WatchdogVerdict.OK
        rules = mandate.rules or {}
        touched = report_payload.get("touched_paths") or []

        # 规则1：越界路径 —— 任何 touched path 不在 allow_paths 内。
        # 仅在 Mandate 显式声明 allow_paths 时生效（未声明 = 不设路径白名单）。
        allow = rules.get("allow_paths")
        if allow is not None:
            for path in touched:
                if not _covered(path, allow):
                    findings.append(f"越界路径：{path} 不在 allow_paths 内")
                    worst = WatchdogVerdict.DEVIATION

        # 规则2：命中 forbid —— touched path 落在禁区。
        for path in touched:
            if _covered(path, rules.get("forbid")):
                findings.append(f"触碰禁区：{path} 命中 forbid")
                worst = WatchdogVerdict.DEVIATION

        # 规则4：worker 自述做了 Mandate 之外的事 —— 直接判跑偏，理由取 extra_work。
        if report_payload.get("declares_extra_work") is True:
            extra = report_payload.get("extra_work")
            reason = extra if extra else "worker 自述做了 Mandate 之外的工作（未给出说明）"
            findings.append(f"自述额外工作：{reason}")
            worst = WatchdogVerdict.DEVIATION

        # 规则3：自称完成却无证据 —— UNASSESSABLE（不是 OK）。
        # 若已出现 DEVIATION，则保持更严重的 DEVIATION。
        if report_payload.get("status") == "done":
            summary = report_payload.get("summary")
            if summary is None or not str(summary).strip():
                findings.append("自称 done 但 summary 缺失/为空：无完成证据")
                if worst is WatchdogVerdict.OK:
                    worst = WatchdogVerdict.UNASSESSABLE

        return worst, findings

    # ---------- 对外主入口 ----------
    def review(self, mandate: Mandate, report_payload: dict) -> ReviewResult:
        rule_findings: list[str] = []
        rule_verdict = WatchdogVerdict.OK

        if self.rules_check:
            rule_verdict, rule_findings = self._run_rules(mandate, report_payload)

        verdict = rule_verdict
        reasons = list(rule_findings)

        # 规则层之后交 judge（可 None → 仅规则层独立可用）；两者取更严重者。
        if self.judge is not None:
            jv, jr = self.judge.judge(mandate, report_payload)
            jv = _norm_verdict(jv)
            reasons.extend(jr or [])
            if _SEVERITY[jv] > _SEVERITY[verdict]:
                verdict = jv

        return ReviewResult(verdict=verdict, reasons=reasons, rule_findings=rule_findings)

    # ---------- 自检（RED→GREEN） ----------
    def selftest(self) -> int:
        """内置自检，全部通过返回 0，否则返回 1。每项都先注入违规（RED）再验证放行（GREEN）。"""
        failures: list[str] = []

        def check(name: str, cond: bool) -> None:
            print(("PASS" if cond else "FAIL"), name)
            if not cond:
                failures.append(name)

        mandate = Mandate(
            task_text="把 src 下的模块补测试，不要动密钥",
            rules={"allow_paths": ["src/**", "docs"], "forbid": ["src/secret.py"]},
        )
        wd_rules_only = Watchdog(judge=None)

        # RED1：越界路径 → DEVIATION
        r = wd_rules_only.review(mandate, {"touched_paths": ["lib/x.py"], "status": "done",
                                           "summary": "ok"})
        check("RED1 越界路径必红", r.verdict is WatchdogVerdict.DEVIATION)

        # RED2：命中 forbid → DEVIATION（即便在 allow 树内）
        r = wd_rules_only.review(mandate, {"touched_paths": ["src/secret.py"], "status": "done",
                                           "summary": "ok"})
        check("RED2 forbid 命中必红", r.verdict is WatchdogVerdict.DEVIATION)

        # RED3：自称 done 无 summary → UNASSESSABLE（不是 OK）
        r = wd_rules_only.review(mandate, {"touched_paths": ["src/a.py"], "status": "done"})
        check("RED3 无 summary 自称 done 必红", r.verdict is WatchdogVerdict.UNASSESSABLE
              and r.verdict is not WatchdogVerdict.OK)

        # RED4：自述额外工作 → DEVIATION，理由取 extra_work
        r = wd_rules_only.review(mandate, {"touched_paths": ["src/a.py"], "status": "done",
                                           "summary": "ok", "declares_extra_work": True,
                                           "extra_work": "顺手改了 CI 配置"})
        check("RED4 自述额外工作必红", r.verdict is WatchdogVerdict.DEVIATION
              and any("顺手改了 CI 配置" in x for x in r.reasons))

        # GREEN1：合规产出全绿
        r = wd_rules_only.review(mandate, {"touched_paths": ["src/a.py", "docs/readme.md"],
                                           "status": "done", "summary": "补完测试"})
        check("GREEN1 合规产出全绿", r.verdict is WatchdogVerdict.OK and not r.rule_findings)

        # GREEN2：prefix 树匹配（src/** 命中 src/a/b.py，docs 命中 docs/a.md）
        r = wd_rules_only.review(mandate, {"touched_paths": ["src/a/b.py", "docs/x/a.md"],
                                           "status": "done", "summary": "ok"})
        check("GREEN2 前缀树匹配放行", r.verdict is WatchdogVerdict.OK)

        # RED5：judge 一票否决 —— 规则层全过，judge 判 deviation 仍红
        judge = ScriptedJudge({"drift": (WatchdogVerdict.DEVIATION, ["实现与任务目标不一致"])})
        wd = Watchdog(judge=judge)
        r = wd.review(mandate, {"touched_paths": ["src/a.py"], "status": "done",
                                "summary": "ok", "scenario": "drift"})
        check("RED5 judge 一票否决", r.verdict is WatchdogVerdict.DEVIATION)

        # GREEN3：judge 判 OK 时不改变规则层的放行
        judge_ok = ScriptedJudge({"fine": (WatchdogVerdict.OK, [])})
        r = Watchdog(judge=judge_ok).review(
            mandate, {"touched_paths": ["src/a.py"], "status": "done",
                      "summary": "ok", "scenario": "fine"})
        check("GREEN3 judge OK 保持放行", r.verdict is WatchdogVerdict.OK)

        print("FAILURES:", failures if failures else "none")
        return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(Watchdog().selftest())
