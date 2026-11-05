"""触达工作台：外呼辅助、短信、号码失效、三次未接搁置、授权撤回。

硬约束（医疗-013/014/017）：非“已授权”一律不启动外呼；未告知授权即不录音；
短信发送不计入完成率。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import audit, security
from .db import now_iso, transaction

MAX_ATTEMPTS = 3


def _load(conn: sqlite3.Connection, schedule_id: int):
    row = conn.execute(
        "SELECT s.*, p.id AS pid, p.inpatient_no, p.discharge_time, p.contact_type, "
        "p.auth_status, p.department FROM schedules s JOIN patients p ON p.id=s.patient_id "
        "WHERE s.id=?", (schedule_id,)).fetchone()
    if row is None:
        raise LookupError("排程不存在")
    return row


def start_call(conn: sqlite3.Connection, schedule_id: int, actor: dict[str, Any],
               consent: bool, at: str | None = None) -> dict[str, Any]:
    """启动外呼：授权前置校验 → 录音告知 → 记录 attempts。"""
    row = _load(conn, schedule_id)
    stamp = at or now_iso()
    if row["auth_status"] != "已授权":
        with transaction(conn):
            audit.write(conn, security.secret(), event_type="authorization",
                        action="call.denied_unauthorized", actor_id=actor["user_id"],
                        subject=f'{row["inpatient_no"]}|{row["discharge_time"]}',
                        at=stamp, outcome="denied",
                        detail={"reason": f"授权状态={row['auth_status']}"})
        # 越权拒绝并告警（医疗-013）
        raise PermissionError(f"授权状态为「{row['auth_status']}」，拒绝外呼与录音")

    with transaction(conn):
        conn.execute(
            "UPDATE schedules SET status='in_progress' WHERE id=? AND status IN "
            "('pending','in_progress')", (schedule_id,))
        conn.execute(
            "INSERT INTO recordings(schedule_id,consent,status,at) VALUES(?,?,?,?)",
            (schedule_id, 1 if consent else 0,
             "active" if consent else "not_recorded", stamp))
        audit.write(conn, security.secret(), event_type="authorization",
                    action="call.start", actor_id=actor["user_id"],
                    subject=f'{row["inpatient_no"]}|{row["discharge_time"]}', at=stamp,
                    detail={"consent": consent,
                            "recording": "active" if consent else "not_recorded",
                            "notice": "通话前已播报录音告知语" if consent else "未告知授权，不录音"})
    return {"recording": "active" if consent else "not_recorded",
            "consent": consent, "message": "外呼已启动"}


def record_attempt(conn: sqlite3.Connection, schedule_id: int, channel: str, result: str,
                   actor: dict[str, Any], at: str | None = None) -> dict[str, Any]:
    if channel not in ("call", "sms"):
        raise ValueError("未知触达渠道")
    row = _load(conn, schedule_id)
    stamp = at or now_iso()
    with transaction(conn):
        conn.execute(
            "INSERT INTO contact_attempts(schedule_id,channel,result,actor_id,at) "
            "VALUES(?,?,?,?,?)", (schedule_id, channel, result, actor["user_id"], stamp))
        attempts = row["attempts"]
        status = row["status"]
        if channel == "call":
            if result == "answered":
                status = "in_progress"
            else:                      # no_answer / invalid_number / busy
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    status = "shelved"
            conn.execute("UPDATE schedules SET attempts=?, status=? WHERE id=?",
                         (attempts, status, schedule_id))
        audit.write(conn, security.secret(), event_type="operation",
                    action="contact.attempt", actor_id=actor["user_id"],
                    subject=f'{row["inpatient_no"]}|{row["discharge_time"]}', at=stamp,
                    detail={"channel": channel, "result": result, "attempts": attempts,
                            "status": status,
                            "counts_toward_completion": channel == "call"})
    return {"attempts": attempts, "status": status,
            "counts_toward_completion": channel == "call",
            "message": "已搁置并进入人工跟进清单" if status == "shelved" else "已记录"}


def revoke(conn: sqlite3.Connection, patient_id: int, actor: dict[str, Any],
           reason: str = "患者电话撤回授权") -> dict[str, Any]:
    """撤回授权：24 小时内停服、停录音，历史录音标记“不得用于训练”。"""
    patient = conn.execute("SELECT * FROM patients WHERE id=?", (patient_id,)).fetchone()
    if patient is None:
        raise LookupError("患者不存在")
    stamp = now_iso()
    with transaction(conn):
        conn.execute("UPDATE patients SET auth_status='撤回' WHERE id=?", (patient_id,))
        cur = conn.execute(
            "UPDATE schedules SET status='shelved' WHERE patient_id=? AND status IN "
            "('pending','in_progress')", (patient_id,))
        conn.execute(
            "UPDATE recordings SET status='revoked_do_not_train' WHERE schedule_id IN "
            "(SELECT id FROM schedules WHERE patient_id=?)", (patient_id,))
        audit.write(conn, security.secret(), event_type="authorization",
                    action="auth.revoke", actor_id=actor["user_id"],
                    subject=f"{patient['inpatient_no']}|{patient['discharge_time']}",
                    batch_version=patient["batch_version"], at=stamp,
                    detail={"reason": reason, "schedules_stopped": cur.rowcount,
                            "recordings_marked": "revoked_do_not_train"})
    return {"ok": True, "stopped_schedules": cur.rowcount,
            "recordings_marked": "不得用于训练"}


def revoke_by_schedule(conn: sqlite3.Connection, schedule_id: int, actor: dict[str, Any],
                       reason: str = "") -> dict[str, Any]:
    row = _load(conn, schedule_id)
    return revoke(conn, row["pid"], actor, reason or "患者撤回授权")
