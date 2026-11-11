# GOVERNANCE.md — superagent-build 治理契约（机器可读规范）

Status: normative conformance contract for the checked-in `agent/` kernel.
Baseline: `pyproject.toml:7` `version = "0.3.1"`, `pyproject.toml:24`
`dependencies = []  # 纯 stdlib 内核，零依赖`.
Every `file:line` anchor below refers to the working tree as read while writing this
spec. Every number, path and identifier is copied verbatim from the cited source
line; nothing is inferred. The tree is under concurrent edit by other agents
(`agent/engine.py`, `agent/calibration.py`, `agent/assessor.py` modified during this
work) — anchors were re-verified after those edits; see §8 for the delta.

Conformance suite: `tests/test_conformance.py`. One test function per MUST, each with
RED (inject an explicit violation, assert it is rejected / blocked / not persisted)
then GREEN (compliant path admitted). Pure stdlib + pytest, `tmp_path` per test.

## 0. RFC2119 keywords

- **MUST** / **MUST NOT**: absolute requirement. A violation is a conformance failure.
- **SHOULD**: recommended; a deviation MUST be recorded in §8 instead of being hidden.
- Terms are used exactly as in RFC 2119 (BCP 14); this document does not redefine them.

## 1. Eight intervention points (enumerate all; mark reality, leave blanks honest)

An "intervention point" is a place where a verdict from the closed set of §2 can stop,
escalate or admit an effect before the effect is persisted. Table is exhaustive: 8 rows.

| # | Intervention point | 本仓现状 | Evidence |
|---|---|---|---|
| 1 | `agent_startup` | **未实现（诚实留白）** — 无启动前裁决挂点 | — |
| 2 | `input` | **已实现** — admission gate + assessor 硬校验 + intake receipt 幂等 | `agent/admission.py:42-67` `gate()`；`agent/admission.py:89-116` `decide()`；`agent/engine.py:82-120` `intake()` |
| 3 | `pre_model_call` | **未实现（诚实留白）** — 模型调用前无裁决 | `agent/executing_executor.py:168-172`（组 payload 后直接 `self._transport(payload)`） |
| 4 | `post_model_call` | **未实现（诚实留白）** — 响应解析前无裁决 | `agent/executing_executor.py:175-188` |
| 5 | `pre_tool_call` | **部分实现** — 仅 `pre_write` / `write_file` / `append_file` 三路径被裁决；其它工具名一律 `unknown_tool` 拒绝 | `agent/executing_executor.py:132-153`；`agent/engine.py:335-347` |
| 6 | `post_tool_call` | **未实现（诚实留白）** — 工具结果回填前无复核 | `agent/executing_executor.py:201-208`（`_exec_tool` 结果直接 append 为 tool message） |
| 7 | `output` | **已实现** — watchdog 复核 → D2 复审熔断 → 修复环 → 预算闸 → `receipt` → `outbox` → `pump` | `agent/engine.py:222-332`；`agent/outbox.py:26-52` |
| 8 | `agent_shutdown` | **未实现（诚实留白）** — 无收尾裁决挂点 | — |

Rule: an intervention point marked 未实现 MUST NOT be described as enforced anywhere
in this spec. The 8-point enumeration is a coverage map, not a claim of coverage.

## 2. Verdict closed set: ALLOW / BLOCK / ESCALATE

The closed set has exactly three states: `ALLOW`, `BLOCK`, `ESCALATE`. No fourth
verdict exists.

- `CheckResult.verdict` is literally `# "ALLOW" | "BLOCK"` (`agent/governance.py:36`).
  `ALLOW` is returned at `agent/governance.py:119-120` and `agent/governance.py:135-137`;
  `BLOCK` at `agent/governance.py:112-115` and `agent/governance.py:139-140`.
- `ESCALATE` is **not** a third return value of `check()`: it is the third state of
  the ruling stream. `request_escalation()` appends
  `{"path": norm, "content_hash": content_hash, "approver": approver, "consumed": False}`
  and writes ruling `reason: "escalation_requested"`, `verdict: "ESCALATION_REQUESTED"`
  (`agent/governance.py:142-159`). `check()` consumes an unconsumed lift only when
  `content_hash is not None` (`agent/governance.py:122`) and
  `lf.get("path") == norm and lf.get("content_hash") == content_hash`
  (`agent/governance.py:127`); it sets `consumed = True`, writes ruling
  `verdict: "ESCALATE_LIFTED"`, `reason: "escalation_lifted"` and returns `ALLOW`
  (`agent/governance.py:128-137`). One lift = one `ALLOW`; second identical call is
  `BLOCK` (`agent/governance.py:139-140`). `content_hash=None` MUST NOT consume any
  lift.
