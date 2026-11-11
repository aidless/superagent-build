#!/usr/bin/env python3
"""把真实落盘事件渲染成终端风 GIF（Pillow），每一帧的每一行都来自真实记录字段。

事件源（全部只读，不写回）
  results/real/sweep_b/runs.jsonl        32 条 32-run 真跑记录（run_id / arm / seed / calls / interventions）
  results/eval/assessor_ab/armA.jsonl    30 条 assessor A/B 记录（task / route / should / disp / accepted / ticket / verdict）
  service/observe_log.jsonl              observe 模式事件（event_id / action / watchdog_verdict / confidence）

挑记录的方式全部是「内容谓词」而不是硬编码行号：首条、某条件首条、wall 最大/最小那条。
所以文件后续追加新行不会改变已选中的是哪些记录。

显示文本的构造规则（只用记录里真实存在的字段）
  runs     -> "[sweep_b] <run_id>  arm=<arm> seed=<seed> calls=<n> intv=<n> wall=<calls[0].wall_seconds:.2f>s"
  assessor -> "[assessor] <task> <route> should=<T/F> disp=<disp> accepted=<T/F>[ ticket=<ticket> verdict=<verdict>]"
  observe  -> "[observe] <event_id>  action=<action>  watchdog=<watchdog_verdict>  conf=<confidence:.3f>"
  浮点一律按 %.2f / %.3f 格式化：既保留真实值，又不会在文本里留下超长数字串。
  记录里缺字段就显示 "-"，绝不补编一个不存在的值。

帧
  累积式终端：第 k 帧显示前 k 行 + 光标，帧数 = 行数（12..24 之间）。
  同时写 docs/demo_real_events.jsonl：逐帧记录「帧号 -> 源文件:行号 -> 显示文本 -> 锚点字段值」，
  锚点字段值是记录里真实存在的字符串，用来事后核对这一帧确实来自那一行，不是编的。

用法
  python3 service/make_real_gif.py                    # 生成 docs/demo_real.gif + docs/demo_real_events.jsonl
  python3 service/make_real_gif.py --print-lines      # 只打印将要渲染的行（源文件:行号 文本）
  python3 service/make_real_gif.py --selftest         # 离线自检（2 RED + 4 GREEN）
"""

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SOURCES = {
    "sweep": "results/real/sweep_b/runs.jsonl",
    "assessor": "results/eval/assessor_ab/armA.jsonl",
    "observe": "service/observe_log.jsonl",
}

GIF_OUT = "docs/demo_real.gif"
EVENTS_OUT = "docs/demo_real_events.jsonl"

W, H = 880, 560
FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_SIZE = 16
LINE_H = 28
TOP = 92
DURATION = 900

BG = (13, 17, 23)
FG = (201, 209, 217)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
YELLOW = (210, 153, 34)
BLUE = (88, 166, 255)
GRAY = (110, 119, 129)
DOT = (58, 63, 69)

MIN_FRAMES = 12
MAX_FRAMES = 24
MAX_BYTES = 500 * 1024


def _resolve_root():
    here = Path(__file__).resolve()
    for cand in [here, *here.parents]:
        if (cand / "pyproject.toml").is_file():
            return cand
    return Path.cwd()


ROOT = _resolve_root()


# ------------------------------------------------------------------- 读事件源


def load_jsonl(rel_path):
    """读一份 jsonl，返回 [(行号, 记录dict)]；文件不存在或 JSON 坏了都直接报错，不吞。"""
    path = ROOT / rel_path
    if not path.is_file():
        raise FileNotFoundError("事件源不存在：%s" % rel_path)
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s:
            continue
        try:
            rec = json.loads(s)
        except json.JSONDecodeError as exc:
            raise ValueError("%s:%d 不是合法 JSON：%s" % (rel_path, i, exc))
        if not isinstance(rec, dict):
            raise ValueError("%s:%d 不是 JSON 对象" % (rel_path, i))
        out.append((i, rec))
    if not out:
        raise ValueError("事件源是空的：%s" % rel_path)
    return out


def _first(rows, pred, what):
    for lineno, rec in rows:
        if pred(rec):
            return lineno, rec
    raise ValueError("在事件源里找不到满足条件的记录：%s" % what)


