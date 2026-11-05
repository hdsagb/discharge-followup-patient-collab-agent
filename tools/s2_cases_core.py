"""S2 用例（一）：数据契约、排程、触达、红旗。"""
from __future__ import annotations

import json
import sqlite3

from s2_lib import DATE, BENIGN


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
    base = {"inpatient_no": "T9001", "name": "测试患者", "department": "骨科一病区",
            "discharge_time": f"{DATE}T10:00:00", "procedure_code": "PG-ORTHO-THR",
            "contact_phone": "13800138000", "contact_type": "本人", "auth_status": "已授权",
            "synthetic": 1}
    base.update(kw)
    return base


# ---------- 5.1 数据契约 ----------

def ct01(ctx, r):
    t = tokens(ctx)
    data = ctx.import_fixture(t["eng"])
    r.expect(data.get("accepted") == 10 and data.get("rejected") == 0, f"导入结果 {data}")
    items = ctx.audit_items(t["qa"])
    hit = [x for x in items if x.get("action") == "batch.import"]
    r.expect(bool(hit), "未找到 batch.import 审计")
    if hit:
        r.note(f"审计 seq={hit[0]['seq']} detail={json.dumps(hit[0].get('detail'), ensure_ascii=False)}")


def ct02(ctx, r):
    t = tokens(ctx)
    b1 = ctx.import_fixture(t["eng"])
    b2 = ctx.import_fixture(t["eng"], "his_batch_v2_pending.json")
    r.expect(b2.get("accepted") == 1 and b2.get("merged") == 3 and b2.get("rejected") == 1,
             f"合并结果 {b2}")
    r.expect(b2.get("dropped_fields") == ["address", "id_card"], f"丢弃字段 {b2.get('dropped_fields')}")
    with ctx.db() as conn:
        cols = [x["name"] for x in conn.execute("PRAGMA table_info(patients)")]
        r.expect("address" not in cols and "id_card" not in cols, f"患者表出现敏感字段 {cols}")
        p = conn.execute("SELECT * FROM patients WHERE inpatient_no=?",
                         (ctx.patient_no("0101"),)).fetchone()
        r.expect(p is not None and p["batch_version"] == b2["batch_version"],
                 "v2 未覆盖 0101 的批次版本")
        blob = json.dumps([dict(x) for x in conn.execute("SELECT * FROM patients")], ensure_ascii=False)
        r.expect("110101199001011234" not in blob and "某市朝阳区" not in blob, "库内仍存地址/证件号原值")
    r.note(f"v1={b1.get('accepted')} v2={b2}")


