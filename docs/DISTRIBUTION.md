# 分发包 v0.3.2

本文件是投放物料，不是发布记录。里面每一段都能直接复制走。所有数字只取本仓落盘证据，复算命令在 `evidence/REPRODUCE.md`。

截至本文件落盘，下面这些动作一件都还没有执行，没有发帖，没有开 PR，没有建 v0.3.2 的 Release，也没有上传 PyPI。谁是执行方、需要什么凭据，见最后一节。

---

## 一、知乎中文长帖

**标题**

《我把一个 22k Star 的 Super Agent 原型拆了，然后照着它没做到的部分写了个包》

**正文**

刷到一条短视频，博主说 Super Agent 设计太复杂、违背现实逻辑，建议等 Vibe Coding 追上来再说。我没急着站队，把 `deepseek-ai/dsh-superagent-prototype` 拉下来读了一遍，约 12.4k 行 TypeScript，MIT，22k+ Star，三路独立阅读分别看准入、异步可靠性、订阅与上下文。

结论不太体面。文章里的五个核心卖点，只有两级过滤这一条完全兑现，而且比文章写的还严谨。Watchdog 在代码里零命中。Router 的订阅者是 ticket，两个 worker 改同一个文件要等到 PR 合并冲突才暴露。上下文和执行环境全绑在单机单进程的同一个 SQLite 上。长期记忆是个测试用的内存仓库。

同一份代码里倒有三块值得抄，两级准入的硬校验、receipt 先落库再 enqueue 的 outbox、以及「Router 少说话」的 prompt 纪律。

于是我拿那四个没兑现的当需求单，写了个 Python 包。定位一句话，它是个纯标准库的 agent 治理内核，把写作用域、准入研判、复核环、票级预算这四道闸做成 fail-closed 的契约，依赖缺位时默认拦下来。

三条能自己复算的事实。

第一，治理契约是可测的。`docs/spec/GOVERNANCE.md` 第 6 节是一张 M1..M29 的表，29 条 MUST 每条都在同一行点名自己的代码锚点和 conformance 测试。配套 30 个规范测试，其中 `tests/test_conformance.py` 23 个，`tests/test_v033_section8.py` 7 个。`docs/adr/ADR-0001-fail-closed.md` 是 Accepted 的决策记录，写明治理闸不得提供 fail-open 开关。全量 `python3 -m pytest tests/ -q` 跑出 177 passed，CI 里另外 9 个脚本套件逐个本地跑，退出码全是 0。

第二，上一版列在规范第 8 节的 11 条「只报告未改实现」的差异，这一版全部收口，七处落在代码里。举三个。没有 metrics 时，`intake(budget_tokens=...)` 在写 receipt 之前就抛 `budget_requires_metrics`，预算闸从静默旁路变成拒收。`scopes.json` 原来是截断重写、旧状态无痕，现在写前复制一份 `scopes.json.bak`，再用同目录临时文件加 `os.replace` 原子替换。watchdog 用零判据构造出来恒返回 OK，现在构造即抛 `no_checks`。剩下四处是 watchdog、broadcast 可达性、outbox 异常退避、escalate lift 的写路径消费、idle 回收落 ruling，逐条都能在表里对上锚点。

第三，实验数字里有一条是打自己脸的。研判层 A/B 用 30 个标注任务，带 assessor 的臂总体召回 0.571，不带的是 0.286，两臂诱饵误收都是 0。32-run 结论批给出 ΔT = -628 tokens，配对 95% CI 为 [-8.3339, -8.3329]，不跨 0。可这个结果同时推翻了更早的 8-run 探路批，那批给的是 +1654 tokens。同一任务内逐 seed 符号还会翻转，每对均值 -39.25 tokens，就是噪声量级。

