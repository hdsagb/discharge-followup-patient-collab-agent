"""审计留痕库：fail-closed 写入 + 哈希链 + append-only 校验。

借鉴 docs/借鉴清单.md A2 / A3 的语义（本项目自实现，未复制上游代码）：
- 审计写不进去 → 业务整体回滚（写入必须发生在调用方事务内、提交前）。
- 每条事件带 prev_hash / entry_hash，日结脚本可重放校验；篡改任意一行即链断。
- DB 层 BEFORE UPDATE/DELETE 触发器拒绝改删（见 db.py SCHEMA）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from typing import Any

GENESIS = "GENESIS"
EVENT_TYPES = ("operation", "escalation", "authorization")


class AuditWriteError(RuntimeError):
    """审计事件无法写入时抛出；调用方事务随之回滚，业务不得落库。"""


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def entry_hash(secret: bytes, prev_hash: str, seq: int, payload: dict[str, Any]) -> str:
    body = f"{prev_hash}|{seq}|{_canonical(payload)}"
    return hmac.new(secret, body.encode("utf-8"), hashlib.sha256).hexdigest()


def write(
    conn: sqlite3.Connection,
    secret: bytes,
    *,
    event_type: str,
    action: str,
    actor_id: str,
    subject: str = "",
    batch_version: str = "",
    outcome: str = "success",
    detail: dict[str, Any] | None = None,
    at: str,
) -> int:
    if event_type not in EVENT_TYPES:
        raise AuditWriteError(f"未知审计类型: {event_type}")
    try:
        last = conn.execute(
            "SELECT seq, entry_hash FROM audit_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = last["entry_hash"] if last else GENESIS
        seq = (last["seq"] + 1) if last else 1
        payload = {
            "seq": seq, "type": event_type, "action": action, "actor": actor_id,
            "subject": subject, "batch": batch_version, "outcome": outcome,
            "detail": detail or {}, "at": at,
        }
        digest = entry_hash(secret, prev_hash, seq, payload)
        cur = conn.execute(
            "INSERT INTO audit_events(seq,event_type,action,actor_id,subject,batch_version,"
            "outcome,detail_json,at,prev_hash,entry_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (seq, event_type, action, actor_id, subject, batch_version, outcome,
             _canonical(payload["detail"]), at, prev_hash, digest),
        )
        return int(cur.lastrowid)
    except sqlite3.Error as exc:  # 审计失败必须让整个事务失败
        raise AuditWriteError(str(exc)) from exc


def verify_chain(conn: sqlite3.Connection, secret: bytes) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM audit_events ORDER BY seq ASC").fetchall()
    prev = GENESIS
    for row in rows:
        payload = {
            "seq": row["seq"], "type": row["event_type"], "action": row["action"],
            "actor": row["actor_id"], "subject": row["subject"],
            "batch": row["batch_version"], "outcome": row["outcome"],
            "detail": json.loads(row["detail_json"]), "at": row["at"],
        }
        expect = entry_hash(secret, prev, row["seq"], payload)
        if not hmac.compare_digest(expect, row["entry_hash"]) or row["prev_hash"] != prev:
            return {"ok": False, "broken_at_seq": row["seq"], "count": len(rows)}
        prev = row["entry_hash"]
    return {"ok": True, "count": len(rows), "head_hash": prev}


def coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    """审计覆盖率：关键动作是否都产生了留痕（借鉴 B3 的评估项思路）。"""
    counts = {
        r["event_type"]: r["n"]
        for r in conn.execute(
            "SELECT event_type, COUNT(*) AS n FROM audit_events GROUP BY event_type"
        )
    }
    total_records = conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"]
    total_tickets = conn.execute("SELECT COUNT(*) AS n FROM tickets").fetchone()["n"]
    confirmed_audited = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action='record.confirm'"
    ).fetchone()["n"]
    tickets_audited = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE action='ticket.create'"
    ).fetchone()["n"]
    return {
        "by_type": counts,
        "records": total_records,
        "records_audited": confirmed_audited,
        "tickets": total_tickets,
        "tickets_audited": tickets_audited,
        "coverage_ok": confirmed_audited >= total_records and tickets_audited >= total_tickets,
    }
