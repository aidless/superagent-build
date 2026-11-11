## v0.3.2 (main HEAD, unreleased) — 2026-11-10

外部口径止血：PyPI 0.1.0 → **0.3.1**（twine 上传，`pypi.org/pypi/superagent-build/json` 验号 latest=0.3.1）。

证据链（方向性证据 → 可复算结论）：
- `agent/evidence.py`：`cost_model_for_value` 统一 V 的两处口径（p* 与 CostModel）、
  `ci_summary` 判 CI 是否跨 0、`v_sensitivity` 按 seed 配对的 V 网格、`flip_point`。
- `service/run_sweep_b.py`：4 任务 × 4 seeds × 2 臂 = **32 runs** 真网关。结果
  Δtokens **−628**（8-run 是 +1654）、每对均值 −39.25、逐 seed 符号翻转、CI
  [−8.3339, −8.3329] 不跨 0、p* 不存在 → **推翻"双层省 token"**，修正为
  "治理链不引入可测 token 税；成本账只作假设压力测试"。
- `evidence/` 证据包：9 文件 + `MANIFEST.sha256`（`sha256sum -c` 兼容）+
  `REPRODUCE.md`；篡改即 `ok:false`，CI 增设 verify 步骤。

治理契约机器可读化：
- `docs/spec/GOVERNANCE.md`：RFC2119、8 干预点覆盖表（5 处诚实留白）、
  ALLOW/BLOCK/ESCALATE 闭合集、REASONS 八词、**M1..M22** 每条 MUST 配
  conformance 测试、S1..S8 偏差表、11 条规范-实现差异清单。
- `tests/test_conformance.py`：23 条测试，每条内部 RED→GREEN。
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