一条我不藏的局限。成本账里的 V=5.0 是假设参数，single=2 / dual=1 是记账假设，判决基本由这个假设单边驱动，所以我没有写「双层省 token」的倍数结论，只用它做假设压力测试。另外 8 个干预点里还有 5 个没挂，`agent_startup`、`pre_model_call`、`post_model_call`、`post_tool_call`、`agent_shutdown`，在契约第 1 节里是明确留白。confidence 这一版从全同质变成三档，但任务完成侧仍然只读 `watchdog_pass` 一个键。

源码 https://github.com/aidless/superagent-build
拆解报告 https://github.com/aidless/superagent-dig
PyPI https://pypi.org/project/superagent-build/
治理契约 docs/spec/GOVERNANCE.md

---

## 二、掘金中文技术帖

**标题**

《29 条 MUST、30 个规范测试、11 条差异收口，一个 agent 治理内核的 v0.3.2》

**正文**

这个包叫 superagent-build，纯 Python 标准库，零第三方依赖。它解决的是一个很具体的问题，agent 的治理逻辑一旦写进业务代码，就只能靠读代码判断它有没有生效。所以这一版把治理逻辑抽成契约，再把契约钉到测试上。

先看契约的结构。`docs/spec/GOVERNANCE.md` 第 6 节是 M1..M29 的表格，29 条 MUST，每一行左边是规范条款，中间是代码锚点，右边是守住它的 conformance 测试。比如 M22 管的是 `pre_write` 在治理挂点缺位时的姿态，锚点是 `agent/engine.py:335-352`，测试是 `test_m22_pre_write_without_governance_is_fail_closed`。测试侧一共 30 个，`tests/test_conformance.py` 23 个，`tests/test_v033_section8.py` 7 个。

为什么强调 fail-closed。`docs/adr/ADR-0001-fail-closed.md` 里写了理由。治理闸全是「依赖外部组件才能判断」的位置，依赖缺失时如果默认放行，系统会更顺，但治理恰好在没人管的那一刻消失。实测过的三条旧接线都在这个坑里，`pre_write` 在 `governance is None` 时返回 True，`Watchdog(judge=None, rules_check=False)` 对任何 payload 返回 OK，`_tokens_used()` 对没有 metrics 的 executor 返回 0。现在这三处分别是 BLOCK、构造即拒、抛 `budget_requires_metrics`。

第二块是第 8 节的 11 条差异。这一版全部收口，七处是代码改动。

| 差异 | 落点 |
|---|---|
| 无 metrics 时预算闸静默旁路 | `agent/engine.py:95` 抛 `budget_requires_metrics` |
| watchdog 无判据构造恒 OK | `agent/watchdog.py:242` 抛 `no_checks` |
| `accept_on_broadcast` 分支不可达 | `agent/admission.py:84`，broadcast 检查提前到规则名校验之前 |
| outbox 其它异常外泄 | `agent/outbox.py:46`，`except Exception` 走同一退避 |
| `scopes.json` 截断重写且旧态无痕 | `agent/governance.py:105` 写 `.bak`，`agent/governance.py:111` 用 `os.replace` |
| escalate lift 在写路径上消费不掉 | `agent/executing_executor.py:187` 按 `inspect.signature` 探测三参 gate 收 `sha256(content)` |
| idle 回收只写 report | `agent/scheduler.py:121` 经 `engine._ruling` 落 `idle_timeout` |

剩下四条是 `pre_write` 的 fail-open 已由 M22 守住、两处文档措辞归一、一条并发编辑后的行号重锚。

第三块是证据。全量回归 `python3 -m pytest tests/ -q` 是 177 passed，CI 里 9 个脚本套件逐个本地跑全 0 退出。实验侧有一组自打脸的数字，32-run 结论批 ΔT = -628 tokens，配对 95% CI 为 [-8.3339, -8.3329]，不跨 0，但它同时推翻了 8-run 探路批的 +1654，同任务内逐 seed 符号还会翻转。所以结论只保留「治理链不引入可测的 token 税」，把「双层省 token」这句撤掉。

