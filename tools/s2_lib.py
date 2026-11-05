"""S2 黑盒测试基础设施：独立数据目录、独立端口、HTTP 客户端、库内取证。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

DATE = date.today().isoformat()
SECRET = "fde-s2-test-secret"
BENIGN = "今天感觉还行，按时吃药，想复查。"
ROOT = Path(__file__).resolve().parents[1]


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def digest(payload) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def forge_token(uid: str, purpose: str, action: str, dg: str, nonce: str, exp: int) -> str:
    payload = {"uid": uid, "purpose": purpose, "action": action, "digest": dg,
               "nonce": nonce, "exp": exp}
    raw = b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    sig = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"


class Result:
    def __init__(self, cid, title, level, expected):
        self.cid, self.title, self.level, self.expected = cid, title, level, expected
        self.failures, self.evidence, self.notes = [], [], []

    def expect(self, cond, msg):
        if not cond:
            self.failures.append(msg)

    def note(self, msg):
        self.notes.append(msg)

    def payload(self, status):
        if status == "BLOCKED":
            return {"id": self.cid, "title": self.title, "level": self.level,
                    "expected": self.expected, "status": "BLOCKED", "actual": self.failures[0],
                    "evidence": self.notes, "defects": []}
        return {"id": self.cid, "title": self.title, "level": self.level,
                "expected": self.expected,
                "status": "PASS" if not self.failures else "FAIL",
                "actual": "；".join(self.failures) if self.failures else "符合预期",
                "evidence": self.evidence, "notes": self.notes, "defects": []}


class Server:
    def __init__(self, case_id, port):
        self.dir = Path(tempfile.mkdtemp(prefix=f"fde_s2_{case_id}_"))
        self.db_path = self.dir / "followup.db"
        self.port = port
        self.proc = None
        self.log_path = self.dir / "server.log"
        self.log = None
        self.extra_env: dict[str, str] = {}

    def start(self):
        self.log = open(self.log_path, "a", encoding="utf-8")
        env = os.environ.copy()
        env.update({"FDE_DATA_DIR": str(self.dir), "FDE_DB": str(self.db_path),
                    "FDE_SECRET": SECRET, "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONIOENCODING": "utf-8"})
        env.update(self.extra_env)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "backend.server", "--port", str(self.port)],
            cwd=str(ROOT), env=env, stdout=self.log, stderr=self.log)
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"服务启动失败：\n{self.read_log()}")
            try:
                status, _ = self.request("GET", "/api/bootstrap")
                if status == 200:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        raise RuntimeError(f"服务启动超时：\n{self.read_log()}")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.log:
            self.log.close()
            self.log = None

    def read_log(self):
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-1500:]
        except FileNotFoundError:
            return ""

    def request(self, method, path, token=None, body=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Session"] = token
        if raw is not None:
            data = raw.encode("utf-8") if isinstance(raw, str) else raw
        elif body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        else:
            data = None
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
        except OSError:
            return 0, {"code": "connection_closed", "error": "服务端关闭连接，未返回任何 HTTP 响应"}
        finally:
            conn.close()
        try:
            payload = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {"raw": text}
        return status, payload

    def raw_length_probe(self, path, token, declared=300000):
        req = (f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Session: {token}\r\n"
               f"Content-Type: application/json\r\nContent-Length: {declared}\r\n"
               "Connection: close\r\n\r\n").encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            s.sendall(req)
            s.sendall(b"{")
            data = s.recv(4096).decode("utf-8", "replace")
        finally:
            s.close()
        head, _, body = data.partition("\r\n\r\n")
        try:
            status = int(head.split()[1])
        except (IndexError, ValueError):
            status = 0
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"raw": data}
        return status, payload


class Ctx:
    def __init__(self, case_id, port):
        self.server = Server(case_id, port)
        self.db_path = self.server.db_path
        self.user_ids = {}

    @staticmethod
    def patient_no(no: str) -> str:
        """替身清单内的住院号为 ZY20261101+两位序号；用例里用 01xx 简写。"""
        if no.isdigit() and len(no) == 4:
            return "ZY20261101" + no[-2:]
        return no

    def start(self):
        self.server.start()
        self.server.request("GET", "/api/bootstrap")
        return self

    def stop(self):
        self.server.stop()

    def login(self, uid):
        status, p = self.server.request("POST", "/api/login", body={"user_id": uid})
        if status != 200:
            raise RuntimeError(f"登录失败 {uid}: {status} {p}")
        token = p["data"]["token"]
        self.user_ids[token] = uid
        return token

    def get(self, path, token=None):
        return self.server.request("GET", path, token=token)

    def post(self, path, token=None, body=None, raw=None):
        return self.server.request("POST", path, token=token, body=body, raw=raw)

    def ok(self, resp, what):
        status, p = resp
        if status != 200:
            raise RuntimeError(f"{what} 失败: HTTP {status} {p}")
        return p["data"]

    def expect_status(self, resp, code, code_name, what):
        status, p = resp
        if status != code:
            raise RuntimeError(f"{what}: 期望 HTTP {code}，实际 {status} {p}")
        if code_name and p.get("code") != code_name:
            raise RuntimeError(f"{what}: 期望 code={code_name}，实际 {p.get('code')} {p['error']}")
        return p

    def db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def import_fixture(self, engineer, name="his_batch_v1.json"):
        """读取替身后按运行日重写 exported_at，避免固定时钟触发 stale 分支。"""
        payload = json.loads((Path(ROOT) / "backend" / "fixtures" / name).read_text(encoding="utf-8"))
        body = {"batch_version": payload["batch_version"],
                "exported_at": f"{DATE}T02:10:00",
                "rows": payload["rows"], "note": payload.get("note", "")}
        return self.ok(self.post("/api/his/import", engineer, body), "导入")

    def import_rows(self, engineer, version, rows, exported_at=None):
        body = {"batch_version": version, "exported_at": exported_at or f"{DATE}T02:10:00",
                "rows": rows}
        return self.ok(self.post("/api/his/import", engineer, body), "导入自定义批次")

    def run_schedule(self, nurse, date=DATE):
        return self.ok(self.post("/api/schedule/run", nurse, {"date": date}), "生成排程")

    def board(self, token, date=DATE):
        return self.ok(self.get(f"/api/schedule?date={date}", token), "查看排程")

    def item(self, token, inpatient_no, date=DATE):
        target = self.patient_no(inpatient_no)
        data = self.board(token, date)
        for it in data["items"]:
            if it["inpatient_no"] == target:
                return it
        return None

    def manual_reason(self, token, inpatient_no, date=DATE):
        target = self.patient_no(inpatient_no)
        data = self.board(token, date)
        for it in data["manual_list"]:
            if it["inpatient_no"] == target:
                return it.get("manual_reason", "")
        return ""

    def call(self, token, sid, consent=True):
        return self.post(f"/api/contact/{sid}/call", token,
                         {"consent": consent, "notice": consent})

    def check(self, token, sid, transcript):
        return self.post("/api/questionnaire/check", token,
                         {"schedule_id": sid, "transcript": transcript})

    def stepup(self, token, purpose, action_id, **extra):
        body = {"purpose": purpose, "action_id": action_id}
        body.update(extra)
        return self.ok(self.post("/api/stepup", token, body), "申请 step-up")

    def confirm(self, token, draft_id, fields, step_token):
        return self.post("/api/records/confirm", token,
                         {"draft_id": draft_id, "fields": fields, "token": step_token})

    def draft_for(self, nurse, engineer, no="0101", transcript=BENIGN):
        sid = self.item(engineer, no)["id"]
        self.ok(self.call(nurse, sid), "启动外呼")
        data = self.ok(self.check(nurse, sid, transcript), "生成草稿")
        return sid, data["draft"]

    def ticket_for(self, nurse, engineer, no="0101", transcript="伤口渗液，有点臭"):
        sid = self.item(engineer, no)["id"]
        self.ok(self.call(nurse, sid), "启动外呼")
        data = self.ok(self.check(nurse, sid, transcript), "红旗检查")
        return sid, data

    def audit_items(self, quality, limit=500):
        return self.ok(self.get(f"/api/audit?limit={limit}", quality), "审计查询")["items"]

    def recordings(self, sid):
        with self.db() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM recordings WHERE schedule_id=? ORDER BY id DESC", (sid,))]

    def insert_schedule(self, inpatient_no, nurse_id="u-nurse-01"):
        target = self.patient_no(inpatient_no)
        with self.db() as conn:
            p = conn.execute("SELECT * FROM patients WHERE inpatient_no=?", (target,)).fetchone()
            seq = conn.execute("SELECT COALESCE(MAX(seq),0)+1 n FROM schedules "
                               "WHERE schedule_date=? AND nurse_id=?", (DATE, nurse_id)).fetchone()["n"]
            cur = conn.execute(
                "INSERT INTO schedules(patient_id,schedule_date,slot,nurse_id,status,attempts,seq,"
                "batch_version,template_version,created_at,idempotency_key) "
                "VALUES(?,?,?,?,'pending',0,?,?,?,?,?)",
                (p["id"], DATE, "daytime", nurse_id, seq, p["batch_version"],
                 "1.0" if p["procedure_code"] == "PG-ORTHO-FIX" else "1.0",
                 time.strftime("%Y-%m-%dT%H:%M:%S"), f"fixture|{inpatient_no}|{DATE}|{time.time()}"))
            return int(cur.lastrowid)