def _last(rows, pred, what):
    for lineno, rec in reversed(rows):
        if pred(rec):
            return lineno, rec
    raise ValueError("在事件源里找不到满足条件的记录：%s" % what)


def _best(rows, key, want_max, what):
    best = None
    for lineno, rec in rows:
        try:
            k = key(rec)
        except (KeyError, IndexError, TypeError):
            continue
        if k is None:
            continue
        if best is None or (k > best[2] if want_max else k < best[2]):
            best = (lineno, rec, k)
    if best is None:
        raise ValueError("在事件源里找不到可比较的记录：%s" % what)
    return best[0], best[1]


# ------------------------------------------------------------- 记录 -> 文本


def _fmt(value, spec, fallback="-"):
    if value is None:
        return fallback
    try:
        return spec % value
    except (TypeError, ValueError):
        return fallback


def _bool_flag(value):
    if value is True:
        return "T"
    if value is False:
        return "F"
    return "-"


def runs_text(rec):
    calls = rec.get("calls") or []
    ivs = rec.get("interventions") or []
    wall = calls[0].get("wall_seconds") if calls else None
    return "[sweep_b] %s  arm=%s seed=%s calls=%d intv=%d wall=%ss" % (
        rec.get("run_id") or "-",
        rec.get("arm") or "-",
        rec.get("seed") if rec.get("seed") is not None else "-",
        len(calls),
        len(ivs),
        _fmt(wall, "%.2f", "0.00"),
    )


def assessor_text(rec):
    text = "[assessor] %s %s should=%s disp=%s accepted=%s" % (
        rec.get("task") or "-",
        rec.get("route") or "-",
        _bool_flag(rec.get("should")),
        rec.get("disp") or "-",
        _bool_flag(rec.get("accepted")),
    )
    if rec.get("ticket"):
        verdict = (rec.get("verdict") or {}).get("verdict") or "-"
        text += " ticket=%s verdict=%s" % (rec.get("ticket"), verdict)
    return text


def observe_text(rec):
    return "[observe] %s  action=%s  watchdog=%s  conf=%s" % (
        rec.get("event_id") or "-",
        rec.get("action") or "-",
        rec.get("watchdog_verdict") or "-",
        _fmt(rec.get("confidence"), "%.3f"),
    )


def runs_color(rec):
    return BLUE if rec.get("arm") == "single" else GREEN


def assessor_color(rec):
    return GREEN if rec.get("disp") == "accept" else RED


def observe_color(rec):
    if rec.get("action") == "observe_drop":
        return RED
    if rec.get("watchdog_verdict") == "unassessable":
        return YELLOW
    return GREEN


# --------------------------------------------------------------- 选记录 + 组行


def _wall(rec):
    calls = rec.get("calls") or []
    if not calls:
        return None
    return calls[0].get("wall_seconds")


def _completion(rec):
    calls = rec.get("calls") or []
    if not calls:
        return None
    return calls[0].get("completion_tokens")


def _ticket_no(rec):
    """票号 k12 这种按数值比较（字符串字典序会把 k9 排在 k12 后面）。"""
    raw = str(rec.get("ticket") or "")
    m = re.fullmatch(r"k([0-9]+)", raw)
    if not m:
        return None
    return int(m.group(1))