- Downstream closure: `Engine.pre_write` maps anything that is not `ALLOW` to a
  `BLOCK` ruling with `reason = "governance_path_block"` and returns `False`
  (`agent/engine.py:347-352`); when `self.governance is None` it MUST also emit the
  same `BLOCK` with `detail: "governance_absent: 治理挂点缺位，fail-closed"` and
  return `False` (`agent/engine.py:339-346`); `ExecutingToolLoopExecutor._exec_tool`
  refuses all writes when the gate is absent or returns false
  (`agent/executing_executor.py:144-148`).

## 3. `agent/engine.py` module-level `REASONS` — verbatim (`agent/engine.py:20-31`)

```python
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
```

Exactly 8 literals (`agent/engine.py:23-30`). `_ruling()` does
`rec.setdefault("reason", "unspecified")` then
`if rec["reason"] not in REASONS: raise ValueError(f"ruling reason 不在词表内: {rec['reason']!r}，" f"合法词表 = {sorted(REASONS)}")`
(`agent/engine.py:64-73`). Therefore `"unspecified"` also raises: a ruling without a
whitelisted reason MUST NOT be persisted.

## 4. fail-closed invariants and ADR-0001

**ADR-0001 (decision record)**: 治理闸一律 fail-closed；实现 **MUST NOT** 提供把闸
开成 fail-open 的配置开关。缺依赖（研判层 / `write_gate` / judge / 计量台账）时
默认拒绝，不默认放行。ADR-0001 正文尚未落盘：本仓当前没有 `docs/adr/` 目录，本规范
把该决策固化成下列可测判据；ADR 文件落盘后以其为准。

Consequences enforced by code:

- `admission.decide()` with `assessor is None` → `"rejected_eval", None`
  (`agent/admission.py:110-111`); bot not in `bot_allowlist` → `GateResult("bot", [])`
  → `"rejected_bot"` (`agent/admission.py:48-49`, `agent/admission.py:100-101`);
  `accept` with empty `intent_evidence` → `"accept_without_evidence"`
  (`agent/admission.py:78-79`); rule name not emitted by `gate()` →
  `f"hallucinated_rule:{a.matched_rule}"` (`agent/admission.py:81-82`).
- `Watchdog._run_rules()` yields `UNASSESSABLE` (not `OK`) when `status == "done"`
  and `summary` is empty/missing (`agent/watchdog.py:274-279`); `LLMJudge.judge()`
  returns `UNASSESSABLE` on exception/parse failure with
  `f"judge_unavailable: {type(e).__name__}: {e}"` (`agent/watchdog.py:192-194`)
  and on an out-of-vocabulary verdict with
  `f"judge_unavailable: hallucinated_verdict:{data.get('verdict')}"`
  (`agent/watchdog.py:196-200`).
- `ExecutingToolLoopExecutor._exec_tool()` with `write_gate is None` →
  `"[governance] BLOCK: write_gate_required (治理挂点缺位，fail-closed 拒绝一切写操作)"`
  (`agent/executing_executor.py:144-146`); unknown tool →
  `"[governance] error: unknown_tool:{name}"` (`agent/executing_executor.py:153`).
  `Engine.pre_write()` 在 `self.governance is None` 时同样 fail-closed：落
  `verdict: "BLOCK"` / `reason: "governance_path_block"` /
  `detail: "governance_absent: 治理挂点缺位，fail-closed"` 并 `return False`
  (`agent/engine.py:339-346`)，该收口由 M22 直接验证。
- `_default_host_exec()` adds `os.O_NOFOLLOW` when `hasattr(os, "O_NOFOLLOW")`
  (`agent/executing_executor.py:44-45`) and returns `False` on `OSError`
  (`agent/executing_executor.py:53-54`).
- `Scheduler.tick()` with failing `pre_deliver_check` does
  `skipped_lease += 1; continue` (`agent/scheduler.py:77-80`), i.e. no dispatch.

