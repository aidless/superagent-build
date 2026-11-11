# superagent-build 面试追问 20 问

> 用途：把 superagent-build 当作品集项目，按 AI 应用 / 推理框架岗的高频追问准备口答。
> 每题三段：**短答**（面试现场直接说）→ **可核验**（file:line，现场读过）→ **复算**（results/ 下落盘路径或命令）。
>
> 口径约定（先看，能挡住 80% 的追问）：
> 1. **行号快照**：本文所有 file:line 于本次会话逐条读取核对（快照时 `agent/engine.py` 359 行、`agent/calibration.py` 365 行；这两个文件在本会话内被并行扩写过，已按最新行号复核）。项目仍在并行改动，函数签名与文件路径是稳定锚点；若行号漂移，按签名 grep。
> 2. **数字只认落盘**：实验数字一律来自 `results/` 下 JSON 字段（写作时逐个打开核对）。README / CHANGELOG / RELEASE_CN 里的数字只当叙述，不当事实源；两者不一致时以 `results/` 为准，并会在下文点名。
> 3. **代码常量不是实验结果**：如 `_NEUTRAL=0.5`、`BACKOFF_CAP=300.0` 是源码常量，现场读自源码，用于解释机制，不冒充实测值。
> 4. **没落的数字不编**：凡未落盘的，写「以 results/… 为准」。

---

## Q1 fail-closed 为什么是治理基线

**短答**
因为 agent 的错误不是"答错一句话"，而是**不可逆副作用**（写盘、发消息、花钱）。所以治理层的默认值必须是"拒"，只有拿到确定性证据才放行。这个项目四道闸全部 fail-closed：准入层研判缺位就拒（不是放行）、写前未声明 scope 就 BLOCK、watchdog 证据不足判 `UNASSESSABLE` 而不是 OK、判决 reason 出词表直接 `ValueError`。最硬的实证：真模型研判曾答 `broadcast`（不在候选规则列表）→ `rejected_eval:hallucinated_rule` 拒单。

**可核验**
- `agent/admission.py:110-111` — `if assessor is None: return "rejected_eval", None  # 无研判层 → fail-closed`
- `agent/admission.py:11-14` — 三条硬校验铁律注释（matched_rule 恰好一条 / accept 需 intent_evidence / 规则必须真实存在于 gate 输出）
- `agent/governance.py:112-115` — "无活动作用域声明——没有声明就不许写"
- `agent/watchdog.py:27-39` — 三值 `OK / DEVIATION / UNASSESSABLE`，`:33-39` 明确 `UNASSESSABLE` 虽非 OK 但比 OK 严重
- `agent/engine.py:64-69` — `_ruling()` 词表外 reason 直接 `ValueError`
- `agent/assessor.py:196-199` — `hallucinated_rule:{matched_rule}` → `_fail_closed`
- `results/eval/assessor_ab/report.json` — 两臂 `false_accept_bait` 均为 0

**复算**
- `results/eval/assessor_ab/report.json` 的 `arm_assessor.false_accept_bait` / `arm_none.false_accept_bait`（均为 0）
- `python3 -m pytest tests/test_v03_upgrades.py -q`（含 symlink / escalate 的 RED 用例）

---

## Q2 p* = ΔN·V/ΔT 怎么推

**短答**
这是**成本盈亏平衡点**，不是"省了多少"的结论。设双层相对单层的 token 差为 ΔT、省下的介入次数为 ΔN、每次介入价值 V（元/次），则相对成本：

```
Δcost = ΔT × p_tok − ΔN × V        （p_tok = 每 token 单价，元/token）
令 Δcost = 0  ⇒  p_tok* = ΔN · V / ΔT
```

`p_tok*` 的语义：token 单价**低于**这个值时，双层省下的注意力价值才盖过 token 成本，双层划算；高于它就反过来。同理反向解出 `C* = ΔT × p / ΔN`（元/次介入），注意力价值高于 C* 时双层划算。

单位是这道题最容易翻车的地方：`breakeven_price` 的 docstring 明写返回**元/token**（`vendor/framework.py:232-233`），而 `CostModel` 的报价字段是**元/1M tokens**（`:190`）。两者差 1e6，比较前必须换算。项目里还有第二处口径分歧：`agent/sweep_bridge.py:254-256` 的 `sweep_curve(price_per_token=0.02)`（=20000 元/1M）与 `CostModel` 默认 4.0 元/1M 差 5000 倍——K1 落盘的 `c_star=8.27` 就是用 0.02 元/token 算的。`agent/evidence.py:1-15` 的 docstring 正是在修这类"两处口径不一致"，32-run 报告已统一改用 `CostModel` 默认单价。

**可核验**
- `vendor/framework.py:230-236` — `breakeven_price()` 实现：`delta_interventions * value_per_intervention / delta_tokens`；`:234-235` Δ≤0 返回 None
- `vendor/framework.py:214-227` — `breakeven()` 两种读法的结构注释
- `vendor/framework.py:197-211` — `CostModel.cost()`：`:200` `token_cost = toks / 1_000_000 * price_per_1m_tokens`
- `vendor/framework.py:335-344` — selftest 现算 `ΔN=6 × 5.0 / 200_000 = 30.0/200_000`，并反向验证 None
- `agent/sweep_bridge.py:254-256` — `sweep_curve(records, value_per_intervention=5.0, price_per_token=0.02)`
- `agent/evidence.py:35-45` — `cost_model_for_value()` 把 V 换成 `hourly = V*60/minutes`，让同一个 V 同时驱动成本模型与 p*

**复算**
- `results/real/sweep_curve.json` 的 `curve.p_star=0.012091898428053204`、`curve.c_star=8.27`、`curve.delta_tokens=1654`、`curve.delta_interventions=4`
- `python3 -m pytest tests/test_j4_sweep_bridge.py -q`（框架数学自检）