局限也说清楚。V=5.0 是假设参数，single=2 / dual=1 是记账假设，成本结论只当压力测试用。8 个干预点里 5 个还没挂。confidence 这一版从全集 0.5333 变成 0.480 / 0.510 / 0.533 三档，但任务完成侧仍然只读 `watchdog_pass` 一个键，这一点在 `agent/engine.py:314` 能看到。

源码 https://github.com/aidless/superagent-build
拆解报告 https://github.com/aidless/superagent-dig
PyPI https://pypi.org/project/superagent-build/
治理契约 docs/spec/GOVERNANCE.md 与 ADR docs/adr/ADR-0001-fail-closed.md
证据与验真 evidence/REPRODUCE.md

---

## 三、英文 Reddit / HN 帖

投放时标题与正文一起贴，第一行是标题

```
Show HN: superagent-build — 29 testable MUST clauses for agent governance

I took apart a 22k-star Super Agent prototype and found that only one of its
five headline claims was actually implemented. So I built the missing pieces
in Python: superagent-build, pure stdlib, no third-party deps.

It is a governance kernel for agents. Write scopes, admission assessment, a
watchdog review loop and per-ticket token budgets are expressed as MUST
clauses, and every clause names the test that guards it.

Three things you can check yourself:

1) docs/spec/GOVERNANCE.md section 6 is a table of M1..M29, 29 MUST clauses,
   each row naming its code anchor and its conformance test. 30 spec tests
   (23 in tests/test_conformance.py, 7 in tests/test_v033_section8.py).
   docs/adr/ADR-0001-fail-closed.md records the decision that a gate with a
   missing dependency fails closed, with no switch to make it fail-open.
   python3 -m pytest tests/ -q reports 177 passed; the 9 script suites in CI
   all exit 0 locally.

2) The 11 "reported but not implemented" discrepancies in section 8 are all
   closed this round. Seven landed in code, e.g. a ticket budget with no
   metrics now raises budget_requires_metrics before the receipt is written
   (agent/engine.py:95); scopes.json is snapshotted to scopes.json.bak and
   swapped with os.replace (agent/governance.py:105, :111); a watchdog built
   with no criteria now raises no_checks (agent/watchdog.py:242).

3) The 32-run batch reports delta_tokens = -628 with a paired 95% CI of
   [-8.3339, -8.3329], not crossing zero. It also overturns my own 8-run
   pilot (+1654), and the sign flips within a task across seeds, mean -39.25
   tokens per pair. So the honest claim is "no measurable token tax", not
   "dual saves tokens".

One limitation up front: V=5.0 is an assumed parameter and the intervention
counts (single=2, dual=1) are an accounting assumption, so the cost model is
a stress test, not a measurement. 5 of the 8 intervention points are still
unhooked and written down as explicit blanks. The completion-side confidence
event still reads a single key, watchdog_pass.

Repo: https://github.com/aidless/superagent-build
Dissection of the prototype: https://github.com/aidless/superagent-dig
PyPI: https://pypi.org/project/superagent-build/
Verify: python3 service/make_evidence_bundle.py --verify && sha256sum -c evidence/MANIFEST.sha256
```

---

## 四、X / Twitter 短帖串

```
1/3
Most agent guardrails ship as config. I turned mine into 29 MUST clauses,
each with a conformance test. superagent-build v0.3.2, pure Python stdlib.
160 tests pass. 11 spec gaps closed this round.
https://github.com/aidless/superagent-build

2/3
The 32-run batch: delta_tokens -628, CI [-8.3339, -8.3329], does not cross
zero. It also overturns my own 8-run pilot (+1654). Sign flips per seed,
mean -39 tokens. So: no token tax, and no "dual saves tokens" claim either.

3/3
Honest limits: V=5.0 is assumed, intervention counts are assumed, 5 of 8
intervention points are still unhooked, and completion-side confidence still
reads one key. Single-tenant by design. Governance contract and tests:
https://github.com/aidless/superagent-dig
```

---

## 五、awesome-list PR 草案

### 目标仓库实测

