## v0.3.2 发版回执（2026-11-11）

- PyPI：twine 上传 `superagent_build-0.3.2-py3-none-any.whl` + sdist，twine check 双 PASSED，`pypi.org/pypi/superagent-build/json` 验号 latest=0.3.2、releases `0.1.0 / 0.3.1 / 0.3.2`；wheel 内含 `agent/calibration_loop.py` 等 21 个模块。
- Git：tag `v0.3.2` 已推；GitHub Release https://github.com/aidless/superagent-build/releases/tag/v0.3.2 已建。
- CI 两次修复：首轮 f3b9618 因 `tests/test_p1a_calibration_apply.py` 读未入库的 `results/real/d4_calibration/calibration.json` 失败（`FileNotFoundError`，1 failed / 176 passed）→ 7e02e63 把该报告收进 evidence bundle（9→10 份副本），测试改读 `evidence/calibration/d4_calibration.json`，CI `completed success`；脚本套件新增 `interview_drill.py --selftest` 步骤。
- 分发：awesome-list PR https://github.com/e2b-dev/awesome-ai-agents/pull/1971 已开（state=open）；知乎/掘金/Reddit/HN/X 五渠道稿子就绪但账号门禁未过，见 `docs/DISTRIBUTION.md` 第七节回执。
- 终态：pytest 177 passed；9 脚本套件 rc=0；evidence `--verify` ok checked=10；coverage 80% 与徽章一致。

## v0.3.2 — 2026-11-11

外部口径止血：PyPI 0.1.0 → **0.3.1**（twine 上传，`pypi.org/pypi/superagent-build/json` 验号 latest=0.3.1）。

证据链（方向性证据 → 可复算结论）：
- `agent/evidence.py`：`cost_model_for_value` 统一 V 的两处口径（p* 与 CostModel）、
  `ci_summary` 判 CI 是否跨 0、`v_sensitivity` 按 seed 配对的 V 网格、`flip_point`。
- `service/run_sweep_b.py`：4 任务 × 4 seeds × 2 臂 = **32 runs** 真网关。结果
  Δtokens **−628**（8-run 是 +1654）、每对均值 −39.25、逐 seed 符号翻转、CI
  [−8.3339, −8.3329] 不跨 0、p* 不存在 → **推翻"双层省 token"**，修正为
  "治理链不引入可测 token 税；成本账只作假设压力测试"。
- `evidence/` 证据包：10 文件 + `MANIFEST.sha256`（`sha256sum -c` 兼容）+
  `REPRODUCE.md`；篡改即 `ok:false`，CI 增设 verify 步骤。

治理 §8 差异七处实修（M23..M29，全部带行号锚点写进规范）：
- 无 metrics 时票级预算闸 `fail-closed 拒收`（`agent/engine.py:95`，
  `ValueError("budget_requires_metrics: ...")`）——没有计量就没有硬闸。
- escalate 的一次性 lift 现在能在写路径真被消费：`ExecutingToolLoopExecutor`
  按 `inspect.signature` 判定 gate 是否收内容哈希，三参时传
  `sha256(content)`（`agent/executing_executor.py:187` `_gate_allows`）。
- `Watchdog(judge=None, rules_check=False)` 构造即 `ValueError("no_checks: ...")`
  （`agent/watchdog.py:242`）——没有判据的 watchdog 会把任何报告判成 OK。
- outbox 非通道异常不再外泄：退避重试并入 `stats["retried"]`（`agent/outbox.py:46`）。
- `Scheduler` idle 回收落 `IDLE_TIMEOUT` 判决（`agent/scheduler.py:121`，
  `reason: idle_timeout` 走词表统一入口）。
- `accept_on_broadcast` 校验分支可达：校验顺序改为先判广播再判幻觉规则名
  （`agent/admission.py:84`），同时补齐"恰好命中一条规则"的文档语义。
- `scopes.json` 落盘改旧态快照 + 原子替换：`.scopes.lock` + `flock` +
  `scopes.json.bak` + `tempfile.mkstemp`/`os.replace`（`agent/governance.py:105`）。
- 规范同步：§6 M1..M29、§7 S4/S5/S6/S8 标已收口、§8 整节改"发版内全量收口"
  逐条 RESOLVED 并列出仍存活的 5 条设计边界、§9 两条复算命令。
- 测试：新增 `tests/test_v033_section8.py` 7 条（每条先 RED）；
  `tests/test_watchdog.py` 旧 GREEN4 语义变更（无判据构造从"放行"改"拒绝"）。

校准闭环回写（P1-A）：
- `agent/calibration_loop.py`：`close_loop(..., cfg=..., apply=True)` 达标即
  `apply_calibration()` 写回 `c_false/c_speak/lam` 并解除 `observe`；新增
  `load_calibrated_cfg(report)` 从已落盘报告建配置，`calibrated` 不为真或三键
  不全则回落 `observe=True`（fail-closed）；`apply=True` 缺 `cfg` 直接
  `ValueError("apply_requires_cfg: ...")`。