---

## Q3 mention 召回只有 40% 说明什么

**短答**
说明研判层在"提到我、但没有显式 `/do`"这条路由上**偏保守**，而且保守是拿召回换 precision 的。现场统计两臂的原始行：A 臂（带 LLMAssessor）在 15 条 mention 合规任务里只接了 6 条（**6/15 = 40%**），B 臂（无研判层）接了 0 条；两臂 precision 都是 1.0、诱饵误收都是 0。所以 40% 不是能力上限，是"宁可漏接也不误接"的 prompt 策略的直接代价。

注意两个分母不能混：`report.json` 里 `recall=0.5714285714285714` 是**30 个任务 / 21 个应接受**的全体口径，40% 的分母是 **15 条 mention 合规**。面试时说"整体 recall 从 0.286 提到 0.571，其中 mention 路由从 0/15 到 6/15"最准确。

**可核验**
- `results/eval/assessor_ab/report.json` — `arm_assessor.tp=12, fn=9`；`arm_none.tp=6, fn=15`；`routes={explicit:6, mention_legit:15, mention_bait:9}`
- `results/eval/assessor_ab/armA.jsonl` / `armB.jsonl` — 现场按 `(route, should, accepted)` 统计：A 为 mention/True/True=6、mention/True/False=9、mention/False/False=9；B 为 mention/True/False=15
- `agent/assessor.py:28-35` — SYSTEM_PROMPT 要求"必须且只能从候选规则列表选择，禁止编造"
- `agent/assessor.py:38-79` — 有界上下文（前 5 + trigger + 后 5、4000 字符封顶）
- `service/eval_assessor_ab.py:44-83` — 30 任务定义，`:83` `assert n == 30`

**复算**
```bash
python3 - <<'PY'
import json
for arm in ('armA','armB'):
    rows=[json.loads(l) for l in open(f'results/eval/assessor_ab/{arm}.jsonl')]
    print(arm, sum(1 for r in rows if r['route']=='mention' and r['should'] and r['accepted']), '/15')
PY
```

---

## Q4 8-run 探路够不够、CI 是否跨 0

**短答**
**不够下结论，只够定方向。** K1 落盘是 8 runs、4 对配对样本：`delta_tokens=1654`、`delta_interventions=4`、`p_star=0.0120919`、`ci_low=-8.332654`、`ci_high=-8.330706`，按 `verdict()` 的三档规则 `ci_high<0` → `DUAL_WINS`，也就是**不跨 0**。但这个"不跨 0"只说明"这 4 对样本里没有反向证据"，不说明结论成立：样本量 4 对、V=5.0 是函数默认假设（`agent/sweep_bridge.py:254-256`），所以 README 自己标了"探路 batch、方向性证据，结论级 32-run 待跑"（`README.md:125-129`）。

32-run 落盘后反而**修正**了这个方向：`results/real/sweep_b/evidence_report.json` 里 `delta_tokens=-628`、`sign_flips_within_batch=true`、`k1_vs_b_sign_agrees=false`，`DUAL_WINS` 由介入计数假设单边驱动（`caveats[3]`）。所以最诚实的说法是：8-run 的 CI 不跨 0 是一个**弱证据**；32-run 证明"token 差是噪声量级"，判决稳是因为介入假设，不是因为省 token。

**可核验**
- `results/real/sweep_curve.json` — `curve.ci_low=-8.332654000000002`、`curve.ci_high=-8.330706000000001`、`curve.verdict="DUAL_WINS"`、`n_runs=8`
- `vendor/framework.py:252-258` — `verdict()` 三档：`ci_high<0 → DUAL_WINS` / `ci_low>0 → SINGLE_WINS` / 否则 `INCONCLUSIVE`
- `vendor/framework.py:261-271` — `paired_bootstrap_ci(deltas, n_boot=2000, seed=0)`
- `agent/sweep_bridge.py:254-256` — `value_per_intervention=5.0` 是默认假设参数
- `results/real/sweep_b/evidence_report.json` — `replication_check` 全字段
- `service/run_sweep_b.py:136-162` — 逐 seed 符号稳定性与 K1 对比的实现

**复算**
- `python3 service/run_sweep_b.py --analyze-only`（只读 `results/real/sweep_b/runs.jsonl` 重算，不调网关；`service/run_sweep_b.py:92-93`、`:99-100`）
- `results/real/sweep_b/evidence_report.json` 的 `replication_check.k1_8run_delta_tokens=1654` 与 `k1_vs_b_sign_agrees=false`

---

## Q5 与 LangGraph、MetaGPT、openai-agents-python 的生态位差别

**短答**
它们卖**编排能力**（图/状态机、角色分工、通用工作流），这个项目卖**治理确定性**：每一层治理都有 fail-closed 默认值、RED→GREEN 测试、可审计的判决流。`README.md:161-173` 的定位表把差别写得很直接：openai-agents-python "通用编排无治理确定性"；MetaGPT "重角色分工轻可靠性底座"；litellm "治理在网关层按请求拦截"，而本项目治理在**任务语义层**（Mandate 边界 + 写前裁决 + 跑偏复核）。

要主动交代一个事实：**README 表里没有 LangGraph 行**（`:163-170`）。所以不要编造星数或能力对比，按"编排框架同类"处理：LangGraph 给你图/节点/状态，治理要自己写节点；本项目把治理做成内核默认值，且每条规则能指到 file:line。生态位一句话：不是又一个编排框架，是"把 Super Agent 原型欠下的可靠性账补齐"的可测内核（`README.md:172-173`）。

