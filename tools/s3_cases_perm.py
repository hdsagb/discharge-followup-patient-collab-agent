"""S3 用例（一）：5.7 权限与越权（PM-01～09）。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from s2_lib import DATE
from s3_lib import make_draft, make_record, row, tokens


def pm01(ctx, r):
    read_paths = ["/api/schedule?date=" + DATE, "/api/summary", "/api/records",
                  "/api/drafts", "/api/tickets", "/api/rules", "/api/templates",
                  "/api/audit", "/api/fixtures"]
    for path in read_paths:
        status, p = ctx.get(path)
        r.expect(status == 401, f"{path} 未登录返回 HTTP {status} {p}")
        if status == 401:
            r.expect(p.get("code") == "unauthorized", f"{path} 错误码={p.get('code')}")
    for path, body in (("/api/schedule/run", {"date": DATE}),
                       ("/api/questionnaire/check", {"schedule_id": 1, "transcript": "x"},
                       )):
        status, p = ctx.post(path, body=body)
        r.expect(status == 401, f"POST {path} 未登录返回 HTTP {status} {p}")
    status, p = ctx.get("/api/bootstrap")
    r.expect(status == 200 and p.get("data", {}).get("users"),
             f"公开 /api/bootstrap 异常：{status} {p}")


def pm02(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    _, draft = make_draft(ctx, t["n1"], t["eng"], "0101")
    cases = [
        ("导入", ctx.post("/api/his/import", t["qa"], {"fixture": "his_batch_v1.json"})),
        ("外呼", ctx.call(t["qa"], sid)),
        ("确认写库", ctx.post("/api/records/confirm", t["qa"],
                              {"draft_id": draft["draft_id"], "fields": draft["fields"],
                               "token": "x"})),
        ("生成排程", ctx.post("/api/schedule/run", t["qa"], {"date": DATE})),
        ("草稿作废", ctx.post("/api/drafts/discard", t["qa"], {"draft_id": draft["draft_id"]})),
    ]
    for name, (status, p) in cases:
        r.expect(status == 403, f"质控执行“{name}”返回 HTTP {status} {p}")
        if status == 403:
            r.expect(p.get("code") == "forbidden", f"“{name}”错误码={p.get('code')}")


def pm03(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, draft = make_draft(ctx, t["n1"], t["eng"], "0101")
    status, p = ctx.post("/api/records/confirm", t["eng"],
                         {"draft_id": draft["draft_id"], "fields": draft["fields"], "token": "x"})
    r.expect(status == 403, f"工程师确认写库返回 HTTP {status} {p}")
    status2, p2 = ctx.post("/api/drafts/discard", t["eng"], {"draft_id": draft["draft_id"]})
    r.expect(status2 == 403, f"工程师作废草稿返回 HTTP {status2} {p2}")
    r.note(f"确认返回={p.get('code')}，作废返回={p2.get('code')}")


def pm04(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    make_record(ctx, t["n1"], t["eng"], "0101")          # 一病区记录
    make_draft(ctx, t["n1"], t["eng"], "0109")           # 一病区未确认草稿
    make_draft(ctx, t["n2"], t["eng"], "0108")           # 二病区未确认草稿
    board = ctx.board(t["n2"])
    nos = {x["inpatient_no"] for x in board["items"]}
    r.expect(ctx.patient_no("0101") not in nos, f"二病区排程可见一病区患者：{sorted(nos)}")
    records = ctx.ok(ctx.get("/api/records", t["n2"]), "二病区查看记录")["items"]
    rec_nos = {x["inpatient_no"] for x in records}
    r.expect(ctx.patient_no("0101") not in rec_nos, f"越病区记录泄漏：{sorted(rec_nos)}")
    drafts = ctx.ok(ctx.get("/api/drafts", t["n2"]), "二病区查看草稿")["items"]
    draft_nos = {x["inpatient_no"] for x in drafts}
    r.expect(ctx.patient_no("0109") not in draft_nos, f"越病区草稿泄漏：{sorted(draft_nos)}")
    if rec_nos or draft_nos:
        r.note(f"/api/records 返回 {len(records)} 条（含 {sorted(rec_nos)}）、"
               f"/api/drafts 返回 {len(drafts)} 条（含 {sorted(draft_nos)}），接口未按病区/归属过滤")


def pm05(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    items = ctx.audit_items(t["qa"])
    r.expect(bool(items), "质控无法读取审计列表")
    for it in items:
        r.expect("prev_hash" not in it, f"审计条目返回 prev_hash：{json.dumps(it, ensure_ascii=False)}")
        for key in ("seq", "event_type", "action", "actor_id", "outcome", "at"):
            r.expect(key in it, f"审计条目缺字段 {key}：{json.dumps(it, ensure_ascii=False)}")
    r.note(f"审计条目 {len(items)} 条，字段={sorted(items[0]) if items else []}")
    status, p = ctx.post("/api/audit/verify", t["qa"])
    r.expect(status == 200 and p.get("data", {}).get("ok") is True, f"验链失败：{status} {p}")


def pm06(ctx, r):
    t = tokens(ctx)
    status, p = ctx.get("/api/audit", t["n1"])
    r.expect(status == 403, f"护士读取审计返回 HTTP {status} {p}")
    if status == 403:
        r.expect(p.get("code") == "forbidden", f"错误码={p.get('code')}")


def pm07(ctx, r):
    status, p = ctx.get("/api/summary")
    r.expect(status == 401, f"无 X-Session 返回 HTTP {status} {p}")
    missing_msg = p.get("error")
    status2, p2 = ctx.get("/api/summary", "garbage-token")
    r.expect(status2 == 401, f"伪造 X-Session 返回 HTTP {status2} {p2}")
    r.expect(p2.get("error") == missing_msg, f"错误文案不统一：{missing_msg!r} vs {p2.get('error')!r}")
    expired = "s3-expired-session"
    with ctx.db() as conn:
        old = (datetime.now() - timedelta(hours=9)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                     (expired, "u-nurse-01", old, old))
        conn.commit()
    status3, p3 = ctx.get("/api/summary", expired)
    r.expect(status3 == 401, f"过期会话返回 HTTP {status3} {p3}")
    r.expect(p3.get("error") == missing_msg, f"过期会话文案不统一：{p3.get('error')!r}")
    r.note(f"统一文案={missing_msg!r}")


def pm08(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    msg_update = msg_delete = ""
    with ctx.db() as conn:
        try:
            conn.execute("UPDATE audit_events SET outcome='failed' WHERE seq=1")
        except sqlite3.DatabaseError as exc:
            msg_update = str(exc)
        try:
            conn.execute("DELETE FROM audit_events WHERE seq=1")
        except sqlite3.DatabaseError as exc:
            msg_delete = str(exc)
        conn.rollback()
        n = conn.execute("SELECT COUNT(*) n FROM audit_events").fetchone()["n"]
        r.expect(n >= 1, f"审计行异常：{n}")
    r.expect("append-only" in msg_update, f"UPDATE 未被库层拒绝：{msg_update!r}")
    r.expect("append-only" in msg_delete, f"DELETE 未被库层拒绝：{msg_delete!r}")
    with ctx.db() as conn:
        try:
            conn.execute("UPDATE patients SET name='运维核对' WHERE inpatient_no=?",
                         (ctx.patient_no("0101"),))
            conn.rollback()
            r.note("业务表（patients）可被库内直接修改，符合方案“不内置运维后台”的既定约束")
        except sqlite3.DatabaseError as exc:
            r.note(f"业务表修改被拒：{exc}")


def pm09(ctx, r):
    status, p = ctx.post("/api/login", body={"user_id": "u-nurse-01"})
    r.expect(status == 200, f"仅 user_id 登录返回 HTTP {status} {p}")
    data = p.get("data", {})
    r.expect(data.get("user", {}).get("role") == "nurse", f"登录身份异常：{data}")
    status2, p2 = ctx.post("/api/login", body={"user_id": "u-nurse-01", "password": "wrong-pass"})
    r.expect(status2 == 200, f"携带错误口令仍返回 HTTP {status2} {p2}")
    status3, p3 = ctx.post("/api/login", body={"user_id": "u-nobody"})
    r.expect(status3 == 400, f"未知账号返回 HTTP {status3} {p3}")
    r.note("登录无口令因子：错误口令照样签发会话，属于方案已记载的试点缺口（待接入院内统一认证）")


CASES = [
    ("PM-01", "未登录访问受保护接口一律 401", "P0",
     "无 X-Session 访问业务接口返回 401，公开接口仅 /api/bootstrap 与 /api/login", pm01),
    ("PM-02", "质控账号越权写操作一律 403", "P0",
     "质量办执行导入/外呼/确认/排程/作废均返回 403", pm02),
    ("PM-03", "工程师账号越权确认/写库一律 403", "P0",
     "工程师执行确认写库、草稿作废返回 403", pm03),
    ("PM-04", "护士只能看到本病区/本人负责数据", "P0",
     "二病区护士的排程、记录、草稿均不含一病区数据", pm04),
    ("PM-05", "质控可只读审计且不暴露链头", "P1",
     "质控可读审计列表与验链，返回不含 prev_hash，具备最小必要字段", pm05),
    ("PM-06", "护士不可读全量审计", "P1",
     "护士访问 /api/audit 返回 403", pm06),
    ("PM-07", "会话伪造/过期统一 401", "P0",
     "缺失、伪造、过期会话均返回 401 且错误文案一致", pm07),
    ("PM-08", "审计表库层禁止改删", "P0",
     "直接 UPDATE/DELETE audit_events 被触发器拒绝", pm08),
    ("PM-09", "登录口令缺口现状核验", "P1",
     "仅 user_id 可登录、错误口令不影响签发；该缺口在方案中已列为待接入统一认证", pm09),
]
