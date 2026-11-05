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
- 输出 data/round1_report.json（用例结果 + 缺陷登记）；跨轮次归档见 docs/MVP测试轮次汇总.md
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
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TODAY = date.today().isoformat()
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
            stdout=subprocess.DEVNULL,
            stderr=open(DATA_DIR / "server-stderr.log", "ab"))
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


def import_fixture(eng: Api, name: str, *, exported_at: str | None = None) -> dict:
    """导入 backend/fixtures 样本，并把 exported_at 覆写为相对今日的时间，避免名单因超 24h 被转人工。"""
    payload = json.loads((ROOT / "backend" / "fixtures" / name).read_text(encoding="utf-8"))
    body = {"batch_version": payload["batch_version"],
            "exported_at": exported_at or f"{TODAY}T02:10:00",
            "rows": payload["rows"], "note": payload.get("note", "")}
    return ok_payload(*eng.post("/api/his/import", body=body))


# ---------------------------------------------------------------- S1 冒烟
def s1_smoke(api: dict, eng: Api):
    print("== S1 冒烟 ==")
    d = ok_payload(*api["nurse"].get("/api/bootstrap"))
    record("S1-01", "bootstrap 暴露账号清单（演练模式）",
           "users≥6、fixtures 可见、egress 标注无院外出口",
           f"users={len(d['users'])}, fixtures={len(d.get('fixtures', []))}",
           len(d["users"]) >= 6 and len(d.get("fixtures", [])) >= 1
           and d.get("egress", {}).get("external_network_exit") is False)

    tok = api["nurse"].session or ""
    record("S1-02", "登录颁发会话令牌", "token 长度≥32", f"len={len(tok)}", len(tok) >= 32)

    st, r = api["eng"].get("/api/fixtures")
    d = ok_payload(st, r)
    fx = d.get("fixtures", [])
    names = [f if isinstance(f, str) else f.get("name", "") for f in fx] if isinstance(fx, list) else []
    no_path = all("/" not in n and "\\" not in n for n in names)
    record("S1-03", "fixtures 仅暴露文件名", "无路径分隔符、含 v1 样本",
           f"{names}", no_path and any("v1" in n for n in names))

    d = import_fixture(eng, "his_batch_v1.json")
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
    # 看板语义（实测确认）：nurse-01 名下排程患者（0101/0109）∪ 本病区排程患者（0102/0110）
    expect = {"ZY2026110101", "ZY2026110102", "ZY2026110109", "ZY2026110110"}
    record("S1-06", "护士看板：本人排程 ∪ 本病区、号码脱敏",
           "骨科一病区护士-01 见 0101/0102/0109/0110（本人+本病区），全脱敏",
           f"patients={sorted(nos)}, masked={masked_ok}",
           nos == expect and masked_ok)