**可核验**
- `README.md:163-170` — 定位对比表（星数为 README 当日快照，本文不作事实引用，也不引用具体数值）
- `README.md:172-173` — 生态位句
- `agent/admission.py:11-14`、`agent/governance.py:104-140`、`agent/watchdog.py:1-14` — 治理三层的确定性实现

**复算**
- 无落盘数字题；引用 `README.md:161-173` 的定位措辞即可，不引用星数。

---

## Q6 outbox 先存后做与幂等收据

**短答**
顺序是 **receipt → enqueue → deliver**，缺一不可，而且是"先存后做"：没有 receipt 就不许 enqueue。receipt 用 SQLite 主键做幂等锚点（重复登记返回 False，调用方跳过副作用）；enqueue 前强制 `has_receipt`，否则直接 `ValueError`；outbox 的 `message_id` 是主键，重复投递幂等跳过。崩溃语义上，这保证不会出现"消息已发但没记账"或"记账了但消息丢"的单边状态——重放是安全的。

退避 5s→300s（×2 封顶），超过 `max_attempts` 也不静默丢弃，继续退避等下轮；**唯二合法放弃**是 `channelGone` / `channelIgnored`，其他异常不许吞。

**可核验**
- `agent/store.py:1-8` — 单一 SQLite、WAL + `synchronous=FULL`、receipts 表是先存后做的幂等锚点
- `agent/store.py:57-67` — `record_receipt(receipt_id, payload="") -> bool`：`IntegrityError → False`
- `agent/store.py:73-85` — `enqueue()`：`:76-77` `f"enqueue before receipt: ..."`；`:84-85` 重复消息幂等跳过
- `agent/store.py:105-111` — `abandon()` 唯二合法放弃
- `agent/outbox.py:1-7` — 效果三角
- `agent/outbox.py:15-16`、`:22-23` — `BACKOFF_BASE=5.0` / `BACKOFF_CAP=300.0`、`backoff_next()`
- `agent/outbox.py:26-52` — `pump()`：`:30-32` 超 max_attempts 不静默丢弃
- `agent/outbox.py:55-60` — `ChannelGone` / `ChannelIgnored`
- `agent/engine.py:89-91` — intake 先 `record_receipt(f"input:{inp.id}")`
- `agent/engine.py:324-329` — 回复先 receipt 再 `store.enqueue`

**复算**
- `python3 -m pytest tests/test_store.py tests/test_admission_outbox.py -q`
- `results/eval/assessor_ab/armA.db` 里可查 `receipts` / `outbox` 两表（SQLite）

---

## Q7 两级准入（gate + 研判硬校验三铁律）

**短答**
gate 是**确定性规则层**，五条路：bot 门、follow_up、broadcast、explicit、mention（`agent/admission.py:42-67`）。研判层只在 gate 之上回答"接不接"，但输出必须过**三铁律**：① `matched_rule` 恰好命中一条（0 条或 2 条都拒）；② `accept` 必须有 `intent_evidence`；③ 研判声明的规则必须真实存在于 gate 输出（防幻觉规则名）。任一条不过就 `rejected_eval:{reason}`。

关键设计：研判层缺位不是"放行"，而是保守策略——explicit 才接，mention/broadcast 拒（`agent/admission.py:95-96`、`:110-111`）。真模型答 `broadcast` 被 `hallucinated_rule` 拒单，就是第三铁律在真实模型上的实证。

**可核验**
- `agent/admission.py:11-14` — 三铁律注释
- `agent/admission.py:42-67` — `gate()` 五路
- `agent/admission.py:72-86` — `validate_assessment()` 四条 `return False`
- `agent/admission.py:89-116` — `decide()`
- `agent/assessor.py:196-199` — 第三铁律的模型侧兜底
- `results/eval/assessor_ab/armA.jsonl` — 9 条 mention 诱饵全 `accepted=False`

**复算**
- `python3 -m pytest tests/test_i2_assessor.py tests/test_admission_outbox.py -q`
- `results/eval/assessor_ab/report.json` 的 `false_accept_bait=0`

---

## Q8 escalate 与审批内容哈希绑定

**短答**
普通 BLOCK 是"路径不在 scope"；escalate 是"人工批一次"。审批记录绑定 `(path, content_hash)`：`check()` 时只有**内容哈希匹配**才一次性消费，消费后第二次同样哈希也回到 BLOCK；`content_hash=None` 时不消费任何 lift，普通校验语义不变。

语义上，审批人同意的是**这份内容的这个动作**，不是"同名路径永久放行"——换内容哈希就重新 BLOCK。这是 AGT `enforced_identity` 的思路：审批对象必须是可执行动作的身份，不是字符串路径。

**可核验**
- `agent/governance.py:104-140` — `check(ticket_id, path, content_hash=None)`
- `agent/governance.py:110` — "content_hash=None 时不消费任何 lift"
- `agent/governance.py:122-138` — `(path, content_hash)` 匹配才消费 + 落 `escalation_lifted`
- `agent/governance.py:142-159` — `request_escalation(ticket_id, path, content_hash, approver)`
- `agent/governance.py:185-198` — 判决流 append-only（`O_APPEND` + `flock`）
- `tests/test_v03_upgrades.py:85-110` — 错哈希仍 BLOCK / 正哈希 ALLOW 一次 / 第二次 BLOCK / ruling trail

**复算**
- `python3 -m pytest tests/test_v03_upgrades.py -q -k escalate`

---

## Q9 票级 token 预算硬闸

**短答**
`intake(budget_tokens=)` 把预算落 kv；`on_worker_report` 用 `_tokens_used()` 从 `executor.metrics.records` 累加 prompt+completion，超过预算就落 `verdict=BUDGET_EXCEEDED`、`reason=budget_tokens_exceeded` 并终止票。这把成本账从"事后度量"升级成"事中硬闸"。

