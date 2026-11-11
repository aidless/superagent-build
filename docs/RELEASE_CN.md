# 发布稿（中文，掘金 / 知乎 / 公众号可直接贴）

> 标题备选
> 1. 《我不看好 Super Agent？我把它拆了，还照着造了一个》
> 2. 《22k Star 的 Super Agent 原型，五个卖点只有一条完全兑现》
> 3. 《给 Agent 装四道闸，从源码拆解到 v0.3.2 可安装包》

版本口径写在前面。本次正式发版 v0.3.2，代码在 main HEAD。PyPI 的 v0.3.2 已上传并验号（latest=0.3.2），tag 与 GitHub Release 同批落地。下面所有数字都来自本仓落盘证据，复算命令放在文末。

---

刷到一条短视频，博主说「Super Agent 最近很火，但我不太看好」。理由挺实在，设计太复杂、违背现实逻辑，建议等 Vibe Coding 体验追上再说。

我没急着站队，去把 `deepseek-ai/dsh-superagent-prototype` 拉下来拆了。约 12.4k 行 TypeScript，MIT，22k+ Star。三路独立阅读分别看准入、异步执行与可靠性、订阅与上下文，最后每条结论都回到 `file:line` 复核。

结论有点尴尬。**文章里的五个核心卖点，只有一条完全兑现**；同一份代码里，有三块工程实现得比文章写得还严谨。

## 兑现度表

| 文章卖点 | 代码现实 | 判定 |
|---|---|---|
| 两级过滤（规则→语义） | `src/policy.ts:197` gate + `src/policy.ts:40-61` 研判硬校验（规则恰好一条、accept 需 intent_evidence、防幻觉规则名）+ `src/sources/admission-context.ts:9` 有界上下文 | **兑现，且更严谨** |
| Watchdog 看任务跑偏 | 全库 0 命中 `watchdog`；只有预算重试投影、效果队列三次标 error、worker 自报 attention | **未兑现** |
| Router 靠订阅 Thread 感知冲突 | 订阅者是 ticket（`src/core/model.ts:164`）；两 worker 改同一文件无检测，只在 PR 层 merge conflict 暴露 | **未兑现** |
| 上下文与执行环境解耦 | 大脑、记录、执行根全绑单机单进程同一 SQLite（`src/app/store.ts:64`） | **未兑现** |
| 长期记忆 | `src/core/memory.ts:1` 是测试用内存仓库，`correlated` 字段只写不读 | **未兑现** |