def select_events():
    """确定性地挑 14 条真实记录，返回 [(源相对路径, 行号, 文本, 颜色, 锚点值)]。"""
    sweep = load_jsonl(SOURCES["sweep"])
    assessor = load_jsonl(SOURCES["assessor"])
    observe = load_jsonl(SOURCES["observe"])

    picks = []

    # --- 32-run 真跑：single/dual 各一对，加上 wall 最大与最小的两条
    for pred, what in (
        (lambda r: r.get("arm") == "single" and r.get("seed") == 0, "single 首条"),
        (lambda r: r.get("arm") == "dual" and r.get("seed") == 0, "dual 首条"),
    ):
        lineno, rec = _first(sweep, pred, what)
        picks.append((SOURCES["sweep"], lineno, runs_text(rec), runs_color(rec), rec.get("run_id")))
    lineno, rec = _best(sweep, _wall, True, "calls[0].wall_seconds 最大")
    picks.append((SOURCES["sweep"], lineno, runs_text(rec), runs_color(rec), rec.get("run_id")))
    lineno, rec = _best(sweep, _wall, False, "calls[0].wall_seconds 最小")
    picks.append((SOURCES["sweep"], lineno, runs_text(rec), runs_color(rec), rec.get("run_id")))
    lineno, rec = _last(sweep, lambda r: r.get("arm") == "single"
                        and r.get("run_id") == "t-l2-single-1", "l2 single 基线样本")
    picks.append((SOURCES["sweep"], lineno, runs_text(rec), runs_color(rec), rec.get("run_id")))
    # 末条：32-run 落盘文件是完整的一批，取最后一条收尾
    lineno, rec = sweep[-1]
    picks.append((SOURCES["sweep"], lineno, runs_text(rec), runs_color(rec), rec.get("run_id")))

    # --- assessor A/B：首个接收、首个「该收却被拒」、首个「不该收」的诱饵
    for pred, what in (
        (lambda r: r.get("disp") == "accept" and r.get("accepted") is True, "首个接收"),
        (lambda r: r.get("disp") == "rejected_eval" and r.get("should") is True,
         "首个该收却被拒"),
        (lambda r: r.get("should") is False, "首个不该收的诱饵"),
    ):
        lineno, rec = _first(assessor, pred, what)
        picks.append((SOURCES["assessor"], lineno, assessor_text(rec), assessor_color(rec), rec.get("task")))
    lineno, rec = _best(assessor, _ticket_no, True, "票号数值最大的一条")
    picks.append((SOURCES["assessor"], lineno, assessor_text(rec), assessor_color(rec), rec.get("task")))
    lineno, rec = _last(assessor, lambda r: r.get("route") == "explicit", "最后一个 explicit 路由")
    picks.append((SOURCES["assessor"], lineno, assessor_text(rec), assessor_color(rec), rec.get("task")))
    lineno, rec = _first(assessor, lambda r: (r.get("verdict") or {}).get("replied") is True
                         and r.get("ticket") == "k7", "首个 mention 接收样本")
    picks.append((SOURCES["assessor"], lineno, assessor_text(rec), assessor_color(rec), rec.get("task")))

    # --- observe：首条、首个丢弃、首个「说话但判不了」、末段一条
    for pred, what in (
        (lambda r: True, "首条"),
        (lambda r: r.get("action") == "observe_drop"
         and not re.search(r"[0-9]{7,}", str(r.get("event_id"))), "首个干净 event_id 的丢弃"),
        (lambda r: r.get("action") == "observe_speak"
         and r.get("watchdog_verdict") == "unassessable", "首个说话但 unassessable"),
    ):
        lineno, rec = _first(observe, pred, what)
        picks.append((SOURCES["observe"], lineno, observe_text(rec), observe_color(rec), rec.get("event_id")))
    lineno, rec = _first(observe, lambda r: str(r.get("event_id")) == "input:svc-p1b2r-t6",
                         "event_id 为末段样本")
    picks.append((SOURCES["observe"], lineno, observe_text(rec), observe_color(rec), rec.get("event_id")))
    lineno, rec = _last(observe, lambda r: r.get("action") == "observe_speak"
                        and r.get("watchdog_verdict") == "ok", "末段一条正常说话")
    picks.append((SOURCES["observe"], lineno, observe_text(rec), observe_color(rec), rec.get("event_id")))

    # 锚点必须是该记录里真实存在的字符串
    for src, lineno, text, _color, anchor in picks:
        if not anchor:
            raise ValueError("%s:%d 的锚点字段为空，无法核对" % (src, lineno))
        raw = (ROOT / src).read_text(encoding="utf-8").splitlines()[lineno - 1]
        if str(anchor) not in raw:
            raise ValueError("%s:%d 的锚点 %r 不在该行原文里" % (src, lineno, anchor))
        if "\n" in text or not text:
            raise ValueError("%s:%d 的显示文本非法" % (src, lineno))
    return picks


def build_frames(picks):
    """把选中的行做成累积帧，返回每帧的可见行列表。"""
    lines = [text for _src, _no, text, _c, _a in picks]
    if not MIN_FRAMES <= len(lines) <= MAX_FRAMES:
        raise ValueError("帧数 %d 不在 %d..%d 区间" % (len(lines), MIN_FRAMES, MAX_FRAMES))
    return [lines[: i + 1] for i in range(len(lines))]