必须主动说的一个诚实点：**没有台账时 `_tokens_used()` 返回 0，预算闸自动旁路**——代码注释原话是"没有计量就没有硬闸"。这是设计取舍（零依赖内核允许不接执行器），不是 bug；接真执行器（`ExecutingToolLoopExecutor`）时台账必开。

**可核验**
- `agent/engine.py:82-88` — `intake(..., budget_tokens=None)`
- `agent/engine.py:110-113` — `store.put(f"budget:{ticket_id}", {...})`
- `agent/engine.py:209-220` — `_tokens_used()` + "没有计量就没有硬闸"注释
- `agent/engine.py:288-300` — 超限 → `BUDGET_EXCEEDED` + `tickets.end`
- `agent/metrics.py:9-19` — `CallRecord` 字段（`kind` / `ticket_id` / tokens）
- `agent/metrics.py:22-47` — `MetricsLedger.summary()`
- `tests/test_v03_upgrades.py:115-138` — 超限 end 票 / 未超限正常回复

**复算**
- `python3 -m pytest tests/test_v03_upgrades.py -q -k budget`
- `results/real/metrics.jsonl` — 逐调用台账格式实证（2 行，含 prompt/completion/latency/ok）

---

## Q10 APx 风格信用门 INELIGIBLE

**短答**
校准不是"把参数拟合到数据"，而是"这批数据配不配被官方采信"。`LabelStore.stats()` 里只要 `speak_bad > 0`（watchdog 抓到过真实坏开口），就把 `ineligible=True`；`run_calibration()` 在 ineligible 时强制 `calibrated=False` 并写 note，`best_params` 只作诊断值保留；应用侧 `apply_calibration()` 不达标绝不改参数。

这是 APx 式**信用**而不是精度：脏数据可以把 precision 刷到 1.0（全是 speak_ok），但信用门让"官方校准"拒绝认这批数据。落盘 K2 批次 `speak_bad=0`，所以本批 eligible、`calibrated=true`——这两个字段要一起讲，才显得不是背结论。

**可核验**
- `agent/calibration.py:245-262` — `stats()`；`:258-261` 信用门注释；`:261` `result["ineligible"] = sb > 0`
- `agent/calibration.py:198` — `VALID_LABELS = ("speak_ok", "speak_bad", "wait_ok", "wait_bad")`
- `agent/calibration_loop.py:49-66` — `run_calibration()` 信用门
- `agent/calibration_loop.py:69-78` — `apply_calibration()` 不达标绝不改
- `tests/test_v03_upgrades.py:177-197` — 有 speak_bad → `ineligible` + `calibrated=False`；干净数据 → eligible

**复算**
- `python3 -m pytest tests/test_v03_upgrades.py -q -k credit`
- `results/real/k2_calibration.json` 的 `stats.speak_bad=0`（本批 eligible）与 `calibration.calibrated=true`

---

## Q11 symlink O_NOFOLLOW 与单租户边界

**短答**
双层防御：**策略层** `governance._matches` 先做 `posixpath.normpath` 归一，堵 `work/../secret` 这类 lexical 穿越；**内核层** `_default_host_exec` 写盘用 `O_NOFOLLOW` 打开，叶子是 symlink 直接拒，目标文件不被写。

边界要讲清，这一条最容易被追问"多租户安全吗"：`O_NOFOLLOW` 解决的是 **symlink TOCTOU**，不是对抗敌对进程的沙箱；Windows 没有 `O_NOFOLLOW`，退回普通 open；执行器 docstring 明写"本执行器是单租户协作设计，不是对抗敌对进程的沙箱"。要隔离得上容器/沙箱，这是能力边界，不是遗漏。

**可核验**
- `agent/governance.py:40-50` — `_matches()`；`:43-45` 双层防御注释；`:47` `posixpath.normpath(path)`
- `agent/executing_executor.py:1-11` — docstring "无 write_gate 时所有写工具拒绝（write_gate_required）——治理缺位宁可不动手"
- `agent/executing_executor.py:29-56` — `_default_host_exec()`；`:32-35` 单租户边界声明；`:44-45` `O_NOFOLLOW`；`:48` `os.open(path, flags, 0o644)`
- `tests/test_v03_upgrades.py:238-265` — `work/../secret` BLOCK；symlink 叶子拒绝且目标文件内容不变；普通写/追加仍工作

**复算**
- `python3 -m pytest tests/test_v03_upgrades.py -q -k "nofollow or normpath"`

---

## Q12 DSML 文本工具协议泄漏的发现与修复

**短答**
发现入口是**真网关服役批**：执行器按 OpenAI 原生 `tool_calls` 取工具调用，但 `deepseek-v4.1-flash` 这类端点走文本协议（`<tool_call>{...}</tool_call>`），或网关未剥离内部协议时 DSML 标记（`<｜DSML｜ invoke name="X">`）会泄漏到 `content`，而原生字段为空。结果就是"模型看起来调了工具，执行器什么都没执行"。

修复是 `_parse_text_tool_calls()` 双格式解析：格式 1 正则 `<tool_call>{json}</tool_call>`；格式 2 正则 DSML `invoke` / `parameter` 块；返回与原生 `tool_calls` 同构的列表，坏块静默跳过。时间线证据是 commit `0d6e03c`，message 明写 `+ text tool-call protocol compat`，同 commit 改了 `agent/executing_executor.py`（+85 行）并新建 `service/run_service.py`。

诚实缺口：`tests/` 下没有 `_parse_text_tool_calls` 的直测（grep 无匹配）；目前红绿证据来自运行行为与 commit，不来自单测。这条别装成"完整测试覆盖"。