**Known fail-open-capable wiring** (reported, not silently accepted — see §8):
`agent/engine.py:213-214` (`_tokens_used` returns `0` when executor has no `metrics`),
`agent/watchdog.py:288-302` (`Watchdog(judge=None, rules_check=False)` returns `OK`).
Neither is a configuration switch that this spec normalizes as fail-open. The
`pre_write` gap that previously appeared here was closed during this work by another
agent (`agent/engine.py:339-346`); M22 now asserts it as a MUST.

## 5. Budget, review fuses, symlink, receipt→outbox ordering

- **预算闸**: `intake(budget_tokens=...)` persists
  `self.store.put(f"budget:{ticket_id}", {"tokens": int(budget_tokens), "set_at": time.time()})`
  (`agent/engine.py:110-113`). `_tokens_used()` sums `prompt_tokens` +
  `completion_tokens` over `metrics.records` where `ticket_id` matches, and returns
  `0` when `metrics is None` (`agent/engine.py:209-220`). `on_worker_report()` does
  `if used > int(budget.get("tokens", 0))` (`agent/engine.py:292`) → `_ruling` with
  `verdict: "BUDGET_EXCEEDED"`, `reason: "budget_tokens_exceeded"` →
  `self.tickets.end(...)` → `{"verdict": "budget_exceeded", "replied": False}`
  (`agent/engine.py:288-300`). No reply receipt, no outbox enqueue on that path.
- **复审熔断（两级，顺序即代码顺序）**:
  1. D2 连续失败熔断（`agent/engine.py:232-250`）：`fail_key = f"review_fail:{assignment.ticket_id}"`；
     `verdict_label == "ok"` 清零，否则 `streak = int(prev.get("count", 0) or 0) + 1`；
     `if streak > self.max_review_failures:` → `_ruling` with
     `reason: "failed_same_sha_retry_limit"`, `consecutive_failures: streak` →
     `tickets.end` → `{"verdict": verdict_label, "replied": False, "escalated": True}`。
     `max_review_failures: int = 3` (`agent/engine.py:41`)。
  2. deviation 修复环（`agent/engine.py:251-286`）：计数以 store kv
     `f"repairs:{assignment.ticket_id}"` 为唯一事实源（`agent/engine.py:255-259`）；
     `max_repairs: int = 1` (`agent/engine.py:38`)；`if used < self.max_repairs` →
     repair `Input(id=f"repair:{assignment.ticket_id}:{used + 1}", ...)` 重投并
     返回 `"repaired": True`；否则 `_ruling` 带 `reason: "deviation_repair_limit"` +
     `tickets.end` + `"escalated": True`。
- **symlink `O_NOFOLLOW`**: `flags = os.O_WRONLY | os.O_CREAT`; `os.O_APPEND` for
  `append`; `os.O_NOFOLLOW` when available; `os.open(path, flags, 0o644)`
  (`agent/executing_executor.py:41-48`). Windows has no `O_NOFOLLOW`; capability
  boundary is declared in the docstring (`agent/executing_executor.py:29-37`).
  Together with `posixpath.normpath(path)` at `agent/governance.py:47` this is the
  two-layer defence: lexical traversal at policy layer, symlink leaf at kernel layer.
- **receipt→outbox 先存后做**: `Store` is SQLite `journal_mode=WAL` +
  `synchronous=FULL` (`agent/store.py:21-22`). `record_receipt()` INSERT,
  `sqlite3.IntegrityError` → `False` (`agent/store.py:58-67`). `enqueue()` raises
  `ValueError(f"enqueue before receipt: {message_id} needs receipt {receipt_id}")`
  when `not self.has_receipt(receipt_id)` (`agent/store.py:76-77`); duplicate
  `message_id` → `False` (`agent/store.py:84-85`). `intake()` calls
  `record_receipt(f"input:{inp.id}", inp.text)` **before** any effect and returns
  `{"disposition": "duplicate", "input_id": inp.id}` on repeat
  (`agent/engine.py:90-91`). Reply path records
  `rid = f"reply:{assignment.ticket_id}:{uuid.uuid4().hex[:6]}"` then
  `self.store.enqueue(f"{rid}:{ch}", rid, ch, body)` for each channel
  (`agent/engine.py:324-332`). `backoff_next(attempts) = min(BACKOFF_BASE * (2 ** attempts), BACKOFF_CAP)`
  with `BACKOFF_BASE = 5.0`, `BACKOFF_CAP = 300.0` (`agent/outbox.py:15-16`,
  `agent/outbox.py:22-23`); `pump(store, now, deliver, max_attempts: int = 6)`
  (`agent/outbox.py:26-27`) calls `deliver(msg["channel"], msg["body"])`
  (`agent/outbox.py:37`) and on `False` does
  `store.mark_retry(msg["message_id"], now + backoff_next(msg["attempts"]))`
  (`agent/outbox.py:50`). `abandon()` accepts only `("channelGone", "channelIgnored")`
  (`agent/store.py:107-108`).