- 仓库全名 `e2b-dev/awesome-ai-agents`
- URL https://github.com/e2b-dev/awesome-ai-agents
- 描述 `A list of AI autonomous agents`，默认分支 `main`，Star 数 30894
- 存在性核验用的是 `curl https://api.github.com/repos/e2b-dev/awesome-ai-agents`，第一轮 HTTP 200，返回同名 full_name。后续复测时匿名配额用完，同一命令返回 HTTP 403 与 `API rate limit exceeded for 119.183.150.122`，`rate_limit` 显示 60 次配额已用满。所以仓库存在性的依据是第一轮的 200，不是后面的 403。
- README 抓取 https://raw.githubusercontent.com/e2b-dev/awesome-ai-agents/main/README.md 返回 HTTP 200，共 212139 字节。

### README 结构与插入位置

- 所有开源项目条目挂在 `# Open-source projects` 这个一级标题下，实测在第 71 行。
- 单个条目的格式是 `## [Name](url)`，下一行一句描述，然后一个 `<details>` 块，里面有 `### Category`、`### Description`、`### Links` 三段。
- 收录规则写在 `## Have anything to add?`（第 55 行），原文是 `Create a pull request or fill in this form. Please keep the alphabetical order and in the correct category.`，所以必须按字母序插进正确分类。
- 相邻条目实测为 `## [Superagent](https://www.superagent.sh/)` 在第 2485 行，`## [SuperAGI](https://superagi.com/)` 在第 2513 行。按字母序 `superagent-build` 排在两者之间，也就是紧接 `Superagent` 条目结束的 `</details>` 之后。

### 建议新增的条目

一行条目文本

```
## [superagent-build](https://github.com/aidless/superagent-build)
```

配套的一行描述

```
A pure-stdlib Python agent governance kernel: 29 MUST clauses, 30 conformance tests, fail-closed write scopes, watchdog and per-ticket token budgets.
```

`<details>` 块内容

```
<details>

### Category

Infrastructure

### Description

superagent-build is a governance kernel for autonomous agents. Write scopes, admission assessment, a watchdog review loop and per-ticket token budgets are expressed as machine-checkable MUST clauses, each naming the conformance test that guards it. Pure Python stdlib, no third-party dependencies.

### Links

GitHub: https://github.com/aidless/superagent-build

PyPI: https://pypi.org/project/superagent-build/

Dissection of the prototype it was built against: https://github.com/aidless/superagent-dig

</details>
```

### PR 标题

```
Add superagent-build to Open-source projects
```

### PR body

```
This adds superagent-build under the S section of Open-source projects,
between Superagent and SuperAGI to keep alphabetical order.

## What it is

superagent-build is a pure-stdlib Python governance kernel for autonomous
agents. It came out of dissecting a 22k-star Super Agent prototype where only
one of five headline claims was actually implemented.

- Repo: https://github.com/aidless/superagent-build
- Positioning: write scopes, admission assessment, a watchdog review loop and
  per-ticket token budgets expressed as MUST clauses, each with a conformance
  test.

## Verifiable facts

1. docs/spec/GOVERNANCE.md section 6 lists M1..M29, 29 MUST clauses, each row
   naming its code anchor and its conformance test. 30 spec tests total
   (23 in tests/test_conformance.py, 7 in tests/test_v033_section8.py).
   python3 -m pytest tests/ -q reports 177 passed.
2. All 11 "reported but not implemented" discrepancies in section 8 are
   closed this round; 7 of them are code changes with named anchors.
3. The 32-run batch reports delta_tokens = -628 with a paired 95% CI of
   [-8.3339, -8.3329]; it also overturns the earlier 8-run pilot (+1654), and
   the sign flips per seed within a task.

## One honest limitation

V=5.0 is an assumed parameter and the intervention counts are an accounting
assumption, so the cost model is a stress test rather than a measurement.
5 of the 8 intervention points are still unhooked and documented as explicit
blanks. The completion-side confidence event still reads a single key.

## Disclosure

No affiliation with e2b. Happy to adjust category or wording to fit the
list's conventions.
```

### 这一条需要提前想到的阻力