**可核验**
- `agent/executing_executor.py:182-187` — 原生字段为空 → 走文本协议解析
- `agent/executing_executor.py:239-287` — `_parse_text_tool_calls()`；`:245-248` DSML 泄漏注释与样例
- `agent/executing_executor.py:255-270` — 格式 1 `<tool_call>{json}</tool_call>`
- `agent/executing_executor.py:273-286` — 格式 2 DSML invoke / parameter
- `README.md:152-155` — 服役批条目含"文本工具协议兼容"
- `service/run_service.py:43-46` — 服役批用 `ExecutingToolLoopExecutor` 接真网关

**复算**
- `grep -n "DSML\|tool_call" agent/executing_executor.py`
- `git show --stat 0d6e03c`（提交级证据；只读）

---

## Q13 _repairs 从内存搬到 SQLite 的崩溃一致性

**短答**
原实现是内存 `dict`：进程崩溃/重启后修复计数清零，`max_repairs` 形同虚设——同一个任务可以无限次"再修一次"。v0.3.1 把计数搬到 store kv `repairs:{tid}` 作为唯一事实源：每次 deviation 读 `store.get()`、`count+1` 写回；用尽后落 `reason=deviation_repair_limit` 并终止票。

RED 测试模拟的正是崩溃场景：同一个 store 起第二个 `Engine` + `resume()`，第二份 deviation 报告必须走 repair-limit（`repaired is not True` 且 `escalated is True`），并断言 kv 里 `count==1`。这是"状态机无状态化、只信落盘"的典型。

**可核验**
- `agent/engine.py:57-58` — "修复计数存 store kv `repairs:{tid}`（唯一事实源，v0.3.1 修复）：内存 dict 在崩溃恢复后清零 → max_repairs 形同虚设"
- `agent/engine.py:251-286` — deviation 复审环
- `agent/engine.py:255-259` — `rec = self.store.get(f"repairs:{assignment.ticket_id}")` + 写回 `count`
- `agent/engine.py:281-285` — 用尽落 `deviation_repair_limit` + `tickets.end`
- `agent/store.py:36-45` — kv `put/get`
- `tests/test_v031_repairs_persist.py:1-4`、`:40-55`、`:47-49`、`:52-54`、`:57-63`

**复算**
- `python3 -m pytest tests/test_v031_repairs_persist.py -q`
- `results/real/service.db` 的 `kv` 表可查 `repairs:*` 键

---

## Q14 assessor A/B precision-recall 口径

**短答**
30 个带 ground-truth 的标注任务（6 explicit 合规 / 15 mention 合规 / 9 mention 诱饵），两臂同任务集：A = `LLMAssessor`，B = `assessor=None`。混淆矩阵按"应接受 vs 实际 accept"算：

| 臂 | tp | fp | fn | tn | precision | recall | accuracy | 诱饵误收 |
|---|---|---|---|---|---|---|---|---|
| A（有研判） | 12 | 0 | 9 | 9 | 1.0 | 0.5714 | 0.7 | 0 |
| B（无研判） | 6 | 0 | 15 | 9 | 1.0 | 0.2857 | 0.5 | 0 |

口径要点：`recall` 分母是**应接受 21**（12+9），不是 30；mention 路由单看是 A 6/15 vs B 0/15；`false_accept_bait` 就是 `fp`。两臂 token/调用只算 **worker 台账**（25 次 / 9295 tokens vs 12 次 / 4495 tokens）。

必须主动交代计量缺口：`metrics_armA.jsonl` 25 行全是 `kind="worker"`，**0 条 assessor**——`agent/assessor.py:152-166` 已实现 `kind="assessor"` 入账能力，但 A/B 脚本构造 `LLMAssessor` 时没把 `metrics` 传进去（`service/eval_assessor_ab.py:86-99`）。这正是 `README.md:91-92` 自己写的"已知计量缺口，其成本未计入上表"。

**可核验**
- `service/eval_assessor_ab.py:2-17` — 任务构成与两臂定义
- `service/eval_assessor_ab.py:86-99` — `run_arm()` 装配（`ledger` 只给 executor，未给 assessor）
- `service/eval_assessor_ab.py:141-160` — 落盘与混淆矩阵、`:151-160` tp/fp/fn/tn
- `service/eval_assessor_ab.py:171-187` — report 生成与写盘
- `results/eval/assessor_ab/report.json` — 上表全部字段
- `results/eval/assessor_ab/metrics_armA.jsonl` — 25 行全 `kind="worker"`
- `README.md:91-92` — 计量缺口自述

**复算**
- `python3 service/eval_assessor_ab.py`（需 Magpie 网关 `127.0.0.1:3425`）
- 或直接统计 `results/eval/assessor_ab/{armA,armB}.jsonl` 的 `(route, should, accepted)` 组合

---

## Q15 成本账里 V 是假设不是实测

**短答**
V（每次介入的价值，元/次）是 `CostModel` 的**构造参数**，不是测量值。项目里原本有两处口径：`sweep_curve()` 默认 `value_per_intervention=5.0`（`agent/sweep_bridge.py:254-256`），而 `CostModel` 默认 `attention_value_per_hour=100.0`、`intervention_minutes=5.0` → `5/60×100 = 8.333` 元/次（`vendor/framework.py:184-195`）。`agent/evidence.py:1-15` 的 docstring 就是为修这个写的：`cost_model_for_value(V)` 把 V 换成 `hourly = V×60/minutes`，让同一个 V 同时驱动成本模型与 p*。

所以报告做的是 **V 敏感性网格**（0.001→20 元/次，9 个点），不是"V 被测量过"。sweep_smoke 报告的 caveats 原话："V 敏感性扫的是'假设成立时结论对 V 稳不稳'。"介入次数本身也是记账假设 `single=2 / dual=1`，不是实测人被打断的次数。

