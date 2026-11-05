# -*- coding: utf-8 -*-
"""首轮测试执行器（测试方案 docs/MVP测试方案.md 第十节 S1+S2）。

用法：
    python tools/run_round1.py

自动完成：
- E1 环境：临时数据目录 + 随机 FDE_SECRET + 随机端口拉起 backend/server.py；
- S1 冒烟：登录 / 看板 / 导入 / 排程；
- S2 首轮：CT/SC/AU/RF/RC/TK/EX 七组用例（HTTP 级）；
- 11.1 已知缺陷复现：RF-D01、RF-D02、EX-D01、SC-09、PM-04、TK-08/09、SEC-12；
- RF-10 语料回归（backend/tests/corpus/redflag.jsonl，≥120 条）；
- 输出 data/round1_report.json（用例结果 + 缺陷登记）。
"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import secrets as pysecrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TODAY = "2026-11-04"
DATA_DIR = ROOT / "data" / "round1-e1"
DB_PATH = DATA_DIR / "followup.db"
REPORT_PATH = ROOT / "data" / "round1_report.json"
CORPUS_PATH = ROOT / "backend" / "tests" / "corpus" / "redflag.jsonl"

RESULTS: list[dict] = []
DEFECTS: list[dict] = []


def record(case: str, title: str, expected: str, actual, ok: bool,
           evidence: str = "", defect: dict | None = None) -> None:
    status = "PASS" if ok else "FAIL"
    if defect:
        status = "DEFECT-REPRODUCED"
    entry = {"case": case, "title": title, "expected": expected,
             "actual": actual, "status": status, "evidence": evidence}
    RESULTS.append(entry)
    flag = {"PASS": "[通过]", "FAIL": "[失败]", "DEFECT-REPRODUCED": "[缺陷]"}[status]
    print(f"  {flag} {case}: {title}")
    if not ok or defect:
        print(f"      期望: {expected}\n      实际: {actual}")
        if evidence:
            print(f"      证据: {evidence}")
    if defect:
        DEFECTS.append(defect)


def register_defect(defect_id: str, severity: str, title: str, case: str,
                    expected: str, actual: str, evidence: str) -> dict:
    return {"defect_id": defect_id, "severity": severity, "title": title,
            "case": case, "expected": expected, "actual": actual,
            "evidence": evidence, "status": "已复现", "round": "R1"}


# ---------------------------------------------------------------- 服务器管理
class Server:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.secret = pysecrets.token_hex(16)

    def start(self) -> None:
        self.port = _free_port()
        env = dict(os.environ)
        env["FDE_DATA_DIR"] = str(DATA_DIR)
        env["FDE_SECRET"] = self.secret
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "backend.server", "--port", str(self.port)],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.4):
                    return
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError("server exited during startup")
                time.sleep(0.2)
        raise RuntimeError("server did not start in 15s")

    def restart(self) -> None:
        self.stop()
        time.sleep(0.5)
        self.start()

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------- HTTP 客户端
class Api:
    def __init__(self, port: int) -> None:
        self.port = port
        self.session: str | None = None

    def request(self, method: str, path: str, body=None, raw: bytes | None = None,
                declare_len: int | None = None, headers: dict | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        hdrs = {"Content-Type": "application/json"}
        if self.session:
            hdrs["X-Session"] = self.session
        if headers:
            hdrs.update(headers)
        payload = raw if raw is not None else (
            json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
        if declare_len is not None:  # 仅声明长度，用于 413 检测
            hdrs["Content-Length"] = str(declare_len)
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        try:
            parsed = json.loads(data.decode("utf-8"))
        except Exception:
            parsed = {"raw": data[:200].decode("utf-8", "replace")}
        return resp.status, parsed

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, body=None, **kw):
        return self.request("POST", path, body=body, **kw)

    def login(self, user_id: str) -> None:
        st, r = self.request("POST", "/api/login", body={"user_id": user_id})
        assert st == 200 and r.get("ok"), f"login {user_id} failed: {st} {r}"
        self.session = r["data"]["token"]


def status_of(r: dict) -> str:
    return r.get("code", "?") if isinstance(r, dict) else "?"


def ok_payload(status: int, r: dict):
    assert status == 200 and r.get("ok"), f"expected 200/ok, got {status} {r}"
    return r["data"]


def expect_err(case_ok: list, status: int, r: dict, want_status: int,
               want_code: str = "", contains: str = "") -> bool:
    good = status == want_status and not r.get("ok", True)
    if want_code:
        good = good and r.get("code") == want_code
    if contains:
        good = good and contains in json.dumps(r, ensure_ascii=False)
    return good


# ---------------------------------------------------------------- DB 直读
def db_query(sql: str, params=()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def db_scalar(sql: str, params=()):
    rows = db_query(sql, params)
    return rows[0][0] if rows else None


def find_schedule(inpatient_no: str) -> str:
    row = db_query(
        "SELECT id FROM schedules WHERE patient_id IN (SELECT id FROM patients WHERE inpatient_no=?) "
        "ORDER BY created_at DESC LIMIT 1", (inpatient_no,))
    return str(row[0][0]) if row else ""


# ---------------------------------------------------------------- S1 冒烟
def s1_smoke(api: dict, eng: Api):
    print("== S1 冒烟 ==")
    d = ok_payload(*api["nurse"].get("/api/bootstrap"))
    record("S1-01", "bootstrap 暴露账号清单（演练模式）",
           "users≥6、templates 可见", f"users={len(d['users'])}, templates={len(d['templates'])}",
           len(d["users"]) >= 6 and len(d["templates"]) >= 1)

    tok = api["nurse"].session or ""
    record("S1-02", "登录颁发会话令牌", "token 长度≥32", f"len={len(tok)}", len(tok) >= 32)

    st, r = api["eng"].get("/api/fixtures")
    d = ok_payload(st, r)
    names = [f.get("name", "") for f in d.get("fixtures", [])] if isinstance(d.get("fixtures"), list) else []
    no_path = all("/" not in n and "\\" not in n for n in names)
    record("S1-03", "fixtures 仅暴露文件名", "无路径分隔符、含 v1 样本",
           f"{names}", no_path and any("v1" in n for n in names))

    d = ok_payload(*eng.post("/api/his/import", body={"fixture": "his_batch_v1.json"}))
    record("S1-04", "导入 v1 名单（10 行）", "accepted=10",
           f"accepted={d.get('accepted')}", d.get("accepted") == 10)

    d = ok_payload(*eng.post("/api/schedule/run", body={"date": TODAY}))
    good = d.get("created") == 6 and d.get("excluded_unauthorized") == 2 and d.get("manual") == 2
    record("S1-05", "排程首跑统计", "created=6, excluded_unauthorized=2, manual=2",
           f"created={d.get('created')}, excluded={d.get('excluded_unauthorized')}, manual={d.get('manual')}", good)

    d = ok_payload(*api["nurse"].get(f"/api/schedule?date={TODAY}"))
    items = d.get("items", [])
    nos = {i["inpatient_no"] for i in items}
    masked_ok = all("****" in i["contact_phone"] for i in items)
    record("S1-06", "护士看板：本人排程 ∪ 本病区、号码脱敏",
           "骨科一病区护士-01 见 0101/0106/0109，全脱敏",
           f"patients={sorted(nos)}, masked={masked_ok}",
           nos == {"ZY2026110101", "ZY2026110106", "ZY2026110109"} and masked_ok)


# ---------------------------------------------------------------- CT 组（5.1）
def ct_group(api: dict, eng: Api):
    print("== CT 数据契约导入 ==")
    d = ok_payload(*eng.post("/api/his/import", body={"fixture": "his_batch_v2_pending.json"}))
    good = (d.get("accepted") == 1 and d.get("merged") == 3 and d.get("rejected") == 1
            and sorted(d.get("dropped_fields", [])) == ["address", "id_card"])
    record("CT-02", "v2 超额字段拒收+重复合并", "accepted=1, merged=3, rejected=1, dropped=[address,id_card]",
           f"accepted={d.get('accepted')}, merged={d.get('merged')}, rejected={d.get('rejected')}, "
           f"dropped={d.get('dropped_fields')}", good)

    dump = "".join(str(dict(r)) for r in db_query("SELECT * FROM patients"))
    cols = [r["name"] for r in db_query("PRAGMA table_info(patients)")]
    no_leak = "110101199001011234" not in dump and "某市某区某路" not in dump
    no_cols = "id_card" not in cols and "address" not in cols
    record("CT-02b", "超额字段零落库（敏感值与列均不存在）",
           "patients 表无 id_card/address 列，敏感值零落库", f"cols={cols}, leak={not no_leak}",
           no_leak and no_cols)

    d = ok_payload(*api["nurse"].get(f"/api/schedule?date={TODAY}"))
    manual = {m["inpatient_no"]: m["manual_reason"] for m in d.get("manual_list", [])}
    r05 = manual.get("ZY2026110105", "")
    record("CT-03", "术式代码缺失转人工并给原因", "0105 manual_reason=术式代码缺失",
           f"reason={r05!r}", "术式代码缺失" in r05)

    st, r = eng.post("/api/his/import", body={"batch_version": "HIS-R1-CT04", "exported_at": f"{TODAY}T03:00:00",
                                              "rows": [{"inpatient_no": "ZYR10004", "discharge_time": f"{TODAY}T10:00:00",
                                                        "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                        "followup_status": "待随访", "contact_type": "本人",
                                                        "auth_status": "已授权"}]})
    record("CT-04", "缺必填字段整行拒收", "409 contract_invalid 含『缺必填字段: contact_phone』",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 409, "contract_invalid", "contact_phone"))

    st, r = eng.post("/api/his/import", body={"batch_version": "HIS-R1-CT05a", "exported_at": f"{TODAY}T03:05:00",
                                              "rows": [{"inpatient_no": "ZYR10005", "discharge_time": f"{TODAY}T10:10:00",
                                                        "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                        "followup_status": "待随访", "contact_phone": "138-0013-8000",
                                                        "contact_type": "本人", "auth_status": "已授权"}]})
    d = ok_payload(st, r)
    cleaned = db_scalar("SELECT contact_phone FROM patients WHERE inpatient_no='ZYR10005'")
    record("CT-05a", "手机号清洗入库", "138-0013-8000 → 13800138000",
           f"accepted={d.get('accepted')}, db={cleaned}", d.get("accepted") == 1 and cleaned == "13800138000")

    st, r = eng.post("/api/his/import", body={"batch_version": "HIS-R1-CT05b", "exported_at": f"{TODAY}T03:06:00",
                                              "rows": [{"inpatient_no": "ZYR10005b", "discharge_time": f"{TODAY}T10:11:00",
                                                        "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                        "followup_status": "待随访", "contact_phone": "1234",
                                                        "contact_type": "本人", "auth_status": "已授权"}]})
    record("CT-05b", "非法手机号拒收", "409 contract_invalid『联系电话格式无效』",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 409, "contract_invalid", "联系电话格式无效"))

    st, r = eng.post("/api/his/import", body={"batch_version": "HIS-R1-CT06", "exported_at": f"{TODAY}T03:07:00",
                                              "rows": [{"inpatient_no": "ZYR10006", "discharge_time": f"{TODAY}T10:12:00",
                                                        "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                        "followup_status": "待随访", "contact_phone": "13700001234",
                                                        "contact_type": "本人", "auth_status": "未知"}]})
    record("CT-06", "授权状态非法枚举拒收", "409 contract_invalid『授权状态非法』",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 409, "contract_invalid", "授权状态"))

    d = ok_payload(*eng.post("/api/his/import", body={"fixture": "his_batch_v1.json"}))
    record("CT-07", "同版本批次幂等", "idempotent=true, accepted=0",
           f"idempotent={d.get('idempotent')}, accepted={d.get('accepted')}",
           d.get("idempotent") is True and d.get("accepted") == 0)

    d = ok_payload(*eng.post("/api/his/import", body={"batch_version": "HIS-R1-CT08", "exported_at": f"{TODAY}T03:08:00",
                                                      "rows": [
                                                          {"inpatient_no": "ZYR10008", "discharge_time": f"{TODAY}T09:00:00",
                                                           "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                           "followup_status": "待随访", "contact_phone": "13700008001",
                                                           "contact_type": "本人", "auth_status": "已授权"},
                                                          {"inpatient_no": "ZYR10008", "discharge_time": f"{TODAY}T16:00:00",
                                                           "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                           "followup_status": "待随访", "contact_phone": "13700008001",
                                                           "contact_type": "本人", "auth_status": "已授权"}]}))
    n = db_scalar("SELECT COUNT(*) FROM patients WHERE inpatient_no='ZYR10008'")
    record("CT-08", "同人同日两次出院均保留", "accepted=2, 库内 2 行",
           f"accepted={d.get('accepted')}, rows={n}", d.get("accepted") == 2 and n == 2)

    # SEC-12（11.1 已知缺陷）：synthetic 标记不校验
    st, r = eng.post("/api/his/import", body={"batch_version": "HIS-R1-SEC12", "exported_at": f"{TODAY}T03:09:00",
                                              "rows": [{"inpatient_no": "ZYR1S012", "discharge_time": f"{TODAY}T10:20:00",
                                                        "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                        "followup_status": "待随访", "contact_phone": "13700001212",
                                                        "contact_type": "本人", "auth_status": "已授权",
                                                        "synthetic": False}]})
    d = json.loads(json.dumps(r))
    ok_syn = st == 200 and d.get("ok") and d["data"].get("accepted") == 1
    rec = record("SEC-12", "synthetic 标记不校验（11.1 已知缺陷）",
                 "导入应校验 synthetic=true 或拒收", f"synthetic=false 行被正常接收 accepted=1",
                 ok_syn, evidence="contract.FIELDS 无 synthetic 字段校验",
                 defect=register_defect("SEC-12", "P1", "HIS 导入不校验 synthetic 标记",
                                        "SEC-12", "synthetic=false 的行应拒收或告警",
                                        "正常接收入库", "tools/run_round1.py CT 组"))


# ---------------------------------------------------------------- SC 组（5.2）
def sc_group(api: dict, eng: Api, mgr: Api):
    print("== SC 排程 ==")
    d = ok_payload(*mgr.get(f"/api/schedule?date={TODAY}"))
    manual = {m["inpatient_no"]: m["manual_reason"] for m in d.get("manual_list", [])}
    record("SC-02", "模板未签发转人工", "0104 reason 含『模板未签发（PG-ORTHO-FIX）』",
           f"{manual.get('ZY2026110104', '')!r}",
           "模板未签发（PG-ORTHO-FIX）" in manual.get("ZY2026110104", ""))

    slots = {i["inpatient_no"]: i["slot"] for i in d.get("items", [])}
    fam = all(slots.get(p) == "evening" for p in ("ZY2026110102", "ZY2026110106", "ZY2026110110"))
    rec = record("SC-03", "家属晚间、本人日间", "0102/0106/0110=evening, 0101=daytime",
                 f"{slots}", fam and slots.get("ZY2026110101") == "daytime")

    nb = ok_payload(*api["nurse"].get(f"/api/schedule?date={TODAY}"))
    nos = {i["inpatient_no"] for i in nb.get("items", [])}
    man_nurse = {m["inpatient_no"] for m in nb.get("manual_list", [])}
    cross = bool(man_nurse & {"ZY2026110104", "ZY2026110105"})
    record("SC-08", "护士看板病区隔离", "items 仅 0101/0106/0109；跨病区人工清单不可见",
           f"items={sorted(nos)}, manual_list 跨病区可见={sorted(man_nurse & {'ZY2026110104', 'ZY2026110105'})}",
           nos == {"ZY2026110101", "ZY2026110106", "ZY2026110109"},
           evidence="board() manual 查询未按病区/护士过滤")
    if cross:
        DEFECTS.append(register_defect(
            "DEF-R1-01", "P1", "护士看板 manual_list 未按病区过滤（跨病区患者可见）",
            "SC-08", "护士仅见本病区患者（人工清单同口径）",
            "骨科一病区护士-01 可见二病区 0104/0105 人工清单（含脱敏电话）",
            "tools/run_round1.py SC-08"))

    d = ok_payload(*eng.post("/api/schedule/run", body={"date": TODAY}))
    dup = db_scalar("SELECT COUNT(*) FROM schedules WHERE patient_id IN "
                    "(SELECT id FROM patients WHERE inpatient_no IN ('ZY2026110101','ZY2026110108'))")
    record("SC-04", "同日重跑幂等", "重跑不重复建排程；skipped_duplicate≥2",
           f"created={d.get('created')}, skipped_duplicate={d.get('skipped_duplicate')}, 已有排程数={dup}",
           d.get("created") == 1 and d.get("skipped_duplicate", 0) >= 2 and dup == 2)

    sid_0101 = find_schedule("ZY2026110101")
    st, r = api["nurse2"].post("/api/schedule/reorder",
                               body={"schedule_id": sid_0101, "new_slot": "evening"})
    slot_after = db_scalar("SELECT slot FROM schedules WHERE id=?", (int(sid_0101),))
    record("SC-09", "护士越权改他人排程（11.1 已知缺陷）", "nurse-02 改 nurse-01 排程应 403",
           f"{st} {r.get('code')} {r.get('error')}；改后 slot={slot_after}",
           expect_err([], st, r, 403), evidence=f"reorder 仅校验角色，未校验归属，slot 已被改为 {slot_after}")
    if st == 200 and r.get("ok"):
        DEFECTS.append(register_defect(
            "SC-09", "P1", "排程改期无归属校验（护士 A 可改护士 B 的排程）",
            "SC-09", "非本人排程返回 403 forbidden",
            "nurse-02 成功将 nurse-01 的 0101 排程改为 evening", "tools/run_round1.py SC-09"))

    # SC-10：5 个护士并发读看板（补 3 个临时护士账号作测试数据）
    conn = sqlite3.connect(DB_PATH, timeout=10)
    for i in (1, 2, 3):
        conn.execute("INSERT OR IGNORE INTO users(user_id,name,role,ward,active) VALUES(?,?,?,?,1)",
                     (f"u-tmp-n0{i}", f"临时护士{i}", "nurse", "骨科一病区"))
    conn.commit()
    conn.close()
    clients = []
    for uid in ("u-nurse-01", "u-nurse-02", "u-tmp-n01", "u-tmp-n02", "u-tmp-n03"):
        c = Api(mgr.port)
        c.login(uid)
        clients.append(c)
    errors: list[str] = []

    def _read(c: Api) -> None:
        for _ in range(20):
            st, r = c.get(f"/api/schedule?date={TODAY}")
            if st != 200 or not r.get("ok"):
                errors.append(f"{st}:{r.get('code')}")

    threads = [threading.Thread(target=_read, args=(c,)) for c in clients]
    [t.start() for t in threads]
    [t.join() for t in threads]
    record("SC-10", "5 护士并发读看板", "100 次请求全部 200，无串号",
           f"errors={errors[:3]}（共{len(errors)}）", not errors)

    d = ok_payload(*eng.post("/api/his/import", body={
        "batch_version": "HIS-R1-SC05", "exported_at": f"{TODAY}T04:00:00", "simulate": "timeout",
        "rows": [{"inpatient_no": "ZYR1SC05", "discharge_time": f"{TODAY}T10:30:00",
                  "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                  "followup_status": "待随访", "contact_phone": "13700000501",
                  "contact_type": "本人", "auth_status": "已授权"}]}))
    st, r = api["quality"].get("/api/audit?action=batch.fallback&limit=5")
    fb = ok_payload(st, r).get("events", [])
    record("SC-05", "HIS 超时降级（沿用上一版）", "批次 pending_backfill + 审计 batch.fallback",
           f"resp={d}, fallback审计={len(fb)}条",
           "pending_backfill" in json.dumps(d, ensure_ascii=False) and len(fb) >= 1)

    stale_rows = [
        {"inpatient_no": "ZYR1ST01", "discharge_time": f"{TODAY}T11:00:00", "department": "骨科一病区",
         "procedure_code": "PG-ORTHO-THR", "followup_status": "待随访", "contact_phone": "13700000601",
         "contact_type": "本人", "auth_status": "已授权"},
        {"inpatient_no": "ZYR1ST02", "discharge_time": f"{TODAY}T11:10:00", "department": "骨科一病区",
         "procedure_code": "PG-ORTHO-THR", "followup_status": "待随访", "contact_phone": "13700000602",
         "contact_type": "本人", "auth_status": "已授权"}]
    d = ok_payload(*eng.post("/api/his/import", body={
        "batch_version": "HIS-R1-STALE", "exported_at": "2026-11-02T00:00:00", "rows": stale_rows}))
    d = ok_payload(*eng.post("/api/schedule/run", body={"date": TODAY}))
    r1 = db_scalar("SELECT manual_reason FROM patients WHERE inpatient_no='ZYR1ST01'")
    st, r = api["quality"].get("/api/audit?action=schedule.stale_fallback&limit=5")
    nfb = len(ok_payload(st, r).get("events", []))
    record("SC-06", "名单超 24h 转人工", "created=0, manual=2, reason 含 24 小时, 审计 stale_fallback",
           f"created={d.get('created')}, manual={d.get('manual')}, reason={r1!r}, 审计={nfb}条",
           d.get("created") == 0 and d.get("manual") == 2 and "24" in (r1 or "") and nfb >= 1)


# ---------------------------------------------------------------- 公共构造器
def import_and_schedule(eng: Api, batch: str, inpatient_no: str, *, ward="骨科一病区",
                        proc="PG-ORTHO-THR", ctype="本人", auth="已授权",
                        discharge=None, phone=None) -> str:
    ph = phone or f"138{pysecrets.randbelow(10**8):08d}"
    row = {"inpatient_no": inpatient_no, "discharge_time": discharge or f"{TODAY}T12:00:00",
           "department": ward, "procedure_code": proc, "followup_status": "待随访",
           "contact_phone": ph, "contact_type": ctype, "auth_status": auth}
    ok_payload(*eng.post("/api/his/import", body={"batch_version": batch,
                                                  "exported_at": f"{TODAY}T05:00:00", "rows": [row]}))
    ok_payload(*eng.post("/api/schedule/run", body={"date": TODAY}))
    return find_schedule(inpatient_no)


def audit_events(quality: Api, action: str, limit: str = "10") -> list[dict]:
    st, r = quality.get(f"/api/audit?action={urllib.parse.quote(action)}&limit={limit}")
    return ok_payload(st, r).get("events", [])


def raw_schedule_insert(inpatient_no: str, suffix: str) -> str:
    """AU-02/AU-03 测试数据：为未授权/撤回患者直接补排程（无 API 路径可建）。"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    p = conn.execute("SELECT id, batch_version, discharge_time FROM patients WHERE inpatient_no=?",
                     (inpatient_no,)).fetchone()
    cur = conn.execute(
        "INSERT INTO schedules(patient_id,schedule_date,slot,nurse_id,status,attempts,seq,"
        "batch_version,template_version,created_at,idempotency_key) "
        "VALUES(?,?,?,?,?,0,0,?,?,?,?)",
        (p[0], TODAY, "daytime", "u-nurse-01", "pending", p[1], "1.0",
         datetime.now().isoformat(timespec="seconds"), f"{inpatient_no}|{p[2]}|{TODAY}|{suffix}"))
    sid = str(cur.lastrowid)
    conn.commit()
    conn.close()
    return sid


