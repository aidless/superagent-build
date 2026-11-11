# Evidence bundle — 一键复算与验真

本目录是只读证据副本，原始数据在仓库 `results/`、`service/`、`docs/` 下。

## 验真（零依赖）

    python3 service/make_evidence_bundle.py --verify
    sha256sum -c evidence/MANIFEST.sha256

任一副本被改动，验真必须返回非零退出码。

## 复算（真网关，需要 Magpie 网关 127.0.0.1:3425）

    python3 -m pytest tests/ -q
    python3 service/run_sweep_b.py --tasks 4 --seeds 4 --resume
    python3 service/eval_assessor_ab.py
    python3 service/make_evidence_bundle.py

`--resume` 按 run_id 跳过已完成 run，records 增量落 JSONL，可断点续跑。
成本账公式在 `vendor/framework.py`（breakeven_price / paired_bootstrap_ci /
verdict / CostModel），V 敏感性与 CI 判读在 `agent/evidence.py`。

## 口径说明（不许省）

- 介入次数是记账假设 single=2 / dual=1，不是实测人打断次数。
- 介入价值 V 是参数不是测量值；见 evidence_report.json 的 v_sensitivity。
- 32-run 任务带 /do 显式标记 → dual 臂 LLMAssessor 不触发；研判开销见
  assessor_ab/report.json。
- K1 8-run 是探路批，只作方向性证据；结论以 32-run 的 CI 判读为准。