**可核验**
- `vendor/framework.py:184-195` — `CostModel` 三个默认值 + `value_per_intervention()`
- `agent/sweep_bridge.py:254-256` — `sweep_curve(..., value_per_intervention=5.0, price_per_token=0.02)`
- `agent/sweep_bridge.py:265-275` — `delta_tokens` / `delta_interventions` / `p_star` / `c_star`
- `agent/evidence.py:1-15` — "两处口径不一致"的 docstring
- `agent/evidence.py:35-45` — `cost_model_for_value()`
- `agent/evidence.py:70-98` — `v_sensitivity()`
- `results/real/sweep_smoke/evidence_report.json:145-149` — 三条 caveats
- `results/real/sweep_b/evidence_report.json` — `caveats` 四条（含 ΔT 符号翻转）

**复算**
- `python3 service/run_sweep_b.py --analyze-only` 后读 `v_sensitivity` 全网格（0.001 / 0.01 / 0.1 / 0.5 / 1 / 2 / 5 / 10 / 20）
- `results/real/sweep_smoke/evidence_report.json` 的 `v_sensitivity` 9 组

---

## Q16 observe 模式 confidence 同质化 0.533 的根因

**短答**
根因是**估计器只看一个证据源**。`engine.on_worker_report()` 给的 evidence 是 `{"watchdog_pass": 1.0}`（只有 watchdog 一源，见 `agent/engine.py:306`），而 `ConfidenceEstimator` 默认三源权重各 1.0（`watchdog_pass` / `worker_verified` / `historical_precision`）。缺失的两源按 `_NEUTRAL=0.5` 参与、最终乘 `_MISSING_DISCOUNT=0.8`：

```
(1.0 + 0.5 + 0.5) / 3 × 0.8 = 0.5333333…
```

12 个任务 watchdog 全 `ok` → 12 条 observe 事件同一个 0.5333，零信息量。所以 `service/RUNBOOK.md:43-44` 的结论是"estimator 证据源扩维**在攒数据前做**（否则又是一批同质 conf=0.533）"——先扩源再攒数据，而不是先攒一堆常数。

**可核验**
- `agent/engine.py:302-322` — 主动性块；`:306` evidence 只有 `watchdog_pass`
- `agent/engine.py:315-322` — observe_* 前缀落 observe_log（p_star / u_effective / action / watchdog_verdict）
- `agent/calibration.py:29-33` — 三源常量、`_NEUTRAL=0.5`、`_MISSING_DISCOUNT=0.8`、`_EMPTY_CONSERVATIVE=0.4`
- `agent/calibration.py:132-166` — `estimate()` 缺失源打折 + clamp
- `results/real/k2_observe_log.jsonl` — 12 行，`confidence` 全为 `0.5333333333333333`
- `results/real/observe_log.jsonl` — 2 行同值
- `service/RUNBOOK.md:40-44` — 冻结清单 + 扩维顺序

**复算**
```bash
python3 -c "import json;print({json.loads(l)['confidence'] for l in open('results/real/k2_observe_log.jsonl')})"
```
（输出应只有一个元素 `0.5333333333333333`）

---

## Q17 32-run 真跑结论

**短答**
报告已落盘：`results/real/sweep_b/evidence_report.json`。现场字段：`n_runs=32`、`n_pairs=16`（4 任务 × 4 seeds × 2 臂）；`curve_default_costmodel.delta_tokens=-628`、`delta_interventions=16`、`p_star=null`、`c_star=null`、`ci_low=-8.333948750000001`、`ci_high=-8.332929`、`verdict="DUAL_WINS"`；`ci_readout.crosses_zero=false`；`flip_point_V=0.001`；`v_sensitivity` 9 个 V 全部 `DUAL_WINS`。

但这是一次**打自己脸的复现**：`replication_check.sign_flips_within_batch=true`、`k1_vs_b_sign_agrees=false`（K1 8-run 的 ΔT=+1654，B 批是 -628）、`mean_delta_tokens_per_pair=-39.25`；`caveats[3]` 原话："ΔT 的符号在本批内逐 seed 翻转……token 差属噪声量级，判决 DUAL_WINS 由介入计数假设单边驱动，不能读成'双层省 token'。" `p_star=null` 不是漏算，是 ΔT<0 时 `breakeven_price` 按设计返回 `None`（`vendor/framework.py:234-235`）。

这份报告的 `new_runs_this_call=0`、`elapsed_s=0.126`，说明是 `--resume`/`--analyze-only` 汇总（32 条 RunRecord 已增量落在 `results/real/sweep_b/runs.jsonl`），不是本次新调网关。

**可核验**
- `results/real/sweep_b/evidence_report.json` — 上述全部字段
- `service/run_sweep_b.py:130-134` — `sweep_curve` / `v_sensitivity` / `paired_bootstrap_ci` / `ci_summary`
- `service/run_sweep_b.py:136-162` — `replication_check`（逐 seed 符号、K1 对比）
- `service/run_sweep_b.py:164-188` — report 字典；`:178-187` 四条 caveats
- `service/run_sweep_b.py:189-191` — 写 `evidence_report.json`
- `agent/evidence.py:48-60` — `crosses_zero = ci_low <= 0.0 <= ci_high`
- `vendor/framework.py:230-236`、`:252-258`

**复算**
- `python3 service/run_sweep_b.py --analyze-only`（只读 `runs.jsonl`，不调网关）
- `results/real/sweep_b/runs.jsonl` 32 行原始 RunRecord（可逐对重算 ΔT）

---

## Q18 K2 校准样本量门与择优键

**短答**
旧 `calibrate()` 的问题是按网格遍历顺序返回**第一组**达标参数，可能选中 `n_speak=1` 的脆弱点（`agent/calibration.py:349-358`）。K3 的 `calibrate_best()` 加两道门：