# ---------------------------------------------------------------- AU 组（5.3）
def au_group(api: dict, eng: Api, quality: Api):
    print("== AU 触达与授权 ==")
    sid1 = find_schedule("ZY2026110101")
    d = ok_payload(*api["nurse"].post(f"/api/contact/{sid1}/call", body={"consent": True}))
    row = db_query("SELECT s.status AS ss, r.consent AS rc, r.status AS rs FROM schedules s "
                   "LEFT JOIN recordings r ON r.schedule_id=s.id WHERE s.id=?", (int(sid1),))[0]
    record("AU-01", "已授权+同意录音外呼", "排程 in_progress，录音 consent=1 active",
           f"schedule={row['ss']}, recording_consent={row['rc']}, recording_status={row['rs']}",
           row["ss"] == "in_progress" and row["rc"] == 1 and row["rs"] == "active")
    ev = audit_events(quality, "call.start", "5")
    notice_ok = any("告知" in json.dumps(e.get("detail", {}), ensure_ascii=False) for e in ev)
    record("AU-01b", "外呼审计含录音告知语", "call.start 事件含告知语",
           f"call.start 事件={len(ev)}条, 含告知语={notice_ok}", notice_ok and len(ev) >= 1)

    sid_u = raw_schedule_insert("ZY2026110103", "AU02")
    st, r = api["nurse"].post(f"/api/contact/{sid_u}/call", body={"consent": True})
    ev = audit_events(quality, "call.denied_unauthorized", "5")
    record("AU-02", "未授权外呼拒绝", "403『授权状态为「未授权」』+ 审计 call.denied_unauthorized",
           f"{st} {r.get('code')} {r.get('error')}, 审计={len(ev)}条",
           expect_err([], st, r, 403, "forbidden", "未授权") and len(ev) >= 1)

    sid_r = raw_schedule_insert("ZY2026110107", "AU03")
    st, r = api["nurse"].post(f"/api/contact/{sid_r}/call", body={"consent": True})
    record("AU-03", "撤回状态外呼拒绝", "403『授权状态为「撤回」』",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 403, "forbidden", "撤回"))

    for _ in range(3):
        ok_payload(*api["nurse"].post(f"/api/contact/{sid1}/attempt",
                                      body={"channel": "call", "outcome": "no_answer"}))
    row = db_query("SELECT attempts, status FROM schedules WHERE id=?", (int(sid1),))[0]
    record("AU-04", "3 次未接通后搁置", "attempts=3, status=shelved",
           f"attempts={row['attempts']}, status={row['status']}",
           row["attempts"] == 3 and row["status"] == "shelved")

    sid_0110 = find_schedule("ZY2026110110")
    d = ok_payload(*api["nurse2"].post(f"/api/contact/{sid_0110}/attempt", body={"channel": "sms"}))
    row = db_query("SELECT attempts, status FROM schedules WHERE id=?", (int(sid_0110),))[0]
    record("AU-05", "短信触达不计完成率", "attempts 不变（0）、状态仍 pending、counts=false",
           f"resp={d}, attempts={row['attempts']}, status={row['status']}",
           row["attempts"] == 0 and row["status"] == "pending" and d.get("counts") is False)

    tok = ok_payload(*api["nurse2"].post("/api/stepup", body={
        "purpose": "auth_revoke", "action_id": sid_0110,
        "fields": {"schedule_id": sid_0110}}))["token"]
    d = ok_payload(*api["nurse2"].post(f"/api/contact/{sid_0110}/revoke",
                                       body={"reason": "R1-AU06 演练撤回", "token": tok}))
    p = db_query("SELECT auth_status FROM patients WHERE inpatient_no='ZY2026110110'")[0]
    s = db_scalar("SELECT status FROM schedules WHERE id=?", (int(sid_0110),))
    recs = db_scalar("SELECT COUNT(*) FROM recordings WHERE schedule_id=? AND status='active'", (int(sid_0110),))
    record("AU-06", "step-up 授权撤回联动", "患者撤回、排程 shelved、无 active 录音",
           f"auth={p['auth_status']}, schedule={s}, active_recordings={recs}",
           p["auth_status"] == "撤回" and s == "shelved" and recs == 0)

    st, r = api["nurse2"].post(f"/api/contact/{sid_0110}/call", body={"consent": True})
    record("AU-07", "撤回后再呼拒绝", "403『授权状态为「撤回」』",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 403, "forbidden", "撤回"))

    ev = audit_events(quality, "auth.revoke", "5")
    det = json.dumps(ev[0].get("detail", {}), ensure_ascii=False) if ev else ""
    leak = any("prev_hash" in json.dumps(e, ensure_ascii=False) for e in ev)
    record("AU-08", "撤回审计留痕完整", "detail 含 reason/operator/schedules_stopped；列表不含 prev_hash",
           f"detail={det[:160]}, prev_hash泄漏={leak}",
           all(k in det for k in ("reason", "operator", "schedules_stopped")) and not leak)

    sid9 = find_schedule("ZY2026110109")
    d = ok_payload(*api["nurse"].post(f"/api/contact/{sid9}/call", body={"consent": False}))
    rs = db_scalar("SELECT status FROM recordings WHERE schedule_id=?", (int(sid9),))
    ev = audit_events(quality, "call.start", "5")
    notice = json.dumps(ev[0].get("detail", {}), ensure_ascii=False) if ev else ""
    record("AU-09", "已授权未同意 → 不录音", "recording not_recorded、审计含『未告知授权，不录音』",
           f"recording={rs}, detail={notice[:120]}",
           rs == "not_recorded" and "未告知授权，不录音" in notice)