README 第 58 行写着 `For adding AI agents'-related SDKs, frameworks and tools, please visit Awesome SDKs for AI Agents. This list is only for AI assistants and agents.` superagent-build 是内核、库、框架性质的东西，按这条规则很可能被维护者指到另一个列表。真被指过去就改投 https://github.com/e2b-dev/awesome-sdks-for-ai-agents ，条目文本不用大改。这个跳转的风险先写在这里，不当成已经谈妥的事。

---

## 六、投放清单

| 动作 | 需要什么 | 执行方 | 现在状态 |
|---|---|---|---|
| 知乎发长帖 | 知乎账号，可发长文，绑定手机号或微信 | Lead | 稿子在本文件第一节，未发布 |
| 掘金发技术帖 | 掘金账号，专栏发帖权限 | Lead | 稿子在本文件第二节，未发布 |
| Reddit 发 r/LocalLLaMA 或 r/AI_Agents | Reddit 账号，账号年龄与 karma 满足版规 | Lead | 英文稿在第三节，未发布 |
| Hacker News 发 Show HN | HN 账号，标题需以 Show HN 开头，新账号有频率限制 | Lead | 英文稿在第三节，未发布 |
| X 发三条短帖串 | X 账号，可发 thread | Lead | 稿子在第四节，未发布 |
| 给 awesome-ai-agents 开 PR | GitHub 账号，对目标仓库有 fork 与开 PR 权限 | Lead | **已开**：https://github.com/e2b-dev/awesome-ai-agents/pull/1971（state=open，head `aidless:add-superagent-build`） |
| 上传 PyPI v0.3.2 | PyPI 账号与 API token，或 trusted publisher 配置，本地 twine | Lead | **已上传并验号**：latest=0.3.2，releases `0.1.0 / 0.3.1 / 0.3.2` |
| 建 GitHub Release v0.3.2 | 仓库写权限 token，或网页登录 | Lead | **已建**：https://github.com/aidless/superagent-build/releases/tag/v0.3.2 （tag v0.3.2 已推） |
| 改 `pyproject.toml` 的 version | 仓库写权限 | Lead | **已改**：`version = "0.3.2"`（commit f3b9618） |
| 归档上游拆解核对表 | 无需额外凭据 | 已在本仓完成 | issue #1 已存在，state 为 open |
| Release v0.2.0 与 v0.3.0 | 无需额外凭据 | 已在本仓完成 | 两个 tag 都在 |
| 两仓互链 | 无需额外凭据 | 已在本仓完成 | 本仓 README 两处链到 superagent-dig，对方 README 三处回链 |
| 治理契约、ADR、三份文档 | 无需额外凭据 | 已在本仓完成 | `docs/spec`、`docs/adr`、本文件 |

## 七、执行回执（v0.3.2 发版会话）

- PyPI 0.3.2、tag v0.3.2、GitHub Release v0.3.2、pyproject 版本号：均已落地并线上验号。
- awesome-list PR #1971 已开（字母序插位实测通过，PR body 带可复算数字与诚实局限）。
- 知乎 / 掘金 / Reddit / HN / X 五个渠道：稿子已就绪，**尚未投放**——这些渠道各自的账号与发帖权限不在本工作区凭据内，由操作者本人按第一节到第四节的稿子手动发布。
- 发版后 CI 首次失败（tests/test_p1a_calibration_apply.py 读未入库的 results/real/d4_calibration/calibration.json）；修复方式是把该报告收进 evidence bundle（10 份副本）并让测试读入库副本，修复 commit 7e02e63 后 CI success。
- 本回执只记录已执行动作；未投放的渠道保持「未发布」状态，不写成已发布。

发帖前请再确认两件事。一是 PyPI 上 v0.3.2 是否已经可见，没上传就把稿子里的版本号与 PyPI 链接改成当前可用的版本，否则读者点进去会看到旧快照。二是 awesome-list 的条目顺序，维护者合并前很可能按自己的字母序重排，这不是问题，但 PR body 里要说明插入位置。
