"""持久层：单一 SQLite 库，WAL + synchronous=FULL。

对齐原型 src/app/store.ts:64 的可靠性三件套：
- 域聚合全序列化进 kv 表（Engine 是唯一写者）；
- receipts 表：先存后做的幂等锚点（outbox.py 消费）；
- journal_mode=WAL + synchronous=FULL：崩溃不丢已确认事实。

不用 ORM、不搞迁移框架——kv + receipts 两张表够用（karpathy 纪律）。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional


class Store:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=FULL;")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS receipts ("
            " receipt_id TEXT PRIMARY KEY, payload TEXT, created_at REAL)")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS outbox ("
            " message_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL,"
            " channel TEXT NOT NULL, body TEXT NOT NULL, attempts INTEGER DEFAULT 0,"
            " next_try_at REAL DEFAULT 0, state TEXT DEFAULT 'pending')")
        self.conn.commit()

    # ---------- kv ----------
    def put(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO kv(k,v) VALUES(?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value, ensure_ascii=False)))
        self.conn.commit()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return json.loads(row["v"]) if row else default

    def keys(self, prefix: str) -> list[str]:
        """按前缀列 kv 键（崩溃回灌用：thread:* / ticket:*）。"""
        rows = self.conn.execute(
            "SELECT k FROM kv WHERE k LIKE ? ORDER BY k", (prefix + "%",)).fetchall()
        return [r["k"] for r in rows]

    def delete(self, key: str) -> None:
        self.conn.execute("DELETE FROM kv WHERE k=?", (key,))
        self.conn.commit()

    # ---------- receipts（先存后做） ----------
    def record_receipt(self, receipt_id: str, payload: str = "") -> bool:
        """幂等登记。返回 True=首次登记；False=重复（调用方跳过副作用）。"""
        try:
            self.conn.execute(
                "INSERT INTO receipts(receipt_id,payload,created_at) VALUES(?,?,strftime('%s','now'))",
                (receipt_id, payload))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def has_receipt(self, receipt_id: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM receipts WHERE receipt_id=?", (receipt_id,)).fetchone() is not None

    # ---------- outbox（效果队列） ----------
    def enqueue(self, message_id: str, receipt_id: str, channel: str, body: str) -> bool:
        """效果入队。要求 receipt 已存在——先存后做，否则拒绝。"""
        if not self.has_receipt(receipt_id):
            raise ValueError(f"enqueue before receipt: {message_id} needs receipt {receipt_id}")
        try:
            self.conn.execute(
                "INSERT INTO outbox(message_id,receipt_id,channel,body) VALUES(?,?,?,?)",
                (message_id, receipt_id, channel, body))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False  # 重复消息幂等跳过

    def due(self, now: float, limit: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT message_id,receipt_id,channel,body,attempts,next_try_at,state "
            "FROM outbox WHERE state='pending' AND next_try_at<=? ORDER BY rowid LIMIT ?",
            (now, limit)).fetchall()
        return [dict(r) for r in rows]

    def mark_sent(self, message_id: str) -> None:
        self.conn.execute("UPDATE outbox SET state='sent' WHERE message_id=?", (message_id,))
        self.conn.commit()

    def mark_retry(self, message_id: str, next_try_at: float) -> None:
        """退避重试。5s→300s 封顶，由调用方算 next_try_at。"""
        self.conn.execute(
            "UPDATE outbox SET attempts=attempts+1, next_try_at=? WHERE message_id=?",
            (next_try_at, message_id))
        self.conn.commit()

    def abandon(self, message_id: str, reason: str) -> None:
        """唯二合法放弃：channelGone / channelIgnored。其他原因调用方必须继续重试。"""
        if reason not in ("channelGone", "channelIgnored"):
            raise ValueError(f"illegal abandon reason: {reason}")
        self.conn.execute(
            "UPDATE outbox SET state='abandoned:'||? WHERE message_id=?", (reason, message_id))
        self.conn.commit()

    # ---------- 崩溃恢复 ----------
    def pending_outbox_count(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) c FROM outbox WHERE state='pending'").fetchone()["c"]