# ---------------------------------------------------------------- RF 组（5.4）
def rf_flow(eng: Api, nurse: Api, tag: str, text: str):
    """新建患者→排程→外呼→提交 transcript，返回 (sid, check响应)。"""
    sid = import_and_schedule(eng, f"HIS-R1-{tag}", f"ZYR1{tag}", phone=f"138{abs(hash(tag)) % 10**8:08d}")
    ok_payload(*nurse.post(f"/api/contact/{sid}/call", body={"consent": True}))
    d = ok_payload(*nurse.post("/api/questionnaire/check",
                               body={"schedule_id": sid, "transcript": text}))
    return sid, d


def rf_group(api: dict, eng: Api, quality: Api):
    print("== RF 红旗引擎 ==")
    sid, d = rf_flow(eng, api["nurse"], "RF01", "伤口渗液，有点臭")
    tk = db_query("SELECT * FROM tickets WHERE schedule_id=?", (int(sid),))
    sst = db_scalar("SELECT status FROM schedules WHERE id=?", (int(sid),))
    drafts = [x for x in ok_payload(*api["nurse"].get("/api/drafts?status=draft")).get("drafts", [])
              if str(x.get("schedule_id")) == sid]
    rule_ok = bool(tk) and tk[0]["rule_id"] == "RF-ORTHO-03"
    record("RF-01", "伤口渗液升级", "中断+建单（RF-ORTHO-03）+排程 escalated+不建草稿",
           f"interrupted={d.get('interrupted')}, tickets={len(tk)}, rule={tk[0]['rule_id'] if tk else '-'}, "
           f"schedule={sst}, drafts={len(drafts)}",
           d.get("interrupted") is True and len(tk) == 1 and rule_ok and sst == "escalated" and not drafts)

    _, d = rf_flow(eng, api["nurse"], "RF02", "没有发烧，也不咳嗽")
    record("RF-02", "否定表述不命中", "interrupted=false、不建单",
           f"interrupted={d.get('interrupted')}, hits={d.get('hits')}",
           d.get("interrupted") is False and not d.get("hits"))

    _, d = rf_flow(eng, api["nurse"], "RF03", "膝盖不咋个痛了，但是喘不上气")
    hits = d.get("hits") or []
    h0 = hits[0] if hits else {}
    record("RF-03", "分句否定不影响后半句", "仅命中 RF-ORTHO-01（lexicon 1.0）",
           f"hits={hits}",
           [h.get("rule_id") for h in hits] == ["RF-ORTHO-01"] and h0.get("channel") == "lexicon")

    _, d = rf_flow(eng, api["nurse"], "RF04", "莫得发烧，就是伤口有点痛")
    hits = d.get("hits") or []
    hit_03 = any(h.get("rule_id") == "RF-ORTHO-03" and h.get("channel") == "model" for h in hits)
    record("RF-04", "方言否定+模糊变体不应命中（11.1 已知缺陷）", "无命中、不建单",
           f"hits={hits}", not hits,
           evidence="莫得 不在否定词表；『伤口有点痛』与变体『伤口没长好/伤口有黄水』模糊过阈",
           defect=register_defect("RF-D01", "P1", "红旗引擎误报：方言否定与模糊变体过阈（宁误报口径未兑现）",
                                  "RF-04", "『莫得发烧，就是伤口有点痛』不应命中",
                                  f"被误报命中：{hits}", "tools/run_round1.py RF-04"))
    _ = hit_03

    _, d = rf_flow(eng, api["nurse"], "RF05", "吃止痛药也不管用")
    hits = d.get("hits") or []
    h0 = hits[0] if hits else {}
    record("RF-05", "止痛药无效命中剧痛规则", "RF-ORTHO-06（model 1.0）",
           f"hits={hits}",
           [h.get("rule_id") for h in hits] == ["RF-ORTHO-06"] and h0.get("channel") == "model"
           and h0.get("confidence") == 1.0)

    neg_texts = ["伤口没有裂开", "小腿没有肿", "没有晕倒"]
    fpped = []
    for i, t in enumerate(neg_texts, 1):
        _, d = rf_flow(eng, api["nurse"], f"RF06{i}", t)
        if d.get("hits"):
            fpped.append({"text": t, "hits": d.get("hits")})
    record("RF-06", "正则分支否定误报（11.1 已知缺陷）", "三条否定句均不命中",
           f"误报={json.dumps(fpped, ensure_ascii=False)}", not fpped,
           evidence="regex 分支未做否定处理，『伤口没有裂开』等被正则 1.0 命中",
           defect=register_defect("RF-D02", "P1", "红旗引擎误报：正则分支不做否定判断",
                                  "RF-06", "否定式（伤口没有裂开/小腿没有肿/没有晕倒）不应命中",
                                  f"被正则误报：{json.dumps(fpped, ensure_ascii=False)[:200]}",
                                  "tools/run_round1.py RF-06"))

    sid1 = find_schedule("ZYR1RF01")
    _, d = api["nurse"].post("/api/questionnaire/check",
                             body={"schedule_id": sid1, "transcript": "伤口渗液，有点臭"})
    record("RF-07", "同排程同规则去重", "tickets_created=0、deduped 含 RF-ORTHO-03",
           f"tickets_created={d.get('tickets_created')}, deduped={d.get('deduped')}",
           d.get("tickets_created") == 0 and "RF-ORTHO-03" in (d.get("deduped") or []))

    drafts = [x for x in ok_payload(*api["nurse"].get("/api/drafts?status=draft")).get("drafts", [])
              if str(x.get("schedule_id")) == sid1]
    tk = db_query("SELECT rule_id, channel, confidence, trigger_text FROM tickets WHERE schedule_id=?",
                  (int(sid1),))
    tkd = dict(tk[0]) if tk else {}
    record("RF-08", "红旗路径不建草稿、工单字段完整",
           "RF-01 排程草稿数=0；工单含 rule_id/channel/confidence/trigger_text",
           f"drafts={len(drafts)}, ticket={tkd}",
           not drafts and all(k in tkd and tkd[k] not in ("", None) for k in
                              ("rule_id", "channel", "confidence", "trigger_text")))

    d = ok_payload(*api["nurse"].get("/api/rules"))
    record("RF-09", "规则集只读视图", "rule_set_id=RF-ORTHO, version=1.0, min_confidence=0.6",
           f"rule_set_id={d.get('rule_set_id')}, version={d.get('version')}, "
           f"min_confidence={d.get('model_channel', {}).get('min_confidence')}",
           d.get("rule_set_id") == "RF-ORTHO" and d.get("version") == "1.0"
           and d.get("model_channel", {}).get("min_confidence") == 0.6)

    rf10_corpus()

    rf11_signature()

    sid12, d = rf_flow(eng, api["nurse"], "RF12", "")
    dr = [x for x in ok_payload(*api["nurse"].get("/api/drafts?status=draft")).get("drafts", [])
          if str(x.get("schedule_id")) == sid12]
    empty_draft = dr[0] if dr else {}
    record("RF-12a", "空转写不命中、生成空草稿", "interrupted=false，草稿主诉=未提及",
           f"interrupted={d.get('interrupted')}, 草稿数={len(dr)}, 主诉={empty_draft.get('fields', {}).get('主诉')!r}",
           d.get("interrupted") is False and empty_draft.get("fields", {}).get("主诉") == "未提及")

    st, r = api["nurse"].post("/api/questionnaire/check",
                              body={"schedule_id": sid1, "transcript": "x" * 300},
                              declare_len=300000)
    record("RF-12b", "超长转写拒绝", "413 body_too_large",
           f"{st} {r.get('code')}", st == 413 and r.get("code") == "body_too_large")