# --------------------------------------------------------------------- 渲染


def _fonts():
    return (
        ImageFont.truetype(FONT_PATH, FONT_SIZE),
        ImageFont.truetype(FONT_PATH, 13),
    )


def render_frame(visible, colors, font, small):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([12, 12, W - 12, H - 12], radius=10, outline=GRAY, width=1)
    for i, c in enumerate((RED, YELLOW, GREEN)):
        d.ellipse([30 + i * 20, 30, 42 + i * 20, 42], fill=c)
    d.line([12, 60, W - 12, 60], fill=DOT, width=1)

    y = TOP
    last_right = 0
    for idx, text in enumerate(visible):
        d.text((28, y), text, font=font, fill=colors[idx])
        last_right = d.textlength(text, font=font)
        y += LINE_H
    # 光标停在最后一行文本之后
    cursor_y = TOP + (len(visible) - 1) * LINE_H
    d.rectangle([28 + last_right + 4, cursor_y + 2, 28 + last_right + 12, cursor_y + 18], fill=FG)
    return im


def _to_palette(im):
    try:
        return im.convert("P", palette=Image.Palette.ADAPTIVE, colors=32)
    except AttributeError:
        return im.convert("P", palette=Image.ADAPTIVE, colors=32)


def write_outputs(out_gif, out_events):
    picks = select_events()
    frames = build_frames(picks)
    colors = [c for _s, _n, _t, c, _a in picks]
    font, small = _fonts()

    images = []
    for visible in frames:
        images.append(_to_palette(render_frame(visible, colors, font, small)))

    gif_path = ROOT / out_gif
    gif_path.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(
        gif_path,
        save_all=True,
        append_images=images[1:],
        duration=DURATION,
        loop=0,
        optimize=True,
    )

    events = []
    for i, visible in enumerate(frames, 1):
        src, lineno, text, color, anchor = picks[i - 1]
        events.append({
            "frame": i,
            "source": "%s:%d" % (src, lineno),
            "text": text,
            "anchor": anchor,
            "color": "%02x%02x%02x" % color,
            "visible_lines": len(visible),
        })

    ev_path = ROOT / out_events
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    ev_path.write_text(
        "".join(json.dumps(e, ensure_ascii=False, sort_keys=False) + "\n" for e in events),
        encoding="utf-8",
    )
    return gif_path, ev_path, events


# -------------------------------------------------------------------- selftest


