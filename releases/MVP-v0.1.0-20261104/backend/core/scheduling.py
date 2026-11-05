"""排程引擎（确定性规则）。

规则：模板版本过滤（医疗-008）→ 授权过滤（医疗-013/014）→ 晚间时段优先（医疗-016）
→ 幂等去重（住院号+出院时间+日期）→ 三次未接自动搁置（医疗-004）。
写入失败一律转人工清单，不降级为通用问卷。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from . import audit, security
from .db import now_iso, transaction

STALE_AFTER_HOURS = 24


def _active_batch(conn: sqlite3.Connection):
    return conn.execute(
        "SELECT * FROM batches WHERE status IN ('active','pending_backfill') "
        "ORDER BY imported_at DESC LIMIT 1").fetchone()


def _stale(batch) -> bool:
    try:
        exported = datetime.fromisoformat(batch["exported_at"])
    except ValueError:
        return True
    return datetime.now() - exported > timedelta(hours=STALE_AFTER_HOURS)


def run(conn: sqlite3.Connection, date: str, actor: dict[str, Any],
        nurses: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    if not nurses:
        nurses = [dict(r) for r in conn.execute(
            "SELECT * FROM users WHERE role='nurse' AND active=1 ORDER BY user_id")]
    if not nurses:
        raise ValueError("没有可用随访护士")

    batch = _active_batch(conn)
    stats = {"date": date, "created": 0, "skipped_duplicate": 0,
             "excluded_unauthorized": 0, "manual": 0, "stale_manual": 0,
             "batch_version": "", "rule": "模板签发 → 授权 → 晚间优先 → 幂等"}

    with transaction(conn):
        if batch is None:
            raise ValueError("尚无名单批次，无法生成排程")
        stats["batch_version"] = batch["batch_version"]

        if _stale(batch):
            # 名单超 24 小时未补：全部转人工，不再自动外呼
            for p in conn.execute(
                    "SELECT id FROM patients WHERE batch_version=? AND manual_reason=''",
                    (batch["batch_version"],)):
                conn.execute("UPDATE patients SET manual_reason=? WHERE id=?",
                             ("名单超 24 小时未补齐，转人工", p["id"]))
                stats["stale_manual"] += 1
            audit.write(conn, security.secret(), event_type="operation",
                        action="schedule.stale_fallback", actor_id=actor["user_id"],
                        subject=batch["batch_version"], batch_version=batch["batch_version"],
                        at=now_iso(), detail={"manual": stats["stale_manual"]})
            stats["manual"] = stats["stale_manual"]
            return stats

        rows = conn.execute(
            "SELECT p.*, t.status AS t_status, t.version AS t_version, "
            "t.effective_date AS t_effective, t.title AS t_title "
            "FROM patients p LEFT JOIN templates t ON t.procedure_code=p.procedure_code "
            "WHERE p.batch_version=? ORDER BY p.discharge_time, p.id",
            (batch["batch_version"],)).fetchall()

        for idx, p in enumerate(rows):
            key = f"{p['inpatient_no']}|{p['discharge_time']}|{date}"
            if p["auth_status"] != "已授权":
                stats["excluded_unauthorized"] += 1
                continue
            if not p["procedure_code"] or p["t_status"] != "signed":
                # 未签发模板 → 该术式整体转人工，不降级通用问卷（REQ-03）
                reason = ("术式代码缺失，转人工" if not p["procedure_code"]
                          else f"模板未签发（{p['procedure_code']}），转人工")
                conn.execute("UPDATE patients SET manual_reason=? WHERE id=?",
                             (reason, p["id"]))
                stats["manual"] += 1
                continue
            if p["t_effective"] > date:
                conn.execute("UPDATE patients SET manual_reason=? WHERE id=?",
                             (f"模板 {p['t_version']} 尚未生效，转人工", p["id"]))
                stats["manual"] += 1
                continue
            exists = conn.execute(
                "SELECT id FROM schedules WHERE idempotency_key=?", (key,)).fetchone()
            if exists is not None:
                stats["skipped_duplicate"] += 1
                continue
            slot = "evening" if p["contact_type"] == "家属" else "daytime"
            nurse = nurses[idx % len(nurses)]
            seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0)+1 AS n FROM schedules "
                "WHERE schedule_date=? AND nurse_id=?", (date, nurse["user_id"])
            ).fetchone()["n"]
            conn.execute(
                "INSERT INTO schedules(patient_id,schedule_date,slot,nurse_id,status,"
                "attempts,seq,batch_version,template_version,created_at,idempotency_key) "
                "VALUES(?,?,?,?,'pending',0,?,?,?,?,?)",
                (p["id"], date, slot, nurse["user_id"], seq, batch["batch_version"],
                 p["t_version"], now_iso(), key))
            stats["created"] += 1

        audit.write(conn, security.secret(), event_type="operation",
                    action="schedule.run", actor_id=actor["user_id"],
                    subject=date, batch_version=batch["batch_version"], at=now_iso(),
                    detail={k: v for k, v in stats.items() if k != "date"})
    return stats


def board(conn: sqlite3.Connection, date: str, nurse: dict[str, Any] | None = None,
          all_wards: bool = False) -> dict[str, Any]:
    query = ("SELECT s.*, p.inpatient_no, p.discharge_time, p.department, "
             "p.procedure_code, p.contact_phone, p.contact_type, p.auth_status, "
             "p.manual_reason, u.name AS nurse_name "
             "FROM schedules s JOIN patients p ON p.id=s.patient_id "
             "JOIN users u ON u.user_id=s.nurse_id WHERE s.schedule_date=? ")
    args: list[Any] = [date]
    if nurse and not all_wards:
        query += "AND (s.nurse_id=? OR p.department=?) "
        args += [nurse["user_id"], nurse["ward"]]
    query += "ORDER BY s.nurse_id, s.seq, s.id"
    rows = [dict(r) for r in conn.execute(query, args)]
    for r in rows:
        r["contact_phone"] = r["contact_phone"][:3] + "****" + r["contact_phone"][-2:]
    manual = [dict(r) for r in conn.execute(
        "SELECT * FROM patients WHERE manual_reason<>'' ORDER BY discharge_time")]
    for r in manual:
        r["contact_phone"] = r["contact_phone"][:3] + "****" + r["contact_phone"][-2:]
    return {"date": date, "items": rows, "manual_list": manual}


def reorder(conn: sqlite3.Connection, schedule_id: int, direction: str,
            actor: dict[str, Any]) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM schedules WHERE id=?", (schedule_id,)).fetchone()
    if row is None:
        raise LookupError("排程不存在")
    swap = conn.execute(
        "SELECT * FROM schedules WHERE schedule_date=? AND nurse_id=? AND seq=?",
        (row["schedule_date"], row["nurse_id"],
         row["seq"] - 1 if direction == "up" else row["seq"] + 1)).fetchone()
    if swap is None:
        return {"ok": False, "message": "已到边界"}
    with transaction(conn):
        conn.execute("UPDATE schedules SET seq=? WHERE id=?", (swap["seq"], row["id"]))
        conn.execute("UPDATE schedules SET seq=? WHERE id=?", (row["seq"], swap["id"]))
        audit.write(conn, security.secret(), event_type="operation",
                    action="schedule.reorder", actor_id=actor["user_id"],
                    subject=str(schedule_id), at=now_iso(),
                    detail={"direction": direction, "from": row["seq"], "to": swap["seq"]})
    return {"ok": True}