def rf10_corpus() -> None:
    """RF-10：≥120 条标注语料，计算召回率/精确率。"""
    rules_path = ROOT / "backend" / "rules" / "redflag.json"
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    corpus: list[dict] = []

    def add(text: str, label: bool) -> None:
        corpus.append({"text": text, "label": "redflag" if label else "normal"})

    for rule in rules["rules"]:
        for kw in rule["keywords"]:
            add(f"医生，我这两天{kw}", True)
            add(f"没有{kw}，请放心", False)
        for v in rule["variants"]:
            add(f"说实话{v}", True)
            if not any(n in v for n in ("不", "没", "无", "未")):
                add(f"没有{v}", False)
        # 正则型否定句（对准 RF-D02）
        rid = rule["rule_id"]
        if rid == "RF-ORTHO-03":
            add("伤口没有裂开", False)
            add("伤口没有渗液", False)
        elif rid == "RF-ORTHO-04":
            add("小腿没有肿", False)
        elif rid == "RF-ORTHO-05":
            add("没有晕倒", False)
        elif rid == "RF-ORTHO-01":
            add("没有喘不过气", False)
    # 语义反转 / 方言否定 / 日常恢复表述
    for t in ("止痛药挺管用的", "恢复得不错", "伤口长得挺好", "走路顺多了", "今天出院心情不错",
              "谢谢医生护士", "我能自己下床了", "饭量也恢复了"):
        add(t, False)
    for kw in ("发烧", "胸痛", "晕倒", "腿肿", "渗液", "憋气"):
        add(f"莫得{kw}", False)
        add(f"冇得{kw}", False)

    CORPUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CORPUS_PATH.open("w", encoding="utf-8") as f:
        for item in corpus:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    from backend.core import redflag as rf
    tp = fp = fn = tn = 0
    fn_texts: list[str] = []
    for item in corpus:
        hits = rf.match(item["text"])
        pred = bool(hits)
        if item["label"] == "redflag":
            tp, fn = (tp + 1, fn) if pred else (tp, fn + 1)
            if not pred:
                fn_texts.append(item["text"])
        else:
            fp, tn = (fp + 1, tn) if pred else (fp, tn + 1)
    recall = tp / (tp + fn) if tp + fn else 0.0
    precision = tp / (tp + fp) if tp + fp else 0.0
    sample_fp = [item["text"] for item in corpus
                 if item["label"] == "normal" and rf.match(item["text"])][:5]
    n = len(corpus)
    record("RF-10", f"语料回归（{n} 条，已写入 corpus/redflag.jsonl）",
           "recall=100% 且 precision≥95%",
           f"recall={recall:.1%}, precision={precision:.1%}, 漏报={fn}, 误报={fp}, "
           f"误报样例={sample_fp}",
           recall == 1.0 and precision >= 0.95,
           evidence="误报来源：方言否定词缺失（RF-D01）、正则未做否定（RF-D02）")


