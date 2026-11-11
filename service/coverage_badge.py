#!/usr/bin/env python3
"""coverage badge 生成器（纯标准库，零依赖）。

做什么
  1) 用 `python3 -m coverage run -m pytest tests/ -q` 跑一遍真实测试与覆盖率；
  2) 解析 `python3 -m coverage report` 输出里的 TOTAL 行百分比；
  3) 用这个百分比写 docs/coverage-badge.svg（扁平徽章，左 "coverage" 右 "NN%"）。

硬规矩
  - 百分比只来自 coverage 的实测 TOTAL 行，不硬编码、不估算、不复用旧值；
  - coverage 没装 -> 打印安装提示并 exit 1，绝不生成假徽章；
  - 解析不到 TOTAL 行 -> exit 1；
  - 颜色档位：< 60 red、< 80 orange、>= 80 green；
  - `--check` 只验不写（实测百分比与现有徽章一致才 exit 0），默认模式才写文件。

模式
  python3 service/coverage_badge.py                   # 跑实测 + 写 docs/coverage-badge.svg
  python3 service/coverage_badge.py --check           # 跑实测 + 校验现有徽章，不写文件
  python3 service/coverage_badge.py --from-report F   # 不跑测，解析已保存的 coverage report
  python3 service/coverage_badge.py --selftest        # 纯 stdlib 离线自检（含 2 条 RED）

注：跑测时把 COVERAGE_FILE 指到临时目录，避免在仓库里留下 .coverage 之类的中间文件；
百分比与是否用仓库内 .coverage 无关，因为解析的就是同一条 TOTAL 行。
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BADGE = "docs/coverage-badge.svg"

# shields.io 同款档位配色
COLORS = (
    (0, "#e05d44", "red"),
    (60, "#fe7d37", "orange"),
    (80, "#97ca00", "green"),
)

TOTAL_LINE_RE = re.compile(r"(?m)^TOTAL\b.*$")
PCT_RE = re.compile(r"(\d{1,3})%")
BADGE_PCT_RE = re.compile(r'aria-label="coverage: (\d{1,3})%"')


def _resolve_root():
    """从本文件向上找含 pyproject.toml 的仓库根（不依赖固定的父级层数）。"""
    here = Path(__file__).resolve()
    for cand in [here, *here.parents]:
        if (cand / "pyproject.toml").is_file():
            return cand
    return Path.cwd()


ROOT = _resolve_root()


def _python():
    return sys.executable or "python3"


# ------------------------------------------------------------------ coverage


def coverage_version():
    """返回 coverage 版本字符串；不可用时返回 None。"""
    try:
        r = subprocess.run(
            [_python(), "-m", "coverage", "--version"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    text = (r.stdout or "").strip() or (r.stderr or "").strip()
    return text or "unknown"


def _install_hint():
    return (
        "coverage 不可用：`%s -m coverage --version` 跑不起来。\n"
        "请先安装（本仓库无依赖，coverage 只是开发工具）：\n"
        "  %s -m pip install coverage\n"
        "装好后重跑本脚本。不会生成任何未实测的徽章。" % (_python(), _python())
    )


def run_coverage_suite(timeout=3600):
    """跑 `coverage run -m pytest tests/ -q` + `coverage report`。

    返回 dict：pytest_returncode / report_text / report_returncode / stderr。
    COVERAGE_FILE 指向临时目录，跑完即删，仓库里不留 .coverage。
    """
    tmp = tempfile.mkdtemp(prefix="covbadge-")
    env = dict(os.environ)
    env["COVERAGE_FILE"] = os.path.join(tmp, ".coverage")
    try:
        run = subprocess.run(
            [_python(), "-m", "coverage", "run", "-m", "pytest", "tests/", "-q"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=timeout,
        )
        rep = subprocess.run(
            [_python(), "-m", "coverage", "report"],
            cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=timeout,
        )
        return {
            "pytest_returncode": run.returncode,
            "pytest_stdout": run.stdout or "",
            "pytest_stderr": run.stderr or "",
            "report_text": rep.stdout or "",
            "report_returncode": rep.returncode,
            "report_stderr": rep.stderr or "",
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def parse_total_percent(report_text):
    """从 coverage report 原文里取 TOTAL 行百分比，返回 (pct, total_line)。

    取最后一条 TOTAL 行（report 若同时打了分组小计，TOTAL 只会有一条，取最后更稳）。
    找不到 TOTAL 或行内没有百分比 -> ValueError，绝不返回猜出来的数。
    """
    lines = TOTAL_LINE_RE.findall(report_text or "")
    if not lines:
        raise ValueError("coverage report 里找不到以 TOTAL 开头的行")
    total_line = lines[-1]
    pcts = PCT_RE.findall(total_line)
    if not pcts:
        raise ValueError("TOTAL 行里找不到 NN%% 形态的百分比：%r" % total_line)
    pct = int(pcts[-1])
    if not 0 <= pct <= 100:
        raise ValueError("TOTAL 行百分比越界：%r" % total_line)
    return pct, total_line


# --------------------------------------------------------------------- badge


def color_for(pct):
    """按档位返回 (fill, name)：<60 red、<80 orange、>=80 green。"""
    fill, name = COLORS[0][1], COLORS[0][2]
    for floor, f, n in COLORS:
        if pct >= floor:
            fill, name = f, n
    return fill, name


def render_svg(pct):
    """渲染扁平徽章 SVG；返回 (svg_text, color_name)。"""
    label = "coverage"
    value = "%d%%" % pct
    left_w = 8 * len(label) + 10
    right_w = 8 * len(value) + 10
    width = left_w + right_w
    fill, name = color_for(pct)
    out = []
    out.append(
        '<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="20" '
        'role="img" aria-label="%s: %s">' % (width, label, value)
    )
    out.append("  <title>%s: %s</title>" % (label, value))
    out.append('  <rect width="%d" height="20" rx="3" fill="#555555"/>' % width)
    out.append('  <rect x="%d" width="%d" height="20" rx="3" fill="%s"/>' % (left_w, right_w, fill))
    out.append('  <rect x="%d" width="3" height="20" fill="%s"/>' % (left_w, fill))
    out.append(
        '  <g fill="#ffffff" text-anchor="middle" '
        'font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">'
    )
    out.append('    <text x="%d" y="14">%s</text>' % (left_w // 2, label))
    out.append('    <text x="%d" y="14">%s</text>' % (left_w + right_w // 2, value))
    out.append("  </g>")
    out.append("</svg>")
    return "\n".join(out) + "\n", name


def read_badge_percent(svg_text):
    """从已有徽章里读回百分比（只认 aria-label，读不到就报错）。"""
    m = BADGE_PCT_RE.search(svg_text or "")
    if not m:
        raise ValueError("徽章文件里找不到 aria-label=\"coverage: NN%\"，无法校验")
    return int(m.group(1))


def write_badge(pct):
    path = ROOT / BADGE
    path.parent.mkdir(parents=True, exist_ok=True)
    svg, name = render_svg(pct)
    path.write_text(svg, encoding="utf-8")
    return path, name


# ---------------------------------------------------------------------- modes


def measure(from_report=None):
    """返回 (pct, total_line, info)。from_report 给定时不跑测，只解析该文件。"""
    if from_report:
        text = Path(from_report).read_text(encoding="utf-8")
        pct, line = parse_total_percent(text)
        return pct, line, {"source": "from-report:%s" % from_report}
    version = coverage_version()
    if version is None:
        raise RuntimeError(_install_hint())
    result = run_coverage_suite()
    if result["report_returncode"] != 0 and not TOTAL_LINE_RE.findall(result["report_text"]):
        raise RuntimeError(
            "`coverage report` 失败（退出码 %d），拿不到 TOTAL 行：\n%s"
            % (result["report_returncode"], (result["report_stderr"] or "").strip())
        )
    pct, line = parse_total_percent(result["report_text"])
    result["coverage_version"] = version
    result["source"] = "coverage run -m pytest tests/ -q"
    return pct, line, result


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="coverage_badge.py",
        description="跑真实 coverage，把 TOTAL 百分比渲染成 docs/coverage-badge.svg。",
    )
    ap.add_argument("--check", action="store_true",
                    help="只验不写：实测百分比必须与现有徽章一致，返回 0/1")
    source = ap.add_mutually_exclusive_group()
    source.add_argument("--from-report", metavar="FILE",
                        help="跳过跑测，解析已保存的 coverage report 文本")
    source.add_argument("--selftest", action="store_true",
                        help="纯 stdlib 离线自检（2 RED + 4 GREEN）")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    try:
        pct, total_line, info = measure(args.from_report)
    except (RuntimeError, ValueError, OSError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1

    if info.get("pytest_returncode") not in (None, 0):
        print("WARN: pytest 退出码 %s（测试有失败），coverage 数据仍然产出，TOTAL 照实解析"
              % info["pytest_returncode"], file=sys.stderr)

    fill, name = color_for(pct)
    print("TOTAL 行原文：%s" % total_line)
    print("实测覆盖率：%d%% -> %s %s" % (pct, name, fill))

    if args.check:
        path = ROOT / BADGE
        if not path.is_file():
            print("ERROR: 徽章不存在：%s（先跑默认模式生成）" % path, file=sys.stderr)
            return 1
        try:
            old = read_badge_percent(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            print("ERROR: %s" % exc, file=sys.stderr)
            return 1
        if old != pct:
            print("ERROR: 徽章过期：文件写的是 %d%%，实测是 %d%%" % (old, pct), file=sys.stderr)
            return 1
        print("CHECK OK: 徽章与实测一致（%d%%）" % pct)
        return 0

    path, name = write_badge(pct)
    print("已写入 %s（%d%%，%s）" % (path.relative_to(ROOT), pct, name))
    return 0


# ------------------------------------------------------------------- selftest


def _fixture_report():
    """格式与 `coverage report` 输出一致的样例，只用于验证解析器。"""
    rows = [
        "Name                      Stmts   Miss  Cover",
        "---------------------------------------------",
        "agent/admission.py          120     18    85%",
        "agent/engine.py             240     60    75%",
        "---------------------------------------------",
        "TOTAL                      4972   1053    79%",
    ]
    return "\n".join(rows) + "\n"


def selftest():
    checks = []

    def check(name, fn):
        try:
            fn()
        except AssertionError as exc:
            print("FAIL %s: %s" % (name, exc))
            checks.append(False)
        except Exception as exc:  # noqa: BLE001 - 自检要报告任何异常
            print("FAIL %s: %r" % (name, exc))
            checks.append(False)
        else:
            print("PASS %s" % name)
            checks.append(True)

    def red_no_total():
        """RED：没有 TOTAL 行必须报错，不许返回一个猜出来的数。"""
        try:
            parse_total_percent("Name  Stmts  Miss  Cover\nagent/x.py  10  1  90%\n")
        except ValueError:
            return
        raise AssertionError("缺 TOTAL 行时没有报错")

    def red_badge_garbage():
        """RED：徽章文件里读不到百分比必须报错。"""
        try:
            read_badge_percent("<svg>no aria-label here</svg>")
        except ValueError:
            return
        raise AssertionError("徽章没有 aria-label 时没有报错")

    def green_parse():
        pct, line = parse_total_percent(_fixture_report())
        assert pct == 79, "解析出的百分比不对：%r" % pct
        assert line.startswith("TOTAL"), "TOTAL 行定位不对：%r" % line

    def green_colors():
        assert color_for(0)[1] == "red"
        assert color_for(59)[1] == "red"
        assert color_for(60)[1] == "orange"
        assert color_for(79)[1] == "orange"
        assert color_for(80)[1] == "green"
        assert color_for(100)[1] == "green"

    def green_render_roundtrip():
        svg, name = render_svg(79)
        assert 'aria-label="coverage: 79%"' in svg, "SVG 缺 aria-label"
        assert name == "orange", "79%% 应该是 orange，实际 %r" % name
        assert read_badge_percent(svg) == 79, "渲染后读回百分比不一致"
        assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")

    def green_write_temp():
        tmp = Path(tempfile.mkdtemp(prefix="covbadge-selftest-"))
        try:
            target = tmp / BADGE
            target.parent.mkdir(parents=True, exist_ok=True)
            svg, _name = render_svg(83)
            target.write_text(svg, encoding="utf-8")
            assert target.is_file() and target.stat().st_size > 0
            assert read_badge_percent(target.read_text(encoding="utf-8")) == 83
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    check("RED   缺 TOTAL 行必须报错", red_no_total)
    check("RED   徽章缺 aria-label 必须报错", red_badge_garbage)
    check("GREEN TOTAL 行解析出百分比", green_parse)
    check("GREEN 三档颜色阈值正确", green_colors)
    check("GREEN SVG 渲染后可读回百分比", green_render_roundtrip)
    check("GREEN 写到临时目录后文件有效", green_write_temp)

    ok = sum(1 for c in checks if c)
    print("SELFTEST %d/%d PASS" % (ok, len(checks)))
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
