# Release draft (English — dev.to / Hacker News / r/LocalLLaMA)

> Title options:
> 1. I took apart a 22k-star Super Agent prototype — only one of its five headline claims held up
> 2. Four gates for agents: from source dissection to a pip-installable 0.3.1
> 3. The cost argument for multi-agent was wrong in my own benchmark

---

A short video arguing "Super Agent is overhyped, the design is too complex" was making the rounds. Rather than pick a side, I cloned `deepseek-ai/dsh-superagent-prototype` (~12.4k lines of TypeScript, MIT, 22k+ stars) and read it — three independent passes: admission, async/reliability, subscriptions/context. Every claim below traces to a `file:line`.

The awkward result: **only one of the five headline claims is fully implemented** — but three pieces of engineering inside that repo are *more* rigorous than the article suggests.

## Claim-by-claim

| Headline claim | What the code does | Verdict |
|---|---|---|
| Two-stage filtering (rules → semantics) | deterministic gate at `src/policy.ts:197` + hard-validated assessment at `src/policy.ts:40-61` (exactly one matched rule, accept requires `intent_evidence`, hallucinated rule names rejected) + bounded context at `src/sources/admission-context.ts:9` | **Delivered, and stricter than advertised** |
| Watchdog detects task drift | zero hits for `watchdog` in the repo; only budget-retry projection, three-strike error marking, and self-reported attention | **Not delivered** |
| Router senses conflicts via Thread subscriptions | subscribers are *tickets* (`src/core/model.ts:164`); two workers editing the same file is undetected until the PR merge conflict | **Not delivered** |
| Context decoupled from execution environment | brain, records, and execution root all bound to one process and one SQLite (`src/app/store.ts:64`) | **Not delivered** |
| Long-term memory | `src/core/memory.ts:1` is an in-memory test fixture; the `correlated` field is written and never read | **Not delivered** |

(Full table + the three pieces worth copying: [dissection report](https://github.com/aidless/superagent-dig/blob/main/01-prototype-dissection.md). Upstream has issues and discussions disabled — verified via API — so the table is archived in [issue #1](https://github.com/aidless/superagent-build/issues/1).)

## Worth copying

1. **Two-stage admission** with hard validation of the model's verdict — not "let the model decide".
2. **Persist-then-act**: receipts land idempotently before the reply is enqueued (`src/app/outbox.ts:27-32`); outbox preserves order, backs off 5s→300s, and only `channelGone`/`channelIgnored` may abandon; SQLite WAL+FULL; state is recomputed by pure functions (`src/core/stage.ts:9-21`) rather than stored as a state machine.
3. **"Router talks little" as prompt discipline**: never wait, don't read code it wasn't asked to, don't paraphrase requests, hard 100-word IM cap, no progress chatter.

## So I turned the four "not delivered" rows into a spec

[aidless/superagent-build](https://github.com/aidless/superagent-build) — pure Python stdlib, no third-party deps, `pip install superagent-build` (0.3.1):

- **File-level write-scope governance**: check before write, BLOCK when undeclared; paths are `normpath`-ed (lexical traversal closed); writes go through `O_NOFOLLOW`.
- **Escalation that can be lifted**: the approval block is bound to the SHA-256 of the content — the approver approves *the action that will actually run*.
- **A real watchdog + review loop**: rule layer checks touched paths, forbid list, missing summary; a deviation gets exactly one repair, then the ticket ends, and the repair count lives in SQLite (survives restart).
- **Per-ticket token budget as a hard gate** and an APx-style credit gate: batches containing a bad speak event are marked INELIGIBLE instead of silently inflating calibration.
- **Input-side proactivity threshold**: `p* = (c_false + c_speak) / (u_effective + c_false)`; observe mode logs without interrupting anyone.
- **Cost accounting**: `p* = ΔN·V/ΔT`, with V-sensitivity and a CI-crosses-zero readout instead of a single flattering number.

## Measured, and one result that refutes my own pitch

- **Admission A/B (30 labeled tasks, real gateway)**: with the assessor, precision 1.0 / recall 0.571; without it, precision 1.0 / recall 0.286. Zero decoys admitted, but mention recall is only 40% — the prompt is too conservative. Report: `evidence/assessor_ab/report.json`.
- **32-run conclusion batch (4 tasks × 4 seeds × 2 arms)**: paired bootstrap 95% CI = [-8.3339, -8.3329], does not cross zero; `DUAL_WINS` across the whole V grid (0.001→20 per intervention). Looks great — **and it refutes the earlier claim**:
  - the 8-run pilot reported ΔT = +1654 tokens ("the dual loop pays a token tax"). The 32-run batch reports ΔT = **-628** — opposite sign;
  - worse, the sign flips **within a single task across seeds** (t-l1: -128 / +287 / -145 / +260). Mean per pair: -39 tokens, i.e. noise;
  - the verdict only holds because it is driven single-handedly by the *assumed* intervention counts (single=2 / dual=1): at V=0.001 the cost delta is -0.0017 CNY.
  - Corrected conclusion: **the governance chain introduces no measurable token tax**, but we cannot claim "dual saves tokens". The cost model is a stress test on assumptions; the real value evidence has to come from deterministic interception/review behaviour.
  - Report: `results/real/sweep_b/evidence_report.json` (includes `replication_check` and `v_sensitivity`), copied into the bundle at `evidence/sweep/b_evidence_report.json`.

## Three limitations I won't hide

1. **V is a parameter, intervention counts are an assumption, and ΔT jitters.** The 32-run batch killed the "token tax" headline; the cost verdict is written as a sensitivity curve plus a replication check, never as a multiplier.
2. **Proactivity confidence collapses to a constant.** In a real 12-task batch (`results/real/k2_observe_log.jsonl`) all 12 observe events had confidence 0.5333 — the estimator only read a single evidence source (`watchdog_pass`). That data can't calibrate anything, so we widen the evidence sources *before* collecting more.
3. **Single-tenant boundary.** `O_NOFOLLOW` closes the symlink TOCTOU hole; it is not a sandbox against an adversarial process, and the docstring says so.

## Links

- Code + CI: https://github.com/aidless/superagent-build
- Dissection + articles: https://github.com/aidless/superagent-dig
- PyPI: https://pypi.org/project/superagent-build/0.3.1/
- Verify the evidence yourself: `python3 service/make_evidence_bundle.py --verify` then `sha256sum -c evidence/MANIFEST.sha256`
