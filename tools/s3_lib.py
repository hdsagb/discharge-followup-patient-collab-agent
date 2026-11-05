"""S3 用例公共辅助（权限 / 安全 / 非功能），复用 s2_lib.Ctx 与只测不改约定。"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime

import s2_lib
from s2_lib import BENIGN, DATE, SECRET


def tokens(ctx):
    return {
        "eng": ctx.login("u-engineer-01"),
        "n1": ctx.login("u-nurse-01"),
        "n2": ctx.login("u-nurse-02"),
        "doc": ctx.login("u-doctor-01"),
        "qa": ctx.login("u-quality-01"),
        "mgr": ctx.login("u-manager-01"),
    }


def row(**kw):
    base = {"inpatient_no": "S9001", "name": "测试患者", "department": "骨科一病区",
            "discharge_time": f"{DATE}T10:00:00", "procedure_code": "PG-ORTHO-THR",
            "contact_phone": "13800138000", "contact_type": "本人", "auth_status": "已授权",
            "synthetic": 1}
    base.update(kw)
    return base


def make_draft(ctx, nurse, engineer, no="0101", transcript=BENIGN):
    return ctx.draft_for(nurse, engineer, no, transcript)


def make_record(ctx, nurse, engineer, no="0101", transcript=BENIGN):
    sid, draft = ctx.draft_for(nurse, engineer, no, transcript)
    step = ctx.stepup(nurse, "record_confirm", str(draft["draft_id"]),
                      fields=draft["fields"])["token"]
    data = ctx.ok(ctx.confirm(nurse, draft["draft_id"], draft["fields"], step), "确认写库")
    return sid, draft, data


def field_digest(draft_id, fields):
    return s2_lib.digest({"draft_id": int(draft_id), "fields": fields})


def backend_audit():
    """导入被测副本的审计模块（只用于容量造数与恢复校验，不修改副本）。"""
    root = str(s2_lib.ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    from backend.core import audit as audit_mod
    return audit_mod


def bulk_schedules(ctx, count, date=DATE, patient_no="0101", prefix="NF-CAP"):
    """直连临时库造排程，验证万级容量下的查询性能。"""
    target = ctx.patient_no(patient_no)
    with ctx.db() as conn:
        p = conn.execute("SELECT * FROM patients WHERE inpatient_no=? ORDER BY id LIMIT 1",
                         (target,)).fetchone()
        if p is None:
            raise RuntimeError("容量造数前置失败：名单尚未导入")
        stamp = datetime.now().isoformat(timespec="seconds")
        conn.execute("BEGIN IMMEDIATE")
        for i in range(count):
            conn.execute(
                "INSERT INTO schedules(patient_id,schedule_date,slot,nurse_id,status,attempts,"
                "seq,batch_version,template_version,created_at,idempotency_key) "
                "VALUES(?,?,?,?,'pending',0,?,?,?,?,?)",
                (p["id"], date, "daytime", "u-nurse-01", i + 1, p["batch_version"],
                 "1.0", stamp, f"{prefix}|{i}"))
        conn.commit()


def bulk_audit(ctx, count, secret=SECRET.encode()):
    """按副本的哈希链算法批量写入审计行（一次事务，保证 verify 可通）。"""
    audit_mod = backend_audit()
    stamp = datetime.now().isoformat(timespec="seconds")
    with ctx.db() as conn:
        last = conn.execute("SELECT seq, entry_hash FROM audit_events "
                            "ORDER BY seq DESC LIMIT 1").fetchone()
        start = (last["seq"] if last else 0) + 1
        prev = last["entry_hash"] if last else audit_mod.GENESIS
        conn.execute("BEGIN IMMEDIATE")
        for offset in range(count):
            i = start + offset
            payload = {"seq": i, "type": "operation", "action": "nf.capacity.seed",
                       "actor": "u-engineer-01", "subject": "", "batch": "",
                       "outcome": "success", "detail": {"i": offset}, "at": stamp}
            digest = audit_mod.entry_hash(secret, prev, i, payload)
            conn.execute(
                "INSERT INTO audit_events(seq,event_type,action,actor_id,subject,batch_version,"
                "outcome,detail_json,at,prev_hash,entry_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (i, "operation", "nf.capacity.seed", "u-engineer-01", "", "", "success",
                 audit_mod._canonical(payload["detail"]), stamp, prev, digest))
            prev = digest
        conn.commit()
    return start


def blocked(r, reason, method):
    r.failures.append(reason)
    r.blocked = True
    r.note("执行方案：" + method)