- `agent/calibration.py`：新增 `build_completion_evidence(ctx)` 纯函数（六维证据）。
- `agent/engine.py`：`Engine(completion_evidence=...)` 注入式扩维；解除 `observe`
  后的开口落 `notices(effect=proactive_speak)` + store kv `proactive:{event_id}`。
  不注入时逐字保持旧单键口径（`tests/test_integration.py:52` 精确断言未动）。
- `tests/test_p1a_calibration_apply.py` 8 条（RED 7 failed / 1 passed 起步）。

服役批三臂真跑（P1-B，Magpie `workbuddy/deepseek-v4.1-flash`）：
- `service/run_daily_batch.py` 新增 `--completion-evidence` / `--apply-calibration` /
  `--model` 与每臂事件独立落盘 `results/real/observe_{date}.jsonl`。
- 臂 1 同构 10 任务：7 calls / 3700 tokens / 10 events / 完成侧 0.6133、0.6267。
- 臂 2 异构 6 任务：6 calls / 5896 tokens / 6 events / 同两档 + 输入侧 0.48、0.51。
- 臂 3 校准回写后 observe=False：6 calls / 4225 tokens / observe 事件 0 /
  **开口 4 条**（conf 0.6133×2、0.6267×2，p*=0.208333）。
- `service/make_p1b_report.py` → `results/real/p1b/summary.json`（可复算，含
  per_speak 逐条与 4 条 caveats：开口未复核 / 档位窄 / 沿用单键校准 / repairs 未激活）。
- 网关事故记录：裸模型名 `deepseek-v4.1-flash` 落到配额耗尽 provider
  （HTTP 429 + `Retry-After: 685146`），批量任务改为显式 `provider/model`。

治理契约机器可读化：
- `docs/spec/GOVERNANCE.md`：RFC2119、8 干预点覆盖表（5 处诚实留白）、
  ALLOW/BLOCK/ESCALATE 闭合集、REASONS 八词、**M1..M29** 每条 MUST 配
  conformance 测试（30 条：23 conformance + 7 §8）、S1..S8 偏差表。
- `tests/test_conformance.py`：23 条测试，每条内部 RED→GREEN；
  `tests/test_v033_section8.py`：7 条 §8 实修回归（RED→GREEN）。
- `docs/adr/ADR-0001-fail-closed.md`：fail-closed 不许做成开关；收口真偏差 S3
  （`pre_write` 治理挂点缺位曾 `return True` → BLOCK + 判决留痕，M22 守住）。

先修再攒 K2：
- ConfidenceEstimator 六维证据（verdict/marker/bot/text_len/tool_rounds/repairs），
  空证据仍 0.4、旧单源口径逐字不变。
- 输入侧 wait/drop 候选入 `observe_log`（8 字段，与完成侧一致）。
- 连续 watchdog 非 OK 计数落 kv，OK 清零，超 3 次熔断
  `reason: failed_same_sha_retry_limit`（对齐 AO autoreview）。
- assessor 成功记 usage、失败记 `ok=False` 零 token。
- 真跑 10 任务批：conf 从全同质 0.533 变 0.480/0.510/0.533 三档；真校准
  `speak_ok=9 / wait_ok=3 / wait_bad=1` → `calibrated=True`、
  `(c_false=2.0, c_speak=0.5, lam=0.05)`（K3 择优键在真数据上选了更保守组）。
  1 条人工覆写（"紧急求助被静默"记 wait_bad），方向性验证非充分统计。

分发与面试：`issues/1` 上游兑现度归档、`docs/RELEASE_CN.md` / `RELEASE_EN.md`、
`docs/INTERVIEW.md`（20 问 + 123 处已验 `file:line`）。

测试：`python3 -m pytest tests/ -q` → **153 passed**；9 个脚本套件全绿；
`service/make_evidence_bundle.py --verify` → ok。

## v0.3.0 — 六条生态升级（2026-11-10）

源自 superagent-dig/06-ecosystem-learning/ 四项目拆解，全部 RED→GREEN：

1. escalate 档：治理 BLOCK/ALLOW 之外新增"可解除 BLOCK"（governance.py
   request_escalation + check(content_hash=) 一次性 lift，审批绑定内容哈希）。
2. 预算进治理：intake(budget_tokens=) 票级 token 上限，超限 BUDGET_EXCEEDED
   终票（engine.py on_worker_report），成本账从事后度量升级为事中硬闸。
3. 派发租约：Scheduler pre_deliver_check 钩子，派发/idle 回收前查票状态，
   False→skipped_lease（sessionguard 式写前重读，消派发与用户消息竞态）。
4. APx 式信用门：calibration stats.ineligible（speak_bad>0 的批次官方校准
   拒绝 INELIGIBLE，只给诊断值）。
5. reason 词表：engine.REASONS frozenset 统一七种判决原因，_ruling() 单入口
   之外词表外的 reason 直接 ValueError。
6. symlink 加固：executing_executor _default_host_exec 改 O_NOFOLLOW 打开
   （TOCTOU 收口内核层），governance._matches 先 normpath 拦 lexical 穿越。

测试：99 pytest passed + 9 脚本套件全绿。

## 2025-06-11