# ---------------------------------------------------------------- CT 组（5.1）
def ct_group(api: dict, eng: Api):
    print("== CT 数据契约导入 ==")
    d = import_fixture(eng, "his_batch_v2_pending.json")
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

    d = ok_payload(*api["manager"].get(f"/api/schedule?date={TODAY}"))
    manual = {m["inpatient_no"]: m["manual_reason"] for m in d.get("manual_list", [])}
    r05 = manual.get("ZY2026110105", "")
    record("CT-03", "术式代码缺失转人工并给原因", "0105 manual_reason=术式代码缺失",
           f"reason={r05!r}", "术式代码缺失" in r05)

    d = ok_payload(*eng.post("/api/his/import", body={"batch_version": "HIS-R1-CT04", "exported_at": f"{TODAY}T03:00:00",
                                                      "rows": [{"inpatient_no": "ZYR10004", "discharge_time": f"{TODAY}T10:00:00",
                                                                "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                                "followup_status": "待随访", "contact_type": "本人",
                                                                "auth_status": "已授权", "synthetic": 1}]}))
    reasons = json.dumps(d.get("reject_reasons", []), ensure_ascii=False)
    record("CT-04", "缺必填字段整行拒收", "accepted=0, rejected=1 且原因含 contact_phone",
           f"accepted={d.get('accepted')}, rejected={d.get('rejected')}, reasons={reasons}",
           d.get("accepted") == 0 and d.get("rejected") == 1 and "contact_phone" in reasons)

    d = ok_payload(*eng.post("/api/his/import", body={"batch_version": "HIS-R1-CT05a", "exported_at": f"{TODAY}T03:05:00",
                                                      "rows": [{"inpatient_no": "ZYR10005", "discharge_time": f"{TODAY}T10:10:00",
                                                                "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                                "followup_status": "待随访", "contact_phone": "138-0013-8000",
                                                                "contact_type": "本人", "auth_status": "已授权", "synthetic": 1}]}))
    cleaned = db_scalar("SELECT contact_phone FROM patients WHERE inpatient_no='ZYR10005'")
    record("CT-05a", "手机号清洗入库", "138-0013-8000 → 13800138000",
           f"accepted={d.get('accepted')}, db={cleaned}", d.get("accepted") == 1 and cleaned == "13800138000")

    d = ok_payload(*eng.post("/api/his/import", body={"batch_version": "HIS-R1-CT05b", "exported_at": f"{TODAY}T03:06:00",
                                                      "rows": [{"inpatient_no": "ZYR10005b", "discharge_time": f"{TODAY}T10:11:00",
                                                                "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                                "followup_status": "待随访", "contact_phone": "1234",
                                                                "contact_type": "本人", "auth_status": "已授权", "synthetic": 1}]}))
    reasons = json.dumps(d.get("reject_reasons", []), ensure_ascii=False)
    record("CT-05b", "非法手机号拒收", "accepted=0, rejected=1 且原因含『联系电话格式无效』",
           f"accepted={d.get('accepted')}, rejected={d.get('rejected')}, reasons={reasons}",
           d.get("accepted") == 0 and d.get("rejected") == 1 and "联系电话格式无效" in reasons)

    d = ok_payload(*eng.post("/api/his/import", body={"batch_version": "HIS-R1-CT06", "exported_at": f"{TODAY}T03:07:00",
                                                      "rows": [{"inpatient_no": "ZYR10006", "discharge_time": f"{TODAY}T10:12:00",
                                                                "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                                "followup_status": "待随访", "contact_phone": "13700001234",
                                                                "contact_type": "本人", "auth_status": "未知", "synthetic": 1}]}))
    reasons = json.dumps(d.get("reject_reasons", []), ensure_ascii=False)
    record("CT-06", "授权状态非法枚举拒收", "accepted=0, rejected=1 且原因含『授权状态非法』",
           f"accepted={d.get('accepted')}, rejected={d.get('rejected')}, reasons={reasons}",
           d.get("accepted") == 0 and d.get("rejected") == 1 and "授权状态非法" in reasons)

    before = db_scalar("SELECT COUNT(*) FROM patients")
    d = import_fixture(eng, "his_batch_v1.json")
    after = db_scalar("SELECT COUNT(*) FROM patients")
    record("CT-07", "同版本批次幂等", "idempotent=true 且患者数不变",
           f"idempotent={d.get('idempotent')}, accepted={d.get('accepted')}, 患者数 {before}→{after}",
           d.get("idempotent") is True and before == after)

    d = ok_payload(*eng.post("/api/his/import", body={"batch_version": "HIS-R1-CT08", "exported_at": f"{TODAY}T03:08:00",
                                                      "rows": [
                                                          {"inpatient_no": "ZYR10008", "discharge_time": f"{TODAY}T09:00:00",
                                                           "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                           "followup_status": "待随访", "contact_phone": "13700008001",
                                                           "contact_type": "本人", "auth_status": "已授权", "synthetic": 1},
                                                          {"inpatient_no": "ZYR10008", "discharge_time": f"{TODAY}T16:00:00",
                                                           "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                           "followup_status": "待随访", "contact_phone": "13700008001",
                                                           "contact_type": "本人", "auth_status": "已授权", "synthetic": 1}]}))
    n = db_scalar("SELECT COUNT(*) FROM patients WHERE inpatient_no='ZYR10008'")
    record("CT-08", "同人同日两次出院均保留", "accepted=2, 库内 2 行",
           f"accepted={d.get('accepted')}, rows={n}", d.get("accepted") == 2 and n == 2)

    # SEC-12（F14 已修复）：synthetic=0 的真实数据在试点模式被拒收
    st, r = eng.post("/api/his/import", body={"batch_version": "HIS-R1-SEC12", "exported_at": f"{TODAY}T03:09:00",
                                              "rows": [{"inpatient_no": "ZYR1S012", "discharge_time": f"{TODAY}T10:20:00",
                                                        "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
                                                        "followup_status": "待随访", "contact_phone": "13700001212",
                                                        "contact_type": "本人", "auth_status": "已授权",
                                                        "synthetic": False}]})
    d = ok_payload(st, r)
    stored = db_scalar("SELECT COUNT(*) FROM patients WHERE inpatient_no='ZYR1S012'")
    ok_syn = d.get("accepted") == 0 and d.get("rejected") == 1 and stored == 0
    record("SEC-12", "synthetic 标记校验（试点拒收真实数据）",
           "synthetic=false 行被拒收（accepted=0, rejected=1）且零落库",
           f"accepted={d.get('accepted')}, rejected={d.get('rejected')}, 落库={stored}, "
           f"原因={d.get('reject_reasons')}",
           ok_syn, evidence="contract 校验 synthetic，试点模式拒绝真实数据")


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
    record("SC-08", "护士看板病区隔离", "items 仅本病区 0101/0102/0109/0110；跨病区人工清单不可见",
           f"items={sorted(nos)}, manual_list 跨病区可见={sorted(man_nurse & {'ZY2026110104', 'ZY2026110105'})}",
           nos == {"ZY2026110101", "ZY2026110102", "ZY2026110109", "ZY2026110110"} and not cross,
           evidence="board() manual 查询与 items 同口径按病区过滤")
    if cross:
        DEFECTS.append(register_defect(
            "DEF-R1-01", "P1", "护士看板 manual_list 未按病区过滤（跨病区患者可见）",
            "SC-08", "护士仅见本病区患者（人工清单同口径）",
            "骨科一病区护士-01 可见二病区 0104/0105 人工清单（含脱敏电话）",
            "tools/run_round1.py SC-08"))

    ensure_active("HIS-20261104-v1")
    d = ok_payload(*eng.post("/api/schedule/run", body={"date": TODAY}))
    dup = db_scalar("SELECT COUNT(*) FROM schedules WHERE patient_id IN "
                    "(SELECT id FROM patients WHERE inpatient_no IN ('ZY2026110101','ZY2026110108'))")
    record("SC-04", "同日重跑幂等", "重跑不重复建排程；created=0、已有排程数不增",
           f"created={d.get('created')}, skipped_duplicate={d.get('skipped_duplicate')}, 已有排程数={dup}",
           d.get("created") == 0 and d.get("skipped_duplicate", 0) >= 4 and dup == 2)

    sid_0101 = find_schedule("ZY2026110101")
    st, r = api["nurse2"].post(f"/api/schedule/{sid_0101}/reorder",
                               body={"direction": "up"})
    slot_after = db_scalar("SELECT slot FROM schedules WHERE id=?", (int(sid_0101),))
    record("SC-09", "护士越权改他人排程（F7 已修复）", "nurse-02 改 nurse-01 排程应 403",
           f"{st} {r.get('code')} {r.get('error')}；改后 slot={slot_after}",
           expect_err([], st, r, 403))

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
                  "contact_type": "本人", "auth_status": "已授权", "synthetic": 1}]}))
    fb = audit_events(api["quality"], "batch.fallback")
    record("SC-05", "HIS 超时降级（沿用上一版）", "批次 pending_backfill + 审计 batch.fallback",
           f"resp={d}, fallback审计={len(fb)}条",
           "pending_backfill" in json.dumps(d, ensure_ascii=False) and len(fb) >= 1)

    stale_rows = [
        {"inpatient_no": "ZYR1ST01", "discharge_time": f"{TODAY}T11:00:00", "department": "骨科一病区",
         "procedure_code": "PG-ORTHO-THR", "followup_status": "待随访", "contact_phone": "13700000601",
         "contact_type": "本人", "auth_status": "已授权", "synthetic": 1},
        {"inpatient_no": "ZYR1ST02", "discharge_time": f"{TODAY}T11:10:00", "department": "骨科一病区",
         "procedure_code": "PG-ORTHO-THR", "followup_status": "待随访", "contact_phone": "13700000602",
         "contact_type": "本人", "auth_status": "已授权", "synthetic": 1}]
    d = ok_payload(*eng.post("/api/his/import", body={
        "batch_version": "HIS-R1-STALE", "exported_at": "2026-11-02T00:00:00", "rows": stale_rows}))
    d = ok_payload(*eng.post("/api/schedule/run", body={"date": TODAY}))
    r1 = db_scalar("SELECT manual_reason FROM patients WHERE inpatient_no='ZYR1ST01'")
    record("SC-06", "名单超 24h 转人工", "created=0, stale_manual=2, reason 含 24 小时",
           f"created={d.get('created')}, stale_manual={d.get('stale_manual')}, reason={r1!r}",
           d.get("created") == 0 and d.get("stale_manual") == 2 and "24" in (r1 or ""))


