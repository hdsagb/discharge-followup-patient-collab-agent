"""S2 用例（二）：草稿确认、工单与模板、异常与降级。"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from s2_lib import BENIGN, DATE, digest, forge_token
from s2_cases_core import row, tokens


# ---------- 5.5 记录与确认 ----------

def rc01(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid, draft = ctx.draft_for(t["n1"], t["eng"])
    fields = draft["fields"]
    r.expect(len(fields) == 9, f"字段数={len(fields)} {list(fields)}")
    r.expect(draft["generator"].startswith("draft_engine_v1"), f"生成器={draft['generator']}")
    items = ctx.ok(ctx.get("/api/drafts", t["n1"]), "草稿")["items"]
    r.expect(any(x["id"] == draft["draft_id"] and x["status"] == "draft" for x in items),
             "草稿列表无刚生成草稿")


def rc02(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, draft = ctx.draft_for(t["n1"], t["eng"])
    status, p = ctx.confirm(t["n1"], draft["draft_id"], draft["fields"], "")
    r.expect(status == 403 and p.get("code") == "forbidden", f"无凭证确认={status} {p}")


def rc03(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid, draft = ctx.draft_for(t["n1"], t["eng"])
    step = ctx.stepup(t["n1"], "record_confirm", str(draft["draft_id"]),
                      fields=draft["fields"])["token"]
    data = ctx.ok(ctx.confirm(t["n1"], draft["draft_id"], draft["fields"], step), "确认")
    r.expect(data.get("signature") == "nurse:u-nurse-01", f"签署人={data.get('signature')}")
    done = ctx.ok(ctx.get("/api/drafts?status=confirmed", t["n1"]), "已确认草稿")["items"]
    r.expect(any(x["id"] == draft["draft_id"] for x in done), "确认后草稿状态未更新")
    it = ctx.item(t["eng"], "0101")
    r.expect(it and it["status"] == "completed", "排程未置 completed")
    records = ctx.ok(ctx.get("/api/records", t["n1"]), "登记表")["items"]
    r.expect(len(records) == 1, f"登记表条数={len(records)}")


def rc04(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, draft = ctx.draft_for(t["n1"], t["eng"])
    step = ctx.stepup(t["n1"], "record_confirm", str(draft["draft_id"]),
                      fields=draft["fields"])["token"]
    altered = dict(draft["fields"])
    altered["主诉"] = "被篡改的主诉"
    status, p = ctx.confirm(t["n1"], draft["draft_id"], altered, step)
    r.expect(status == 403, f"改字段后确认={status} {p}")


def rc05(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, draft = ctx.draft_for(t["n1"], t["eng"])
    step = ctx.stepup(t["n1"], "record_confirm", str(draft["draft_id"]),
                      fields=draft["fields"])["token"]
    first = ctx.confirm(t["n1"], draft["draft_id"], draft["fields"], step)
    r.expect(first[0] == 200, f"首次确认={first}")
    second = ctx.confirm(t["n1"], draft["draft_id"], draft["fields"], step)
    r.expect(second[0] == 403, f"凭证重放返回 {second[0]} {second[1]}")
    r.note("校验顺序为先验凭证（nonce 防重放），重放恒返回 403")


def rc06(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, draft = ctx.draft_for(t["n1"], t["eng"])
    dg = digest({"draft_id": draft["draft_id"], "fields": draft["fields"]})
    expired = forge_token("u-nurse-01", "record_confirm", str(draft["draft_id"]), dg,
                          "expired-nonce", int(time.time()) - 60)
    status, p = ctx.confirm(t["n1"], draft["draft_id"], draft["fields"], expired)
    r.expect(status == 403 and "过期" in p.get("error", ""), f"过期凭证={status} {p}")


def rc07(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, draft = ctx.draft_for(t["n1"], t["eng"])
    step = ctx.stepup(t["n1"], "record_confirm", str(draft["draft_id"]),
                      fields=draft["fields"])["token"]
    status, p = ctx.confirm(t["n2"], draft["draft_id"], draft["fields"], step)
    r.expect(status == 403 and "不匹配" in p.get("error", ""), f"换人使用凭证={status} {p}")


def rc08(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, draft = ctx.draft_for(t["n1"], t["eng"])
    step = ctx.stepup(t["n1"], "record_confirm", str(draft["draft_id"]),
                      fields=draft["fields"])["token"]
    ctx.ok(ctx.confirm(t["n1"], draft["draft_id"], draft["fields"], step), "首次确认")
    step2 = ctx.stepup(t["n1"], "record_confirm", str(draft["draft_id"]),
                       fields=draft["fields"])["token"]
    status, p = ctx.confirm(t["n1"], draft["draft_id"], draft["fields"], step2)
    r.expect(status == 409 and "已确认" in p.get("error", ""), f"重复确认={status} {p}")


def rc09(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid, draft = ctx.draft_for(t["n1"], t["eng"])
    ctx.ok(ctx.post("/api/drafts/discard", t["n1"], {"draft_id": draft["draft_id"]}), "作废")
    items = ctx.ok(ctx.get("/api/drafts?status=discarded", t["n1"]), "已作废")["items"]
    r.expect(any(x["id"] == draft["draft_id"] for x in items), "作废状态未落库")
    it = ctx.item(t["eng"], "0101")
    r.expect(it and it["status"] == "in_progress", "作废后排程不应完成")
    audit = ctx.audit_items(t["qa"])
    r.expect(any(x.get("action") == "draft.discard" for x in audit), "缺作废审计")


def rc10(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, draft = ctx.draft_for(t["n1"], t["eng"])
    altered = dict(draft["fields"])
    altered["主诉"] = "复查时顺便开了药"
    step = ctx.stepup(t["n1"], "record_confirm", str(draft["draft_id"]),
                      fields=altered)["token"]
    ctx.ok(ctx.confirm(t["n1"], draft["draft_id"], altered, step), "确认")
    rec = ctx.ok(ctx.get("/api/records", t["n1"]), "登记表")["items"][0]
    for key in ("batch_version", "template_version", "diff", "nurse_id", "confirmed_at", "signature"):
        r.expect(key in rec and rec[key] not in (None, "", []), f"登记表缺 {key}")
    r.expect(any(x["field"] == "主诉" for x in rec["diff"]), f"修改差异未留痕 {rec['diff']}")


def rc11(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, draft = ctx.draft_for(t["n1"], t["eng"])
    step = ctx.stepup(t["doc"], "record_confirm", str(draft["draft_id"]),
                      fields=draft["fields"])["token"]
    status, p = ctx.confirm(t["doc"], draft["draft_id"], draft["fields"], step)
    r.expect(status == 403, f"医生确认={status} {p}")


# ---------- 5.6 工单与模板 ----------

def prepare_ticket(ctx, t):
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, data = ctx.ticket_for(t["n1"], t["eng"])
    ticket = ctx.ok(ctx.get("/api/tickets", t["doc"]), "工单")["items"][0]
    return ticket["id"]


def tk01(ctx, r):
    t = tokens(ctx)
    tid = prepare_ticket(ctx, t)
    status, p = ctx.post(f"/api/tickets/{tid}/resolve", t["doc"],
                         {"decision": "approved", "note": "已电话处置", "token": ""})
    r.expect(status == 403, f"无凭证处置={status} {p}")


def tk02(ctx, r):
    t = tokens(ctx)
    tid = prepare_ticket(ctx, t)
    step = ctx.stepup(t["doc"], "ticket_resolve", str(tid),
                      decision="approved", note="已电话处置")["token"]
    data = ctx.ok(ctx.post(f"/api/tickets/{tid}/resolve", t["doc"],
                           {"decision": "approved", "note": "已电话处置", "token": step}), "处置")
    r.expect(data.get("status") == "approved" and data.get("resolved_by") == "u-doctor-01",
             f"处置结果={data}")
    r.expect(ctx.ok(ctx.get("/api/records", t["doc"]), "登记表")["items"] == [],
             "处置工单写入了登记表")


def tk03(ctx, r):
    t = tokens(ctx)
    tid = prepare_ticket(ctx, t)
    step = ctx.stepup(t["doc"], "ticket_resolve", str(tid),
                      decision="approved", note="已处置")["token"]
    data = ctx.ok(ctx.post(f"/api/tickets/{tid}/resolve", t["doc"],
                           {"decision": "approved", "note": "已处置", "token": step,
                            "resolved_by": "u-nurse-01"}), "处置")
    r.expect(data.get("resolved_by") == "u-doctor-01", f"伪造签署人生效={data}")


def tk04(ctx, r):
    t = tokens(ctx)
    tid = prepare_ticket(ctx, t)
    step = ctx.stepup(t["doc"], "ticket_resolve", str(tid),
                      decision="approved", note="已处置")["token"]
    ctx.ok(ctx.post(f"/api/tickets/{tid}/resolve", t["doc"],
                    {"decision": "approved", "note": "已处置", "token": step}), "首次处置")
    status, p = ctx.post(f"/api/tickets/{tid}/resolve", t["doc"],
                         {"decision": "approved", "note": "再次", "token": step})
    r.expect(status == 400 and "已处置" in p.get("error", ""), f"重复处置={status} {p}")


def tk05(ctx, r):
    t = tokens(ctx)
    tid = prepare_ticket(ctx, t)
    status, p = ctx.post(f"/api/tickets/{tid}/resolve", t["n1"],
                         {"decision": "approved", "note": "护士处置", "token": ""})
    r.expect(status == 403, f"护士处置={status} {p}")


def tk06(ctx, r):
    t = tokens(ctx)
    step = ctx.stepup(t["doc"], "template_sign", "PG-ORTHO-FIX")["token"]
    data = ctx.ok(ctx.post("/api/templates/PG-ORTHO-FIX/sign", t["doc"], {"token": step}), "签发")
    r.expect(data.get("version") == "1.0" and data.get("effective_date") == DATE, f"签发结果={data}")
    audit = ctx.audit_items(t["qa"])
    hit = [x for x in audit if x.get("action") == "template.sign"]
    r.expect(bool(hit), "缺 template.sign 审计")
    if hit:
        d = json.dumps(hit[0].get("detail"), ensure_ascii=False)
        r.expect("0.9" in d and "1.0" in d, f"版本变化未留痕 {d}")


def tk07(ctx, r):
    t = tokens(ctx)
    status, p = ctx.post("/api/templates/PG-ORTHO-FIX/sign", t["n1"], {"token": ""})
    r.expect(status == 403, f"护士签发={status} {p}")


def tk08(ctx, r):
    t = tokens(ctx)
    s1 = ctx.stepup(t["doc"], "template_sign", "PG-ORTHO-FIX")["token"]
    first = ctx.ok(ctx.post("/api/templates/PG-ORTHO-FIX/sign", t["doc"], {"token": s1}), "首签")
    s2 = ctx.stepup(t["doc"], "template_sign", "PG-ORTHO-FIX")["token"]
    status, p = ctx.post("/api/templates/PG-ORTHO-FIX/sign", t["doc"], {"token": s2})
    r.expect(status == 400 and "已签发" in p.get("error", ""), f"重复签发={status} {p}")
    with ctx.db() as conn:
        ver = conn.execute("SELECT version FROM templates WHERE procedure_code='PG-ORTHO-FIX'"
                           ).fetchone()["version"]
    r.expect(ver == first.get("version") == "1.0", f"重复签发后版本={ver}")


def tk09(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    r.expect(ctx.item(t["eng"], "0104") is None, "0104 初始不应排程")
    step = ctx.stepup(t["doc"], "template_sign", "PG-ORTHO-FIX")["token"]
    ctx.ok(ctx.post("/api/templates/PG-ORTHO-FIX/sign", t["doc"], {"token": step}), "签发")
    ctx.run_schedule(t["n1"])
    it = ctx.item(t["eng"], "0104")
    r.expect(it is not None, "签发后同日重跑未补排 0104")
    reason = ctx.manual_reason(t["eng"], "0104")
    r.expect(reason == "", f"0104 已有排程但人工原因仍为 {reason!r}")
    r.note("补排成功已清理 manual_reason，患者不再同时出现在排程与人工清单")


# ---------- 5.8 异常与降级 ----------

def ex01(ctx, r):
    t = tokens(ctx)
    body = {"batch_version": "T-EX01", "exported_at": f"{DATE}T02:10:00",
            "rows": [row(inpatient_no="T9101"), "not-a-dict"]}
    status, p = ctx.post("/api/his/import", t["eng"], body)
    with ctx.db() as conn:
        nb = conn.execute("SELECT COUNT(*) n FROM batches WHERE batch_version='T-EX01'").fetchone()["n"]
        np = conn.execute("SELECT COUNT(*) n FROM patients WHERE inpatient_no='T9101'").fetchone()["n"]
        r.expect(nb == 0 and np == 0, f"半批落库：batch={nb} patient={np}")
    r.note(f"中途异常返回 HTTP {status} code={p.get('code')}，事务已回滚")


def ex02(ctx, r):
    t = tokens(ctx)
    with ctx.db() as conn:
        conn.execute("CREATE TRIGGER s2_audit_block BEFORE INSERT ON audit_events "
                     "BEGIN SELECT RAISE(ABORT, 'audit disabled'); END;")
        conn.commit()
    status, p = ctx.post("/api/his/import", t["eng"], {"fixture": "his_batch_v1.json"})
    r.expect(status == 503 and p.get("code") == "audit_unavailable", f"审计失败={status} {p}")
    with ctx.db() as conn:
        nb = conn.execute("SELECT COUNT(*) n FROM batches").fetchone()["n"]
        np = conn.execute("SELECT COUNT(*) n FROM patients").fetchone()["n"]
        r.expect(nb == 0 and np == 0, f"审计失败仍落库：batch={nb} patient={np}")


def ex03(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid1, d1 = ctx.draft_for(t["n1"], t["eng"], "0101")
    sid2, d2 = ctx.draft_for(t["n1"], t["eng"], "0102")
    s1 = ctx.stepup(t["n1"], "record_confirm", str(d1["draft_id"]), fields=d1["fields"])["token"]
    barrier = threading.Barrier(2)
    def go_same():
        barrier.wait()
        return ctx.confirm(t["n1"], d1["draft_id"], d1["fields"], s1)[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda _: go_same(), range(2)))
    with ctx.db() as conn:
        n1 = conn.execute("SELECT COUNT(*) n FROM records WHERE draft_id=?", (d1["draft_id"],)).fetchone()["n"]
    r.expect(codes.count(200) == 1 and codes.count(400) + codes.count(403) == 1 and n1 == 1,
             f"同一凭证并发 codes={codes} records={n1}")
    s2a = ctx.stepup(t["n1"], "record_confirm", str(d2["draft_id"]), fields=d2["fields"])["token"]
    s2b = ctx.stepup(t["n1"], "record_confirm", str(d2["draft_id"]), fields=d2["fields"])["token"]
    barrier2 = threading.Barrier(2)
    def go_diff(tok):
        barrier2.wait()
        return ctx.confirm(t["n1"], d2["draft_id"], d2["fields"], tok)[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes2 = list(pool.map(go_diff, [s2a, s2b]))
    with ctx.db() as conn:
        n2 = conn.execute("SELECT COUNT(*) n FROM records WHERE draft_id=?", (d2["draft_id"],)).fetchone()["n"]
    r.expect(codes2.count(200) == 1 and n2 == 1, f"双凭证并发 codes={codes2} records={n2}")


def ex_d01(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, d = ctx.draft_for(t["n1"], t["eng"], "0101")
    tokens8 = [ctx.stepup(t["n1"], "record_confirm", str(d["draft_id"]),
                           fields=d["fields"])["token"] for _ in range(4)]
    jobs = tokens8 + tokens8  # 4 凭证 × 2 轮，含同凭证重复提交

    barrier = threading.Barrier(len(jobs))
    def go(tok):
        barrier.wait()
        return ctx.confirm(t["n1"], d["draft_id"], d["fields"], tok)[0]

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        codes = list(pool.map(go, jobs))
    with ctx.db() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM records WHERE draft_id=?",
                         (d["draft_id"],)).fetchone()["n"]
        st = conn.execute("SELECT status FROM drafts WHERE id=?",
                          (d["draft_id"],)).fetchone()["status"]
        nonces = conn.execute("SELECT COUNT(*) n FROM stepup_nonces WHERE action_id=?",
                              (str(d["draft_id"]),)).fetchone()["n"]
    r.expect(codes.count(200) == 1, f"并发 8 次确认成功数={codes.count(200)} codes={codes}")
    r.expect(all(c in (403, 409) for c in codes if c != 200), f"异常响应码={codes}")
    r.expect(n == 1, f"登记表条数={n}")
    r.expect(st == "confirmed", f"草稿状态={st}")
    r.expect(nonces <= len(tokens8), f"nonce 记录异常={nonces}")
    r.note(f"8 并发（4 凭证 × 2 轮）codes={codes}；records={n}，draft={st}")


def ex04(ctx, r):
    t = tokens(ctx)
    s1, p1 = ctx.get("/api/does-not-exist")
    r.expect(s1 == 401, f"未登录未知路由={s1} {p1}")
    s2, p2 = ctx.get("/api/does-not-exist", t["n1"])
    r.expect(s2 == 404, f"已登录未知路由={s2} {p2}")


def ex05(ctx, r):
    status, p = ctx.post("/api/login", raw="{")
    r.expect(status == 400 and p.get("code") == "bad_json", f"坏 JSON={status} {p}")


def ex06(ctx, r):
    status, p = ctx.post("/api/login", raw=json.dumps({"user_id": "x" * 300000}))
    r.expect(status == 413 and p.get("code") == "body_too_large", f"300KB 报文={status} {p}")


def ex07(ctx, r):
    nested = {}
    cur = nested
    for _ in range(40):
        cur["x"] = {}
        cur = cur["x"]
    status, p = ctx.post("/api/login", raw=json.dumps(nested))
    r.expect(status == 400 and p.get("code") == "too_deep", f"40 层嵌套={status} {p}")


def ex08(ctx, r):
    t = tokens(ctx)
    statuses = [ctx.get("/api/summary", t["n1"])[0] for _ in range(121)]
    r.expect(statuses.count(200) == 120, f"前 120 次出现非 200：{set(statuses)}")
    r.expect(statuses[-1] == 403, f"第 121 次返回 {statuses[-1]}")


def ex09(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.server.stop()
    ctx.server.start()
    status, p = ctx.get("/api/summary", t["n1"])
    r.expect(status == 200, f"重启后旧会话={status} {p}")
    qa = ctx.login("u-quality-01")
    data = ctx.ok(ctx.post("/api/audit/verify", qa), "校验审计链")
    r.expect(data.get("ok") is True, f"审计链校验={data}")


CASES = [
    ("RC-01", "草稿 9 字段与生成器标识", "P0", "9 字段，含 draft_engine_v1", rc01),
    ("RC-02", "无凭证不可确认", "P0", "403", rc02),
    ("RC-03", "凭证确认落地", "P0", "草稿 confirmed/排程 completed/登记 1 条", rc03),
    ("RC-04", "改字段后凭证失效", "P0", "403", rc04),
    ("RC-05", "凭证重放拒绝", "P0", "403", rc05),
    ("RC-06", "过期凭证拒绝", "P0", "403 且提示过期", rc06),
    ("RC-07", "凭证换人使用拒绝", "P0", "403", rc07),
    ("RC-08", "重复确认", "P1", "400 已确认", rc08),
    ("RC-09", "作废草稿不归档", "P1", "discarded 且排程未完成", rc09),
    ("RC-10", "登记表版本/差异/签署留痕", "P0", "六项齐全且 diff 有主诉", rc10),
    ("RC-11", "医生不能代替护士确认", "P0", "403", rc11),
    ("TK-01", "工单处置需凭证", "P0", "403", tk01),
    ("TK-02", "凭证处置成功且不写登记表", "P0", "approved/doctor/登记 0", tk02),
    ("TK-03", "resolved_by 取会话", "P1", "忽略请求体伪造值", tk03),
    ("TK-04", "重复处置拒绝", "P1", "400 已处置", tk04),
    ("TK-05", "护士不能处置", "P0", "403", tk05),
    ("TK-06", "专科签发模板", "P0", "版本 1.0/生效当日/审计含版本变化", tk06),
    ("TK-07", "护士不能签发", "P0", "403", tk07),
    ("TK-08", "重复签发拒绝且不前进版本", "P1", "400 且版本保持 1.0", tk08),
    ("TK-09", "签发后同日重跑补排且清理人工原因", "P1", "0104 排程且 manual_reason 为空", tk09),
    ("EX-01", "导入中途异常整批回滚", "P0", "无半批数据", ex01),
    ("EX-02", "审计写失败整体回滚", "P0", "503 audit_unavailable，零落库", ex02),
    ("EX-03", "并发确认不产生重复登记", "P0", "同凭证 1 成功 1 拒绝；双凭证仅 1 条", ex03),
    ("EX-D01", "双凭证并发确认原子化", "P0", "8 并发仅 1 成功，其余 403/409，登记 1 条", ex_d01),
    ("EX-04", "未知路由先鉴权", "P1", "未登录 401，已登录 404", ex04),
    ("EX-05", "坏 JSON", "P1", "400 bad_json", ex05),
    ("EX-06", "300KB 报文", "P0", "413 body_too_large", ex06),
    ("EX-07", "40 层嵌套", "P1", "400 too_deep", ex07),
    ("EX-08", "频率限制", "P0", "第 121 次 403", ex08),
    ("EX-09", "重启后会话与审计链", "P0", "旧会话可用，链校验通过", ex09),
]
