"""J3：校准闭环——observe 攒的原始事件 → 人工/AI 标注 → calibrate 出参数。

闭环三段：
1) ingest_observe_log：把 Engine.observe_log 的原始事件（observe_speak/...）
   批量灌入 LabelStore，label 由标注函数决定（默认启发式标注：watchdog_pass
   的开口事件 → speak_ok；watchdog 抓到 deviation 的开口 → speak_bad；
   wait/drop 无金标 → wait_ok）。真实使用时换成人工标注。
2) run_calibration：LabelStore.load() → calibrate() → 达标则返回可写回
   ProactivityConfig 的新参数。
3) apply_calibration：calibrate 结果回写 ProactivityConfig（缺口 C3 收口）。

precision≥target 才允许解除 observe（北极星线：主动开口 precision≥80%）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.calibration import LabelStore                      # noqa: E402
from agent.calibration_best import calibrate_best             # noqa: E402
from agent.proactivity import ProactivityConfig               # noqa: E402


def heuristic_label(ev: dict) -> str:
    """默认启发式标注（无人工时的自标）：watchdog 判 OK 的开口=speak_ok，
    deviation=开口说错话=speak_bad，wait/drop 无反证=wait_ok。"""
    action = ev.get("action", "")
    wd = str(ev.get("watchdog_verdict", "")).upper()
    if action.endswith("speak"):
        if wd == "DEVIATION":
            return "speak_bad"
        return "speak_ok"
    return "wait_ok"


def ingest_observe_log(events: list[dict], store: LabelStore,
                       labeler=None) -> int:
    """Engine.observe_log 原始事件 → LabelStore。返回写入条数。
    events 元素即 engine.observe_log 里的 dict（event_id/utility/confidence/
    created_at/p_star/u_effective/action/watchdog_verdict）。"""
    fn = labeler if labeler is not None else heuristic_label
    n = 0
    for ev in events:
        store.append(ev, fn(ev))
        n += 1
    return n


def run_calibration(store: LabelStore,
                    target_precision: float = 0.8,
                    min_speak: int = 3) -> dict:
    """标注 → 全网格择优搜索（K3 修复 C2：样本量门 + (precision, n_speak,
    保守度) 择优）。

    信用门（APx 借鉴）：库里存在 speak_bad（watchdog 抓到过真实坏开口）
    → 官方校准置 INELIGIBLE：calibrated 强制 False + note 说明，best_params
    只作诊断值保留。precision≥target 解除 observe 的北极星线只认干净数据。
    """
    result = calibrate_best(store.load(), target_precision=target_precision,
                            min_speak=min_speak)
    if store.stats().get("ineligible"):
        result = dict(result)
        result["calibrated"] = False
        result["note"] = ("ineligible_run: speak_bad present — credit gate "
                          "(APx-style)，本批数据只做诊断，官方校准拒绝")
    return result


def apply_calibration(cfg: ProactivityConfig, result: dict) -> bool:
    """达标时把参数写回 ProactivityConfig（并解除 observe）。
    返回是否已应用。不达标绝不改参数——宁可继续 observe。"""
    if not result.get("calibrated"):
        return False
    cfg.c_false = float(result["c_false"])
    cfg.c_speak = float(result["c_speak"])
    cfg.lam = float(result["lam"])
    cfg.observe = False
    return True


def close_loop(observe_events: list[dict], state_dir: str,
               target_precision: float = 0.8) -> dict:
    """端到端：攒事件 → 标注入库 → 校准 → （达标则）出可应用参数。
    返回 {"n_labeled", "stats", "calibration", "applied_params"(可选)}。
    """
    store = LabelStore(path=os.path.join(state_dir, "labels.jsonl"))
    n = ingest_observe_log(observe_events, store)
    stats = store.stats()
    result = run_calibration(store, target_precision=target_precision)
    out = {"n_labeled": n, "stats": stats, "calibration": result}
    if result.get("calibrated"):
        out["applied_params"] = {k: result[k] for k in
                                 ("c_false", "c_speak", "lam")}
    return out
