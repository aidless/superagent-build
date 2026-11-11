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
