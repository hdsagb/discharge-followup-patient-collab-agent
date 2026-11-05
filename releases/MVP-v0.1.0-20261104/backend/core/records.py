"""记录草稿服务 + 护士确认工作台。

流程：陈述 → 红旗检查（命中即中断）→ 结构化草稿 → 护士逐字段确认 → 随访登记表。
约束：AI 不签名；未确认只存草稿不归档（医疗-017、018）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import audit, redflag, security
from .db import now_iso, transaction

GENERATOR = "draft_engine_v1（本地规则整理，非院外模型）"

_SYMPTOM_TERMS = ["疼", "痛", "肿", "红", "发热", "伤口", "头晕", "乏力", "恶心",
                  "睡不着", "咳嗽", "麻木"]
_ADHERENCE = {"按时吃药": "按医嘱服药", "漏服": "有漏服", "忘了吃": "有漏服",
              "有做康复训练": "已按计划训练", "有锻炼": "已按计划训练",
              "没练": "未完成训练", "不敢动": "未完成训练"}
_REVISIT = {"复查": "需要复诊", "复诊": "需要复诊", "来医院": "需要复诊",
            "不用来": "暂不需要", "不需要": "暂不需要"}
_SPLIT = re.compile(r"[。！？；\n]+")


def _structure(transcript: str, template_title: str) -> dict[str, Any]:
    sentences = [s.strip() for s in _SPLIT.split(transcript or "") if s.strip()]
    symptoms = sorted({t for t in _SYMPTOM_TERMS if t in (transcript or "")})
    adherence = next((v for k, v in _ADHERENCE.items() if k in (transcript or "")),
                     "未提及")
    revisit = next((v for k, v in _REVISIT.items() if k in (transcript or "")),
                   "未提及")
    wound = next((s for s in sentences if "伤口" in s or "刀口" in s), "未提及")
    return {
        "问卷模板": template_title,
        "主诉": sentences[0][:60] if sentences else "未提及",
        "症状": "、".join(symptoms) if symptoms else "未提及",
        "伤口情况": wound,
        "用药依从性": adherence,
        "康复训练": adherence if "训练" in adherence else "未提及",
        "复诊意向": revisit,
        "患者原话": transcript or "",
        "草稿来源": GENERATOR,
    }


def _load_schedule(conn: sqlite3.Connection, schedule_id: int):
    row = conn.execute(
        "SELECT s.*, p.id AS pid, p.inpatient_no, p.discharge_time, p.auth_status, "
        "t.title AS t_title FROM schedules s JOIN patients p ON p.id=s.patient_id "
        "LEFT JOIN templates t ON t.procedure_code=p.procedure_code AND t.version=s.template_version "
        "WHERE s.id=?", (schedule_id,)).fetchone()
    if row is None:
        raise LookupError("排程不存在")
    return row


def check_and_draft(conn: sqlite3.Connection, schedule_id: int, transcript: str,
                    actor: dict[str, Any], at: str | None = None) -> dict[str, Any]:
    """红旗优先：命中即中断问卷并建单，不生成草稿、不写登记表。"""
    row = _load_schedule(conn, schedule_id)
    stamp = at or now_iso()
    subject = f'{row["inpatient_no"]}|{row["discharge_time"]}'
    hits = redflag.match(transcript)

    if hits:
        created, deduped = [], []
        with transaction(conn):
            for hit in hits:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO tickets(schedule_id,patient_id,rule_id,channel,"
                    "confidence,trigger_text,created_at,status) VALUES(?,?,?,?,?,?,?, 'pending')",
                    (schedule_id, row["pid"], hit["rule_id"], hit["channel"],
                     hit["confidence"], transcript, stamp))
                if cur.rowcount == 1:
                    created.append(hit)
                    audit.write(conn, security.secret(), event_type="escalation",
                                action="ticket.create", actor_id=actor["user_id"],
                                subject=subject, batch_version=row["batch_version"],
                                at=stamp,
                                detail={"rule_id": hit["rule_id"], "channel": hit["channel"],
                                        "confidence": hit["confidence"],
                                        "trigger_text": transcript,
                                        "escalation": hit["escalation"]})
                else:
                    deduped.append(hit["rule_id"])
            conn.execute("UPDATE schedules SET status='escalated' WHERE id=?", (schedule_id,))
            audit.write(conn, security.secret(), event_type="escalation",
                        action="questionnaire.interrupted", actor_id=actor["user_id"],
                        subject=subject, batch_version=row["batch_version"], at=stamp,
                        detail={"hits": [h["rule_id"] for h in hits],
                                "action": "已按院内既有流程转人工，问卷中断"})
        return {"interrupted": True, "hits": hits, "tickets_created": len(created),
                "deduped": deduped,
                "message": "红旗命中：问卷已中断，已生成升级工单并转人工，未生成草稿"}

    fields = _structure(transcript, row["t_title"] or "（模板缺失）")
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO drafts(schedule_id,raw_text,fields_json,generator,status,created_at) "
            "VALUES(?,?,?,?, 'draft', ?)",
            (schedule_id, transcript, json.dumps(fields, ensure_ascii=False), GENERATOR, stamp))
        draft_id = int(cur.lastrowid)
        audit.write(conn, security.secret(), event_type="operation", action="draft.create",
                    actor_id=actor["user_id"], subject=subject,
                    batch_version=row["batch_version"], at=stamp,
                    detail={"draft_id": draft_id, "generator": GENERATOR,
                            "note": "仅草稿，未经护士确认不归档"})
    return {"interrupted": False, "draft": {"draft_id": draft_id, "schedule_id": schedule_id,
                                            "fields": fields, "generator": GENERATOR},
            "message": "未检出红旗，草稿已生成，等待护士逐字段确认"}


def confirm(conn: sqlite3.Connection, draft_id: int, fields: dict[str, Any], token: str,
            actor: dict[str, Any], at: str | None = None) -> dict[str, Any]:
    """护士确认：step-up 凭证绑定草稿内容，差异全量留痕，签署人恒为护士。"""
    draft = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if draft is None:
        raise LookupError("草稿不存在")
    if draft["status"] != "draft":
        raise ValueError("草稿已确认或已作废")
    row = _load_schedule(conn, draft["schedule_id"])
    stamp = at or now_iso()

    digest = security.canonical_digest({"draft_id": draft_id, "fields": fields})
    security.verify_stepup(conn, token, actor, "record_confirm", str(draft_id), digest)

    original = json.loads(draft["fields_json"])
    diff = [{"field": k, "draft": original.get(k, ""), "confirmed": fields.get(k, "")}
            for k in fields if original.get(k, "") != fields.get(k, "")]

    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO records(draft_id,schedule_id,patient_id,fields_json,diff_json,nurse_id,"
            "confirmed_at,batch_version,template_version,signature) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (draft_id, draft["schedule_id"], row["pid"],
             json.dumps(fields, ensure_ascii=False), json.dumps(diff, ensure_ascii=False),
             actor["user_id"], stamp, row["batch_version"], row["template_version"],
             f"nurse:{actor['user_id']}"))
        conn.execute("UPDATE drafts SET status='confirmed' WHERE id=?", (draft_id,))
        conn.execute("UPDATE schedules SET status='completed' WHERE id=?",
                     (draft["schedule_id"],))
        conn.execute(
            "INSERT INTO contact_attempts(schedule_id,channel,result,actor_id,at) "
            "VALUES(?, 'call', 'questionnaire_completed', ?, ?)",
            (draft["schedule_id"], actor["user_id"], stamp))
        audit.write(conn, security.secret(), event_type="operation",
                    action="record.confirm", actor_id=actor["user_id"],
                    subject=f'{row["inpatient_no"]}|{row["discharge_time"]}',
                    batch_version=row["batch_version"], at=stamp,
                    detail={"record_id": int(cur.lastrowid), "draft_id": draft_id,
                            "diff_count": len(diff), "signature": f"nurse:{actor['user_id']}",
                            "model_signs": False})
    return {"record_id": int(cur.lastrowid), "diff": diff, "signature": f"nurse:{actor['user_id']}",
            "message": "已写入随访登记表（签署人为护士）"}


def discard(conn: sqlite3.Connection, draft_id: int, actor: dict[str, Any]) -> dict[str, Any]:
    draft = conn.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if draft is None:
        raise LookupError("草稿不存在")
    with transaction(conn):
        conn.execute("UPDATE drafts SET status='discarded' WHERE id=?", (draft_id,))
        audit.write(conn, security.secret(), event_type="operation", action="draft.discard",
                    actor_id=actor["user_id"], subject=str(draft_id), at=now_iso(),
                    detail={"note": "护士不确认，仅存草稿不归档"})
    return {"ok": True}


def list_drafts(conn: sqlite3.Connection, status: str = "draft") -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT d.*, s.schedule_date, p.inpatient_no, p.discharge_time, p.procedure_code "
        "FROM drafts d JOIN schedules s ON s.id=d.schedule_id "
        "JOIN patients p ON p.id=s.patient_id WHERE d.status=? ORDER BY d.id DESC",
        (status,)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["fields"] = json.loads(item.pop("fields_json"))
        out.append(item)
    return out


def list_records(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT r.*, p.inpatient_no, p.discharge_time, u.name AS nurse_name "
        "FROM records r JOIN patients p ON p.id=r.patient_id "
        "JOIN users u ON u.user_id=r.nurse_id ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["fields"] = json.loads(item.pop("fields_json"))
        item["diff"] = json.loads(item.pop("diff_json"))
        out.append(item)
    return out


def stats(conn: sqlite3.Connection, date: str | None = None) -> dict[str, Any]:
    if date:
        total = conn.execute(
            "SELECT COUNT(*) n FROM schedules WHERE schedule_date=?", (date,)).fetchone()["n"]
        done = conn.execute(
            "SELECT COUNT(*) n FROM schedules WHERE schedule_date=? AND status='completed'",
            (date,)).fetchone()["n"]
        escalated = conn.execute(
            "SELECT COUNT(*) n FROM schedules WHERE schedule_date=? AND status='escalated'",
            (date,)).fetchone()["n"]
        shelved = conn.execute(
            "SELECT COUNT(*) n FROM schedules WHERE schedule_date=? AND status='shelved'",
            (date,)).fetchone()["n"]
    else:
        total = conn.execute("SELECT COUNT(*) n FROM schedules").fetchone()["n"]
        done = conn.execute("SELECT COUNT(*) n FROM schedules WHERE status='completed'").fetchone()["n"]
        escalated = conn.execute("SELECT COUNT(*) n FROM schedules WHERE status='escalated'").fetchone()["n"]
        shelved = conn.execute("SELECT COUNT(*) n FROM schedules WHERE status='shelved'").fetchone()["n"]
    sms = conn.execute(
        "SELECT COUNT(*) n FROM contact_attempts WHERE channel='sms'").fetchone()["n"]
    return {"scheduled": total, "completed": done, "escalated": escalated,
            "shelved": shelved,
            "completion_rate": round(done / total * 100, 1) if total else 0.0,
            "sms_attempts_not_counted": sms,
            "metric_rule": "完成率 = 成功完成问卷 / 当日排程（短信不计入，医疗-017）"}
