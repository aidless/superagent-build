#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""governance —— 治理内嵌模块（gov.py 的内嵌库版，被执行层直接 import）。

把工作区治理纪律内嵌进 agent 执行层，三件事（对齐 superagent-dig/03-governance-tool/gov.py）：

1. 写作用域声明与校验：worker 开工前 claim 声明允许写的路径 pattern，写前 check；
2. 冲突检测：两个活动 ticket 的写作用域重叠 → conflicts() 报警（advisory，不是锁）；
3. 审阅留痕：判决/裁决追加进 append-only JSONL（flock + O_APPEND 单次写，永不整文件重写）。

两种状态模式：
- state_dir=None → 纯内存态（测试/一次性编排用）；
- 给了 state_dir → 落 state_dir/scopes.json + state_dir/rulings.jsonl（跨实例持久）。

路径匹配规则（逐字对齐 gov.py 的 _matches）：
- pattern 以 `/**` 结尾 = 前缀树匹配（`src/**` 匹配 src 下一切）；
- 否则精确匹配或目录前缀匹配（pattern `docs` 匹配 `docs/a.md`，不匹配 `docsx/a.md`）。

纯 stdlib，无第三方依赖，无网络请求，无密钥。
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CheckResult:
    verdict: str  # "ALLOW" | "BLOCK"
    reason: str


def _matches(path: str, pattern: str) -> bool:
    """对齐 gov.py：pattern 以 /** 结尾 = 前缀树匹配；否则精确/目录前缀匹配。"""
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    return path == pattern or path.startswith(pattern.rstrip("/") + "/")


def _overlaps(pattern_a: str, pattern_b: str) -> bool:
    """两个作用域 pattern 重叠 = 任意一方能覆盖另一方（把 pattern 当路径交叉匹配）。"""
    return _matches(pattern_a, pattern_b) or _matches(pattern_b, pattern_a)


