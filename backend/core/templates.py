"""术式问卷模板注册表：专科签发版本方可下发（医疗-008、REQ-03）。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import audit, security
from .db import now_iso, transaction


def list_templates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM templates ORDER BY procedure_code").fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["questions"] = json.loads(item.pop("questions_json"))
        out.append(item)
    return out


def sign(conn: sqlite3.Connection, procedure_code: str, token: str, actor: dict[str, Any],
         at: str | None = None) -> dict[str, Any]:
    """专科医生签发模板：step-up 绑定 procedure_code，版本号前进。"""
    if actor["role"] != "doctor":
        raise PermissionError("仅专科医生可签发问卷模板")
    row = conn.execute("SELECT * FROM templates WHERE procedure_code=?",
                       (procedure_code,)).fetchone()
    if row is None:
        raise LookupError("模板不存在")
    digest = security.canonical_digest({"procedure_code": procedure_code})
    security.verify_stepup(conn, token, actor, "template_sign", procedure_code, digest)

    if row["status"] == "signed":
        raise ValueError("模板已签发，禁止重复签发")

    current = row["version"]
    try:
        major = int(str(current).split(".")[0]) + 1
    except ValueError:
        major = 1
    new_version = f"{major}.0"
    if new_version <= str(current):
        raise ValueError("模板版本号必须单调递增")
    stamp = at or now_iso()
    with transaction(conn):
        # 版本 CAS：并发签发仅一个成功，杜绝同版本重复下发
        cur = conn.execute(
            "UPDATE templates SET version=?, status='signed', signed_by=?, effective_date=? "
            "WHERE procedure_code=? AND version=?",
            (new_version, actor["user_id"], stamp[:10], procedure_code, current))
        if cur.rowcount != 1:
            raise ValueError("模板版本已被并发更新，请刷新后重试")
        audit.write(conn, security.secret(), event_type="operation", action="template.sign",
                    actor_id=actor["user_id"], subject=procedure_code, at=stamp,
                    detail={"old_version": current, "new_version": new_version,
                            "signed_by": actor["user_id"], "effective_date": stamp[:10]})
    return {"procedure_code": procedure_code, "version": new_version,
            "effective_date": stamp[:10], "signed_by": actor["user_id"]}