## 6. MUST clauses (M1..M22) — each row names its conformance test on the same line

| ID | Normative MUST / MUST NOT clause | Code anchor | Conformance test |
|---|---|---|---|
| M1 | 未 `claim` 或已 `release` 的 ticket MUST NOT 写：MUST 返回 `BLOCK`，理由为 `ticket {ticket_id} 无活动作用域声明——没有声明就不许写`；已声明且命中才 MUST 返回 `ALLOW` | `agent/governance.py:112-120`, `agent/governance.py:161-167` | `tests/test_conformance.py::test_m1_undeclared_scope_write_blocked` |
| M2 | 作用域匹配 MUST 按 `/**` 前缀树（`path.startswith(pattern[:-2])`）或精确/目录前缀（`path == pattern or path.startswith(pattern.rstrip("/") + "/")`）判定，`docsx/a.md` MUST NOT 命中 `docs` | `agent/governance.py:43-50` | `tests/test_conformance.py::test_m2_scope_pattern_boundaries` |
| M3 | 写路径 MUST 先 `posixpath.normpath` 归一；`work/../secret` 这类文本穿越 MUST NOT 靠 `work/**` 拿到 `ALLOW` | `agent/governance.py:47`, `agent/governance.py:116` | `tests/test_conformance.py::test_m3_lexical_traversal_normalized` |
| M4 | 每条 ruling 的 `reason` MUST 属于 `REASONS` 八词表且 MUST NOT 是 `"unspecified"`；越界 MUST `raise ValueError(f"ruling reason 不在词表内: {rec['reason']!r}，" f"合法词表 = {sorted(REASONS)}")` 且不落痕 | `agent/engine.py:22-31`, `agent/engine.py:64-73` | `tests/test_conformance.py::test_m4_ruling_reason_vocabulary_closed` |
| M5 | `pre_write` 收到非 `ALLOW` MUST 落 `verdict: "BLOCK"` + `reason: "governance_path_block"` 并返回 `False`；`ALLOW` MUST 返回 `True` 且不留 `BLOCK` 判决 | `agent/engine.py:335-352` | `tests/test_conformance.py::test_m5_pre_write_block_rules_and_denies` |
| M6 | escalate 一次性 lift MUST 与 `(path, content_hash)` 精确绑定：`content_hash=None`、错哈希、错路径 MUST NOT 消费；命中 MUST `ALLOW` 一次并写 `verdict: "ESCALATE_LIFTED"` / `reason: "escalation_lifted"`，第二次同参 MUST `BLOCK`；对无活动作用域的 ticket 登记 MUST `raise ValueError` | `agent/governance.py:122-159` | `tests/test_conformance.py::test_m6_escalation_lift_one_shot_hash_bound` |
| M7 | 判决流 `rulings.jsonl` MUST 以 `os.O_WRONLY \| os.O_CREAT \| os.O_APPEND`, `0o644` + `flock` 追加，MUST NOT 整文件重写；已有行 MUST 原样保留且跨实例可读回；坏行 MUST NOT 被静默吞掉 | `agent/governance.py:185-209` | `tests/test_conformance.py::test_m7_rulings_stream_append_only` |
| M8 | `enqueue` MUST 要求 `receipt` 已存在，缺 receipt MUST `raise ValueError(f"enqueue before receipt: {message_id} needs receipt {receipt_id}")` 且 MUST NOT 落 outbox 行；先 `record_receipt` 再 `enqueue` MUST 成功入队；重复 `message_id` MUST 返回 `False` | `agent/store.py:58-85`, `agent/engine.py:324-332` | `tests/test_conformance.py::test_m8_enqueue_requires_receipt` |
| M9 | `abandon` 的原因 MUST 属于闭合集 `("channelGone", "channelIgnored")`，其它原因 MUST `raise ValueError(f"illegal abandon reason: {reason}")` 且 MUST NOT 改 outbox 状态；合法放弃 MUST 移出 pending | `agent/store.py:105-111` | `tests/test_conformance.py::test_m9_abandon_reason_closed_set` |
| M10 | 投递返回 `False` MUST `mark_retry(now + backoff_next(attempts))` 且 MUST NOT 丢弃消息；`backoff_next(0) == 5.0`、`backoff_next(20) == 300.0`；`deliver` 返回 `True` MUST `mark_sent` | `agent/outbox.py:15-16`, `agent/outbox.py:22-23`, `agent/outbox.py:26-52` | `tests/test_conformance.py::test_m10_failed_delivery_backoff_not_dropped` |
| M11 | 配置 `budget:{ticket_id}` 且 executor 有 `metrics` 时，`used > int(budget.get("tokens", 0))` MUST 落 `verdict: "BUDGET_EXCEEDED"` / `reason: "budget_tokens_exceeded"`、MUST `tickets.end`、MUST NOT 回复（`"replied": False`）；未超限 MUST 回复 | `agent/engine.py:110-113`, `agent/engine.py:209-220`, `agent/engine.py:288-300` | `tests/test_conformance.py::test_m11_budget_gate_terminates_ticket` |
| M12 | deviation 复审次数以 store kv `repairs:{tid}` 为唯一事实源：`used < self.max_repairs` MUST 重投修复且 `"repaired": True`；用尽 MUST 落 `reason: "deviation_repair_limit"` + `tickets.end` + `"escalated": True`、MUST NOT 回复；`ok` 报告 MUST 回复 | `agent/engine.py:38`, `agent/engine.py:251-286` | `tests/test_conformance.py::test_m12_deviation_repair_circuit_breaker` |
| M13 | 准入 MUST fail-closed：bot 非 `bot_allowlist` → `rejected_bot`；无 `assessor` → `rejected_eval`；`accept` 空证据 → `accept_without_evidence`；幻觉规则名 → `hallucinated_rule:{a.matched_rule}`；白名单显式指令 `/do` MUST `accept` 且 `matched_rule == "explicit_marker"` | `agent/admission.py:26-30`, `agent/admission.py:42-67`, `agent/admission.py:72-116` | `tests/test_conformance.py::test_m13_admission_fail_closed` |
| M14 | watchdog MUST NOT 把无证据/不可判说成 `OK`：`done` 空 `summary` → `UNASSESSABLE`；judge 异常 → `judge_unavailable: {type(e).__name__}: {e}`；幻觉判词 → `hallucinated_verdict`；`forbid` 命中 → `DEVIATION`；合规报告 MUST `OK` | `agent/watchdog.py:192-212`, `agent/watchdog.py:243-302` | `tests/test_conformance.py::test_m14_watchdog_unassessable_not_ok` |
| M15 | 写工具 MUST 经 `write_gate`：`write_gate is None` MUST 返回 `write_gate_required` 且 MUST NOT 调 `host_exec`；gate 返回 `False` MUST 返回 `未过治理，禁止落盘` 且 MUST NOT 落盘；未知工具 MUST 返回 `unknown_tool:{name}`；gate `True` MUST 真落盘 | `agent/executing_executor.py:26`, `agent/executing_executor.py:132-153` | `tests/test_conformance.py::test_m15_write_gate_required_and_unknown_tool` |
| M16 | `_default_host_exec` MUST 带 `os.O_NOFOLLOW`（可用时）且叶子为 symlink 时 MUST 返回 `False`、MUST NOT 改写 symlink 目标；普通文件 MUST 写成 `0o644` 内容 | `agent/executing_executor.py:29-56` | `tests/test_conformance.py::test_m16_host_exec_rejects_symlink_leaf` |
| M17 | 信用门：标注库含 `speak_bad` → `stats()["ineligible"]` 为真 → `run_calibration` MUST 置 `calibrated = False` + `note` 含 `ineligible_run`；`apply_calibration` MUST 返回 `False` 且 MUST NOT 改 `ProactivityConfig`、MUST NOT 解除 `observe`；干净标注达标 MUST 回写参数并 `observe = False` | `agent/calibration.py:245-261`, `agent/calibration_loop.py:49-78` | `tests/test_conformance.py::test_m17_credit_gate_blocks_calibration` |
| M18 | `Scheduler.tick` 派发前 MUST 过 `pre_deliver_check`；检查不过 MUST `skipped_lease += 1` 且 MUST NOT 调 `executor.deliver`；检查通过 MUST 派发并 `mark_delivered` | `agent/scheduler.py:27-30`, `agent/scheduler.py:64-93` | `tests/test_conformance.py::test_m18_scheduler_lease_check_skips_dispatch` |
| M19 | 主动性入参 MUST 校验 `utility >= 0` 与 `confidence ∈ [0,1]`，越界 MUST `raise ValueError` 且 MUST NOT 记入 `log`；`observe: bool = True` 时动作 MUST 带 `observe_` 前缀；`u_eff < c_speak` MUST 先判 `drop`；合规事件 MUST 记入 `log` | `agent/proactivity.py:32-38`, `agent/proactivity.py:71-124` | `tests/test_conformance.py::test_m19_proactivity_input_validation_and_observe` |
| M20 | `intake` MUST 先 `record_receipt(f"input:{inp.id}", inp.text)` 再做任何副作用；同 `id` 重投 MUST 返回 `{"disposition": "duplicate", "input_id": inp.id}` 且 MUST NOT 新建第二张 ticket；新 `id` MUST `accept` | `agent/engine.py:82-91`, `agent/store.py:58-67` | `tests/test_conformance.py::test_m20_intake_receipt_idempotent` |
| M21 | D2 复审熔断：`verdict_label == "ok"` MUST 把 `review_fail:{tid}` 清零；连续非 OK 到 `streak > self.max_review_failures` MUST 落 `reason: "failed_same_sha_retry_limit"` + `consecutive_failures: streak` + `tickets.end` + `"escalated": True`、MUST NOT 进修复环回复 | `agent/engine.py:41`, `agent/engine.py:232-250` | `tests/test_conformance.py::test_m21_consecutive_review_failure_fuse` |
| M22 | 治理挂点缺位（`self.governance is None`）时 `pre_write` MUST 返回 `False` 并落 `verdict: "BLOCK"` / `reason: "governance_path_block"` / `detail` 含 `governance_absent` 的判决；MUST NOT 静默放行；接入 `ScopeRegistry` 且已 `claim` 的路径 MUST 放行（ADR-0001 fail-closed） | `agent/engine.py:339-346` | `tests/test_conformance.py::test_m22_pre_write_without_governance_is_fail_closed` |