class ScopeRegistry:
    """写作用域登记表 + 冲突检测 + append-only 判决流。"""

    def __init__(self, state_dir: str | None = None):
        self.state_dir = Path(state_dir) if state_dir else None
        # 内存态登记表：ticket_id -> {"scopes": [...], "active": bool, "claimed_at": ...}
        self._scopes: dict = {}
        # 内存态判决流（有 state_dir 时不走这里，直接落 rulings.jsonl）
        self._rulings: list[dict] = []
        if self.state_dir:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._scopes = self._load_scopes()

    # ---- 登记表落盘（读-改-写全程持 flock，防并发互踩；内存态是 no-op） ----

    def _scopes_path(self) -> Path:
        return self.state_dir / "scopes.json"

    def _rulings_path(self) -> Path:
        return self.state_dir / "rulings.jsonl"

    def _load_scopes(self) -> dict:
        if not self._scopes_path().exists():
            return {}
        return json.loads(self._scopes_path().read_text(encoding="utf-8"))

    def _save_scopes(self) -> None:
        if not self.state_dir:
            return
        data = json.dumps(self._scopes, ensure_ascii=False, indent=2)
        with open(self._scopes_path(), "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.write(data)
            fcntl.flock(f, fcntl.LOCK_UN)

    # ---- 对外 API（编排层按此调用） ----

    def claim(self, ticket_id: str, patterns: list[str]) -> None:
        """worker 开工前声明写作用域。同 ticket 重复 claim = 覆盖。"""
        self._scopes[ticket_id] = {
            "scopes": sorted(set(patterns)),
            "active": True,
            "claimed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        self._save_scopes()

    def check(self, ticket_id: str, path: str) -> CheckResult:
        """写前校验：未声明即 BLOCK；声明了且路径命中即 ALLOW。"""
        entry = self._scopes.get(ticket_id)
        if not entry or not entry.get("active"):
            return CheckResult(
                "BLOCK", f"ticket {ticket_id} 无活动作用域声明——没有声明就不许写")
        for pattern in entry["scopes"]:
            if _matches(path, pattern):
                return CheckResult(
                    "ALLOW", f"{path} ∈ {ticket_id} 声明的作用域 '{pattern}'")
        return CheckResult(
            "BLOCK", f"{path} 不在 {ticket_id} 声明的作用域 {entry['scopes']} 内")

    def release(self, ticket_id: str) -> None:
        """释放 ticket 的写作用域（保留记录供审计，active 置 False）。"""
        entry = self._scopes.get(ticket_id)
        if entry:
            entry["active"] = False
            entry["released_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            self._save_scopes()

    def conflicts(self) -> list[tuple[str, str, list[str]]]:
        """返回 [(ticket_a, ticket_b, overlapping_patterns)]——两 ticket 模式重叠即报警。"""
        active = {k: v for k, v in self._scopes.items() if v.get("active")}
        ids = sorted(active)
        out: list[tuple[str, str, list[str]]] = []
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                overlap = sorted(
                    {pa for pa in active[a]["scopes"]
                     if any(_overlaps(pa, pb) for pb in active[b]["scopes"])}
                    | {pb for pb in active[b]["scopes"]
                       if any(_overlaps(pa, pb) for pa in active[a]["scopes"])})
                if overlap:
                    out.append((a, b, overlap))
        return out

    def ruling(self, verdict: dict) -> None:
        """append-only 判决流：flock + O_APPEND 单次写（对齐 gov.py 的安全写法）。"""
        record = dict(verdict)
        record.setdefault("at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        line = json.dumps(record, ensure_ascii=False)
        if self.state_dir:
            fd = os.open(self._rulings_path(),
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                os.write(fd, (line + "\n").encode("utf-8"))
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        else:
            self._rulings.append(record)

    def read_rulings(self) -> list[dict]:
        """读回判决流（内存态返回副本；落盘态逐行解析 rulings.jsonl）。"""
        if self.state_dir:
            if not self._rulings_path().exists():
                return []
            text = self._rulings_path().read_text(encoding="utf-8")
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        return [dict(r) for r in self._rulings]

    # ---- 内嵌自检 ----

    def selftest(self) -> int:
        """内嵌自检入口：RED→GREEN，返回 0=全过 / 1=有失败。可独立运行。"""
        failures: list[str] = []

        def check(name, cond):
            print(("PASS" if cond else "FAIL"), name)
            if not cond:
                failures.append(name)

        # ---- 内存态 ----
        reg = ScopeRegistry()
        reg.claim("T1", ["src/a/**", "docs"])
        # RED1: 未声明 ticket 写 → BLOCK
        check("RED1 未声明 ticket 写被拦",
              reg.check("T2", "src/a/foo.ts").verdict == "BLOCK")
        # RED2: 越界写 → BLOCK（pattern "docs" 不命中 "docsx/a.md"）
        check("RED2 越界写被拦",
              reg.check("T1", "src/b/leak.ts").verdict == "BLOCK")
        check("RED3 目录前缀边界被拦",
              reg.check("T1", "docsx/a.md").verdict == "BLOCK")
        # GREEN1-3: 声明范围内放行
        check("GREEN1 范围内精确路径放行",
              reg.check("T1", "src/a/foo.ts").verdict == "ALLOW")
        check("GREEN2 ** 前缀树放行",
              reg.check("T1", "src/a/sub/x.ts").verdict == "ALLOW")
        check("GREEN3 目录前缀放行",
              reg.check("T1", "docs/a.md").verdict == "ALLOW")
        # RED4: 重叠报警 / GREEN4: 无重叠不报警
        reg.claim("T2", ["src/a/sub/**"])
        check("RED4 重叠报警", reg.conflicts() != [])
        reg.release("T2")
        reg.claim("T2", ["src/c/**"])
        check("GREEN4 无重叠不报警", reg.conflicts() == [])
        # GREEN5: 判决流 append-only 且可读回
        reg.ruling({"id": "R1", "verdict": "supported", "why": "证据齐全"})
        reg.ruling({"id": "R2", "verdict": "needs-work", "why": "缺 file:line 引用"})
        rulings = reg.read_rulings()
        check("GREEN5 判决流可读回",
              len(rulings) == 2 and rulings[0]["id"] == "R1"
              and rulings[1]["id"] == "R2")

        # ---- 落盘态（tempfile）----
        tmp = tempfile.mkdtemp()
        try:
            reg_a = ScopeRegistry(state_dir=tmp)
            reg_a.claim("T1", ["src/a/**"])
            reg_a.ruling({"id": "R1", "verdict": "supported", "why": "证据齐全"})
            before = self._read_bytes(tmp, "rulings.jsonl")
            # RED5: 持久化后未声明 ticket 被拦
            reg_b = ScopeRegistry(state_dir=tmp)
            check("RED5 持久化后未声明被拦",
                  reg_b.check("T9", "src/a/foo.ts").verdict == "BLOCK")
            # GREEN6: 持久化后范围内放行
            check("GREEN6 持久化后范围内放行",
                  reg_b.check("T1", "src/a/foo.ts").verdict == "ALLOW")
            # GREEN7: append-only——追加后旧字节前缀逐字不变
            reg_b.ruling({"id": "R2", "verdict": "needs-work", "why": "缺引用"})
            after = self._read_bytes(tmp, "rulings.jsonl")
            check("GREEN7 append-only 旧条目不可变",
                  after.startswith(before) and len(after) > len(before))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        print("GOV SELFTEST: " + ("PASS" if not failures else f"FAIL × {len(failures)}"))
        return 0 if not failures else 1

    @staticmethod
    def _read_bytes(state_dir: str, name: str) -> bytes:
        return (Path(state_dir) / name).read_bytes()


if __name__ == "__main__":
    sys_exit_code = ScopeRegistry().selftest()
    raise SystemExit(sys_exit_code)