def ct03(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    reason = ctx.manual_reason(t["eng"], "0105")
    r.expect("术式代码缺失" in reason, f"0105 人工原因={reason!r}")
    r.expect(ctx.item(t["eng"], "0105") is None, "0105 仍被自动排程")


def ct04(ctx, r):
    t = tokens(ctx)
    bad = row(inpatient_no="T9002")
    bad.pop("contact_phone")
    data = ctx.import_rows(t["eng"], "T-CT04", [bad])
    r.expect(data.get("accepted") == 0 and data.get("rejected") == 1, f"结果 {data}")
    text = " ".join(data.get("reject_reasons", []))
    r.expect("缺必填字段" in text and "contact_phone" in text, f"拒绝原因={text}")


def ct05(ctx, r):
    t = tokens(ctx)
    rows = [row(inpatient_no="T9003", contact_phone="138-0013-8000"),
            row(inpatient_no="T9004", contact_phone="1234")]
    data = ctx.import_rows(t["eng"], "T-CT05", rows)
    r.expect(data.get("accepted") == 1 and data.get("rejected") == 1, f"结果 {data}")
    with ctx.db() as conn:
        p = conn.execute("SELECT contact_phone FROM patients WHERE inpatient_no='T9003'").fetchone()
        r.expect(p["contact_phone"] == "13800138000", f"号码未清洗：{p['contact_phone']}")
        p2 = conn.execute("SELECT COUNT(*) n FROM patients WHERE inpatient_no='T9004'").fetchone()
        r.expect(p2["n"] == 0, "短号 1234 被入库")
    r.expect("联系电话格式无效" in " ".join(data.get("reject_reasons", [])), "缺号码格式拒绝原因")


def ct06(ctx, r):
    t = tokens(ctx)
    data = ctx.import_rows(t["eng"], "T-CT06", [row(inpatient_no="T9005", auth_status="未知")])
    r.expect(data.get("accepted") == 0 and data.get("rejected") == 1, f"结果 {data}")
    r.expect("授权状态非法" in " ".join(data.get("reject_reasons", [])), "缺授权状态拒绝原因")


def ct07(ctx, r):
    t = tokens(ctx)
    first = ctx.import_fixture(t["eng"])
    second = ctx.import_fixture(t["eng"])
    r.expect(second.get("idempotent") is True, f"第二次导入 {second}")
    r.expect(second.get("accepted") == 10 and second.get("rejected") == 0, f"第二次计数 {second}")
    with ctx.db() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM patients").fetchone()["n"]
        r.expect(n == 10, f"患者数={n}")


def ct08(ctx, r):
    t = tokens(ctx)
    rows = [row(inpatient_no="T9006", discharge_time=f"{DATE}T09:00:00"),
            row(inpatient_no="T9006", discharge_time=f"{DATE}T11:00:00",
                procedure_code="PG-ORTHO-TKR")]
    data = ctx.import_rows(t["eng"], "T-CT08", rows)
    r.expect(data.get("accepted") == 2 and data.get("rejected") == 0, f"结果 {data}")
    with ctx.db() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM patients WHERE inpatient_no='T9006'").fetchone()["n"]
        r.expect(n == 2, f"两条同名同住院号记录只落了 {n} 条")


def ct09(ctx, r):
    t = tokens(ctx)
    data = ctx.ok(ctx.get("/api/fixtures", t["eng"]), "查询替身")
    names = data.get("fixtures", data if isinstance(data, list) else [])
    r.expect(bool(names), f"替身清单 {data}")
    r.expect(all("/" not in n and "\\" not in n for n in names), f"清单泄漏绝对路径 {names}")
    r.note(f"fixtures={names}")


# ---------- 5.2 排程 ----------

def sc01(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    s = ctx.run_schedule(t["n1"])
    r.expect(s.get("created") == 6, f"created={s.get('created')} {s}")
    r.expect(s.get("excluded_unauthorized") == 2, f"excluded={s.get('excluded_unauthorized')}")
    r.expect(s.get("manual") == 2, f"manual={s.get('manual')}")
    for no in ("0103", "0107"):
        r.expect(ctx.item(t["eng"], no) is None, f"{no} 未授权仍被排程")


def sc02(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    reason = ctx.manual_reason(t["eng"], "0104")
    r.expect(reason == "模板未签发（PG-ORTHO-FIX），转人工", f"0104 人工原因={reason!r}")
    r.expect(ctx.item(t["eng"], "0104") is None, "0104 仍被自动排程")


def sc03(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    for no, slot in (("0102", "evening"), ("0101", "daytime")):
        it = ctx.item(t["eng"], no)
        r.expect(it and it["slot"] == slot, f"{no} slot={it and it['slot']}")


def sc04(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    first = ctx.run_schedule(t["n1"])
    second = ctx.run_schedule(t["n1"])
    r.expect(second.get("created") == 0 and second.get("skipped_duplicate") == 6, f"第二次={second}")
    with ctx.db() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM schedules WHERE schedule_date=?", (DATE,)).fetchone()["n"]
        r.expect(n == first.get("created"), f"排程重复：{n}")


def sc05(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.ok(ctx.post("/api/his/import", t["eng"], {"simulate": "timeout"}), "模拟超时")
    with ctx.db() as conn:
        b = conn.execute("SELECT status FROM batches ORDER BY imported_at DESC, batch_version DESC LIMIT 1").fetchone()
        r.expect(b["status"] == "pending_backfill", f"批次状态={b['status']}")
        n = conn.execute("SELECT COUNT(*) n FROM patients").fetchone()["n"]
        r.expect(n == 10, f"沿用名单患者数={n}")
    ctx.run_schedule(t["n1"])
    audit = ctx.audit_items(t["qa"])
    r.expect(any(x.get("action") == "batch.fallback" for x in audit), "缺 batch.fallback 审计")


def sc06(ctx, r):
    t = tokens(ctx)
    rows = [row(inpatient_no="T9007"), row(inpatient_no="T9008", contact_type="家属")]
    ctx.import_rows(t["eng"], "T-SC06", rows, exported_at="2026-11-02T10:00:00")
    s = ctx.run_schedule(t["n1"])
    r.expect(s.get("created") == 0 and s.get("stale_manual") == 2, f"超时名单={s}")
    r.expect("名单超 24 小时" in ctx.manual_reason(t["eng"], "T9007"), "缺超时转人工原因")
    r.expect(ctx.item(t["eng"], "T9007") is None, "超时名单仍被排程")


def sc07(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    with ctx.db() as conn:
        conn.execute("UPDATE templates SET effective_date='2026-12-01' WHERE procedure_code='PG-ORTHO-THR'")
        conn.commit()
    s = ctx.run_schedule(t["n1"])
    r.expect("尚未生效" in ctx.manual_reason(t["eng"], "0101"), "0101 未体现模板未生效")
    r.expect("尚未生效" in ctx.manual_reason(t["eng"], "0108"), "0108 未体现模板未生效")
    r.expect("尚未生效" in ctx.manual_reason(t["eng"], "0110"), "0110 未体现模板未生效")
    r.expect(s.get("manual") == 5, f"manual={s.get('manual')}（应为 0101/0108/0110 三个 THR + 0104/0105）")
    it = ctx.item(t["eng"], "0102")
    r.expect(it is not None, "TKR 排程未生成")


def sc08(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    n1_items = ctx.board(t["n1"])["items"]
    n2_items = ctx.board(t["n2"])["items"]
    own = [x for x in n2_items if x["inpatient_no"] == ctx.patient_no("0106")]
    r.expect(own and own[0]["nurse_id"] == "u-nurse-02",
             f"0106 未按病区分配给二病区护士：{own}")
    r.expect(all(x["inpatient_no"] != ctx.patient_no("0106") for x in n1_items),
             "一病区护士看到其他病区且非本人排程的 0106")
    # 显式构造本人名下的跨病区排程，验证 nurse_id 口径可见性
    sid_cross = ctx.insert_schedule("0106", nurse_id="u-nurse-01")
    n1_ids = {x["id"] for x in ctx.board(t["n1"])["items"]}
    n2_ids = {x["id"] for x in ctx.board(t["n2"])["items"]}
    r.expect(sid_cross in n1_ids, "本人承接的跨病区排程不可见")
    r.expect(sid_cross in n2_ids, "同病区排程应因病区口径可见（nurse_id OR department）")
    with ctx.db() as conn:
        mismatch = conn.execute(
            "SELECT COUNT(*) n FROM schedules s JOIN patients p ON p.id=s.patient_id "
            "JOIN users u ON u.user_id=s.nurse_id "
            "WHERE u.ward != p.department AND s.id != ?", (sid_cross,)).fetchone()["n"]
    r.expect(mismatch == 0, f"存在非显式构造的跨病区排程 {mismatch} 条")
    for x in ctx.board(t["n1"])["items"] + ctx.board(t["n2"])["items"]:
        r.expect("*" in x["contact_phone"] and len(x["contact_phone"]) == 9,
                 f"手机号未脱敏：{x['contact_phone']}")


def sc09(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0106")["id"]
    status, p = ctx.post(f"/api/schedule/{sid}/reorder", t["n1"], {"direction": "up"})
    r.expect(status == 403, f"跨护士调整返回 {status} {p}")


def sc10(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    from concurrent.futures import ThreadPoolExecutor
    def read(tok):
        return [ctx.get(f"/api/schedule?date={DATE}", tok)[0] for _ in range(4)]
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(read, [t["n1"], t["n2"], t["doc"], t["mgr"], t["qa"]]))
    flat = [x for group in results for x in group]
    r.expect(all(x == 200 for x in flat), f"并发读出现非 200：{flat}")


# ---------- 5.3 触达 ----------

def au01(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    data = ctx.ok(ctx.call(t["n1"], sid, True), "外呼")
    r.expect(data.get("recording") == "active", f"录音状态={data}")
    rec = ctx.recordings(sid)[0]
    r.expect(rec["status"] == "active" and rec["consent"] == 1, f"录音行={rec}")
    audit = ctx.audit_items(t["qa"])
    hit = [x for x in audit if x.get("action") == "call.start"]
    r.expect(bool(hit) and "告知" in json.dumps(hit[0], ensure_ascii=False), "缺外呼告知审计")


def au02(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.insert_schedule("0103")
    status, p = ctx.call(t["n1"], sid)
    r.expect(status == 403 and p.get("code") == "forbidden", f"未授权外呼返回 {status} {p}")
    r.expect(not ctx.recordings(sid), "未授权仍创建录音")
    audit = ctx.audit_items(t["qa"])
    r.expect(any(x.get("action") == "call.denied_unauthorized" for x in audit), "缺拒绝审计")


def au03(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.insert_schedule("0107")
    status, p = ctx.call(t["n1"], sid)
    r.expect(status == 403, f"撤回患者外呼返回 {status} {p}")
    r.expect(not ctx.recordings(sid), "撤回患者仍创建录音")


def au04(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    ctx.call(t["n1"], sid)
    last = None
    for _ in range(3):
        last = ctx.ok(ctx.post(f"/api/contact/{sid}/attempt", t["n1"],
                               {"channel": "call", "result": "no_answer"}), "记录未接")
    r.expect(last.get("attempts") == 3 and last.get("status") == "shelved", f"三次未接={last}")
    it = ctx.item(t["eng"], "0101")
    r.expect(it is not None and it["status"] == "shelved", "未接患者不在人工可见清单")


def au05(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    ctx.call(t["n1"], sid)
    data = ctx.ok(ctx.post(f"/api/contact/{sid}/attempt", t["n1"],
                           {"channel": "sms", "result": "sent"}), "发短信")
    r.expect(data.get("attempts") == 0 and data.get("counts_toward_completion") is False,
             f"短信计数={data}")
    summary = ctx.ok(ctx.get("/api/summary", t["mgr"]), "看板")
    touch = summary.get("sms_touchpoints") or {}
    r.expect(isinstance(touch, dict) and touch.get("sms", 0) >= 1,
             f"短信未进入顶层独立统计：{touch}")
    r.expect((summary.get("stats") or {}).get("sms_attempts", 0) >= 1,
             f"按日短信统计缺失：{summary.get('stats')}")


def au06(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    ctx.call(t["n1"], sid, True)
    step = ctx.stepup(t["n1"], "auth_revoke", str(sid))["token"]
    data = ctx.ok(ctx.post(f"/api/contact/{sid}/revoke", t["n1"],
                           {"token": step, "reason": "患者电话撤回授权"}), "撤回授权")
    r.expect(data.get("stopped_schedules", 0) >= 1, f"停排数={data}")
    r.expect(ctx.recordings(sid)[0]["status"] == "revoked_do_not_train", "历史录音未标记不得训练")
    with ctx.db() as conn:
        p = conn.execute("SELECT auth_status FROM patients WHERE inpatient_no=?",
                         (ctx.patient_no("0101"),)).fetchone()
        r.expect(p["auth_status"] == "撤回", f"授权状态={p['auth_status']}")
    r.expect(ctx.item(t["eng"], "0101") is None or ctx.item(t["eng"], "0101")["status"] == "shelved",
             "撤回后排程仍在自动队列")


def au07(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid, draft = ctx.draft_for(t["n1"], t["eng"], "0101")
    step = ctx.stepup(t["n1"], "auth_revoke", str(sid))["token"]
    ctx.ok(ctx.post(f"/api/contact/{sid}/revoke", t["n1"], {"token": step, "reason": "撤回"}), "撤回")
    status, p = ctx.call(t["n1"], sid)
    r.expect(status == 403, f"撤回后再次外呼={status} {p}")
    step2 = ctx.stepup(t["n1"], "record_confirm", str(draft["draft_id"]),
                       fields=draft["fields"])["token"]
    status2, p2 = ctx.confirm(t["n1"], draft["draft_id"], draft["fields"], step2)
    r.expect(status2 == 403, f"撤回后仍可确认：HTTP {status2} {p2}")
    if status2 == 200:
        r.note("已复现：确认链路未校验授权状态")


def au08(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    ctx.call(t["n1"], sid)
    step = ctx.stepup(t["n1"], "auth_revoke", str(sid))["token"]
    ctx.ok(ctx.post(f"/api/contact/{sid}/revoke", t["n1"],
                    {"token": step, "reason": "明确拒绝随访"}), "撤回")
    items = ctx.audit_items(t["qa"])
    hit = [x for x in items if x.get("action") == "auth.revoke"]
    r.expect(bool(hit), "缺 auth.revoke 审计")
    if hit:
        detail = json.dumps(hit[0].get("detail"), ensure_ascii=False)
        r.expect("明确拒绝随访" in detail and "schedules_stopped" in detail, f"审计 detail={detail}")
    try:
        with ctx.db() as conn:
            conn.execute("UPDATE audit_events SET action='tampered' WHERE seq=1")
            conn.commit()
        r.expect(False, "审计记录可被 UPDATE")
    except sqlite3.DatabaseError as exc:
        r.note(f"审计 UPDATE 被拒：{exc}")


def au09(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    data = ctx.ok(ctx.call(t["n1"], sid, False), "未告知外呼")
    r.expect(data.get("recording") == "not_recorded", f"录音状态={data}")
    rec = ctx.recordings(sid)[0]
    r.expect(rec["status"] == "not_recorded", f"录音行={rec}")
    audit = ctx.audit_items(t["qa"])
    hit = [x for x in audit if x.get("action") == "call.start"]
    r.expect(bool(hit) and "不录音" in json.dumps(hit[0], ensure_ascii=False), "缺不录音告知审计")


# ---------- 5.4 红旗 ----------

def rf01(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid, data = ctx.ticket_for(t["n1"], t["eng"])
    ids = [h["rule_id"] for h in data["hits"]]
    r.expect(data.get("interrupted") is True and "RF-ORTHO-03" in ids, f"命中={data}")
    r.expect(data.get("tickets_created") == 1, f"建单数={data.get('tickets_created')}")
    drafts = ctx.ok(ctx.get("/api/drafts", t["n1"]), "草稿")["items"]
    r.expect(not drafts, f"红旗命中仍生成草稿 {len(drafts)}")
    ticket = ctx.ok(ctx.get("/api/tickets", t["doc"]), "工单")["items"][0]
    for key in ("rule_id", "channel", "confidence", "trigger_text", "status", "patient_id"):
        r.expect(key in ticket and ticket[key] not in (None, ""), f"工单缺字段 {key}")
    it = ctx.item(t["eng"], "0101")
    r.expect(it and it["status"] == "escalated", "排程未升级")


def rf02(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    ctx.call(t["n1"], sid)
    data = ctx.ok(ctx.check(t["n1"], sid, "没有发烧，也不咳嗽"), "红旗")
    r.expect(data.get("interrupted") is False and not data.get("hits"),
             f"否定句误报 interrupted={data.get('interrupted')} hits={data.get('hits')}")
    r.expect(bool(data.get("draft")), "否定句未生成草稿")


def rf03(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    ctx.call(t["n1"], sid)
    data = ctx.ok(ctx.check(t["n1"], sid, "膝盖不咋个痛了，但是喘不上气"), "红旗")
    ids = [h["rule_id"] for h in data.get("hits", [])]
    r.expect("RF-ORTHO-01" in ids, f"方言否定句未命中真正红旗 {ids}")
    r.expect(len(ids) == 1, f"多报 {ids}")


def rf04(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    ctx.call(t["n1"], sid)
    data = ctx.ok(ctx.check(t["n1"], sid, "莫得发烧，就是伤口有点痛"), "红旗")
    ids = [h["rule_id"] for h in data.get("hits", [])]
    r.expect(ids == [], f"阴性句误报 {ids}")
    if ids:
        r.note("已复现 RF-D01：模糊变体跨过否定词窗口")


def rf05(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    ctx.call(t["n1"], sid)
    data = ctx.ok(ctx.check(t["n1"], sid, "吃止痛药也不管用"), "红旗")
    hit = next((h for h in data.get("hits", []) if h["rule_id"] == "RF-ORTHO-06"), None)
    r.expect(hit and hit["evidence"] == "吃止痛药也不管用" and hit["confidence"] >= 0.6,
             f"未识别变体 {data.get('hits')}")


def rf06(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    bad = []
    for no, text in (("0101", "伤口没有裂开"), ("0102", "小腿没有肿"), ("0108", "没有晕倒")):
        sid = ctx.item(t["eng"], no)["id"]
        ctx.call(t["n1"], sid)
        data = ctx.ok(ctx.check(t["n1"], sid, text), "红旗")
        if data.get("hits"):
            bad.append((text, [h["rule_id"] for h in data["hits"]]))
    r.expect(not bad, f"否定式误报 {bad}")
    if bad:
        r.note("已复现 RF-D02：正则通道未做否定判定")


def rf07(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    ctx.call(t["n1"], sid)
    first = ctx.ok(ctx.check(t["n1"], sid, "伤口渗液，有点臭"), "首次")
    second = ctx.ok(ctx.check(t["n1"], sid, "伤口流脓了"), "二次")
    r.expect(first.get("tickets_created") == 1, f"首次建单={first.get('tickets_created')}")
    r.expect(second.get("tickets_created") == 0 and "RF-ORTHO-03" in second.get("deduped", []),
             f"二次去重={second}")
    with ctx.db() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM tickets WHERE schedule_id=?", (sid,)).fetchone()["n"]
        r.expect(n == 1, f"工单数={n}")


def rf08(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    ctx.ticket_for(t["n1"], t["eng"])
    drafts = ctx.ok(ctx.get("/api/drafts", t["n1"]), "草稿")["items"]
    r.expect(drafts == [], f"命中后仍有草稿 {len(drafts)}")
    ticket = ctx.ok(ctx.get("/api/tickets", t["doc"]), "工单")["items"][0]
    nums = [ticket.get("rule_id"), ticket.get("channel"), ticket.get("trigger_text")]
    r.expect(all(nums), f"工单取证字段缺失 {nums}")


def rf09(ctx, r):
    t = tokens(ctx)
    data = ctx.ok(ctx.get("/api/rules", t["n1"]), "规则")
    threshold = (data.get("model_channel") or {}).get("min_confidence")
    r.expect(threshold == 0.6 and data.get("version") == "1.0", f"规则元数据 {data}")
    rules = data.get("rules", [])
    r.expect(len(rules) == 6, f"规则数={len(rules)}")
    r.expect(all(x.get("rule_id") and x.get("keywords") and x.get("escalation") for x in rules),
             "规则定义不完整")


def rf10(ctx, r):
    r.blocked = True
    r.failures.append("未建立 ≥120 条独立标注语料，无法计算命中率/误报率（C 类阻塞，见修复方案 4.3）")


def rf11(ctx, r):
    t = tokens(ctx)
    data = ctx.ok(ctx.get("/api/rules", t["n1"]), "规则")
    sig = data.get("signature") or {}
    r.expect(sig.get("verified") is True and sig.get("algorithm") == "hmac-sha256",
             f"规则签名未验证：{sig}")
    r.note(f"加载时已比对 redflag.json.sig，摘要={sig.get('digest')}")


def rf12(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid = ctx.item(t["eng"], "0101")["id"]
    ctx.call(t["n1"], sid)
    empty = ctx.ok(ctx.check(t["n1"], sid, ""), "空陈述")
    r.expect(empty.get("interrupted") is False and empty.get("draft", {}).get("fields"),
             f"空陈述未生成草稿：{empty}")
    long_text = "术后恢复。" * 35000
    status, p = ctx.post("/api/questionnaire/check", t["n1"],
                         {"schedule_id": sid, "transcript": long_text})
    r.expect(status == 413 and p.get("code") == "body_too_large", f"超长文本={status} {p}")


CASES = [
    ("CT-01", "导入 v1 与批次审计", "P0", "accepted=10，审计含 batch.import", ct01),
    ("CT-02", "导入 v2 合并与敏感字段丢弃", "P0", "accepted=1/merged=3/rejected=1，库内无地址证件号", ct02),
    ("CT-03", "术式缺失转人工", "P0", "0105 排程跳过且原因=术式代码缺失", ct03),
    ("CT-04", "缺手机号拒绝", "P0", "rejected=1 且原因含 contact_phone", ct04),
    ("CT-05", "号码清洗与短号拒绝", "P0", "清洗为 11 位，1234 拒绝", ct05),
    ("CT-06", "未知授权状态拒绝", "P0", "rejected=1 且原因含授权状态非法", ct06),
    ("CT-07", "批次幂等", "P0", "二次导入 idempotent=true，患者仍 10", ct07),
    ("CT-08", "同住院号不同出院时间", "P1", "两行均入库", ct08),
    ("CT-09", "替身清单不泄漏路径", "P1", "仅文件名，无路径", ct09),
    ("SC-01", "首轮排程计数", "P0", "created=6/excluded=2/manual=2", sc01),
    ("SC-02", "未签发模板转人工", "P0", "0104 不排程且原因准确", sc02),
    ("SC-03", "晚间优先", "P0", "家属 evening，本人 daytime", sc03),
    ("SC-04", "同日重跑幂等", "P0", "第二次 created=0/skipped=6", sc04),
    ("SC-05", "超时沿用上一版本名单", "P0", "批次 pending_backfill，名单保留", sc05),
    ("SC-06", "名单超 24 小时全转人工", "P0", "created=0/stale_manual=2", sc06),
    ("SC-07", "模板尚未生效转人工", "P1", "0101 转人工", sc07),
    ("SC-08", "护士仅见本科室与非本科室本人排程且脱敏", "P0",
     "0106 由二病区护士承接；显式跨病区本人排程可见、他院不可见，手机号 3+4+2", sc08),
    ("SC-09", "跨护士调整应拒绝", "P1", "返回 403", sc09),
    ("SC-10", "并发读看板", "P2", "5 并发无 5xx", sc10),
    ("AU-01", "已授权外呼与录音", "P0", "录音 active，审计含告知语", au01),
    ("AU-02", "未授权外呼拒绝", "P0", "403 且无录音、有拒绝审计", au02),
    ("AU-03", "撤回授权外呼拒绝", "P0", "403 且无录音", au03),
    ("AU-04", "三次未接搁置", "P0", "attempts=3/status=shelved", au04),
    ("AU-05", "短信不计完成率", "P0", "attempts 不变，独立计数", au05),
    ("AU-06", "撤回授权停排并标记录音", "P0", "排程 shelved，录音不得训练", au06),
    ("AU-07", "撤回后禁呼禁确认", "P0", "外呼/确认均 403", au07),
    ("AU-08", "撤回审计与不可篡改", "P0", "审计含原因，UPDATE 被拒", au08),
    ("AU-09", "未告知不录音", "P0", "recordings=not_recorded", au09),
    ("RF-01", "红旗命中中断问卷并建单", "P0", "RF-ORTHO-03，无草稿，排程升级", rf01),
    ("RF-02", "常规否定句不误报", "P0", "hits 为空", rf02),
    ("RF-03", "方言否定句仍命中真正红旗", "P0", "仅 RF-ORTHO-01", rf03),
    ("RF-04", "方言阴性句不误报", "P0", "hits 为空", rf04),
    ("RF-05", "变体识别", "P0", "RF-ORTHO-06/model/1.0", rf05),
    ("RF-06", "否定式正则不误报", "P1", "三条均无命中", rf06),
    ("RF-07", "同规则去重", "P0", "二次 tickets_created=0", rf07),
    ("RF-08", "命中不生成草稿且工单可取证", "P0", "无草稿，字段齐全", rf08),
    ("RF-09", "规则元数据与版本", "P1", "threshold=0.6/version=1.0/6 条", rf09),
    ("RF-10", "120 条语料命中率/误报率", "P0", "≥95%/≤5%", rf10),
    ("RF-11", "规则文件不可静默篡改", "P1", "加载时验签通过，签名元数据可见", rf11),
    ("RF-12", "空陈述与超长文本", "P2", "空陈述可草稿，超长 413", rf12),
]