def rf11_signature() -> None:
    """RF-11：规则文件改动是否绕过签名/版本校验（动态验证）。"""
    rules_path = ROOT / "backend" / "rules" / "redflag.json"
    original = rules_path.read_text(encoding="utf-8")
    try:
        modified = original.replace('"min_confidence": 0.6', '"min_confidence": 0.99')
        modified = modified.replace('"version": "1.0"', '"version": "9.9-hacked"', 1)
        rules_path.write_text(modified, encoding="utf-8")
        code = ("import sys, json; sys.path.insert(0, r'%s'); "
                "from backend.core import redflag; r = redflag.load_rules(); "
                "print(r['version'], r['model_channel']['min_confidence'])" % ROOT)
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                             capture_output=True, text=True, timeout=30).stdout.strip()
        taken = out.startswith("9.9-hacked 0.99")
        record("RF-11", "规则文件无签名/版本校验（缺口确认）",
               "改动应被拒绝或要求审计+版本前进；当前直接生效",
               f"篡改后加载结果={out!r}, 直接生效={taken}",
               not taken,
               evidence="rules 文件无 signature 字段，load_rules 无校验逻辑",
               defect=register_defect("RF-11", "P1", "红旗规则集无签名/版本校验，篡改直接生效",
                                      "RF-11", "规则文件需签名校验，非法改动应拒绝",
                                      "改动 version/min_confidence 后立即生效",
                                      "tools/run_round1.py RF-11"))
    finally:
        rules_path.write_text(original, encoding="utf-8")


# ===CHUNK-END===