## 7. SHOULD clauses (recommendations; deviations are listed in §8)

| ID | SHOULD | 现状 |
|---|---|---|
| S1 | 每条 ruling SHOULD 带 `at` 时间戳 | 已满足：`record.setdefault("at", ...)` `agent/governance.py:188`；`rec.setdefault("at", time.time())` `agent/engine.py:70` |
| S2 | 干预点 1/3/4/6/8 SHOULD 在未来版本补齐挂点 | 未实现，见 §1 |
| S3 | ~~`Engine.pre_write` 在 `self.governance is None` 时 SHOULD 拒绝而非返回 `True`~~ | **已收口**：现为 BLOCK + 判决留痕 `agent/engine.py:339-347`，由 M22 守住 |
| S4 | executor 无 `metrics` 时预算闸 SHOULD 拒绝派工而非旁路 | 现为 `return 0` `agent/engine.py:213-214` |
| S5 | `outbox.pump` SHOULD 捕获其它异常并退避重试 | 现只捕获 `ChannelGone` / `ChannelIgnored` `agent/outbox.py:38-45` |
| S6 | escalate lift SHOULD 可从 J2 写路径消费 | 现 `WriteGate = Callable[[str, str], bool]` 不传 `content_hash` `agent/executing_executor.py:26` |
| S7 | ~~`docs/adr/ADR-0001` SHOULD 落盘~~ | **已收口**：`docs/adr/ADR-0001-fail-closed.md` |
| S8 | `idle_timeout` SHOULD 产生一条 `_ruling` | 现只写 `a.report = "idle_timeout"` `agent/scheduler.py:114`；`REASONS` 中该词未被 ruling 使用 |

