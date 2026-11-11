#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""superagent-build 面试模拟练习器（纯标准库，零依赖，离线）。

数据源：docs/INTERVIEW.md 里的 20 道追问。所有题目、短答要点、可核验
file:line 引用都在运行时从该文件真实字节解析，不在本脚本里硬编码标题或词形。

关键词抽取 / 评分规则（--drill 用，写在这里便于复算）：
  关键词完全来自该题「**短答**」原文，保序去重，最多取 60 个：
    1) 反引号包裹的代码片段（去掉首尾空白）；
    2) 英文/数字技术词，正则 [A-Za-z_][A-Za-z0-9_./:-]{2,}；
    3) 连续汉字串 [\u4e00-\u9fff]+：长度 2..12 整串作一个关键词，
       超过 12 字按每 6 字滑窗切成若干关键词。
  评分 grade(answer, kws)：
    answer 去首尾空白后为空 -> 0 分、命中词空表；
    否则每个关键词做大小写无关的子串匹配，score = round(100 * hits / len(kws))，
    夹在 0..100；kws 为空 -> 0 分。
  「命中词」即匹配到的关键词列表，原样来自短答，不编造。
  --drill 每题答案读到空行或 EOF 为止（支持多行口答），行间用空格拼接多行后打分。
  「命中词」即匹配到的关键词列表，原样来自短答，不编造。

--selftest：纯 stdlib、不联网，跑 2 条 RED + 2 条 GREEN：
  RED   越界题号必须报错（get_question(0) / get_question(21) 抛 ValueError）
  RED   空答案必须 0 分
  GREEN 正常出题返回 0 分 < score <= 100
  GREEN seed 固定可复现（同 seed 抽题序列一致）
