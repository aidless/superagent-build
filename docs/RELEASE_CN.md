# 发布稿（中文，掘金 / 知乎 / 公众号可直接贴）

> 标题备选：
> 1. 《我不看好 Super Agent？我把它拆了，还照着造了一个》
> 2. 《22k Star 的 Super Agent 原型，五个卖点只有一条完全兑现》
> 3. 《给 Agent 装四道闸：从源码拆解到 0.3.1 可安装包》

---

刷到一条短视频，博主说「Super Agent 最近很火，但我不太看好」。理由挺实在：设计太复杂、违背现实逻辑，建议等 Vibe Coding 体验追上再说。

我没急着站队，去把 `deepseek-ai/dsh-superagent-prototype` 拉下来拆了。约 12.4k 行 TypeScript，MIT，22k+ Star。三路独立阅读分别看准入、异步执行/可靠性、订阅与上下文，最后每条结论都回到 `file:line` 复核。

结论有点尴尬：**文章里的五个核心卖点，只有一条完全兑现**；但同一份代码里，有三块工程实现得比文章写得还严谨。

## 兑现度表

| 文章卖点 | 代码现实 | 判定 |
|---|---|---|
| 两级过滤（规则→语义） | `src/policy.ts:197` gate + `src/policy.ts:40-61` 研判硬校验（规则恰好一条、accept 需 intent_evidence、防幻觉规则名）+ `src/sources/admission-context.ts:9` 有界上下文 | **兑现，且更严谨** |
| Watchdog 看任务跑偏 | 全库 0 命中 `watchdog`；只有预算重试投影、效果队列三次标 error、worker 自报 attention | **未兑现** |
| Router 靠订阅 Thread 感知冲突 | 订阅者是 ticket（`src/core/model.ts:164`）；两 worker 改同一文件无检测，只在 PR 层 merge conflict 暴露 | **未兑现** |
| 上下文与执行环境解耦 | 大脑/记录/执行根全绑单机单进程同一 SQLite（`src/app/store.ts:64`） | **未兑现** |
| 长期记忆 | `src/core/memory.ts:1` 是测试用内存仓库，`correlated` 字段只写不读 | **未兑现** |

（完整表与三块值得抄的工程：[拆解报告](https://github.com/aidless/superagent-dig/blob/main/01-prototype-dissection.md)；上游没开 issue/discussions，所以核对表归档在[本仓 issue #1](https://github.com/aidless/superagent-build/issues/1)。）

## 值得直接抄的三块

1. **两级准入**：确定性规则门 + 模型研判，且研判输出过三铁律硬校验，不是「让模型自己说说」。
2. **先存后做**：`src/app/outbox.ts:27-32` receipt 幂等落库后才 enqueue 回复；outbox 保序 + 5s→300s 退避，唯二合法放弃是 `channelGone/channelIgnored`；SQLite WAL+FULL；`src/core/stage.ts:9-21` 用纯函数重算状态而不是存状态机。
3. **Router 少说话**是 prompt 纪律：never wait、不越权读代码、不改写用户请求、IM 回复 100 词硬顶、进度不上浮。

## 于是把四个「未兑现」当需求单，造了一个

[aidless/superagent-build](https://github.com/aidless/superagent-build)，Python 纯标准库、零第三方依赖、`pip install superagent-build`（0.3.1）：

- **文件级写作用域治理**：写前 check，未声明即 BLOCK；越界路径 `normpath` 后判定，堵 lexical 穿越；写盘走 `O_NOFOLLOW` 拒 symlink。
- **可解除的 BLOCK（escalate）**：审批块绑定内容的 SHA-256——审批人同意的是真正要执行的那个动作，不是同名路径。
- **真 watchdog + 复审环**：规则层查 touched_paths 越界 / forbid / 无 summary；deviation 先给一次修复机会，次数用尽才终止票，并把「修了几次」落进 SQLite（重启不丢）。
- **票级 token 预算硬闸 + 信用门**：累计用量超限即 deny；出现坏开口的批次直接判 INELIGIBLE，不让脏数据把校准刷成满分。
- **输入侧开口阈值**：`p* = (c_false + c_speak) / (u_effective + c_false)`，observe 模式只记账不打扰人。
- **成本账**：`p* = ΔN·V/ΔT`，V 敏感性和 CI 是否跨 0 都进报告，不拿一个假设参数当结论。

## 实测数字（全部能复算）

- **研判层 A/B（30 个标注任务，真网关）**：带 assessor 的臂 precision 1.0 / recall 0.571；无研判层 precision 1.0 / recall 0.286。诱饵误收 0，但 mention 召回只有 40%——研判 prompt 偏保守。报告：`evidence/assessor_ab/report.json`。
- **32-run 结论级真跑（4 任务 × 4 seeds × 2 臂）**：配对重采样 95% CI = [-8.3339, -8.3329]，不跨 0；V 在 0.001→20 元/次全网格都是 `DUAL_WINS`。看起来很美——**但这是一次打自己脸的复现**：
  - 8-run 探路批曾给出 ΔT = +1654 tokens（「双层交 token 税」）。32-run 的 ΔT = **-628**，符号反了；
  - 更关键的是**同任务内逐 seed 符号翻转**（t-l1 的四个 seed：-128 / +287 / -145 / +260；t-l3：-441 / -180 / +656 / +29）。均值 -39 tokens/run，就是噪声量级；
  - 判决之所以稳，是因为它由**介入计数假设**（single=2 / dual=1）单边驱动：V=0.001 时成本差只有 -0.0017 元。
  - 结论修正：**治理链不引入可测的 token 税**，但不能宣称「双层省 token」。成本账的正确用法是当"上限压力测试"，真正的价值证据得来自确定性的拦截/复核行为，不是成本。
  - 报告：`evidence/sweep/b_evidence_report.json`（含 `replication_check` 与 `v_sensitivity`）。
- **测试与验真**：`python3 -m pytest tests/ -q`；`sha256sum -c evidence/MANIFEST.sha256`；复算命令见 `evidence/REPRODUCE.md`。

## 我不打算藏的三处局限

1. **介入价值 V 是参数，介入次数是假设，ΔT 还会抖**。32-run 把「token 税」这个卖点打掉了：ΔT 在同批内逐 seed 翻转，均值属噪声；判决由介入假设单边驱动。所以成本结论只写成敏感性曲线 + 复制核验，不写倍数。
2. **主动开口的置信度会同质化**。真跑 12 个任务（`results/real/k2_observe_log.jsonl`），12 条 observe 事件 confidence 全是 0.5333——因为估计器当时只看 `watchdog_pass` 一个证据源。这个数不能拿去校准，所以我们先扩证据源再攒数据，而不是先攒一堆常数。
3. **单租户边界**。`O_NOFOLLOW` 解决的是 symlink TOCTOU，不是对抗进程的沙箱；这一点在代码 docstring 里写明了。

## 收尾

那条短视频对了一半：消费级 Super Agent 现在确实不成熟，主动性边界、权限粒度、成本三件事都还没平。但把「不成熟」归因于「设计过复杂」，押错了地方——复杂度该由治理层买单，而治理层是可以拆开、测出来、写成 MUST 的。

- 源码与 CI：https://github.com/aidless/superagent-build
- 拆解与文章：https://github.com/aidless/superagent-dig
- PyPI：https://pypi.org/project/superagent-build/0.3.1/
- 证据 bundle：`evidence/REPRODUCE.md`