## 8. Spec-vs-implementation discrepancies (reported, implementation untouched)

1. **RESOLVED**: `pre_write()` 在 `self.governance is None` 时曾返回 `True`（Engine 级
   fail-open 接线，违背 ADR-0001）。现为 BLOCK + `reason: "governance_path_block"` +
   `detail` 含 `governance_absent`（`agent/engine.py:339-347`），判据落 M22。M5 仍只描述
   "挂点已接线"的 BLOCK 语义，两者互补。
2. `agent/engine.py:213-214`: `_tokens_used()` returns `0` when executor has no
   `metrics` ("预算闸自动旁路——没有计量就没有硬闸"). M11 is therefore stated with
   the explicit precondition "且 executor 有 `metrics`"; S4 records the gap.
3. `agent/watchdog.py:288-302`: `Watchdog(judge=None, rules_check=False)` returns
   `OK` for any payload — a constructor-level fail-open configuration. M14 is stated
   for the shipped review path (`rules_check=True` by default); ADR-0001 §4 lists this
   as a known fail-open-capable wiring.
4. `agent/admission.py:72-85`: docstring says `matched_rule 恰好一条`; code only checks
   `a.matched_rule not in g.matched_rules`. The
   `return False, "accept_on_broadcast"` branch (`agent/admission.py:84-85`) is
   unreachable through `decide()` because `gate()` returns `GateResult("broadcast", [])`
   (`agent/admission.py:61`) and the membership check fires first. M13 asserts the
   real behaviour, not the docstring.