1. **样本量门**：`min_speak=3`，`trusted = n_speak >= min_speak`；全部候选都不足 3 时不宣布达标（"脆弱点不能当校准结论"）。
2. **择优键**：按 `(achieved_precision, n_speak, c_false, c_speak)` 降序，先精度、再开口数、再保守度；并返回 `alternatives` 全达标组做审计。

落盘 `results/real/k2_calibration.json` 现场核对：`n_labeled=12`、`stats.speak_precision=1.0`、`calibration.n_speak=8`、`calibrated=true`、`applied_params={c_false:0.5, c_speak:0.2, lam:0.05}`；`alternatives` 27 组，其中最后 2 组 `n_speak=2` 且 `trusted=false`。

**可核验**
- `agent/calibration.py:309-365` — `calibrate()`；`:318` "n_speak>0 才算达标（不能靠沉默刷分）"；`:349-358` 返回第一组
- `agent/calibration_best.py:1-16` — K3 docstring（旧行为缺陷）
- `agent/calibration_best.py:27-29` — `min_speak: int = 3`
- `agent/calibration_best.py:58-63` — `trusted = n_speak >= min_speak`
- `agent/calibration_best.py:65-81` — 全部样本量不足不宣布达标
- `agent/calibration_best.py:83-89` — 择优键 `keyfn`
- `agent/calibration_best.py:91-95` — `alternatives` 返回
- `agent/calibration_best.py:119-148` — selftest RED→GREEN（`:125` `assert r["n_speak"] >= 3`）
- `service/RUNBOOK.md:28-31` — 30 条 + ≥3 坏开口才做真校准

**复算**
- `python3 -m pytest tests/test_k3_calibration_best.py tests/test_i5_calibration.py -q`
- 读 `results/real/k2_calibration.json` 的 `calibration.n_speak` / `calibration.alternatives`

---

## Q19 PyPI 包版本与 README 口径一致性

**短答**
版本以 `pyproject.toml` 为准：`name = "superagent-build"`、`version = "0.3.1"`、`requires-python = ">=3.10"`、`dependencies = []`。三处一致：`pyproject.toml:7`、`README.md:148`（v0.3.1 修复条目）、`docs/RELEASE_CN.md:36`/`:68`（`pip install superagent-build` 0.3.1 与 PyPI 0.3.1 链接）。

不一致的三处要如实说：
1. `CHANGELOG.md:1` 只到 v0.3.0，`:20` 还有一个空的 `## 2025-06-11` 节，v0.3.1 没有单独 changelog 条目；
2. 测试计数四个来源彼此不一致：`README.md:6` 徽章、`README.md:67` 正文、`CHANGELOG.md:18`、`docs/demo_events.jsonl:16` 各自给出的通过数不同，且都不在 `results/` 落盘（具体数值本文不引用，以实跑为准）；
3. 这些计数都不在 `results/` 落盘，按本文口径**不引用具体数字**，只说"以 `python3 -m pytest tests/ -q` 实跑为准"。

面试口径：版本号本身是一致的（0.3.1）；测试计数不背数字，只背复算命令。

**可核验**
- `pyproject.toml:6-10`、`:24`、`:29-30`
- `README.md:6`、`:67`、`:137-151`
- `CHANGELOG.md:1`、`:18`、`:20`
- `docs/RELEASE_CN.md:36`、`:68`
- `docs/demo_events.jsonl:16`

**复算**
- `python3 -m pytest tests/ -q`（以实跑为准）
- `grep -n version pyproject.toml`

---

## Q20 如果重做会先砍什么

**短答**
先砍**"成本账 DUAL_WINS 当卖点"的叙事**，不是砍代码。32-run 已经把这条叙事打掉：ΔT 符号逐 seed 翻转、`k1_vs_b_sign_agrees=false`、判决由介入假设单边驱动（`results/real/sweep_b/evidence_report.json` 的 `replication_check` 与 `caveats[3]`；`docs/RELEASE_CN.md:48-52` 的"结论修正"）。代码保留，定位改成"上限压力测试 + V 敏感性工具"，不再说"双层省 token"。

第二砍 **estimator 的单源口径**：先扩证据源再攒数据（`service/RUNBOOK.md:43-44`），否则 12 条同质 0.5333 的 observe 事件是白攒（`results/real/k2_observe_log.jsonl`）。

第三砍 **8-run 探路批的"方向性证据"标签**（`README.md:125-129`）：直接上 32-run + `--resume` 断点续跑 + `--analyze-only` 重算（`service/run_sweep_b.py:1-7`、`:92-93`、`:99-100`）。

留下的核心是确定性证据：两级准入三铁律、写前 scope BLOCK、watchdog 三值、先存后做、repairs 落库——这些不依赖"成本省了多少"的叙事。

**可核验**
- `results/real/sweep_b/evidence_report.json` — `replication_check` / `caveats`
- `docs/RELEASE_CN.md:48-52` — 32-run 结论修正段
- `service/RUNBOOK.md:40-44` — 冻结清单 + 扩维顺序
- `README.md:125-129` — 8-run 探路批自述
- `service/run_sweep_b.py:1-7`、`:92-100`

**复算**
- `python3 service/run_sweep_b.py --analyze-only`
- 对比 `results/real/sweep_curve.json` 与 `results/real/sweep_b/evidence_report.json` 的 `delta_tokens` 符号

---

## 90 秒口播

**（约 90 秒，可照读）**

