"""S3 用例（二）：第六节 安全专项（SEC-01～12）。"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta

import s2_lib
from s2_lib import DATE
from s3_lib import field_digest, make_draft, make_record, row, tokens

HOOK = '''
import os
import sys

_log = os.environ.get("FDE_EGRESS_LOG")


def _hook(event, args):
    if not _log:
        return
    line = ""
    if event == "socket.connect":
        addr = args[1] if len(args) > 1 else ""
        host = addr[0] if isinstance(addr, tuple) and addr else str(addr)
        line = "CONNECT %s\\n" % (host,)
    elif event == "socket.getaddrinfo":
        line = "DNS %s\\n" % (args[0],)
    if line:
        with open(_log, "a", encoding="utf-8") as fh:
            fh.write(line)


sys.addaudithook(_hook)
'''


def sec01(ctx, r):
    """运行期零外联：审计钩子记录服务端的 socket.connect / DNS 事件。"""
    ctx.server.stop()
    hook_dir = ctx.server.dir / "hook"
    hook_dir.mkdir(exist_ok=True)
    (hook_dir / "sitecustomize.py").write_text(HOOK, encoding="utf-8")
    egress = ctx.server.dir / "egress.log"
    ctx.server.extra_env = {"PYTHONPATH": str(hook_dir), "FDE_EGRESS_LOG": str(egress)}
    ctx.server.start()

    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    make_record(ctx, t["n1"], t["eng"], "0101")
    ctx.ok(ctx.post("/api/audit/verify", t["qa"]), "验链")
    ctx.ok(ctx.get("/api/summary", t["qa"]), "汇总")
    ctx.get("/")
    ctx.get("/app.js")

    lines = egress.read_text(encoding="utf-8").splitlines() if egress.exists() else []
    connect = [x for x in lines if x.startswith("CONNECT")]
    dns = [x for x in lines if x.startswith("DNS")]
    r.expect(not connect, f"服务端发生出站连接：{connect[:5]}")
    r.expect(not dns, f"服务端发生域名解析：{dns[:5]}")
    r.note(f"审计钩子事件：CONNECT={len(connect)} DNS={len(dns)}（日志 {'存在' if lines else '为空'}）")

    srcs = list((s2_lib.ROOT / "backend").rglob("*.py"))
    forbidden = ("import requests", "urllib.request", "http.client", "httpx",
                 "socket.create_connection", "openai", "anthropic")
    hits = []
    for path in srcs:
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in forbidden:
            if token in text:
                hits.append(f"{path.name}:{token}")
    r.expect(not hits, f"后端源码出现外部网络客户端：{hits}")
    r.note(f"静态扫描 {len(srcs)} 个后端源文件，未发现外部模型/短信/HTTP 客户端引用" if not hits else f"命中 {hits}")


def sec02(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid, _ = make_draft(ctx, t["n1"], t["eng"], "0101")

    def fresh_draft():
        data = ctx.ok(ctx.check(t["n1"], sid, s2_lib.BENIGN), "生成草稿")
        return data["draft"]

    def attempt(name, mode, session=None):
        d = fresh_draft()
        did, fields = d["draft_id"], d["fields"]
        dg = field_digest(did, fields)
        exp = int(time.time()) + 300
        good = ctx.stepup(t["n1"], "record_confirm", str(did), fields=fields)["token"]
        raw, sig = good.split(".", 1)
        if mode == "sig":
            token, out_fields = f"{raw}." + "0" * len(sig), fields
        elif mode == "expired":
            token = s2_lib.forge_token("u-nurse-01", "record_confirm", str(did), dg,
                                       f"nonce-exp-{did}", int(time.time()) - 60)
            out_fields = fields
        elif mode == "purpose":
            token = s2_lib.forge_token("u-nurse-01", "auth_revoke", str(did), dg,
                                       f"nonce-purpose-{did}", exp)
            out_fields = fields
        elif mode == "user":
            token = s2_lib.forge_token("u-nurse-01", "record_confirm", str(did), dg,
                                       f"nonce-user-{did}", exp)
            out_fields = fields
        else:
            token = good
            out_fields = {**fields, next(iter(fields)): "被篡改的值"}
        status, p = ctx.confirm(session or t["n1"], did, out_fields, token)
        r.expect(status == 403, f"{name}返回 HTTP {status} {p}")
        r.note(f"{name}: HTTP {status} code={p.get('code')} error={p.get('error')}")
        with ctx.db() as conn:
            st = conn.execute("SELECT status FROM drafts WHERE id=?", (did,)).fetchone()["status"]
            n = conn.execute("SELECT COUNT(*) n FROM records WHERE draft_id=?", (did,)).fetchone()["n"]
        r.expect(st == "draft", f"{name}后草稿状态被改变：{st}")
        r.expect(n == 0, f"{name}后仍写入登记表：{n} 条")

    attempt("签名篡改", "sig")
    attempt("凭证过期", "expired")
    attempt("用途不符", "purpose")
    attempt("用户不符", "user", session=t["n2"])
    attempt("内容不符", "content")


def sec03(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, draft = make_draft(ctx, t["n1"], t["eng"], "0101")
    did, fields = draft["draft_id"], draft["fields"]
    step = ctx.stepup(t["n1"], "record_confirm", str(did), fields=fields)["token"]
    first = ctx.confirm(t["n1"], did, fields, step)
    r.expect(first[0] == 200, f"首次确认失败：{first}")
    replay = ctx.confirm(t["n1"], did, fields, step)
    r.expect(replay[0] == 403, f"凭证重放返回 HTTP {replay[0]} {replay[1]}")
    r.note(f"重放实际：HTTP {replay[0]} code={replay[1].get('code')} error={replay[1].get('error')}")
    with ctx.db() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM records WHERE draft_id=?", (did,)).fetchone()["n"]
    r.expect(n == 1, f"重放导致重复登记：{n} 条")


def sec04(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    make_record(ctx, t["n1"], t["eng"], "0101")
    make_draft(ctx, t["n1"], t["eng"], "0109")
    records = ctx.ok(ctx.get("/api/records", t["n2"]), "二病区读登记表")["items"]
    rec_nos = {x["inpatient_no"] for x in records}
    r.expect(ctx.patient_no("0101") not in rec_nos, f"二病区读到一病区登记：{sorted(rec_nos)}")
    drafts = ctx.ok(ctx.get("/api/drafts", t["n2"]), "二病区读草稿")["items"]
    draft_nos = {x["inpatient_no"] for x in drafts}
    r.expect(ctx.patient_no("0109") not in draft_nos, f"二病区读到一病区草稿：{sorted(draft_nos)}")
    r.note(f"/api/records 共 {len(records)} 条、/api/drafts 共 {len(drafts)} 条，均未按病区过滤")


def sec05(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    msg_update = msg_delete = ""
    with ctx.db() as conn:
        try:
            conn.execute("UPDATE audit_events SET detail_json='{}' WHERE seq=2")
        except sqlite3.DatabaseError as exc:
            msg_update = str(exc)
        try:
            conn.execute("DELETE FROM audit_events WHERE seq=2")
        except sqlite3.DatabaseError as exc:
            msg_delete = str(exc)
        conn.rollback()
    r.expect("append-only" in msg_update, f"UPDATE 未被库层拒绝：{msg_update!r}")
    r.expect("append-only" in msg_delete, f"DELETE 未被库层拒绝：{msg_delete!r}")
    with ctx.db() as conn:
        conn.execute("DROP TRIGGER IF EXISTS audit_events_no_update")
        conn.execute("UPDATE audit_events SET detail_json='{\"tampered\":true}' WHERE seq=2")
        conn.commit()
    status, p = ctx.post("/api/audit/verify", t["qa"])
    data = p.get("data", {})
    r.expect(status == 200, f"验链接口异常：{status} {p}")
    r.expect(data.get("ok") is False, f"篡改后验链仍通过：{data}")
    r.expect(data.get("broken_at_seq") == 2, f"断链位置={data.get('broken_at_seq')}，期望 2")
    r.note("库层触发器可被具备文件权限者 DROP 后改写，验链能检出断链；防篡改依赖院内部署权限管控")


def sec06(ctx, r):
    t = tokens(ctx)
    before_rows = len(ctx.audit_items(t["qa"]))
    with ctx.db() as conn:
        conn.execute("CREATE TRIGGER sec06_block BEFORE INSERT ON audit_events "
                     "BEGIN SELECT RAISE(ABORT, 'disk full'); END")
        conn.commit()
    with ctx.db() as conn:
        before_sessions = conn.execute(
            "SELECT COUNT(*) n FROM sessions WHERE user_id='u-manager-01'").fetchone()["n"]
    status, p = ctx.post("/api/login", body={"user_id": "u-manager-01"})
    r.expect(status == 503, f"留痕写失败返回 HTTP {status} {p}")
    r.expect(p.get("code") == "audit_unavailable", f"错误码={p.get('code')}")
    with ctx.db() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM sessions WHERE user_id='u-manager-01'").fetchone()["n"]
    r.expect(n == before_sessions, f"留痕失败后会话仍被持久化：{before_sessions}→{n}")
    after_rows = len(ctx.audit_items(t["qa"]))
    r.expect(after_rows == before_rows, f"审计行数变化：{before_rows}→{after_rows}")
    log = ctx.server.read_log()
    r.expect("Traceback" not in log, "503 场景仍打印堆栈")
    r.note(f"留痕失败时业务（登录）已回滚，会话数保持 {before_sessions}；服务端日志无堆栈（尾部 {len(log)} 字符已核查）")


def sec07(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid7 = ctx.insert_schedule("0107")
    status7, p7 = ctx.call(t["n1"], sid7)
    r.expect(status7 == 403, f"撤回授权患者仍可外呼：HTTP {status7} {p7}")
    r.expect(not ctx.recordings(sid7), "撤回授权患者产生录音行")
    sid = ctx.item(t["eng"], "0101")["id"]
    first = ctx.call(t["n1"], sid, True)
    r.expect(first[0] == 200 and first[1]["data"]["recording"] == "active", f"首次外呼异常：{first}")
    step = ctx.stepup(t["n1"], "auth_revoke", str(sid))
    status, p = ctx.post(f"/api/contact/{sid}/revoke", t["n1"],
                        {"token": step["token"], "reason": "患者要求撤回授权"})
    r.expect(status == 200, f"撤回失败：HTTP {status} {p}")
    recs = ctx.recordings(sid)
    r.expect(all(x["status"] == "revoked_do_not_train" for x in recs),
             f"撤回后录音状态={[x['status'] for x in recs]}")
    with ctx.db() as conn:
        auth = conn.execute("SELECT auth_status FROM patients WHERE inpatient_no=?",
                            (ctx.patient_no("0101"),)).fetchone()["auth_status"]
    r.expect(auth == "撤回", f"患者授权状态={auth}")
    after = ctx.call(t["n1"], sid, True)
    r.expect(after[0] == 403, f"撤回后仍可外呼：HTTP {after[0]} {after[1]}")
    old = (datetime.now() - timedelta(hours=25)).isoformat(timespec="seconds")
    with ctx.db() as conn:
        conn.execute("UPDATE recordings SET at=? WHERE schedule_id=?", (old, sid))
        conn.commit()
    later = ctx.call(t["n1"], sid, True)
    r.expect(later[0] == 403, f"撤回 25 小时后仍可外呼：HTTP {later[0]} {later[1]}")
    r.expect(len(ctx.recordings(sid)) == len(recs), "撤回后又新增录音行")
    r.note("撤回为状态型硬约束（无 24h 到期恢复），因此把录音时间回拨 25 小时后仍拒绝外呼")


def sec08(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    _, draft = make_draft(ctx, t["n1"], t["eng"], "0101")
    with ctx.db() as conn:
        d = conn.execute("SELECT * FROM drafts WHERE id=?", (draft["draft_id"],)).fetchone()
    fields = json.loads(d["fields_json"])
    source = fields.get("草稿来源", "")
    r.expect("本地" in source and "非院外" in source, f"草稿来源标注={source!r}")
    leaked = [k for k in fields if k in ("contact_phone", "phone", "name", "inpatient_no")]
    r.expect(not leaked, f"草稿字段含敏感键：{leaked}")
    r.note(f"草稿字段键={sorted(fields)}；生成器={d['generator']}")
    code = (s2_lib.ROOT / "backend" / "core" / "records.py").read_text(encoding="utf-8")
    r.expect("def _structure(transcript: str, template_title: str)" in code,
             "结构化入口签名与设计不符（疑似引入外部调用参数）")
    for bad in ("requests", "urllib", "http.client", "openai", "anthropic"):
        r.expect(bad not in code, f"草稿生成模块引用外部客户端：{bad}")


def sec09(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    sid, draft = make_draft(ctx, t["n1"], t["eng"], "0101")
    step = ctx.stepup(t["n1"], "record_confirm", str(draft["draft_id"]), fields=draft["fields"])["token"]
    ctx.ok(ctx.confirm(t["n1"], draft["draft_id"], draft["fields"], step), "确认写库")
    ctx.ok(ctx.post("/api/audit/verify", t["qa"]), "验链")
    with ctx.db() as conn:
        phone = conn.execute("SELECT contact_phone FROM patients WHERE inpatient_no=?",
                             (ctx.patient_no("0101"),)).fetchone()["contact_phone"]
        no = ctx.patient_no("0101")
    log = ctx.server.read_log()
    for name, needle in (("会话 token", t["n1"]), ("step-up token", step),
                         ("联系电话", phone), ("住院号", no)):
        r.expect(needle not in log, f"服务端日志泄漏{name}：{needle}")
    r.note(f"已扫描服务端日志 {len(log)} 字符（请求行不含 query 中的患者标识；日志仅含方法/路径/状态）")


def sec10(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    make_record(ctx, t["n1"], t["eng"], "0101")
    ctx.ok(ctx.post("/api/audit/verify", t["qa"]), "验链")
    items = ctx.audit_items(t["qa"], limit=500)
    blob = json.dumps(items, ensure_ascii=False)
    r.expect("prev_hash" not in blob, "审计接口返回链头 prev_hash")
    with ctx.db() as conn:
        phone = conn.execute("SELECT contact_phone FROM patients WHERE inpatient_no=?",
                             (ctx.patient_no("0101"),)).fetchone()["contact_phone"]
        cols = {x[1] for x in conn.execute("PRAGMA table_info(patients)")}
    r.expect(phone not in blob, f"审计返回联系电话：{phone}")
    r.expect("name" not in cols, "patients 表新增了姓名字段，需追加姓名泄漏校验")
    bad_keys = []
    for it in items:
        detail = it.get("detail") or {}
        for k in detail:
            if k in ("name", "contact_phone", "phone", "id_card", "address"):
                bad_keys.append(f"{it.get('action')}:{k}")
    r.expect(not bad_keys, f"审计 detail 出现敏感字段：{bad_keys}")
    subj = [it["subject"] for it in items if it.get("subject")]
    r.note(f"审计 {len(items)} 条；detail 键位未出现姓名/电话；subject 示例={subj[:2]}")
    boot = ctx.ok(ctx.get("/api/bootstrap"), "公开账号清单")
    r.note("公开 /api/bootstrap 依方案返回 active 账号姓名/角色/病区（已列为允许例外）")


def sec11(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    big = '{"transcript":"' + "x" * 300000 + '"}'
    status_big, p_big = ctx.post("/api/questionnaire/check", t["n1"], raw=big)
    r.expect(status_big in (400, 413), f"300KB 载荷返回 HTTP {status_big} {p_big}")
    deep = {"a": 1}
    for _ in range(40):
        deep = {"a": deep}
    status_deep, p_deep = ctx.post("/api/questionnaire/check", t["n1"], body=deep)
    r.expect(status_deep in (400, 413), f"40 层嵌套返回 HTTP {status_deep} {p_deep}")
    log = ctx.server.read_log()
    r.expect("Traceback" not in log, "超限载荷在服务端日志打印堆栈")
    r.note(f"300KB → HTTP {status_big} {p_big.get('code')}；40 层 → HTTP {status_deep} "
           f"{p_deep.get('code')}；日志含堆栈={'Traceback' in log}")


def sec12(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    real_rows = [
        row(inpatient_no="REAL-0001", name="样例真实患者", contact_phone="13900139000",
            synthetic=0),
        row(inpatient_no="REAL-0002", name="样例真实患者乙", contact_phone="13700137000",
            synthetic=0),
    ]
    data = ctx.import_rows(t["eng"], "SEC12-REAL-BATCH", real_rows)
    r.expect(data.get("accepted") == 0, f"未带 synthetic 标记的真实样式数据被接收：{data}")
    with ctx.db() as conn:
        cols = [x["name"] for x in conn.execute("PRAGMA table_info(patients)")]
        n = conn.execute("SELECT COUNT(*) n FROM patients WHERE inpatient_no LIKE 'REAL-%'").fetchone()["n"]
    r.expect("synthetic" in cols, f"patients 表缺少合成标记列：{cols}")
    r.expect(n == 0, f"真实样式数据入库 {n} 条")
    fixtures = sorted(x.name for x in (s2_lib.ROOT / "backend" / "fixtures").glob("*.json"))
    r.note(f"导入原样接收 {data.get('accepted')} 条、拒绝 {data.get('rejected')} 条；"
           f"patients 含 synthetic 列；现有替身={fixtures}")
    r.note(f"本轮所有用例库均位于临时目录（当前 {ctx.db_path}），未使用真实患者数据")


CASES = [
    ("SEC-01", "运行期零外部网络出口", "P0",
     "服务端运行全流程不产生任何 socket 出站连接/域名解析，源码无外部客户端引用", sec01),
    ("SEC-02", "step-up 凭证五类校验失效场景", "P0",
     "签名篡改、过期、用途不符、用户不符、内容不符均 403，且不写库", sec02),
    ("SEC-03", "step-up 凭证重放拒绝", "P0",
     "同一凭证第二次使用返回 403（防重放），登记表只有一条记录", sec03),
    ("SEC-04", "跨病区数据隔离", "P0",
     "二病区护士读取不到一病区登记表与草稿", sec04),
    ("SEC-05", "审计链防篡改与验链", "P0",
     "库层拒绝改删；即使绕过触发器改写，验链能定位断链位置", sec05),
    ("SEC-06", "留痕失败即回滚并 503", "P0",
     "审计写入失败时业务回滚、返回 503，且不打印堆栈", sec06),
    ("SEC-07", "授权撤回后立即停呼停训", "P0",
     "撤回授权患者外呼 403、录音标记不得用于训练，回拨 25 小时后仍拒绝", sec07),
    ("SEC-08", "草稿生成本地闭环", "P0",
     "草稿生成标注本地规则整理，字段不含姓名/电话，模块无外部客户端", sec08),
    ("SEC-09", "服务端日志不落敏感原文", "P1",
     "日志不含会话凭证、step-up 凭证、联系电话、住院号", sec09),
    ("SEC-10", "审计接口最小必要字段", "P1",
     "审计返回不含 prev_hash、联系电话、姓名与证件/住址字段", sec10),
    ("SEC-11", "超限载荷优雅拒绝", "P1",
     "300KB 与 40 层嵌套返回 4xx，不 5xx、不打堆栈", sec11),
    ("SEC-12", "导入仅接受合成数据", "P1",
     "synthetic 列存在；未带 synthetic 标记的真实样式数据被拒收，库内零落库", sec12),
]