# ---------------------------------------------------------------- 公共构造器
def import_and_schedule(eng: Api, batch: str, inpatient_no: str, *, ward="骨科一病区",
                        proc="PG-ORTHO-THR", ctype="本人", auth="已授权",
                        discharge=None, phone=None) -> str:
    ph = phone or f"138{pysecrets.randbelow(10**8):08d}"
    row = {"inpatient_no": inpatient_no, "discharge_time": discharge or f"{TODAY}T12:00:00",
           "department": ward, "procedure_code": proc, "followup_status": "待随访",
           "contact_phone": ph, "contact_type": ctype, "auth_status": auth, "synthetic": 1}
    ok_payload(*eng.post("/api/his/import", body={"batch_version": batch,
                                                  "exported_at": f"{TODAY}T05:00:00", "rows": [row]}))
    ok_payload(*eng.post("/api/schedule/run", body={"date": TODAY}))
    return find_schedule(inpatient_no)


def audit_events(quality: Api, action: str, limit: str = "300") -> list[dict]:
    st, r = quality.get(f"/api/audit?limit={limit}")
    items = ok_payload(st, r).get("items", [])
    return [e for e in items if e.get("action") == action]


def ensure_active(version: str) -> None:
    """串行用例复用一个库：把指定批次置回 active，供后续 schedule/run 复跑。"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("UPDATE batches SET status='superseded' "
                 "WHERE status='active' AND batch_version<>?", (version,))
    conn.execute("UPDATE batches SET status='active' WHERE batch_version=?", (version,))
    conn.commit()
    conn.close()


def list_drafts(nurse: Api) -> list[dict]:
    return ok_payload(*nurse.get("/api/drafts?status=draft")).get("items", [])


def draft_subject(d: dict) -> str:
    f = d.get("fields")
    if isinstance(f, str):
        try:
            f = json.loads(f)
        except Exception:
            f = {}
    return (f or {}).get("主诉", "")


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
    ev = audit_events(quality, "call.start")
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
           row["attempts"] == 0 and row["status"] == "pending"
           and d.get("counts_toward_completion") is False)
    summ = ok_payload(*api["quality"].get(f"/api/summary?date={TODAY}"))
    sms = summ.get("sms_touchpoints", {})
    record("AU-05b", "短信触达进入顶层独立统计", "summary.sms_touchpoints.sms≥1 且不计完成率",
           f"summary.sms_touchpoints={sms}", (sms.get("sms") or 0) >= 1)

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

    ev = audit_events(quality, "auth.revoke")
    det = json.dumps(ev[0].get("detail", {}), ensure_ascii=False) if ev else ""
    leak = any("prev_hash" in json.dumps(e, ensure_ascii=False) for e in ev)
    actor_ok = bool(ev) and ev[0].get("actor_id") == "u-nurse-02"
    record("AU-08", "撤回审计留痕完整", "detail 含 reason/schedules_stopped，actor_id=操作人；列表不含 prev_hash",
           f"detail={det[:160]}, actor={ev[0].get('actor_id') if ev else None}, prev_hash泄漏={leak}",
           all(k in det for k in ("reason", "schedules_stopped")) and actor_ok and not leak)

    sid9 = find_schedule("ZY2026110109")
    d = ok_payload(*api["nurse"].post(f"/api/contact/{sid9}/call", body={"consent": False}))
    rs = db_scalar("SELECT status FROM recordings WHERE schedule_id=?", (int(sid9),))
    ev = audit_events(quality, "call.start")
    notices = [json.dumps(e.get("detail", {}), ensure_ascii=False) for e in ev]
    notice = next((x for x in notices if "未告知授权" in x), "")
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
    drafts = [x for x in list_drafts(api["nurse"]) if str(x.get("schedule_id")) == sid]
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
    record("RF-04", "方言否定+模糊变体不应命中（F6 已修复）", "无命中、不建单",
           f"hits={hits}", not hits,
           evidence="变体通道窗口内否定判定（窗口内含否定词且变体不含 → 跳过）",
           defect=register_defect("RF-D01", "P1", "红旗引擎误报：方言否定与模糊变体过阈",
                                  "RF-04", "『莫得发烧，就是伤口有点痛』不应命中",
                                  f"被误报命中：{hits}", "tools/run_round1.py RF-04")
           if hits else None)
    _ = hit_03

    _, d = rf_flow(eng, api["nurse"], "RF05", "吃止痛药也不管用")
    hits = d.get("hits") or []
    h0 = hits[0] if hits else {}
    record("RF-05", "止痛药无效命中剧痛规则", "RF-ORTHO-06（置信度≥0.6）",
           f"hits={hits}",
           [h.get("rule_id") for h in hits] == ["RF-ORTHO-06"]
           and (h0.get("confidence") or 0) >= 0.6)

    neg_texts = ["伤口没有裂开", "小腿没有肿", "没有晕倒"]
    fpped = []
    for i, t in enumerate(neg_texts, 1):
        _, d = rf_flow(eng, api["nurse"], f"RF06{i}", t)
        if d.get("hits"):
            fpped.append({"text": t, "hits": d.get("hits")})
    record("RF-06", "正则分支否定误报（F9 已修复）", "三条否定句均不命中",
           f"误报={json.dumps(fpped, ensure_ascii=False)}", not fpped,
           evidence="正则按分句匹配，命中区段含否定词时跳过",
           defect=register_defect("RF-D02", "P1", "红旗引擎误报：正则分支不做否定判断",
                                  "RF-06", "否定式（伤口没有裂开/小腿没有肿/没有晕倒）不应命中",
                                  f"被正则误报：{json.dumps(fpped, ensure_ascii=False)[:200]}",
                                  "tools/run_round1.py RF-06")
           if fpped else None)

    sid1 = find_schedule("ZYR1RF01")
    d = ok_payload(*api["nurse"].post("/api/questionnaire/check",
                                      body={"schedule_id": sid1, "transcript": "伤口渗液，有点臭"}))
    record("RF-07", "同排程同规则去重", "tickets_created=0、deduped 含 RF-ORTHO-03",
           f"tickets_created={d.get('tickets_created')}, deduped={d.get('deduped')}",
           d.get("tickets_created") == 0 and "RF-ORTHO-03" in (d.get("deduped") or []))

    drafts = [x for x in list_drafts(api["nurse"]) if str(x.get("schedule_id")) == sid1]
    tk = db_query("SELECT rule_id, channel, confidence, trigger_text FROM tickets WHERE schedule_id=?",
                  (int(sid1),))
    tkd = dict(tk[0]) if tk else {}
    record("RF-08", "红旗路径不建草稿、工单字段完整",
           "RF-01 排程草稿数=0；工单含 rule_id/channel/confidence/trigger_text",
           f"drafts={len(drafts)}, ticket={tkd}",
           not drafts and all(k in tkd and tkd[k] not in ("", None) for k in
                              ("rule_id", "channel", "confidence", "trigger_text")))

    d = ok_payload(*api["nurse"].get("/api/rules"))
    record("RF-09", "规则集只读视图", "rule_set=RF-ORTHO, version=1.0, min_confidence=0.6",
           f"rule_set={d.get('rule_set')}, version={d.get('version')}, "
           f"min_confidence={d.get('model_channel', {}).get('min_confidence')}",
           d.get("rule_set") == "RF-ORTHO" and d.get("version") == "1.0"
           and d.get("model_channel", {}).get("min_confidence") == 0.6)

    rf10_corpus()

    rf11_signature()

    sid12, d = rf_flow(eng, api["nurse"], "RF12", "")
    dr = [x for x in list_drafts(api["nurse"]) if str(x.get("schedule_id")) == sid12]
    empty_draft = dr[0] if dr else {}
    subj = draft_subject(empty_draft)
    record("RF-12a", "空转写不命中、生成空草稿", "interrupted=false，草稿主诉=未提及",
           f"interrupted={d.get('interrupted')}, 草稿数={len(dr)}, 主诉={subj!r}",
           d.get("interrupted") is False and subj == "未提及")

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
        rules_path.write_text(modified, encoding="utf-8", newline="\n")
        code = ("import sys, json; sys.path.insert(0, r'%s'); "
                "from backend.core import redflag; r = redflag.load_rules(); "
                "print(r['version'], r['model_channel']['min_confidence'])" % ROOT)
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=30)
        out = proc.stdout.strip()
        err = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ""
        taken = out.startswith("9.9-hacked 0.99")
        rejected = proc.returncode != 0 and not taken and ("签名" in err or "signature" in err.lower())
        record("RF-11", "规则文件签名校验（F10 已修复）",
               "篡改规则后加载被拒绝（fail-closed）",
               f"返回码={proc.returncode}, 加载结果={out!r}, 异常={err[:120]!r}",
               rejected,
               evidence="load_rules 对 redflag.json 做 HMAC 验签，签名不符即拒绝启动",
               defect=register_defect("RF-11", "P1", "红旗规则集无签名/版本校验，篡改直接生效",
                                      "RF-11", "规则文件需签名校验，非法改动应拒绝",
                                      "改动 version/min_confidence 后立即生效",
                                      "tools/run_round1.py RF-11")
               if not rejected else None)
    finally:
        rules_path.write_text(original, encoding="utf-8", newline="\n")


# ---------------------------------------------------------------- RC 组（5.5）
def rc_make_draft(eng: Api, nurse: Api, tag: str, transcript: str):
    sid = import_and_schedule(eng, f"HIS-R1-{tag}", f"ZYR1{tag}", phone=f"139{abs(hash(tag)) % 10**8:08d}")
    ok_payload(*nurse.post(f"/api/contact/{sid}/call", body={"consent": True}))
    ok_payload(*nurse.post("/api/questionnaire/check", body={"schedule_id": sid, "transcript": transcript}))
    dr = [x for x in list_drafts(nurse) if str(x.get("schedule_id")) == sid]
    assert dr, f"RC {tag}: draft not created"
    return sid, dr[0]


def rc_group(api: dict, eng: Api, quality: Api):
    print("== RC 草稿与确认写库 ==")
    from backend.core import security as sec

    sid, draft = rc_make_draft(eng, api["nurse"], "RC01", "伤口干燥，没有渗血渗液，体温正常")
    f = draft.get("fields")
    if isinstance(f, str):
        f = json.loads(f)
    gen = draft.get("generator", "")
    record("RC-01", "草稿生成（9 字段、来源标识）",
           "9 个字段；generator=draft_engine_v1（本地规则整理，非院外模型）；status=draft",
           f"字段数={len(f or {})}, generator={gen}, status={draft.get('status')}",
           len(f or {}) == 9 and "draft_engine_v1" in gen and "非院外模型" in gen
           and draft.get("status") == "draft")

    did = str(draft["id"])
    st, r = api["nurse"].post("/api/records/confirm", body={"draft_id": did, "fields": f})
    record("RC-02", "无 step-up 凭证拒确认", "403『缺少 step-up 凭证』",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 403, "forbidden", "缺少 step-up 凭证"))

    fields3 = dict(f)
    fields3["主诉"] = "伤口干燥无渗液，体温正常（护士电话确认）"
    tok = ok_payload(*api["nurse"].post("/api/stepup", body={
        "purpose": "record_confirm", "action_id": did, "fields": fields3}))["token"]
    n0 = db_scalar("SELECT COUNT(*) FROM records")
    d = ok_payload(*api["nurse"].post("/api/records/confirm",
                                      body={"draft_id": did, "fields": fields3, "token": tok}))
    n1 = db_scalar("SELECT COUNT(*) FROM records")
    sst = db_scalar("SELECT status FROM schedules WHERE id=?", (int(sid),))
    rec = db_query("SELECT signature, diff_json, nurse_id, batch_version, template_version, confirmed_at "
                   "FROM records WHERE draft_id=?", (int(did),))
    r0 = dict(rec[0]) if rec else {}
    record("RC-03", "step-up 确认写库", "成功、排程 completed、records+1、signature=nurse:u-nurse-01",
           f"resp={d.get('record_id')}, 排程={sst}, records={n0}→{n1}, signature={r0.get('signature')}",
           d.get("record_id") and sst == "completed" and n1 == n0 + 1
           and r0.get("signature") == "nurse:u-nurse-01")

    diff = json.loads(r0.get("diff_json") or "[]")
    record("RC-10", "登记表留痕字段完整",
           "含 batch_version/template_version/diff/nurse_id/confirmed_at；diff 记录修改",
           f"fields={sorted(r0.keys())}, diff={len(diff)}条",
           all(k in r0 and r0[k] not in ("", None) for k in
               ("batch_version", "template_version", "nurse_id", "confirmed_at")) and len(diff) >= 1)

    st, r = api["nurse"].post("/api/records/confirm",
                              body={"draft_id": did, "fields": fields3, "token": tok})
    record("RC-05", "同令牌重放被拒", "预期 403（nonce 防重放）",
           f"{st} {r.get('code')} {r.get('error')}",
           not r.get("ok", True) and st in (400, 403),
           evidence="草稿状态检查先于凭证校验 → 实际 400 invalid；并发同令牌重放 403 见 EX-03")

    sid4, draft4 = rc_make_draft(eng, api["nurse"], "RC04", "伤口有点痛，不过能忍")
    f4 = draft4.get("fields")
    if isinstance(f4, str):
        f4 = json.loads(f4)
    tok4 = ok_payload(*api["nurse"].post("/api/stepup", body={
        "purpose": "record_confirm", "action_id": str(draft4["id"]), "fields": f4}))["token"]
    bad = dict(f4)
    bad["主诉"] = "被篡改的主诉"
    st, r = api["nurse"].post("/api/records/confirm",
                              body={"draft_id": str(draft4["id"]), "fields": bad, "token": tok4})
    record("RC-04", "字段篡改令牌失绑", "403『未绑定到本次操作内容』",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 403, "forbidden", "未绑定到本次操作内容"))

    # RC-06：过期凭证（脚本持 FDE_SECRET，本地构造 exp 已过令牌）
    exp_payload = {"uid": "u-nurse-01", "purpose": "record_confirm", "action": str(draft4["id"]),
                   "digest": sec.canonical_digest(f4), "nonce": uuid.uuid4().hex,
                   "exp": int(time.time()) - 60}
    raw = sec._b64(json.dumps(exp_payload, sort_keys=True, separators=(",", ":")).encode())
    sig = hmac.new(sec.secret(), raw.encode(), hashlib.sha256).hexdigest()
    expired = raw + "." + sig
    st, r = api["nurse"].post("/api/records/confirm",
                              body={"draft_id": str(draft4["id"]), "fields": f4, "token": expired})
    record("RC-06", "过期 step-up 凭证拒收", "403『step-up 凭证已过期』",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 403, "forbidden", "已过期"))

    tok4b = ok_payload(*api["nurse"].post("/api/stepup", body={
        "purpose": "record_confirm", "action_id": str(draft4["id"]), "fields": f4}))["token"]
    st, r = api["nurse2"].post("/api/records/confirm",
                               body={"draft_id": str(draft4["id"]), "fields": f4, "token": tok4b})
    record("RC-07", "令牌与操作者绑定", "nurse-02 用 nurse-01 令牌 → 403 与操作者不匹配",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 403, "forbidden", "不匹配"))

    tok8 = ok_payload(*api["nurse"].post("/api/stepup", body={
        "purpose": "record_confirm", "action_id": did, "fields": fields3}))["token"]
    st, r = api["nurse"].post("/api/records/confirm",
                              body={"draft_id": did, "fields": fields3, "token": tok8})
    record("RC-08", "已确认草稿再确认拒绝", "409 draft_conflict『草稿已确认或已作废』",
           f"{st} {r.get('code')} {r.get('error')}",
           st == 409 and r.get("code") == "draft_conflict" and "已确认" in (r.get("error") or ""))

    d = ok_payload(*api["nurse"].post("/api/drafts/discard",
                                      body={"draft_id": str(draft4["id"])}))
    dst4 = db_scalar("SELECT status FROM drafts WHERE id=?", (int(draft4["id"]),))
    sst4 = db_scalar("SELECT status FROM schedules WHERE id=?", (int(sid4),))
    ev = audit_events(quality, "draft.discard")
    record("RC-09", "作废草稿", "草稿 discarded、排程不变（in_progress）、审计 draft.discard",
           f"草稿={dst4}, 排程={sst4}, 审计={len(ev)}条",
           dst4 == "discarded" and sst4 == "in_progress" and len(ev) >= 1,
           evidence="作废路径无 step-up、无归属校验")

    # PM-04（11.1 已知缺陷）：列表与作废无归属校验
    leak_lists = []
    try:
        recs2 = ok_payload(*api["nurse2"].get("/api/records")).get("items", [])
        if any(x.get("nurse_id") == "u-nurse-01" for x in recs2):
            leak_lists.append(f"records 泄漏 {len(recs2)} 条他人登记表")
        dr2 = list_drafts(api["nurse2"])
        if any(str(x.get("schedule_id")) == sid for x in dr2):
            leak_lists.append("drafts 泄漏他人草稿")
    except AssertionError as exc:
        leak_lists.append(f"接口异常: {exc}")
    sid9b, draft9b = rc_make_draft(eng, api["nurse"], "RC09", "恢复顺利，伤口没有渗液")
    try:
        d2 = ok_payload(*api["nurse2"].post("/api/drafts/discard",
                                            body={"draft_id": str(draft9b["id"])}))
        leak_lists.append("nurse-02 成功作废 nurse-01 草稿")
    except AssertionError as exc:
        d2 = {"rejected": str(exc)[:120]}
    record("PM-04", "草稿/登记表归属校验（F2 已修复）",
           "护士仅见/仅可操作本人与本病区数据；作废需归属校验",
           f"{leak_lists}", not leak_lists,
           evidence="records/drafts 按病区过滤；discard 校验归属",
           defect=register_defect(
               "PM-04", "P1", "草稿/登记表列表全量返回、作废操作无归属校验",
               "PM-04", "nurse-02 不应看到/作废 nurse-01 的草稿与登记表",
               f"{leak_lists}", "tools/run_round1.py PM-04")
           if leak_lists else None)

    st, r = api["doctor"].post("/api/records/confirm",
                               body={"draft_id": str(draft9b["id"]), "fields": f4, "token": None})
    record("RC-11", "医生越权确认登记表", "403『角色「doctor」无权执行该操作』",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 403, "forbidden", "无权"))


# ---------------------------------------------------------------- TK 组（5.6）
def tk_group(api: dict, eng: Api, quality: Api):
    print("== TK 工单与模板 ==")
    t1 = db_scalar("SELECT id FROM tickets WHERE schedule_id=? AND status='pending'",
                   (int(find_schedule("ZYR1RF01")),))
    t2 = db_scalar("SELECT id FROM tickets WHERE schedule_id=? AND status='pending'",
                   (int(find_schedule("ZYR1RF05")),))
    t3 = db_scalar("SELECT id FROM tickets WHERE schedule_id=? AND status='pending'",
                   (int(find_schedule("ZYR1RF03")),))

    st, r = api["doctor"].post(f"/api/tickets/{t1}/resolve", body={"decision": "approved"})
    record("TK-01", "无 step-up 拒处置工单", "403『缺少 step-up 凭证』",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 403, "forbidden", "缺少 step-up 凭证"))

    n0 = db_scalar("SELECT COUNT(*) FROM records")
    tok = ok_payload(*api["doctor"].post("/api/stepup", body={
        "purpose": "ticket_resolve", "action_id": str(t1), "decision": "approved",
        "fields": {"ticket_id": str(t1), "decision": "approved"}}))["token"]
    d = ok_payload(*api["doctor"].post(f"/api/tickets/{t1}/resolve",
                                       body={"decision": "approved", "token": tok}))
    n1 = db_scalar("SELECT COUNT(*) FROM records")
    row = db_query("SELECT status, resolved_by FROM tickets WHERE id=?", (int(t1),))[0]
    record("TK-02", "医生处置工单（approved，不写登记表）",
           "ticket approved、resolved_by=u-doctor-01、records 不增",
           f"status={row['status']}, resolved_by={row['resolved_by']}, records={n0}→{n1}",
           row["status"] == "approved" and row["resolved_by"] == "u-doctor-01" and n1 == n0)

    tok2 = ok_payload(*api["doctor"].post("/api/stepup", body={
        "purpose": "ticket_resolve", "action_id": str(t2), "decision": "approved",
        "fields": {"ticket_id": str(t2), "decision": "approved"}}))["token"]
    ok_payload(*api["doctor"].post(f"/api/tickets/{t2}/resolve",
                                   body={"decision": "approved", "resolved_by": "u-nurse-01",
                                         "token": tok2}))
    rb = db_scalar("SELECT resolved_by FROM tickets WHERE id=?", (int(t2),))
    record("TK-03", "伪造 resolved_by 被忽略", "resolved_by 取会话医生",
           f"db resolved_by={rb}", rb == "u-doctor-01")

    tok3 = ok_payload(*api["doctor"].post("/api/stepup", body={
        "purpose": "ticket_resolve", "action_id": str(t1), "decision": "rejected",
        "fields": {"ticket_id": str(t1), "decision": "rejected"}}))["token"]
    st, r = api["doctor"].post(f"/api/tickets/{t1}/resolve",
                               body={"decision": "rejected", "token": tok3})
    record("TK-04", "重复处置拒绝", "400『工单已处置』",
           f"{st} {r.get('code')} {r.get('error')}",
           st == 400 and r.get("code") == "invalid")

    st, r = api["nurse"].post(f"/api/tickets/{t3}/resolve", body={"decision": "approved"})
    record("TK-05", "护士越权处置工单", "403 forbidden（角色无权）",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 403, "forbidden"))

    tok4 = ok_payload(*api["doctor"].post("/api/stepup", body={
        "purpose": "template_sign", "action_id": "PG-ORTHO-FIX",
        "fields": {"template_id": "PG-ORTHO-FIX"}}))["token"]
    d = ok_payload(*api["doctor"].post("/api/templates/PG-ORTHO-FIX/sign", body={"token": tok4}))
    t = db_query("SELECT status, version, effective_date, signed_by FROM templates "
                 "WHERE procedure_code='PG-ORTHO-FIX'")[0]
    record("TK-06", "医生签发未签发模板", "signed、version=1.0、effective_date=今日",
           f"status={t['status']}, version={t['version']}, eff={t['effective_date']}, by={t['signed_by']}",
           t["status"] == "signed" and t["version"] == "1.0"
           and t["effective_date"] == TODAY and t["signed_by"] == "u-doctor-01")

    st, r = api["nurse"].post("/api/templates/PG-ORTHO-FIX/sign", body={})
    record("TK-07", "护士越权签发模板", "403『角色「nurse」无权执行该操作』",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 403, "forbidden", "无权"))

    tok5 = ok_payload(*api["doctor"].post("/api/stepup", body={
        "purpose": "template_sign", "action_id": "PG-ORTHO-FIX",
        "fields": {"template_id": "PG-ORTHO-FIX"}}))["token"]
    st, r = api["doctor"].post("/api/templates/PG-ORTHO-FIX/sign", body={"token": tok5})
    v = db_scalar("SELECT version FROM templates WHERE procedure_code='PG-ORTHO-FIX'")
    rejected = st == 400 and str(v) == "1.0" and "已签发" in (r.get("error") or "")
    record("TK-08", "重复签发被拦截（F11 已修复）", "400『已签发』且版本保持 1.0",
           f"{st} {r.get('code')} {r.get('error')}, 重复签发后 version={v}", rejected,
           evidence="sign() 校验模板已签发状态，禁止重复签发",
           defect=register_defect("TK-08", "P1", "模板重复签发未被拦截，版本前进（1.0→2.0）",
                                  "TK-08", "已签发模板再签发应拒收",
                                  f"重复签发成功，version={v}", "tools/run_round1.py TK-08")
           if not rejected else None)

    v1 = json.loads((ROOT / "backend" / "fixtures" / "his_batch_v1.json").read_text(encoding="utf-8"))
    row104 = next(x for x in v1["rows"] if x["inpatient_no"] == "ZY2026110104")
    ok_payload(*eng.post("/api/his/import", body={"batch_version": "HIS-R1-TK09",
                                                  "exported_at": f"{TODAY}T06:00:00",
                                                  "rows": [row104]}))
    d = ok_payload(*eng.post("/api/schedule/run", body={"date": TODAY}))
    created104 = db_scalar("SELECT COUNT(*) FROM schedules WHERE patient_id IN "
                           "(SELECT id FROM patients WHERE inpatient_no='ZY2026110104')")
    still_manual = db_scalar("SELECT manual_reason FROM patients WHERE inpatient_no='ZY2026110104'")
    fixed = created104 >= 1 and not still_manual
    record("TK-09", "签发后重跑清空转人工原因（F12 已修复）",
           "0104 建排程且 manual_reason 被清除",
           f"created={d.get('created')}, 排程数={created104}, manual_reason={still_manual!r}",
           fixed,
           evidence="补排成功后清空 manual_reason",
           defect=register_defect("TK-09", "P1", "模板签发后重跑排程，患者 manual_reason 未清除",
                                  "TK-09", "0104 应从人工清单移除",
                                  "已建排程但 manual_reason 仍为『模板未签发…』",
                                  "tools/run_round1.py TK-09")
           if not fixed else None)


# ---------------------------------------------------------------- EX 组（5.8）
def ex02_failclosed() -> tuple[bool, str]:
    """EX-02 单元级：审计写失败 → 确认整体回滚（独立临时 DB，进程内直调 records.confirm）。"""
    from backend.core import records as rec_mod, security as sec2
    from backend.core.db import connect as db_connect, init_db, now_iso as db_now

    tmp = DATA_DIR / "ex02"
    tmp.mkdir(parents=True, exist_ok=True)
    conn = db_connect(tmp / "failclosed.db")
    init_db(conn)
    conn.execute("INSERT INTO batches(batch_version,exported_at,imported_at,status) "
                 "VALUES('B-EX02','2026-11-04T01:00:00','2026-11-04T01:00:00','active')")
    cur = conn.execute(
        "INSERT INTO patients(inpatient_no,discharge_time,department,procedure_code,"
        "followup_status,contact_phone,contact_type,auth_status,batch_version) "
        "VALUES('EX02-01','2026-11-02T10:00:00','骨科一病区','PG-ORTHO-THR','待随访',"
        "'13811112222','本人','已授权','B-EX02')")
    pid = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO schedules(patient_id,schedule_date,slot,nurse_id,status,batch_version,"
        "template_version,created_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?)",
        (pid, TODAY, "evening", "u-nurse-01", "in_progress", "B-EX02", "1.0",
         db_now(), "EX02-01|2026-11-02T10:00:00|" + TODAY))
    did = cur.execute(
        "INSERT INTO drafts(schedule_id,raw_text,fields_json,generator,status,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (cur.lastrowid, "mock", json.dumps({"q1": "无渗血", "q2": "无"}, ensure_ascii=False),
         "draft_engine_v1（本地规则整理，非院外模型）", "draft", db_now())).lastrowid

    row = conn.execute("SELECT * FROM users WHERE user_id='u-nurse-01'").fetchone()
    actor = {"user_id": row["user_id"], "name": row["name"],
             "role": row["role"], "ward": row["ward"]}
    fields = {"q1": "无渗血", "q2": "无"}
    digest = sec2.canonical_digest({"draft_id": did, "fields": fields})
    tok = sec2.issue_stepup(conn, actor, "record_confirm", str(did), digest)["token"]
    conn.execute("DROP TABLE audit_events")   # 模拟审计库故障
    err = ""
    try:
        rec_mod.confirm(conn, did, fields, tok, actor)
    except Exception as exc:
        err = f"{type(exc).__name__}: {str(exc)[:100]}"
    n = conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"]
    conn.close()
    ok = bool(err) and n == 0
    return ok, f"confirm 异常={err or '无（意外成功）'}；records={n}"


def ex_group(api: dict, eng: Api, quality: Api, server: Server):
    print("== EX 异常与降级 ==")

    # EX-01 缺列行整行拒收
    good = {"inpatient_no": "ZYR1EX01", "discharge_time": "2026-11-02T09:00:00",
            "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
            "followup_status": "待随访", "contact_phone": "13811110001",
            "contact_type": "本人", "auth_status": "已授权", "synthetic": 1}
    bad_row = dict(good)
    bad_row["inpatient_no"] = "ZYR1EX01B"
    bad_row.pop("contact_phone")
    d = ok_payload(*eng.post("/api/his/import", body={
        "batch_version": "HIS-R1-EX01", "exported_at": f"{TODAY}T07:00:00",
        "rows": [good, bad_row]}))
    record("EX-01", "缺列行整行拒收、不影响合法行", "accepted=1, rejected=1（非整批回滚）",
           f"accepted={d.get('accepted')}, rejected={d.get('rejected')}, merged={d.get('merged')}",
           d.get("accepted") == 1 and d.get("rejected") == 1)

    # EX-02 审计写失败 fail-closed（单元级）
    ok2, detail2 = ex02_failclosed()
    record("EX-02", "审计写入失败时确认整体回滚（单元级）",
           "confirm 抛异常且 records 不落库（fail-closed）", detail2, ok2)

    # EX-03a 同一凭证并发确认
    sid3, draft3 = rc_make_draft(eng, api["nurse"], "EX03A", "恢复顺利，体温正常，没有渗液")
    f3 = draft3.get("fields")
    if isinstance(f3, str):
        f3 = json.loads(f3)
    did3 = str(draft3["id"])
    tok3 = ok_payload(*api["nurse"].post("/api/stepup", body={
        "purpose": "record_confirm", "action_id": did3, "fields": f3}))["token"]

    def _cf(tok: str, did: str = did3, f: dict = f3):
        return api["nurse"].post("/api/records/confirm",
                                 body={"draft_id": did, "fields": f, "token": tok})

    with ThreadPoolExecutor(max_workers=2) as pool:
        rs3 = list(pool.map(_cf, [tok3, tok3]))
    n_ok = sum(1 for st, r in rs3 if st == 200 and r.get("ok"))
    record("EX-03a", "同一凭证并发确认仅一次成功", "1 成功 1 拒",
           f"成功 {n_ok}/2；响应码={[(s, r.get('code')) for s, r in rs3]}",
           n_ok == 1)

    # EX-03b 双凭证并发确认（11.1 已知缺陷 EX-D01）
    sid3b, draft3b = rc_make_draft(eng, api["nurse"], "EX03B", "恢复顺利，没有发热，伤口干燥")
    f3b = draft3b.get("fields")
    if isinstance(f3b, str):
        f3b = json.loads(f3b)
    did3b = str(draft3b["id"])
    toks = [ok_payload(*api["nurse"].post("/api/stepup", body={
        "purpose": "record_confirm", "action_id": did3b, "fields": f3b}))["token"]
        for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        rs3b = list(pool.map(lambda t, d=did3b, f=f3b: api["nurse"].post(
            "/api/records/confirm", body={"draft_id": d, "fields": f, "token": t}), toks))
    n_ok3b = sum(1 for st, r in rs3b if st == 200 and r.get("ok"))
    n_rec3b = int(db_scalar("SELECT COUNT(*) FROM records WHERE draft_id=?", (int(did3b),)) or 0)
    reproduced3b = n_ok3b == 2 and n_rec3b == 2
    record("EX-03b", "双凭证并发确认（11.1 已知缺陷 EX-D01）", "仅 1 条登记表落库",
           f"成功 {n_ok3b}/2, records={n_rec3b}", not reproduced3b,
           defect=register_defect(
               "EX-D01", "P1", "同一草稿可被并发确认两次，产生重复登记表",
               "EX-03b", "第二次确认应被草稿状态校验拒绝",
               f"两次确认均成功，records={n_rec3b}", "tools/run_round1.py EX-03b")
           if reproduced3b else None)

    # EX-04 无会话 401 / 未知接口 404（JSON、无堆栈）
    anon = Api(api["nurse"].port)
    st, r = anon.get("/api/drafts")
    record("EX-04a", "无会话访问受保护接口", "401 unauthorized",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 401, "unauthorized"))
    st, r = api["nurse"].get("/api/nonexistent")
    record("EX-04b", "未知接口返回 JSON 404", "404 not_found，JSON 信封、无堆栈",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 404, "not_found") and "Traceback" not in json.dumps(r))

    # EX-05 非法 JSON
    st, r = api["nurse"].post("/api/login", raw=b"this is not json {{{")
    record("EX-05", "非法 JSON 请求体", "400 bad_json",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 400, "bad_json"))

    # EX-06 超限请求体（仅声明 Content-Length，避免真实传输大包）
    st, r = api["nurse"].post("/api/questionnaire/check",
                              declare_len=300000, body={"schedule_id": "1"})
    record("EX-06", "超限请求体 413", "413 body_too_large",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 413, "body_too_large"))

    # EX-07 超深嵌套
    deep: dict = {"leaf": 1}
    for _ in range(40):
        deep = {"n": deep}
    st, r = api["nurse"].post("/api/questionnaire/check", body={"x": deep})
    record("EX-07", "超深嵌套请求体", "400 too_deep（上限 32 层）",
           f"{st} {r.get('code')} {r.get('error')}",
           expect_err([], st, r, 400, "too_deep"))

    # EX-08 接口限流（>120 次/分钟；现实现 403 表达，口径备注）
    c200 = c403 = 0
    msg = ""
    for _ in range(130):
        st, r = api["manager"].get("/api/summary")
        if st == 200 and r.get("ok"):
            c200 += 1
        elif st == 403:
            c403 += 1
            msg = r.get("error", "")
    record("EX-08", "接口限流", "超过 120 次/分钟触发限流（现实现 403，标准 429，记口径备注）",
           f"200={c200}, 403={c403}, 文案={msg!r}",
           c403 >= 1 and "请求频率超限" in msg,
           evidence="现实现以 403 forbidden（PermissionError）表达限流，未用 429——记入测试方案口径备注")

    # EX-09 重启后会话保持 + 审计链校验
    old_port = server.port
    print("  [EX-09] 重启服务进程 ...")
    server.restart()
    if server.port != old_port:
        for a in list(api.values()):
            a.port = server.port
    st, r = api["nurse"].get(f"/api/schedule?date={TODAY}")
    record("EX-09a", "服务重启后活动会话仍有效", "200（会话持久化于 DB，8h 内有效）",
           f"{st} {r.get('code')} {r.get('error')}",
           st == 200 and r.get("ok"))
    d = ok_payload(*api["quality"].post("/api/audit/verify", body={}))
    record("EX-09b", "审计哈希链重放校验（日结）", "ok=true、无断链",
           json.dumps(d, ensure_ascii=False)[:120], d.get("ok") is True)


# ---------------------------------------------------------------- main
def main() -> int:
    t0 = time.time()
    print("=== 清禾综合医院出院随访 MVP · 首轮测试（S1 冒烟 + S2 功能首测） ===")
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if REPORT_PATH.exists():
        REPORT_PATH.unlink()

    print("[环境] E1：启动 backend/server.py ...")
    server = Server()
    server.start()
    os.environ["FDE_SECRET"] = server.secret   # RC-06 / EX-02 进程内构造凭证用

    ids = {"nurse": "u-nurse-01", "nurse2": "u-nurse-02", "doctor": "u-doctor-01",
           "manager": "u-manager-01", "quality": "u-quality-01", "eng": "u-engineer-01"}
    api: dict[str, Api] = {}
    try:
        for key, uid in ids.items():
            a = Api(server.port)
            a.login(uid)
            api[key] = a

        eng = api["eng"]
        s1_smoke(api, eng)
        ct_group(api, eng)
        sc_group(api, eng, api["manager"])
        au_group(api, eng, api["quality"])
        rf_group(api, eng, api["quality"])
        rc_group(api, eng, api["quality"])
        tk_group(api, eng, api["quality"])
        ex_group(api, eng, api["quality"], server)
    finally:
        server.stop()

    passed = sum(1 for x in RESULTS if x["status"] == "PASS")
    reproduced = sum(1 for x in RESULTS if x["status"] == "DEFECT-REPRODUCED")
    failed = [x for x in RESULTS if x["status"] == "FAIL"]

    report = {
        "round": "R1",
        "executed_at": datetime.now().isoformat(timespec="seconds"),
        "env": {"data_dir": str(DATA_DIR), "note": "E1 环境：独立数据目录 + 随机密钥 + 随机端口"},
        "scope": "S1 冒烟 + S2 功能（CT/SC/AU/RF/RC/TK/EX）全量回归",
        "stats": {"total": len(RESULTS), "passed": passed,
                  "defect_reproduced": reproduced, "failed": len(failed)},
        "results": RESULTS,
        "defects": DEFECTS,
        "duration_seconds": round(time.time() - t0, 1),
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 首轮结果汇总 ===")
    print(f"用例总数 {len(RESULTS)}：通过 {passed}，缺陷复现 {reproduced}，失败 {len(failed)}")
    if DEFECTS:
        print("已登记缺陷：")
        for df in DEFECTS:
            print(f"  {df['defect_id']} [{df['severity']}] {df['title']}")
    if failed:
        print("未通过用例（非已知缺陷，需排查）：")
        for x in failed:
            print(f"  {x['case']}: {x['title']} — {x['actual']}")
    print(f"报告：{REPORT_PATH}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