> 我造这个东西，是因为刷到一条"我不看好 Super Agent"的短视频后，去把那个宣传得很响的原型拆了：它宣传的 watchdog、文件级治理、主动阈值、调度四件事，源码里基本没兑现。我就把这四件事当需求单，造了一个"把可靠性账补齐"的可测内核——纯标准库、零依赖，每层治理都有 fail-closed 默认值。
>
> 最硬的一处工程不是"写完能跑"，是**崩溃一致性**：回复先落 receipt 再进 outbox，messageId 幂等、退避 5 秒到 300 秒、只有 channelGone / channelIgnored 两种合法放弃；复审环的"修了几次"从内存 dict 搬到 SQLite 的 `repairs:{tid}`，崩了重启还知道修过几次，`max_repairs` 不再是摆设。这条有专门的 RED 测试：同一个 store 起第二个 Engine，第二份 deviation 必须走 repair-limit。
>
> 最诚实的一处局限：我的 32-run 把自己 8-run 的结论推翻了。8-run 说双层交 1654 个 token 的税，32-run 的 ΔT 是 **-628**，而且同任务内逐 seed 符号翻转，均值 -39 tokens 就是噪声；判决之所以稳，是因为介入次数是 single=2 / dual=1 的**记账假设**，不是实测。所以我不再说"双层省 token"，成本账只当上限压力测试，真正的价值证据是确定性的拦截和复核行为。

---

## 面试官可能继续追问的 3 个我还没做的点

> 全部如实引用仓库里已声明的降级措辞与遗留池，不自己加戏。

### 1. 研判层自身的成本还没并入全口径账（已知计量缺口）

`README.md:91-92` 原话："assessor 自身的 ~24 次调用未入 worker 台账（已知计量缺口，其成本未计入上表）"。现场证据：`results/eval/assessor_ab/metrics_armA.jsonl` 25 行全是 `kind="worker"`，**0 条** `kind="assessor"`；`agent/assessor.py:152-166` 已经实现 `kind="assessor"` 的入账能力，但 `service/eval_assessor_ab.py:86-99` 构造 `LLMAssessor` 时没传 `metrics`。所以 A/B 表里的 token 差是"worker 侧"的差，不是全成本差。

要做的事：把研判/判官的调用并入同一个 `MetricsLedger`，重算 A/B 的全成本，再回答"研判层值不值这个钱"。

### 2. 结论级样本与"充分统计"还没到位

`README.md:129` 原话："8 runs 是探路 batch，结论级 32-run sweep 待跑，DUAL_WINS 暂作方向性证据"；`README.md:133` 原话："当前为方向性验证，非充分统计"。

32-run 报告虽然已落盘（`results/real/sweep_b/evidence_report.json`），但结论被它自己修正了：`sign_flips_within_batch=true`、`k1_vs_b_sign_agrees=false`，ΔT 属噪声量级（`caveats[3]`）。要做的事：要么把任务/seed 扩到让 ΔT 符号稳定，要么按 `docs/RELEASE_CN.md:56-60` 的三处局限直接把"省 token"叙事去掉，只留确定性证据。现在的位置是"方向性 + 复制核验"，不是"充分统计"。

### 3. 主动层数据池还差得远（30 条 + ≥3 坏开口的门槛）

`service/RUNBOOK.md:3-4`：功能冻结一个月，让 agent 干真活攒数据；`:28-31`：攒满 **30 条且含 ≥3 个坏开口**（watchdog 非 ok）才跑一次真校准；`:43-44`：estimator 证据源扩维**在攒数据前做**（否则又是一批同质 `conf=0.533`）。

当前落盘只有 12 条 observe 事件、confidence 全是 `0.5333333333333333`（`results/real/k2_observe_log.jsonl`），离 30 条门槛还差；`stats.speak_bad=0`，离"≥3 个坏开口"更远。所以官方校准目前是"单批方向性结论"，不是"数据池支撑的结论"。要做的事：先扩 estimator 证据源，再按 RUNBOOK 攒到门槛，重跑 `close_loop`。

---

## 附：落盘证据清单与一键复算

| 证据 | 路径 | 复算入口 |
|---|---|---|
| A/B 研判对照（30 任务） | `results/eval/assessor_ab/report.json` | `python3 service/eval_assessor_ab.py` |
| A/B 原始行 | `results/eval/assessor_ab/armA.jsonl` / `armB.jsonl` | 直接统计 `(route, should, accepted)` |
| A/B worker 台账 | `results/eval/assessor_ab/metrics_armA.jsonl` / `metrics_armB.jsonl` | 数 `kind` 分布 |
| K1 8-run 探路 | `results/real/sweep_curve.json` | `agent.sweep_bridge.sweep_curve()`（`agent/sweep_bridge.py:254-256`） |
| 32-run 结论级 | `results/real/sweep_b/evidence_report.json` | `python3 service/run_sweep_b.py --analyze-only` |
| 32-run 原始记录 | `results/real/sweep_b/runs.jsonl` | 逐对重算 ΔT / ΔN |
| 2-run smoke | `results/real/sweep_smoke/evidence_report.json` | `python3 service/run_sweep_b.py --tasks 1 --seeds 1` |
| K2 校准 | `results/real/k2_calibration.json` | `agent/calibration_loop.py:81-94` 的 `close_loop` |
| K2 observe 事件 | `results/real/k2_observe_log.jsonl` | 读 `confidence` 集合 |
| 真网关成本台账 | `results/real/metrics.jsonl` | 逐行求和（2 行：prompt 539 + completion 1836 = 2375） |
| 证据 bundle（只读副本） | `evidence/` + `evidence/MANIFEST.sha256` | `python3 service/make_evidence_bundle.py --verify`；`sha256sum -c evidence/MANIFEST.sha256` |

**复算注意**
- `service/run_sweep_b.py --analyze-only` 只读 `runs.jsonl` 重算，不调网关；其他脚本需要 Magpie 网关 `http://127.0.0.1:3425`。
- `evidence/` 是只读副本，映射关系在 `service/make_evidence_bundle.py:23-33`；原始证据始终在 `results/`。
- 本文所有实验数字均可在上表文件里逐字段核到；未落盘的测试计数不引用。
