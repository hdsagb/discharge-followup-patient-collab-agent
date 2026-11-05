"""数据契约 v1.0：8 字段白名单、清洗、版本与幂等导入（HIS 只读替身）。

设计依据：docs/设计方案.md §6；docs/05-构建集成.md 任务 5.1。
超出白名单的字段一律拒收（医疗-013）。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import audit, security
from .db import now_iso, transaction

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"

FIELDS = ("inpatient_no", "discharge_time", "department", "procedure_code",
          "followup_status", "contact_phone", "contact_type", "auth_status")

CONTACT_TYPES = {"本人", "家属"}
AUTH_STATUS = {"已授权", "未授权", "撤回"}
FOLLOWUP_STATUS = {"待随访", "进行中", "完成", "搁置", "已升级"}


def _clean_phone(value: str) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits


def validate_row(row: dict[str, Any]) -> tuple[dict[str, Any] | None, str, list[str]]:
    """返回 (清洗后行, 拒绝原因, 被丢弃的字段名)。"""
    dropped = sorted(set(row) - set(FIELDS))
    missing = [f for f in ("inpatient_no", "discharge_time", "department",
                           "contact_phone", "contact_type", "auth_status") if not row.get(f)]
    if missing:
        return None, f"缺必填字段: {','.join(missing)}", dropped
    phone = _clean_phone(row["contact_phone"])
    if len(phone) < 7:
        return None, "联系电话格式无效", dropped
    if row["contact_type"] not in CONTACT_TYPES:
        return None, f"联系人类型非法: {row['contact_type']}", dropped
    if row["auth_status"] not in AUTH_STATUS:
        return None, f"授权状态非法: {row['auth_status']}", dropped
    status = row.get("followup_status") or "待随访"
    if status not in FOLLOWUP_STATUS:
        return None, f"随访状态非法: {status}", dropped
    cleaned = {
        "inpatient_no": str(row["inpatient_no"]).strip(),
        "discharge_time": str(row["discharge_time"]).strip(),
        "department": str(row["department"]).strip(),
        "procedure_code": str(row.get("procedure_code") or "").strip(),
        "followup_status": status,
        "contact_phone": phone,
        "contact_type": row["contact_type"],
        "auth_status": row["auth_status"],
    }
    return cleaned, "", dropped


def load_fixture(name: str) -> dict[str, Any]:
    path = FIXTURE_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"接口替身不存在: {name}")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def list_fixtures() -> list[str]:
    return sorted(p.name for p in FIXTURE_DIR.glob("*.json"))


def import_batch(conn: sqlite3.Connection, rows: list[dict[str, Any]], batch_version: str,
                 exported_at: str, actor: dict[str, Any],
                 note: str = "") -> dict[str, Any]:
    """导入名单批次。同名批次版本再次导入为幂等空操作。"""
    existing = conn.execute(
        "SELECT * FROM batches WHERE batch_version=?", (batch_version,)).fetchone()
    if existing is not None:
        return {"batch_version": batch_version, "idempotent": True,
                "accepted": existing["accepted"], "rejected": existing["rejected"],
                "message": "批次已存在，按幂等跳过（未重复导入）"}

    accepted, rejected, merges, drops = 0, 0, 0, set()
    reject_reasons: list[str] = []
    with transaction(conn):
        conn.execute(
            "INSERT INTO batches(batch_version,exported_at,imported_at,status,note) "
            "VALUES(?,?,?,?,?)",
            (batch_version, exported_at, now_iso(), "active", note))
        conn.execute("UPDATE batches SET status='superseded' "
                     "WHERE batch_version<>? AND status='active'", (batch_version,))
        for row in rows:
            cleaned, reason, dropped = validate_row(row)
            drops.update(dropped)
            if cleaned is None:
                rejected += 1
                reject_reasons.append(f"{row.get('inpatient_no') or '?'}: {reason}")
                continue
            dup = conn.execute(
                "SELECT * FROM patients WHERE inpatient_no=? AND discharge_time=?",
                (cleaned["inpatient_no"], cleaned["discharge_time"])).fetchone()
            if dup is not None:
                merges += 1
                conn.execute(
                    "UPDATE patients SET department=?, procedure_code=?, contact_phone=?, "
                    "contact_type=?, auth_status=?, batch_version=? WHERE id=?",
                    (cleaned["department"], cleaned["procedure_code"],
                     cleaned["contact_phone"], cleaned["contact_type"],
                     cleaned["auth_status"], batch_version, dup["id"]))
                continue
            manual = "术式代码缺失，转人工" if not cleaned["procedure_code"] else ""
            conn.execute(
                "INSERT INTO patients(inpatient_no,discharge_time,department,procedure_code,"
                "followup_status,contact_phone,contact_type,auth_status,batch_version,"
                "manual_reason) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (cleaned["inpatient_no"], cleaned["discharge_time"], cleaned["department"],
                 cleaned["procedure_code"], cleaned["followup_status"],
                 cleaned["contact_phone"], cleaned["contact_type"], cleaned["auth_status"],
                 batch_version, manual))
            accepted += 1
        conn.execute("UPDATE batches SET accepted=?, rejected=? WHERE batch_version=?",
                     (accepted, rejected, batch_version))
        audit.write(conn, security.secret(), event_type="operation", action="batch.import",
                    actor_id=actor["user_id"], subject=batch_version,
                    batch_version=batch_version, at=now_iso(),
                    detail={"accepted": accepted, "rejected": rejected, "merged": merges,
                            "dropped_fields": sorted(drops), "reasons": reject_reasons[:10]})
    return {"batch_version": batch_version, "idempotent": False, "accepted": accepted,
            "rejected": rejected, "merged": merges, "dropped_fields": sorted(drops),
            "reject_reasons": reject_reasons}


def simulate_timeout(conn: sqlite3.Connection, actor: dict[str, Any],
                     note: str = "HIS 导出超时") -> dict[str, Any]:
    """接口超时降级：沿用上一版本名单并标记“待补”。"""
    prev = conn.execute(
        "SELECT * FROM batches WHERE status IN ('active','pending_backfill') "
        "ORDER BY imported_at DESC LIMIT 1").fetchone()
    if prev is None:
        raise ValueError("无可回退的名单版本")
    with transaction(conn):
        conn.execute("UPDATE batches SET status='pending_backfill' WHERE batch_version=?",
                     (prev["batch_version"],))
        audit.write(conn, security.secret(), event_type="operation",
                    action="batch.fallback", actor_id=actor["user_id"],
                    subject=prev["batch_version"], batch_version=prev["batch_version"],
                    at=now_iso(),
                    detail={"note": note, "exported_at": prev["exported_at"],
                            "rule": "超 24 小时转人工"})
    return {"fallback_batch": prev["batch_version"], "status": "pending_backfill",
            "exported_at": prev["exported_at"], "note": note}
