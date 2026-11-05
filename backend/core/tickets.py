"""红旗升级工单：只建单/处置，不写登记表（医疗-009）。

借鉴 docs/借鉴清单.md A9 的幂等与三态决策；修掉上游三处缺陷：
resolved_by 只从服务端会话取值、决策必须有 step-up 凭证、函数不自行 commit。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import audit, security
from .db import now_iso, transaction

DECISIONS = ("approved", "rejected", "continued")


def list_tickets(conn: sqlite3.Connection, limit: int = 60) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT t.*, p.inpatient_no, p.discharge_time, p.procedure_code, p.department, "
        "s.schedule_date, s.slot, u.name AS resolved_name "
        "FROM tickets t JOIN patients p ON p.id=t.patient_id "
        "JOIN schedules s ON s.id=t.schedule_id "
        "LEFT JOIN users u ON u.user_id=t.resolved_by "
        "ORDER BY CASE t.status WHEN 'pending' THEN 0 ELSE 1 END, t.id DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


def resolve(conn: sqlite3.Connection, ticket_id: int, decision: str, note: str, token: str,
            actor: dict[str, Any], at: str | None = None) -> dict[str, Any]:
    if actor["role"] != "doctor":
        raise PermissionError("仅专科医生可处置红旗工单")
    if decision not in DECISIONS:
        raise ValueError("处置结论仅支持 approved / rejected / continued")
    ticket = conn.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if ticket is None:
        raise LookupError("工单不存在")
    if ticket["status"] != "pending":
        raise ValueError("工单已处置")

    digest = security.canonical_digest({"ticket_id": ticket_id, "decision": decision,
                                       "note": note or ""})
    security.verify_stepup(conn, token, actor, "ticket_resolve", str(ticket_id), digest)
    stamp = at or now_iso()

    with transaction(conn):
        conn.execute(
            "UPDATE tickets SET status=?, resolved_by=?, resolved_at=?, resolution=? WHERE id=?",
            (decision, actor["user_id"], stamp, note or "", ticket_id))
        audit.write(conn, security.secret(), event_type="escalation",
                    action="ticket.resolve", actor_id=actor["user_id"],
                    subject=str(ticket_id), at=stamp,
                    detail={"decision": decision, "rule_id": ticket["rule_id"],
                            "channel": ticket["channel"], "note": note or "",
                            "resolved_by": actor["user_id"],
                            "wrote_registry": False})
    return {"ticket_id": ticket_id, "status": decision, "resolved_by": actor["user_id"],
            "note": "工单已处置；登记表仍须护士确认后由写库服务落库"}