"""
import argparse
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTERVIEW = ROOT / "docs" / "INTERVIEW.md"
DRILL_SIZE = 5
KEYWORD_CAP = 60

_Q_HEADER = re.compile(r"^##\s+Q(\d+)\s+(.+?)\s*$")


def _sections(text):
    """按二级标题切块，返回 [(header, [body lines]), ...]。"""
    out = []
    header = None
    body = []
    for line in text.splitlines():
        if line.startswith("## "):
            if header is not None:
                out.append((header, body))
            header = line
            body = []
        else:
            body.append(line)
    if header is not None:
        out.append((header, body))
    return out


def _block(body, start_marker, stop_markers):
    """截取从含 start_marker 的行（含该行剩余内联内容）到含任一 stop_marker 的行。"""
    lines = []
    started = False
    for line in body:
        s = line.strip()
        if not started:
            pos = line.find(start_marker)
            if pos >= 0:
                started = True
                rest = line[pos + len(start_marker):].lstrip("：: ")
                if rest:
                    lines.append(rest)
            continue
        if any(m in line for m in stop_markers):
            break
        lines.append(line)
    return [x for x in lines if x.strip()]


_REF_RE = re.compile(r"^[A-Za-z0-9_./-]+\.[A-Za-z0-9]+(?::\d[\d,\-]*)?$")


def _refs(block_lines):
    """逐条抽取反引号 token，保序去重；纯原文抽取，不改写、不编造。

    返回 (refs, quotes)：refs 是路径[冒号行号]形态的 file:line 引用，
    quotes 是其余代码/字段证据片段。判定只看 token 形状，不改写原文。
    """
    refs, quotes = [], []
    for line in block_lines:
        for tok in re.findall(r"`([^`]+)`", line):
            t = tok.strip()
            if not t:
                continue
            if _REF_RE.match(t):
                if t not in refs:
                    refs.append(t)
            elif t not in quotes:
                quotes.append(t)
    return refs, quotes


def keywords(short_text):
    """按 docstring 里的规则从短答原文抽关键词。"""
    out = []

    def add(x):
        x = x.strip()
        if not x or x in out or len(x) > 24:
            return
        if re.fullmatch(r"[\d.]+", x):
            return
        # 代码标识符里不会出现行内标点/粗体/换行；含这些的整串一律不是关键词。
        if re.search(r"[\n\r，。；：（）「」【】*]", x):
            return
        if len(x) == 1 and re.match(r"[\u4e00-\u9fff]", x):
            return
        out.append(x)

    # 代码围栏行先剥掉，否则 ``` 会与行内反引号错配，把散文吞成"关键词"。
    prose = re.sub(r"(?m)^\s*```.*$", "", short_text)
    for t in re.findall(r"`([^`]+)`", prose):
        add(t)
    for t in re.findall(r"[A-Za-z_][A-Za-z0-9_./:\-]{2,}", prose):
        add(t)
    # 连续汉字串：2..12 字整串；超 12 字按每 6 字滑窗切，避免整段废话变成一个关键词。
    for run in re.findall(r"[\u4e00-\u9fff]+", prose):
        if len(run) < 2:
            continue
        if len(run) <= 12:
            add(run)
        else:
            for i in range(0, len(run) - 5, 6):
                add(run[i:i + 6])
    return out[:KEYWORD_CAP]


def grade(answer, kws):
    """返回 (score, hits)。空答案 / 空关键词 -> (0, [])。"""
    a = (answer or "").strip()
    if not a or not kws:
        return 0, []
    low = a.lower()
    hits = [k for k in kws if k.lower() in low]
    score = round(100 * len(hits) / len(kws))
    return min(100, max(0, score)), hits


def load_questions(path=None):
    """读真实字节解析 INTERVIEW.md，返回 20 个题目 dict。"""
    p = Path(path) if path else INTERVIEW
    text = p.read_text(encoding="utf-8")
    qs = []
    for header, body in _sections(text):
        m = _Q_HEADER.match(header)
        if not m:
            continue
        num = int(m.group(1))
        title = m.group(2)
        short_lines = _block(body, "**短答**", ("**可核验**", "---", "## "))
        verify_lines = _block(body, "**可核验**", ("**复算**", "---", "## "))
        short_text = "\n".join(short_lines).strip()
        refs, quotes = _refs(verify_lines)
        qs.append({
            "num": num,
            "title": title,
            "short_lines": short_lines,
            "short_text": short_text,
            "verify_lines": verify_lines,
            "refs": refs,
            "quotes": quotes,
            "keywords": keywords(short_text),
        })
    if not qs:
        raise ValueError(
            "未从 %s 解析到任何题目（期望形如 '## Q1 标题' 的二级标题节）" % p)
    qs.sort(key=lambda q: q["num"])
    return qs


QUESTIONS = load_questions()


def get_question(n):
    """取第 n 题（1..20）；越界抛 ValueError。"""
    if not isinstance(n, int) or isinstance(n, bool):
        raise ValueError("题号必须是整数")
    if n < 1 or n > len(QUESTIONS):
        raise ValueError("题号越界: %s，有效范围 1..%d" % (n, len(QUESTIONS)))
    return QUESTIONS[n - 1]


def pick_numbers(seed=None):
    """随机抽 DRILL_SIZE 个不重复题号；给定 seed 时可复现。"""
    rng = random.Random(seed)
    pool = [q["num"] for q in QUESTIONS]
    return rng.sample(pool, min(DRILL_SIZE, len(pool)))


def show_list():
    for q in QUESTIONS:
        print("Q%d %s" % (q["num"], q["title"]))
    return 0


def show_ask(n):
    q = get_question(n)
    print("Q%d %s" % (q["num"], q["title"]))
    print()
    print("[短答（原文抽取）]")
    for line in q["short_lines"]:
        print(line)
    print()
    print("[可核验 file:line（原文抽取，共 %d 条）]" % len(q["refs"]))
    for r in q["refs"]:
        print("- `%s`" % r)
    print()
    print("[可核验证据片段（原文抽取，共 %d 条）]" % len(q["quotes"]))
    for r in q["quotes"]:
        print("- `%s`" % r)
    print()
    print("[关键词（评分规则见 --help，共 %d 个）]" % len(q["keywords"]))
    print(" ".join(q["keywords"]))
    return 0


def _read_answer(stream):
    """读一题的口答：读到空行或 EOF 为止，多行用空格拼接成一条答案。

    这样多行口答不会被截成一行、也不会把后续行误当成下一题的答案。
    空行本身表示「本题作答结束」，直接空行 = 弃答（空答案 -> 0 分）。
    """
    parts = []
    while True:
        line = stream.readline()
        if line is None:
            break
        s = line.strip("\r\n")
        if not s.strip():
            break
        parts.append(s.strip())
    return " ".join(parts).strip()


def drill(seed=None, stdin=None, stdout=None):
    stream = stdin if stdin is not None else sys.stdin
    write = stdout.write if stdout is not None else sys.stdout.write
    nums = pick_numbers(seed)
    total = 0
    results = []
    write("说明：每题答案可多行，输入空行提交；直接回车（空行）= 弃答。\n\n")
    for i, num in enumerate(nums, 1):
        q = get_question(num)
        write("[%d/%d] Q%d %s\n" % (i, len(nums), num, q["title"]))
        write("> ")
        if stdout is None:
            sys.stdout.flush()
        answer = _read_answer(stream)
        score, hits = grade(answer, q["keywords"])
        total += score
        results.append((num, score, hits))
        write("本题得分：%d/100\n\n" % score)
    write("总分：%d/%d\n" % (total, len(nums) * 100))
    write("每题命中词：\n")
    for num, _score, hits in results:
        write("  Q%d: %s\n" % (num, hits))
    return total


def selftest():
    checks = []

    ok = True
    for bad in (0, 21):
        try:
            get_question(bad)
            ok = False
        except ValueError:
            pass
    checks.append(("RED", "越界题号必须报错", ok))

    q = get_question(1)
    sc, hits = grade("", q["keywords"])
    sc2, hits2 = grade("   ", q["keywords"])
    checks.append(("RED", "空答案必须 0 分", sc == 0 and not hits and sc2 == 0 and not hits2))

    sc3, _ = grade(q["short_text"], q["keywords"])
    checks.append(("GREEN", "正常出题 0<score<=100", 0 < sc3 <= 100))

    a, b = pick_numbers(7), pick_numbers(7)
    checks.append(("GREEN", "seed 固定可复现", a == b and len(a) == DRILL_SIZE))

    for tag, name, passed in checks:
        print("%-5s %s ... %s" % (tag, name, "PASS" if passed else "FAIL"))
    good = sum(1 for _t, _n, p in checks if p)
    print("SELFTEST %d/%d PASS" % (good, len(checks)))
    return 0 if good == len(checks) else 1


def main(argv=None):
    p = argparse.ArgumentParser(
        description="superagent-build 面试模拟练习器：读 docs/INTERVIEW.md 的 20 问，可列题、看答案、模拟口答打分。"
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="打印 20 个题号与标题")
    g.add_argument("--ask", type=int, metavar="N", help="打印第 N 题短答要点 + 可核验 file:line 引用")
    g.add_argument("--drill", action="store_true", help="随机抽 5 题，逐题等 stdin 口答并按关键词覆盖给分")
    g.add_argument("--selftest", action="store_true", help="纯 stdlib 离线自检：2 RED + 2 GREEN")
    p.add_argument("--seed", type=int, default=None, help="--drill 的随机种子（固定即可复现）")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.list:
        return show_list()
    if args.ask is not None:
        try:
            return show_ask(args.ask)
        except ValueError as exc:
            print("ERROR: %s" % exc, file=sys.stderr)
            return 2
    if args.drill:
        drill(args.seed)
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
