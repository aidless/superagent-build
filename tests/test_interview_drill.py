"""service/interview_drill.py 的行为测试：每条都先证明「违规输入必须失败」再证明「正常输入成立」。

RED 侧（注入违规输入必须报错 / 必须 0 分，不许静默通过）：
  - 题号越界、题号类型不对 -> ValueError / 进程非 0 退出；
  - 空答案、纯空白答案 -> 0 分且命中词为空；
  - 没有题目的文档 -> load_questions 必须报错，不许返回空表糊弄过去。
GREEN 侧（正常输入下的契约）：
  - 真实 INTERVIEW.md 解析出 1..20 共 20 题，每题都有 file:line 引用与关键词；
  - 引用/证据片段逐字来自原文（不编造）；
  - 标准短答得 100 分；--drill 同 seed 可复现；多行答案不被截断。
纯标准库 + pytest，不依赖网络。
"""

import importlib.util
import io
import subprocess
import sys
from pathlib import Path

import pytest

def _resolve_root():
    """从本文件向上找到含 service/interview_drill.py 的仓库根，避免依赖相对层级。"""
    here = Path(__file__).resolve()
    for cand in [here, *here.parents]:
        if (cand / "service" / "interview_drill.py").is_file():
            return cand
    raise RuntimeError("找不到仓库根目录（service/interview_drill.py 不在任何父目录里）")


ROOT = _resolve_root()
SCRIPT = ROOT / "service" / "interview_drill.py"
INTERVIEW = ROOT / "docs" / "INTERVIEW.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("interview_drill_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def ivd():
    return _load_module()


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------- RED -> GREEN


def test_question_lookup_red_out_of_range_then_green_ok(ivd):
    """RED：越界 / 类型不对的题号必须报错；GREEN：合法题号返回完整题目。"""
    for bad in (0, -1, 21, 999):
        with pytest.raises(ValueError):
            ivd.get_question(bad)
    for bad in ("1", 1.0, True, None):
        with pytest.raises(ValueError):
            ivd.get_question(bad)

    q = ivd.get_question(1)
    assert q["num"] == 1
    assert q["title"].strip()
    assert q["short_text"].strip()
    assert q["keywords"]
    assert q["refs"]


def test_grading_red_empty_then_green_full(ivd):
    """RED：空答案 / 纯空白答案必须 0 分、命中词空表；GREEN：标准短答拿满分。"""
    for q in ivd.QUESTIONS:
        assert ivd.grade("", q["keywords"]) == (0, [])
        assert ivd.grade("   \n\t  ", q["keywords"]) == (0, [])
        assert ivd.grade("完全不相干的答案", q["keywords"])[0] == 0

        score, hits = ivd.grade(q["short_text"], q["keywords"])
        assert score == 100
        assert len(hits) == len(q["keywords"])


def test_load_questions_red_on_doc_without_questions(tmp_path, ivd):
    """RED：没有题目的文档必须报错；GREEN：真实文档解析出 1..20。"""
    bogus = tmp_path / "bogus.md"
    bogus.write_text("# 这里没有题目\n\n只有一段散文。\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ivd.load_questions(bogus)

    qs = ivd.load_questions(INTERVIEW)
    assert [q["num"] for q in qs] == list(range(1, 21))


def test_cli_red_exit_nonzero_then_green_exit_zero():
    """RED：--ask 越界必须非 0 退出并在 stderr 报错；GREEN：--ask/--list 正常退出。"""
    for bad in ("0", "21", "-3"):
        r = _run_cli("--ask", bad)
        assert r.returncode != 0
        assert "ERROR" in r.stderr

    r = _run_cli("--ask", "1")
    assert r.returncode == 0
    assert "Q1" in r.stdout

    r = _run_cli("--list")
    assert r.returncode == 0
    listed = [ln for ln in r.stdout.splitlines() if ln.startswith("Q")]
    assert len(listed) == 20


# ---------------------------------------------------------------- GREEN 契约


def test_refs_and_quotes_are_verbatim_from_source(ivd):
    """引用与证据片段必须逐字来自 INTERVIEW.md，不得自己编。"""
    raw = INTERVIEW.read_text(encoding="utf-8")
    for q in ivd.QUESTIONS:
        assert q["refs"], "Q%d 缺少 file:line 引用" % q["num"]
        for ref in q["refs"]:
            assert ref in raw, "Q%d 的引用不在原文里: %s" % (q["num"], ref)
        for quote in q["quotes"]:
            assert quote in raw, "Q%d 的证据片段不在原文里: %s" % (q["num"], quote)


def test_pick_numbers_seed_reproducible_and_distinct(ivd):
    """同 seed 抽题序列可复现、题号不重复；换 seed 结果应不同。"""
    a = ivd.pick_numbers(7)
    b = ivd.pick_numbers(7)
    assert a == b
    assert len(a) == ivd.DRILL_SIZE
    assert len(set(a)) == len(a)
    assert all(1 <= n <= len(ivd.QUESTIONS) for n in a)
    assert ivd.pick_numbers(8) != a


def test_drill_multiline_answer_not_truncated(ivd):
    """多行口答必须整段参与评分（空行分隔），不被截成首行。"""
    nums = ivd.pick_numbers(7)
    blocks = ["\n".join(ivd.get_question(n)["short_lines"]) for n in nums]
    payload = "\n\n".join(blocks) + "\n\n"

    out = io.StringIO()
    total = ivd.drill(seed=7, stdin=io.StringIO(payload), stdout=out)
    text = out.getvalue()
    assert "总分：500/500" in text
    assert total == 500
    # 每一题都真的被答满，且题目顺序与抽题一致
    for i, n in enumerate(nums, 1):
        assert "[%d/5] Q%d" % (i, n) in text


def test_drill_red_first_line_only_scores_below_full(ivd):
    """RED：只喂每段首行（等价于旧的一行读取），总分必须低于满分。"""
    nums = ivd.pick_numbers(7)
    first_lines = [ivd.get_question(n)["short_lines"][0] for n in nums]
    payload = "\n\n".join(first_lines) + "\n\n"

    out = io.StringIO()
    total = ivd.drill(seed=7, stdin=io.StringIO(payload), stdout=out)
    assert 0 < total < 500


def test_selftest_cli_passes():
    """--selftest 必须自跑 2 RED + 2 GREEN 并整体通过。"""
    r = _run_cli("--selftest")
    assert r.returncode == 0
    assert "RED" in r.stdout and "GREEN" in r.stdout
    assert "SELFTEST 4/4 PASS" in r.stdout
