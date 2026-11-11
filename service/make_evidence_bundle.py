"""B3：把真跑证据打成可一键复算/验真的 bundle。

用法：
    python3 service/make_evidence_bundle.py            # 生成 evidence/
    python3 service/make_evidence_bundle.py --verify  # 只验 sha256 清单

bundle 里放的是**只读副本**：原始证据留在 results/，篡改检测针对副本，
不会动真数据。清单格式与 `sha256sum -c` 兼容（两空格分隔）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BUNDLE = os.path.join(_ROOT, "evidence")

# (源相对路径, bundle 内相对路径)。缺文件要如实登记 skipped，不能静默略过。
SOURCES = [
    ("results/real/sweep_curve.json", "sweep/k1_8run_curve.json"),
    ("results/real/sweep_b/runs.jsonl", "sweep/b_32run_records.jsonl"),
    ("results/real/sweep_b/evidence_report.json", "sweep/b_evidence_report.json"),
    ("results/eval/assessor_ab/report.json", "assessor_ab/report.json"),
    ("results/eval/assessor_ab/armA.jsonl", "assessor_ab/armA.jsonl"),
    ("results/eval/assessor_ab/armB.jsonl", "assessor_ab/armB.jsonl"),
    ("results/real/k2_calibration.json", "calibration/k2_calibration.json"),
    ("results/real/d4_calibration/calibration.json", "calibration/d4_calibration.json"),
    ("service/observe_log.jsonl", "calibration/service_observe.jsonl"),
    ("docs/demo_events.jsonl", "demo/events.jsonl"),
]

REPRODUCE = """# Evidence bundle — 一键复算与验真

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
"""


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def make_manifest(bundle_dir: str, files: list[tuple[str, str]]) -> str:
    """files: [(bundle 内相对路径, 绝对路径)] → MANIFEST.sha256 文本。"""
    lines = []
    for rel, _abs in sorted(files):
        lines.append(f"{sha256_file(_abs)}  {rel}")
    return "\n".join(lines) + "\n"


def build() -> dict:
    os.makedirs(_BUNDLE, exist_ok=True)
    copied: list[tuple[str, str]] = []
    skipped: list[str] = []
    for src_rel, dst_rel in SOURCES:
        src = os.path.join(_ROOT, src_rel)
        if not os.path.exists(src):
            skipped.append(src_rel)
            continue
        dst = os.path.join(_BUNDLE, dst_rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied.append((dst_rel, dst))
    manifest = make_manifest(_BUNDLE, copied)
    with open(os.path.join(_BUNDLE, "MANIFEST.sha256"), "w",
              encoding="utf-8") as f:
        f.write(manifest)
    with open(os.path.join(_BUNDLE, "REPRODUCE.md"), "w",
              encoding="utf-8") as f:
        f.write(REPRODUCE)
    status = {"copied": [r for r, _ in copied],
              "skipped_missing": skipped,
              "manifest_sha256": sha256_file(os.path.join(_BUNDLE,
                                                        "MANIFEST.sha256"))}
    with open(os.path.join(_BUNDLE, "bundle_status.json"), "w",
              encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)
    return status


def verify() -> dict:
    path = os.path.join(_BUNDLE, "MANIFEST.sha256")
    if not os.path.exists(path):
        return {"ok": False, "checked": 0, "bad": ["MANIFEST.sha256 missing"]}
    bad: list[str] = []
    checked = 0
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        want, rel = line.split("  ", 1)
        fp = os.path.join(_BUNDLE, rel)
        if not os.path.exists(fp):
            bad.append(f"missing:{rel}")
            continue
        got = sha256_file(fp)
        checked += 1
        if got != want:
            bad.append(f"hash_mismatch:{rel}")
    return {"ok": not bad, "checked": checked, "bad": bad}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    if args.verify:
        r = verify()
        print(json.dumps(r, ensure_ascii=False))
        return 0 if r["ok"] else 2
    status = build()
    print(json.dumps(status, ensure_ascii=False, indent=2))
    r = verify()
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