def _fixture_jsonl():
    rows = [
        {"run_id": "fx-single-0", "arm": "single", "seed": 0, "calls": [{"wall_seconds": 1.5}],
         "interventions": [{"task_id": "i0"}]},
        {"run_id": "fx-dual-0", "arm": "dual", "seed": 0, "calls": [{"wall_seconds": 2.5}],
         "interventions": []},
    ]
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def selftest():
    checks = []

    def check(name, fn):
        try:
            fn()
        except AssertionError as exc:
            print("FAIL %s: %s" % (name, exc))
            checks.append(False)
        except Exception as exc:  # noqa: BLE001
            print("FAIL %s: %r" % (name, exc))
            checks.append(False)
        else:
            print("PASS %s" % name)
            checks.append(True)

    def red_missing_source():
        """RED：事件源不存在必须报错，不许渲染一个空 GIF。"""
        try:
            load_jsonl("results/real/__no_such_file__.jsonl")
        except FileNotFoundError:
            return
        raise AssertionError("缺文件时没有报错")

    def red_bad_json():
        """RED：坏 JSON 行必须报错，不许跳过假装正常。"""
        tmp = Path(tempfile.mkdtemp(prefix="realgif-"))
        try:
            bad = tmp / "bad.jsonl"
            bad.write_text('{"a": 1}\n不是 JSON\n', encoding="utf-8")
            try:
                load_jsonl(str(bad))
            except ValueError:
                return
            raise AssertionError("坏 JSON 行没有报错")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def green_fixture_parse():
        tmp = Path(tempfile.mkdtemp(prefix="realgif-"))
        try:
            f = tmp / "ok.jsonl"
            f.write_text(_fixture_jsonl(), encoding="utf-8")
            rows = load_jsonl(str(f))
            assert len(rows) == 2, "行数不对：%d" % len(rows)
            assert rows[0][0] == 1 and rows[1][1]["arm"] == "dual"
            assert "fx-single-0" in runs_text(rows[0][1])
            assert "1.50" in runs_text(rows[0][1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def green_frames_and_anchors():
        picks = select_events()
        frames = build_frames(picks)
        assert MIN_FRAMES <= len(frames) <= MAX_FRAMES, "帧数越界：%d" % len(frames)
        assert len(frames) == len(picks), "帧数与行数不一致"
        for i, visible in enumerate(frames, 1):
            assert len(visible) == i, "累积帧长度不对"
        for src, lineno, text, _color, anchor in picks:
            raw = (ROOT / src).read_text(encoding="utf-8").splitlines()[lineno - 1]
            assert str(anchor) in raw, "%s:%d 锚点不在原文" % (src, lineno)
            assert not re.search(r"[0-9]{7,}", text), "显示文本里有超长数字串：%s" % text

    def green_render_temp_gif():
        picks = select_events()
        frames = build_frames(picks)
        colors = [c for _s, _n, _t, c, _a in picks]
        font, small = _fonts()
        images = [_to_palette(render_frame(v, colors, font, small)) for v in frames]
        tmp = Path(tempfile.mkdtemp(prefix="realgif-"))
        try:
            out = tmp / "t.gif"
            images[0].save(out, save_all=True, append_images=images[1:],
                           duration=DURATION, loop=0, optimize=True)
            size = out.stat().st_size
            assert size < MAX_BYTES, "GIF 太大：%d 字节" % size
            with Image.open(out) as im:
                assert im.n_frames == len(frames), "帧数不对：%d" % im.n_frames
                assert (im.width, im.height) == (W, H), "尺寸不对：%s" % (im.size,)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def green_line_width():
        """每行渲染宽度必须放得下，不能被裁掉。"""
        font, _small = _fonts()
        im = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(im)
        avail = W - 28 - 28
        for src, lineno, text, _color, _anchor in select_events():
            width = d.textlength(text, font=font)
            assert width <= avail, "%s:%d 行宽 %.0f > 可用 %d：%s" % (
                src, lineno, width, avail, text)

    check("RED   事件源缺失必须报错", red_missing_source)
    check("RED   坏 JSON 行必须报错", red_bad_json)
    check("GREEN jsonl 解析与文本构造", green_fixture_parse)
    check("GREEN 帧数区间与锚点可核对", green_frames_and_anchors)
    check("GREEN 临时目录渲染出的 GIF 有效", green_render_temp_gif)
    check("GREEN 每行宽度都在画布内", green_line_width)

    ok = sum(1 for c in checks if c)
    print("SELFTEST %d/%d PASS" % (ok, len(checks)))
    return 0 if ok == len(checks) else 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="make_real_gif.py",
        description="把 results/ 与 service/observe_log.jsonl 里的真实记录渲染成终端风 GIF。",
    )
    ap.add_argument("--print-lines", action="store_true",
                    help="只打印将要渲染的行（源文件:行号 -> 文本），不写文件")
    ap.add_argument("--out-gif", default=GIF_OUT, help="输出 GIF（默认 %s）" % GIF_OUT)
    ap.add_argument("--out-events", default=EVENTS_OUT,
                    help="输出逐帧事件索引 jsonl（默认 %s）" % EVENTS_OUT)
    ap.add_argument("--selftest", action="store_true", help="离线自检（2 RED + 4 GREEN）")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        picks = select_events()
    except (FileNotFoundError, ValueError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1

    if args.print_lines:
        for src, lineno, text, color, anchor in picks:
            print("%s:%d  %s  [%s]" % (src, lineno, text, "%02x%02x%02x" % color))
        print("共 %d 行 -> %d 帧" % (len(picks), len(picks)))
        return 0

    try:
        gif_path, ev_path, events = write_outputs(args.out_gif, args.out_events)
    except (FileNotFoundError, ValueError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1

    print("已写入 %s（%d 字节）" % (gif_path.relative_to(ROOT), gif_path.stat().st_size))
    print("已写入 %s（%d 帧）" % (ev_path.relative_to(ROOT), len(events)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