（完整表与三块值得抄的工程见[拆解报告](https://github.com/aidless/superagent-dig/blob/main/01-prototype-dissection.md)；上游没开 issue 与 discussions，核对表归档在[本仓 issue 1](https://github.com/aidless/superagent-build/issues/1)。）

## 值得直接抄的三块

1. **两级准入**。确定性规则门加模型研判，研判输出过三铁律硬校验，模型自己随口说的判词进不来。
2. **先存后做**。`src/app/outbox.ts:27-32` receipt 幂等落库后才 enqueue 回复；outbox 保序 + 5s→300s 退避，唯二合法放弃是 `channelGone/channelIgnored`；SQLite WAL+FULL；`src/core/stage.ts:9-21` 用纯函数重算状态，状态机字段一律不存。
3. **Router 少说话**是 prompt 纪律。never wait、不越权读代码、不转述改写用户原话、IM 回复 100 词硬顶、进度不上浮。

## 于是把四个「未兑现」当需求单，造了一个

[aidless/superagent-build](https://github.com/aidless/superagent-build)，Python 纯标准库、零第三方依赖，`pip install superagent-build`。

- **文件级写作用域治理**。写前 check，未声明即 BLOCK；越界路径 `normpath` 后判定，堵 lexical 穿越；写盘走 `O_NOFOLLOW` 挡住 symlink。
- **可解除的 BLOCK（escalate）**。审批块绑定内容的 SHA-256，换掉内容就得重新审批。
- **真 watchdog + 复审环**。规则层查 touched_paths 越界 / forbid / 无 summary；deviation 先给一次修复机会，次数用尽才终止票，并把「修了几次」落进 SQLite，重启后还在。
- **票级 token 预算硬闸 + 信用门**。累计用量超限即 deny；出现坏开口的批次直接判 INELIGIBLE，脏数据刷不出满分校准。
- **输入侧开口阈值**。`p* = (c_false + c_speak) / (u_effective + c_false)`，observe 模式只记账不打扰人。
- **成本账**。`p* = ΔN·V/ΔT`，V 敏感性和 CI 是否跨 0 都进报告，一个假设参数撑不起结论。

## v0.3.2 新增，治理契约与十一条差异收口

这一版把「说过」改成「可测」。`docs/spec/GOVERNANCE.md` 第 6 节是一张 M1..M29 的表，29 条 MUST 每条都在同一行点名代码锚点和 conformance 测试；第 8 节逐条记差异收口；第 1 节把 8 个干预点里没挂的 5 个明确留白。`docs/adr/ADR-0001-fail-closed.md` 记录 Accepted 决策，治理闸缺依赖时一律不放行、判 BLOCK，实现里没有把闸开成 fail-open 的配置开关，放宽只能来自显式人为动作（claim 作用域、一次性 escalate lift、bot_allowlist 白名单）。

测试侧 `tests/test_conformance.py` 有 23 个测试函数，`tests/test_v033_section8.py` 有 7 个，合计 30 个规范测试。全量回归 `python3 -m pytest tests/ -q` 跑出 177 passed；CI 里另外 9 个脚本套件逐个本地跑，退出码全是 0。

第 8 节原先 11 条「只报告未改实现」的差异本轮全部收口，七处落在代码里。

| 差异 | 落点 | 锚点 |
|---|---|---|
| 无 metrics 时预算闸静默旁路 | `intake(budget_tokens=...)` 在写 receipt 之前抛 `budget_requires_metrics`，改成拒收 | `agent/engine.py:95` |
| watchdog 无判据构造恒返回 OK | 构造即抛 `no_checks`，带 judge 时关规则层仍可用 | `agent/watchdog.py:242` |
| `accept_on_broadcast` 分支不可达 | broadcast 检查排到规则名校验之前，`matched_rule` 改为「恰好一条」计数 | `agent/admission.py:84` |
| outbox 其它异常外泄 | `except Exception` 走同一退避 `mark_retry` | `agent/outbox.py:46` |
| `scopes.json` 截断重写且旧态无痕 | 独立锁文件 `flock`，写前复制 `scopes.json.bak`，同目录临时文件加 `os.replace` 原子替换 | `agent/governance.py:105`、`agent/governance.py:111` |
| escalate lift 在写路径上消费不掉 | 按 `inspect.signature` 探测 gate 参数个数，三参 gate 收 `sha256(content)`，两参老 gate 零改动兼容 | `agent/executing_executor.py:187` |
| idle 回收只写 report | 经 `engine._ruling` 落 `reason: "idle_timeout"` | `agent/scheduler.py:121` |

另外四条是 `pre_write` 的 fail-open 由 M22 守住、两处文档措辞归一（`posixpath.normpath` lexical 归一、`CheckResult.verdict` 补注 ESCALATE 由 ruling 流承载）、一条并发编辑后的行号重锚。第 8 节末尾还留着设计边界，M28 只保证分支可判、escalate lift 要人工先登记、watchdog 的 LLM judge 要外部注入，这些是能力范围，契约没有把它们算成已实现。

## 实测数字（全部能复算）

- **研判层 A/B（30 个标注任务，真网关）**。带 assessor 的臂 precision 1.0 / 总体召回 0.571；无研判层 precision 1.0 / 总体召回 0.286。两臂诱饵误收都是 0。单独看 mention 子集，15 条合法 mention 里 assessor 命中 6 条，无研判层 0 条，研判 prompt 偏保守。报告见 `results/eval/assessor_ab/report.json`，只读副本在 `evidence/assessor_ab/report.json`。
- **32-run 结论级真跑（4 任务 × 4 seeds × 2 臂）**。配对 bootstrap 95% CI 为 [-8.3339, -8.3329]，不跨 0；V 在 0.001→20 元每次的全网格都是 `DUAL_WINS`。看起来很美。**但这是一次打自己脸的复现**。
  - 8-run 探路批曾给出 ΔT = +1654 tokens，说是「双层交 token 税」。32-run 的 ΔT = **-628**，符号反了；
  - 同任务内逐 seed 符号翻转，t-l1 的四个 seed 为 -128 / +287 / -145 / +260，t-l3 为 -441 / -180 / +656 / +29。每对均值 -39.25 tokens，就是噪声量级；
  - 判决之所以稳，由**介入计数假设**（single=2 / dual=1）单边驱动，V=0.001 时成本差只有 -0.0017 元。
  - 结论修正。**治理链不引入可测的 token 税**，同时没有证据写「双层省 token」。成本账的正确用法是当假设压力测试，价值证据得来自确定性的拦截与复核行为。
  - 报告见 `results/real/sweep_b/evidence_report.json`（含 `replication_check` 与 `v_sensitivity`），只读副本 `evidence/sweep/b_evidence_report.json`。
- **输入侧校准**。`results/real/d4_calibration/calibration.json` 记 13 条标注，speak_ok 9 / wait_ok 3 / wait_bad 1，`speak_precision` 1.0，`applied_params=(2.0, 0.5, 0.05)`，另有 1 条人工覆写把「紧急求助被静默」记成 wait_bad。
- **测试与验真**。`python3 -m pytest tests/ -q` 得 177 passed；`python3 service/make_evidence_bundle.py --verify` 与 `sha256sum -c evidence/MANIFEST.sha256` 可逐份验真 `evidence/` 的 9 份只读副本；复算命令见 `evidence/REPRODUCE.md`。

## 我不打算藏的五处局限

1. **V 是参数，介入次数是假设，ΔT 还会抖**。32-run 把「token 税」这个卖点打掉了，成本结论只写成敏感性曲线加复制核验，没有写倍数。
2. **成本账只作假设压力测试**。V=5.0 是假设参数，介入计数 single=2 / dual=1 是记账假设，V 从 0.001 扫到 20 全 `DUAL_WINS` 恰恰说明判决由这个假设单边驱动。
3. **confidence 同质化有改进，完成侧仍单键**。真跑 12 任务批曾出现 12 条 observe 事件 confidence 全是 0.5333，估计器当时只看 `watchdog_pass`；扩成六维证据加输入侧候选后，真跑出现 0.480 / 0.510 / 0.533 三档。可任务完成侧 `agent/engine.py:314` 仍只喂 `watchdog_pass` 一个键，方向性验证的性质没变。
4. **5 个干预点未挂**。`agent_startup`、`pre_model_call`、`post_model_call`、`post_tool_call`、`agent_shutdown` 在治理契约第 1 节明确留白。
5. **单租户边界**。`O_NOFOLLOW` 挡的是 symlink TOCTOU，挡不住对抗进程；代码 docstring 里写明了。

## 收尾

那条短视频对了一半。消费级 Super Agent 现在确实不成熟，主动性边界、权限粒度、成本三件事都还没平。但把「不成熟」归因于「设计过复杂」，押错了地方。复杂度该由治理层买单，而治理层可以拆开、测出来、写成 MUST。

- 源码与 CI https://github.com/aidless/superagent-build
- 拆解与文章 https://github.com/aidless/superagent-dig
- PyPI https://pypi.org/project/superagent-build/
- 治理契约 docs/spec/GOVERNANCE.md
- fail-closed 决策 docs/adr/ADR-0001-fail-closed.md
- 证据 bundle evidence/REPRODUCE.md
