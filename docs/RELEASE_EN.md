# Release draft (English — dev.to / Hacker News / r/LocalLLaMA)

> Title options:
> 1. I took apart a 22k-star Super Agent prototype — only one of its five headline claims held up
> 2. Four gates for agents: from source dissection to a pip-installable v0.3.2
> 3. The cost argument for multi-agent was wrong in my own benchmark

Version note before the article. This is the v0.3.2 release, code at main HEAD. The v0.3.2 upload to PyPI is done by the maintainer in this same batch; the published snapshot right now is 0.3.1. Every number below comes from artifacts on disk in this repo, and the reproduction commands are at the end.

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

(Full table + the three pieces worth copying: [dissection report](https://github.com/aidless/superagent-dig/blob/main/01-prototype-dissection.md). Upstream has issues and discussions disabled, so the table is archived in [issue #1](https://github.com/aidless/superagent-build/issues/1).)

## Worth copying

1. **Two-stage admission** with hard validation of the model's verdict, so a verdict the model invents does not get through on its own say-so.
2. **Persist-then-act**: receipts land idempotently before the reply is enqueued (`src/app/outbox.ts:27-32`); outbox preserves order, backs off 5s→300s, and only `channelGone`/`channelIgnored` may abandon; SQLite WAL+FULL; state is recomputed by pure functions (`src/core/stage.ts:9-21`) rather than stored as a state machine.
3. **"Router talks little" as prompt discipline**: never wait, don't read code it wasn't asked to, don't paraphrase requests, hard 100-word IM cap, no progress chatter.

## So I turned the four "not delivered" rows into a spec

[aidless/superagent-build](https://github.com/aidless/superagent-build) — pure Python stdlib, no third-party deps, `pip install superagent-build`.

- **File-level write-scope governance**: check before write, BLOCK when undeclared; paths are `normpath`-ed (lexical traversal closed); writes go through `O_NOFOLLOW`.
- **Escalation that can be lifted**: the approval block is bound to the SHA-256 of the content, so changing the content means a fresh approval.
- **A real watchdog + review loop**: rule layer checks touched paths, forbid list, missing summary; a deviation gets exactly one repair, then the ticket ends, and the repair count lives in SQLite (survives restart).
- **Per-ticket token budget as a hard gate** and an APx-style credit gate: batches containing a bad speak event are marked INELIGIBLE instead of silently inflating calibration.
- **Input-side proactivity threshold**: `p* = (c_false + c_speak) / (u_effective + c_false)`; observe mode logs without interrupting anyone.
- **Cost accounting**: `p* = ΔN·V/ΔT`, with V-sensitivity and a CI-crosses-zero readout instead of a single flattering number.

## What v0.3.2 adds: a governance contract and 11 closed gaps

This release turns "we said so" into "you can test it". `docs/spec/GOVERNANCE.md` section 6 is a table of M1..M29, 29 MUST clauses, and every row names its code anchor and its conformance test on the same line. Section 8 records each resolved discrepancy; section 1 writes the 5 of 8 intervention points that are still unhooked as explicit blanks. `docs/adr/ADR-0001-fail-closed.md` is the Accepted decision: a governance gate with a missing dependency fails closed and returns BLOCK, there is no configuration switch that turns a gate into fail-open, and any loosening comes from an explicit human action (a `claim` on a write scope, a one-shot escalate lift, a `bot_allowlist` entry).

On the test side, `tests/test_conformance.py` holds 23 test functions and `tests/test_v033_section8.py` holds 7, so 30 specification tests. Full regression `python3 -m pytest tests/ -q` reports 177 passed; the other 9 script suites in CI were each run locally and all exit 0.

The 11 "reported but not implemented" discrepancies in section 8 are all closed, seven of them in code.

| Discrepancy | What changed | Anchor |
|---|---|---|
| budget gate silently bypassed when `metrics` is absent | `intake(budget_tokens=...)` raises `budget_requires_metrics` before the receipt is written, so the gate rejects | `agent/engine.py:95` |
| a watchdog with no criteria returned OK for any payload | construction now raises `no_checks`; with a judge injected, turning the rule layer off still works | `agent/watchdog.py:242` |
| `accept_on_broadcast` branch unreachable | the broadcast check now runs before rule-name validation, and `matched_rule` is a "exactly one" count check | `agent/admission.py:84` |
| other outbox exceptions leaked | `except Exception` takes the same `mark_retry` backoff | `agent/outbox.py:46` |
| `scopes.json` truncated in place with no previous state | `flock` on a separate lock file, a `scopes.json.bak` snapshot copied before the write, then a same-directory temp file and `os.replace` atomic swap | `agent/governance.py:105`, `agent/governance.py:111` |
| escalate lift could not be consumed on the write path | `inspect.signature` probes the gate arity; a three-arg gate receives `sha256(content)`, two-arg gates keep working unchanged | `agent/executing_executor.py:187` |
| idle reclamation only wrote `a.report` | idle reclamation now goes through `engine._ruling` with `reason: "idle_timeout"` | `agent/scheduler.py:121` |

The other four are `pre_write` fail-open now held by M22, two doc wordings normalized (`posixpath.normpath` lexical normalization, and a note that ESCALATE is carried by the ruling stream rather than returned by `check()`), and one line-number re-anchor after concurrent edits. Section 8 still lists the design boundaries at the end: M28 only guarantees the branch is decidable, escalate lift needs a human to register it first, and the watchdog LLM judge needs an injected gateway. Those are capability limits, not clauses the contract counts as done.

## Measured, and one result that refutes my own pitch

- **Admission A/B (30 labeled tasks, real gateway)**: with the assessor, precision 1.0 / overall recall 0.571; without it, precision 1.0 / overall recall 0.286. Both arms admit zero decoys. On the mention subset alone, the assessor catches 6 of 15 legitimate mentions and the no-assessor arm catches 0, so the prompt is still too conservative. Report: `results/eval/assessor_ab/report.json`, read-only copy at `evidence/assessor_ab/report.json`.
- **32-run conclusion batch (4 tasks × 4 seeds × 2 arms)**: paired bootstrap 95% CI = [-8.3339, -8.3329], does not cross zero; `DUAL_WINS` across the whole V grid (0.001→20 per intervention). Looks great. **And it refutes the earlier claim**:
  - the 8-run pilot reported ΔT = +1654 tokens ("the dual loop pays a token tax"). The 32-run batch reports ΔT = **-628**, opposite sign;
  - worse, the sign flips **within a single task across seeds**: t-l1 gives -128 / +287 / -145 / +260 and t-l3 gives -441 / -180 / +656 / +29. Mean per pair: -39.25 tokens, i.e. noise;
  - the verdict only holds because it is driven single-handedly by the *assumed* intervention counts (single=2 / dual=1): at V=0.001 the cost delta is -0.0017 CNY.
  - Corrected conclusion: **the governance chain introduces no measurable token tax**, and there is no evidence for "dual saves tokens". The cost model is a stress test on assumptions; the real value evidence has to come from deterministic interception/review behaviour.
  - Report: `results/real/sweep_b/evidence_report.json` (includes `replication_check` and `v_sensitivity`), copied into the bundle at `evidence/sweep/b_evidence_report.json`.
- **Input-side calibration**: `results/real/d4_calibration/calibration.json` records 13 labeled events (speak_ok 9 / wait_ok 3 / wait_bad 1), `speak_precision` 1.0, `applied_params=(2.0, 0.5, 0.05)`, plus one human override that recorded an urgent-help silence as wait_bad.
- **Tests and verification**: `python3 -m pytest tests/ -q` reports 177 passed; `python3 service/make_evidence_bundle.py --verify` and `sha256sum -c evidence/MANIFEST.sha256` verify the 9 read-only copies under `evidence/`; the reproduction notes are in `evidence/REPRODUCE.md`.

## Five limitations I won't hide

1. **V is a parameter, intervention counts are an assumption, and ΔT jitters.** The 32-run batch killed the "token tax" headline; the cost verdict is written as a sensitivity curve plus a replication check, never as a multiplier.
2. **The cost model is a stress test on assumptions.** V=5.0 is an assumed parameter and single=2 / dual=1 is an accounting assumption; the fact that the whole V grid from 0.001 to 20 reads `DUAL_WINS` is exactly the sign that the verdict is driven by that assumption alone.
3. **Confidence is less homogeneous, but the completion side is still single-key.** A real 12-task batch once produced 12 observe events all at 0.5333 because the estimator read `watchdog_pass` only. After widening to six-dimension evidence plus input-side candidates, real runs show three values, 0.480 / 0.510 / 0.533. The completion side at `agent/engine.py:314` still feeds `watchdog_pass` as its only key, so the nature of that directional check is unchanged.
4. **5 intervention points are unhooked.** `agent_startup`, `pre_model_call`, `post_model_call`, `post_tool_call`, `agent_shutdown` are explicit blanks in section 1 of the governance contract.
5. **Single-tenant boundary.** `O_NOFOLLOW` closes the symlink TOCTOU hole; it is not a sandbox against an adversarial process, and the docstring says so.

## Links

- Code + CI: https://github.com/aidless/superagent-build
- Dissection + articles: https://github.com/aidless/superagent-dig
- PyPI: https://pypi.org/project/superagent-build/
- Governance contract: docs/spec/GOVERNANCE.md
- Fail-closed decision: docs/adr/ADR-0001-fail-closed.md
- Verify the evidence yourself: `python3 service/make_evidence_bundle.py --verify` then `sha256sum -c evidence/MANIFEST.sha256`
