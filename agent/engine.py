"""Engine：编排器。对齐原型 docs/007 的 pass 语义——每个 pass 做一轮：
intake 准入 → 建票派工 → 收报告过 Watchdog → 治理拦截 → 回复出站（outbox 三角）。

Router 纪律（抄原型 prompt 层）：Engine 自己不做技术工作，只路由、校验、转发；
所有对外话术经由 outbox，所有判定落 ruling/日志，可审计。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict
from typing import Callable, Optional

from .admission import AdmissionConfig, decide
from .model import Assessment, Decision, Input, Mandate
from .outbox import pump as outbox_pump
from .store import Store
from .tickets import Tickets

# reason 词表（AO autoreview 借鉴）：engine 产生的每条 ruling 都必须带
# 词表内的 reason——可枚举、可统计、可审计，禁止裸字符串散落。
REASONS = frozenset({
    "deviation_repair_limit",     # 复审次数用尽，票被 deviation 终止
    "budget_tokens_exceeded",     # 票级 token 预算超限（AGT budgets.rego 语义）
    "governance_path_block",      # 写前治理拦截（路径越界/未声明）
    "escalation_requested",       # 人工放行登记
    "escalation_lifted",          # 一次性放行被消费
    "idle_timeout",               # 调度器 idle 回收
    "hallucinated_rule",          # 研判幻觉规则名，fail-closed
    "failed_same_sha_retry_limit",  # 连续 watchdog 非 OK 复审次数超上限（AO autoreview=3）
})


class Engine:
    def __init__(self, store: Store, cfg: AdmissionConfig, executor,
                 deliver: Callable[[str, str], bool],
                 watchdog=None, governance=None, proactivity=None,
                 assessor=None, max_repairs: int = 1,
                 confidence_estimator=None,
                 input_event_evidence: Optional[dict] = None,
                 max_review_failures: int = 3):
        self.store = store
        self.cfg = cfg
        self.executor = executor
        self.deliver = deliver          # (channel, body) -> bool
        self.watchdog = watchdog        # .review(mandate, report) -> ReviewResult
        self.governance = governance    # .claim/.check/.ruling
        self.proactivity = proactivity  # .evaluate(CandidateEvent) -> Decision
        self.assessor = assessor        # mention 消息的研判层
        self.max_repairs = max_repairs  # deviation 复审环：修复机会次数（0=直接终止）
        self.confidence_estimator = confidence_estimator  # I5：开口确证度的证据源合成
        # D1b：输入侧开口候选的可注入证据（None → 由输入信号自动构造）
        self.input_event_evidence = input_event_evidence
        # D2：连续 watchdog 非 OK 报告上限（对齐 AO autoreview 的 3）
        self.max_review_failures = max_review_failures
        self.tickets = Tickets(store)
        # 修复计数存 store kv "repairs:{tid}"（唯一事实源，v0.3.1 修复）：
        # 内存 dict 在崩溃恢复后清零 → max_repairs 形同虚设（RED 见 test_v031）
        self.notices: list[dict] = []   # Router 主会话收到的通知（进度地图）
        self.observe_log: list[dict] = []    # I5：observe 判定的标注旁路（原始事件三数）
        self._rulings: list[dict] = []  # 无 governance 时的本地判决留痕

    # ---------- 判决留痕（统一入口：reason 必须在词表内） ----------
    def _ruling(self, record: dict) -> None:
        rec = dict(record)
        rec.setdefault("reason", "unspecified")
        if rec["reason"] not in REASONS:
            raise ValueError(f"ruling reason 不在词表内: {rec['reason']!r}，"
                             f"合法词表 = {sorted(REASONS)}")
        rec.setdefault("at", time.time())
        self._rulings.append(rec)
        if self.governance is not None:
            self.governance.ruling(rec)

    def governance_rulings(self) -> list[dict]:
        """读回判决流：有 governance 走它的 append-only 流，否则本地副本。"""
        if self.governance is not None:
            return self.governance.read_rulings()
        return list(self._rulings)

    # ---------- 入口：一条消息进入 ----------
    def intake(self, inp: Input, mandate_rules: Optional[dict] = None,
               budget_tokens: Optional[int] = None) -> dict:
        """mandate_rules: 可选的 Mandate 附加规则（如 {"allow_paths": [...]}），
        建 Mandate 时带上——治理预声明依赖它。
        budget_tokens: 票级 token 预算（AGT budgets.rego 借鉴）——累计用量
        超限即终止票 + 落 ruling reason=budget_tokens_exceeded（成本从
        "事后度量"升级为"事中硬闸"）。"""
        # 幂等：同 id 消息只处理一次（先存后做）
        if not self.store.record_receipt(f"input:{inp.id}", inp.text):
            return {"disposition": "duplicate", "input_id": inp.id}

        thread_open = {}
        ot = self.tickets.open_ticket_on(inp.thread_key)
        if ot:
            thread_open[inp.thread_key] = ot

        disposition, assessment = decide(inp, self.cfg, thread_open, self.assessor)

        # D1b：输入侧候选事件（仅 observe 模式；不写回复、不改 disposition）
        self._maybe_input_candidate(inp, disposition)

        if disposition == "follow_up":
            t = self.tickets.get(ot)
            self.tickets.add_input(ot, inp)
            return {"disposition": "follow_up", "ticket_id": ot}

        if disposition == "accept":
            ticket_id = self._open_ticket(inp, assessment, mandate_rules)
            if budget_tokens is not None:
                self.store.put(f"budget:{ticket_id}",
                               {"tokens": int(budget_tokens),
                                "set_at": time.time()})
            return {"disposition": "accept", "ticket_id": ticket_id}

        # rejected_* / observe：静默是产品能力，但判定必须留痕
        self.store.put(f"rejected:{inp.id}",
                       {"disposition": disposition, "text": inp.text[:200],
                        "at": time.time()})
        return {"disposition": disposition, "input_id": inp.id}

    def _maybe_input_candidate(self, inp: Input, disposition: str) -> None:
        """D1b：输入侧 wait/drop 候选事件（observe 模式专属标注旁路）。

        仅对闲聊/被拒输入造低效用候选：accept / follow_up / duplicate 已有
        任务完成侧的开口事件，避免重复计数（I5 旧测试断言 observe_log 长度为 1）。
        不写回复、不改 disposition；非 observe 模式零副作用。
        """
        pro = self.proactivity
        if pro is None:
            return
        if not getattr(getattr(pro, "cfg", None), "observe", False):
            return
        if not (disposition.startswith("rejected") or disposition == "observe"):
            return

        from .proactivity import CandidateEvent  # 延迟 import，容忍缺模块
        if self.input_event_evidence is None:
            markers = getattr(self.cfg, "explicit_markers", ())
            evidence = {
                "has_explicit_marker": any(
                    inp.text.startswith(m) or f" {m}" in inp.text for m in markers),
                "is_bot": bool(inp.is_bot),
                "text_len": len(inp.text),
                "tool_rounds": 0,
                "repairs": 0,
            }
        else:
            evidence = dict(self.input_event_evidence)
        utility = float(evidence.pop("utility", 0.01))
        if self.confidence_estimator is not None:
            conf = self.confidence_estimator.estimate(evidence)
        else:
            conf = 0.95
        ev = CandidateEvent(event_id=f"input:{inp.id}", utility=utility,
                            confidence=conf, created_at=time.time())
        dec = pro.evaluate(ev)
        if dec.action.startswith("observe_"):
            # 字段与 on_worker_report 的 observe_log 条目逐字一致；
            # 输入侧没有 watchdog 结论，记 unassessable。
            self.observe_log.append({
                "event_id": ev.event_id, "utility": ev.utility,
                "confidence": ev.confidence, "created_at": ev.created_at,
                "p_star": dec.p_star, "u_effective": dec.u_effective,
                "action": dec.action, "watchdog_verdict": "unassessable"})

    def _open_ticket(self, inp: Input, assessment: Optional[Assessment],
                     mandate_rules: Optional[dict] = None) -> str:
        decision = Decision(
            id=f"d-{uuid.uuid4().hex[:8]}", trigger_input_id=inp.id,
            thread_keys=[inp.thread_key],
            assessment=assessment or Assessment(True, "explicit_marker",
                                                intent_evidence="explicit"),
            mandate=Mandate(task_text=inp.text, rules=mandate_rules or {}),
            created_at=time.time())
        evidence = "explicit marker"
        if assessment and assessment.intent_evidence:
            evidence = assessment.intent_evidence
        self.store.put(f"decision:{decision.id}", asdict(decision))

        t = self.tickets.create(decision.id, decision.mandate,
                                channels=[f"thread:{inp.thread_key}"],
                                thread_keys=[inp.thread_key])
        self.tickets.add_input(t.id, inp)

        # 治理：按 Mandate 的 allow_paths 预声明（worker 写前还有二次 check）
        if self.governance is not None:
            allow = decision.mandate.rules.get("allow_paths")
            if allow:
                self.governance.claim(t.id, allow)

        # 先造 assignment 壳，launch 回填 session_id（launch 需要 assignment 引用）
        from .model import Assignment
        ticket = self.tickets.get(t.id)
        a = Assignment(ticket_id=t.id, session_id="")
        a.session_id = self.executor.launch(a, ticket)
        ticket.assignments.append(a)
        # 投递未送达输入
        for iid in list(ticket.undelivered):
            inp_i = next(i for i in ticket.inputs if i.id == iid)
            if self.executor.deliver(a, inp_i):
                self.tickets.mark_delivered(t.id, iid)
        self.tickets.get(t.id).assignments[-1].delivered_at = time.time()
        # I6 idle 语义：交付完成即进入 idle 等待（调度器据此回收超时会话）
        self.tickets.get(t.id).assignments[-1].idle_at = time.time()
        self.tickets._persist(self.tickets.get(t.id))
        return t.id
    # ---------- worker 报告回调 ----------
    def _tokens_used(self, ticket_id: str) -> int:
        """该票累计 token 用量（从 executor 的 metrics 台账读）。
        executor 无台账时返回 0（预算闸自动旁路——没有计量就没有硬闸）。"""
        metrics = getattr(self.executor, "metrics", None)
        if metrics is None:
            return 0
        total = 0
        for r in getattr(metrics, "records", []):
            if getattr(r, "ticket_id", None) == ticket_id:
                total += int(getattr(r, "prompt_tokens", 0) or 0)
                total += int(getattr(r, "completion_tokens", 0) or 0)
        return total

    def on_worker_report(self, assignment, report_payload: dict) -> dict:
        """Watchdog 复核 → 预算闸 → 治理判决 → 回复出站。"""
        ticket = self.tickets.get(assignment.ticket_id)
        verdict_label = "ok"
        if self.watchdog is not None:
            review = self.watchdog.review(ticket.mandate, report_payload)
            verdict_label = review.verdict.value
            self.store.put(f"watchdog:{assignment.ticket_id}:{report_payload.get('summary','')[:32]}",
                           {"verdict": verdict_label, "reasons": review.reasons,
                            "at": time.time()})
            # D2：按票统计连续 watchdog 非 OK 报告；OK 清零，落库可恢复。
            # 第 max_review_failures+1 次失败直接熔断，不进修复环、不回复。
            fail_key = f"review_fail:{assignment.ticket_id}"
            if verdict_label == "ok":
                self.store.put(fail_key, {"count": 0, "at": time.time()})
            else:
                prev = self.store.get(fail_key) or {}
                streak = int(prev.get("count", 0) or 0) + 1
                self.store.put(fail_key, {"count": streak, "at": time.time()})
                if streak > self.max_review_failures:
                    self._ruling({
                        "ticket_id": assignment.ticket_id,
                        "verdict": "DEVIATION",
                        "reason": "failed_same_sha_retry_limit",
                        "reasons": list(review.reasons),
                        "consecutive_failures": streak})
                    self.tickets.end(assignment.ticket_id)
                    return {"verdict": verdict_label, "replied": False,
                            "escalated": True}
            if review.verdict.value == "deviation":
                # 复审环（I4）：修复次数没用完 → 不终止票，把违规理由
                # 作为修复指令重投 worker，下一份报告再复核。
                # 修复计数以 store kv 为唯一事实源（崩溃恢复后不清零）
                rec = self.store.get(f"repairs:{assignment.ticket_id}")
                used = int(rec.get("count", 0)) if rec else 0
                if used < self.max_repairs:
                    self.store.put(f"repairs:{assignment.ticket_id}",
                                   {"count": used + 1, "at": time.time()})
                    tk = ticket.channels[0][len("thread:"):] if ticket.channels else "inbox"
                    repair = Input(
                        id=f"repair:{assignment.ticket_id}:{used + 1}",
                        thread_key=tk,
                        text="修复指令：" + "；".join(review.reasons)[:2000],
                        is_human=True)
                    self.tickets.add_input(assignment.ticket_id, repair)
                    n_notices = len(self.notices)
                    if self.executor.deliver(assignment, repair):
                        self.tickets.mark_delivered(assignment.ticket_id, repair.id)
                    # 修复重投可能同步触发 worker 新报告（echo 闭环）：
                    # 复核转 ok 即视为修复后已回复
                    reply_after = any(
                        x.get("kind") == "worker_report" and x.get("verdict") == "ok"
                        for x in self.notices[n_notices:])
                    self.notices.append({"kind": "repair_round",
                                         "ticket_id": assignment.ticket_id,
                                         "round": used + 1})
                    return {"verdict": verdict_label, "replied": False,
                            "repaired": True, "repair_round": used + 1,
                            "reply_after_repair": reply_after}
                self._ruling({
                    "ticket_id": assignment.ticket_id,
                    "verdict": "DEVIATION", "reason": "deviation_repair_limit",
                    "reasons": review.reasons})
                self.tickets.end(assignment.ticket_id)
                return {"verdict": verdict_label, "replied": False, "escalated": True}

        # 预算闸（②）：复核证据已留痕，这里做成本硬闸——超限即终止，不回复。
        budget = self.store.get(f"budget:{assignment.ticket_id}")
        if budget is not None:
            used = self._tokens_used(assignment.ticket_id)
            if used > int(budget.get("tokens", 0)):
                self._ruling({
                    "ticket_id": assignment.ticket_id,
                    "verdict": "BUDGET_EXCEEDED",
                    "reason": "budget_tokens_exceeded",
                    "used_tokens": used,
                    "budget_tokens": int(budget.get("tokens", 0))})
                self.tickets.end(assignment.ticket_id)
                return {"verdict": "budget_exceeded", "replied": False}

        # 主动性：任务完成是一个候选开口事件。
        # confidence 来自证据源合成（I5）：缺 estimator 时保守用历史硬编码值。
        if self.proactivity is not None:
            from .proactivity import CandidateEvent  # 延迟 import，容忍缺模块
            evidence = {"watchdog_pass": 1.0 if verdict_label == "ok" else 0.0}
            if self.confidence_estimator is not None:
                conf = self.confidence_estimator.estimate(evidence)
            else:
                conf = 0.95
            ev = CandidateEvent(
                event_id=f"done:{assignment.ticket_id}",
                utility=10.0, confidence=conf, created_at=time.time())
            dec = self.proactivity.evaluate(ev)
            if dec.action.startswith("observe_"):
                # observe 模式的标注旁路：原始事件三数 + 判定依据一起留，
                # 可直接喂 calibration.LabelStore.append（I5 闭环）。
                self.observe_log.append({
                    "event_id": ev.event_id, "utility": ev.utility,
                    "confidence": ev.confidence, "created_at": ev.created_at,
                    "p_star": dec.p_star, "u_effective": dec.u_effective,
                    "action": dec.action, "watchdog_verdict": verdict_label})

        # 回复走 outbox 三角：receipt 已在 intake 记过，这里补回复的 receipt
        rid = f"reply:{assignment.ticket_id}:{uuid.uuid4().hex[:6]}"
        self.store.record_receipt(rid, report_payload.get("summary", ""))
        body = f"[ticket {assignment.ticket_id}] {report_payload.get('summary', '')}"
        for ch in ticket.channels:
            self.store.enqueue(f"{rid}:{ch}", rid, ch, body)
        self.notices.append({"kind": "worker_report", "ticket_id": assignment.ticket_id,
                             "verdict": verdict_label})
        return {"verdict": verdict_label, "replied": True}

    # ---------- 写前治理挂点（executor 层调用） ----------
    def pre_write(self, ticket_id: str, path: str,
                  content_hash: str | None = None) -> bool:
        """worker 要写文件时先问治理。True=放行。
        content_hash 可选：escalate 档消费一次性人工放行（路径+内容哈希绑定）。"""
        if self.governance is None:
            # ADR-0001 fail-closed：治理挂点缺位时默认拒绝，绝不默认放行。
            # （S3 曾记录此处 return True 的 fail-open 接线，现按规范收口。）
            self._ruling({"ticket_id": ticket_id, "verdict": "BLOCK",
                          "reason": "governance_path_block",
                          "path": path,
                          "detail": "governance_absent: 治理挂点缺位，fail-closed"})
            return False
        res = self.governance.check(ticket_id, path, content_hash=content_hash)
        if res.verdict != "ALLOW":
            self._ruling({"ticket_id": ticket_id, "verdict": "BLOCK",
                          "reason": "governance_path_block",
                          "path": path, "detail": res.reason})
            return False
        return True

    # ---------- 出站 pump ----------
    def flush_outbox(self, now: Optional[float] = None) -> dict:
        return outbox_pump(self.store, now if now is not None else time.time(),
                           self.deliver)

    # ---------- 崩溃恢复 ----------
    def resume(self) -> int:
        """重开库后回灌票/线程状态（I1），再统计 pending 效果数（outbox 继续 pump）。
        返回 pending 数。"""
        self.tickets.load_all()
        return self.store.pending_outbox_count()