5. `agent/outbox.py:30-32` docstring says "抛其他异常或返回 False → 退避重试";
   `pump()` only catches `ChannelGone` / `ChannelIgnored` (`agent/outbox.py:38-45`),
   so other exceptions propagate out of `pump()` (`agent/outbox.py:37`). M10 covers
   `False` and the two legal abandons; S5 records the gap.
6. `agent/governance.py:84-91`: `_save_scopes()` uses `open(self._scopes_path(), "w", ...)`
   (truncating rewrite under flock), so `scopes.json` is **not** append-only; only
   `rulings.jsonl` is (`agent/governance.py:190-198`). M7 is deliberately scoped to
   the ruling stream.
7. `agent/executing_executor.py:26` + `agent/engine.py:335-341`: `write_gate` is a
   2-argument callable `(ticket_id, path)`, so the write path never passes
   `content_hash`; escalation lifts registered by `request_escalation` cannot be
   consumed through the J2 executor. M6 is tested at the `ScopeRegistry` API level;
   S6 records the reachability gap.
8. `agent/scheduler.py:114`: `idle_timeout` is written as `a.report` only; no
   `_ruling` with `reason: "idle_timeout"` is emitted, although the literal exists
   in `REASONS` (`agent/engine.py:28`). Vocabulary completeness ≠ usage.
9. `tests/test_v03_upgrades.py:14-15` claims "`governance.check` realpath 归一";
   the implementation is `posixpath.normpath(path)` (`agent/governance.py:47`), i.e.
   lexical normalisation plus kernel-level `O_NOFOLLOW`, not `realpath`. M3 states
   the real two-layer mechanism.
10. `agent/governance.py:36` documents `CheckResult.verdict` as only
    `"ALLOW" | "BLOCK"`. `ESCALATE` is not a third return value of `check()`; it is
    carried by ruling verdicts `"ESCALATION_REQUESTED"` / `"ESCALATE_LIFTED"`
    (`agent/governance.py:134`, `agent/governance.py:158`). §2 states this honestly.
11. 并发改动记录（撰写期间现场在变，本规范每次都按合并后的现场重锚）：
    `agent/engine.py` 由 349 行扩到 **365** 行——`REASONS` 从 7 词变 **8** 词（新增
    `"failed_same_sha_retry_limit"` `agent/engine.py:30`），`on_worker_report` 在修复环
    之前插入 D2 连续失败熔断（`agent/engine.py:232-250`）；随后 `pre_write` 的
    `governance is None` 分支由 `return True` 收口为 BLOCK + 判决留痕
    （`agent/engine.py:339-346`），新增 M22 覆盖该收口；`agent/calibration.py` 的
    `LabelStore` 由 `agent/calibration.py:89` 移到 `agent/calibration.py:198-262`。
    本规范与 `tests/test_conformance.py` 均已按改动后的现场行号重锚；实现文件的
    改动由其它代理完成，本任务零改动实现文件。

## 9. Reproduction

```bash
python3 -m pytest tests/test_conformance.py -q
python3 -m pytest tests/ -q
```

Both commands run from `superagent-build/`. §6 maps MUST M1..M22 to its primary test
function one-to-one; the file additionally carries
`test_m21_review_fail_streak_fuses`（并发代理追加，作为 M21 的补充交叉验证，
共 23 个测试函数 / 22 条 MUST）。所有测试用 `tmp_path`，测试之间不共享临时文件。
