"""HTTP API（标准库单进程服务）：路由、角色权限矩阵、请求防护与日志卫生。

启动：python -m backend.server --port 8765 （仅监听 127.0.0.1，无外部网络出口）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
from datetime import date as _date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .core import (audit, contact, contract, db, records, redflag, scheduling,
                   security, templates, tickets)

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"

ROLES = {"nurse", "doctor", "engineer", "quality", "manager"}
DRAIN_LIMIT_BYTES = 16 * 1024 * 1024
PUBLIC_PATHS = frozenset({"/api/login", "/api/bootstrap"})


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


# ---------------------------------------------------------------- 路由表

Routes = list[tuple[str, re.Pattern, Callable]]

routes: Routes = []


def route(method: str, pattern: str):
    compiled = re.compile("^" + pattern + "$")

    def deco(fn: Callable):
        routes.append((method, compiled, fn))
        return fn
    return deco


def _allow(user: dict[str, Any], *roles: str) -> None:
    if user["role"] not in roles:
        raise ApiError(403, "forbidden", f"角色「{user['role']}」无权执行该操作")


def _int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "bad_param", "非法的记录编号") from exc


# ---------------------------------------------------------------- 基础

@route("GET", "/api/bootstrap")
def bootstrap(ctx) -> dict[str, Any]:
    users = [dict(r) for r in ctx.conn.execute(
        "SELECT user_id,name,role,ward FROM users WHERE active=1 ORDER BY role,user_id")]
    batches = [dict(r) for r in ctx.conn.execute(
        "SELECT * FROM batches ORDER BY imported_at DESC LIMIT 10")]
    return {"today": _date.today().isoformat(), "users": users, "batches": batches,
            "fixtures": contract.list_fixtures(),
            "egress": {"mode": "院内单机", "external_network_exit": False,
                       "outbound_calls": "无（未调用任何院外模型或短信网关）"}}


@route("POST", "/api/login")
def login(ctx) -> dict[str, Any]:
    user_id = ctx.body.get("user_id")
    with db.transaction(ctx.conn):
        result = security.login(ctx.conn, user_id)
        audit.write(ctx.conn, security.secret(), event_type="operation",
                    action="session.login", actor_id=user_id, at=db.now_iso(),
                    detail={"role": result["user"]["role"]})
    return result


@route("GET", "/api/summary")
def summary(ctx) -> dict[str, Any]:
    _allow(ctx.user, *ROLES)
    d = ctx.query.get("date") or _date.today().isoformat()
    stats = records.stats(ctx.conn, d)
    pending_tickets = ctx.conn.execute(
        "SELECT COUNT(*) n FROM tickets WHERE status='pending'").fetchone()["n"]
    manual = ctx.conn.execute(
        "SELECT COUNT(*) n FROM patients WHERE manual_reason<>''").fetchone()["n"]
    return {"date": d, "stats": stats, "pending_tickets": pending_tickets,
            "manual_list": manual, "sms_touchpoints": records.sms_touchpoints(ctx.conn, d)}


# ---------------------------------------------------------------- 名单

@route("GET", "/api/fixtures")
def fixtures(ctx) -> dict[str, Any]:
    _allow(ctx.user, "engineer", "nurse", "quality")
    return {"fixtures": contract.list_fixtures()}


@route("POST", "/api/his/import")
def his_import(ctx) -> dict[str, Any]:
    _allow(ctx.user, "engineer")
    if ctx.body.get("simulate") == "timeout":
        return contract.simulate_timeout(ctx.conn, ctx.user)
    if ctx.body.get("fixture"):
        payload = contract.load_fixture(ctx.body["fixture"])
        rows = payload["rows"]
        version = payload["batch_version"]
        exported = payload["exported_at"]
        note = payload.get("note", "")
    else:
        rows = ctx.body.get("rows") or []
        version = ctx.body.get("batch_version")
        exported = ctx.body.get("exported_at")
        note = ctx.body.get("note", "")
        if not version or not exported:
            raise ApiError(400, "bad_contract", "批次缺少版本号或导出时间戳")
    return contract.import_batch(ctx.conn, rows, version, exported, ctx.user, note)


# ---------------------------------------------------------------- 排程

@route("POST", "/api/schedule/run")
def schedule_run(ctx) -> dict[str, Any]:
    _allow(ctx.user, "nurse", "engineer")
    d = ctx.body.get("date") or _date.today().isoformat()
    return scheduling.run(ctx.conn, d, ctx.user)


@route("GET", "/api/schedule")
def schedule_board(ctx) -> dict[str, Any]:
    _allow(ctx.user, "nurse", "doctor", "manager", "engineer", "quality")
    d = ctx.query.get("date") or _date.today().isoformat()
    all_wards = ctx.user["role"] != "nurse"
    nurse = ctx.user if ctx.user["role"] == "nurse" else None
    return scheduling.board(ctx.conn, d, nurse, all_wards)


@route("POST", r"/api/schedule/(?P<sid>\d+)/reorder")
def schedule_reorder(ctx) -> dict[str, Any]:
    _allow(ctx.user, "nurse")
    return scheduling.reorder(ctx.conn, _int(ctx.match["sid"]),
                              ctx.body.get("direction", "up"), ctx.user)


# ---------------------------------------------------------------- 触达

@route("POST", r"/api/contact/(?P<sid>\d+)/call")
def contact_call(ctx) -> dict[str, Any]:
    _allow(ctx.user, "nurse")
    return contact.start_call(ctx.conn, _int(ctx.match["sid"]), ctx.user,
                              bool(ctx.body.get("consent")))


@route("POST", r"/api/contact/(?P<sid>\d+)/attempt")
def contact_attempt(ctx) -> dict[str, Any]:
    _allow(ctx.user, "nurse")
    return contact.record_attempt(ctx.conn, _int(ctx.match["sid"]),
                                  ctx.body.get("channel", "call"),
                                  ctx.body.get("result", "no_answer"), ctx.user)


@route("POST", r"/api/contact/(?P<sid>\d+)/revoke")
def contact_revoke(ctx) -> dict[str, Any]:
    _allow(ctx.user, "nurse", "quality")
    sid = _int(ctx.match["sid"])
    digest = security.canonical_digest({"schedule_id": sid})
    security.verify_stepup(ctx.conn, ctx.body.get("token", ""), ctx.user,
                           "auth_revoke", str(sid), digest)
    return contact.revoke_by_schedule(ctx.conn, sid, ctx.user,
                                     ctx.body.get("reason", ""))


# ---------------------------------------------------------------- 问卷 / 草稿 / 登记表

@route("POST", "/api/questionnaire/check")
def questionnaire_check(ctx) -> dict[str, Any]:
    _allow(ctx.user, "nurse")
    sid = _int(str(ctx.body.get("schedule_id", "")))
    return records.check_and_draft(ctx.conn, sid, ctx.body.get("transcript", ""), ctx.user)


@route("GET", "/api/drafts")
def draft_list(ctx) -> dict[str, Any]:
    _allow(ctx.user, "nurse", "doctor", "quality")
    return {"items": records.list_drafts(ctx.conn, ctx.query.get("status", "draft"), ctx.user)}


@route("GET", "/api/records")
def record_list(ctx) -> dict[str, Any]:
    _allow(ctx.user, "nurse", "doctor", "manager", "quality")
    return {"items": records.list_records(ctx.conn, ctx.user)}


@route("POST", "/api/records/confirm")
def record_confirm(ctx) -> dict[str, Any]:
    _allow(ctx.user, "nurse")
    return records.confirm(ctx.conn, _int(str(ctx.body.get("draft_id", ""))),
                           ctx.body.get("fields") or {}, ctx.body.get("token", ""), ctx.user)


@route("POST", "/api/drafts/discard")
def draft_discard(ctx) -> dict[str, Any]:
    _allow(ctx.user, "nurse")
    return records.discard(ctx.conn, _int(str(ctx.body.get("draft_id", ""))), ctx.user)


# ---------------------------------------------------------------- step-up

@route("POST", "/api/stepup")
def stepup(ctx) -> dict[str, Any]:
    _allow(ctx.user, "nurse", "doctor", "quality")
    purpose = ctx.body.get("purpose", "")
    action_id = str(ctx.body.get("action_id", ""))
    if not action_id:
        raise ApiError(400, "bad_stepup", "缺少 action_id")
    if purpose == "record_confirm":
        digest = security.canonical_digest(
            {"draft_id": _int(action_id), "fields": ctx.body.get("fields") or {}})
    elif purpose == "ticket_resolve":
        digest = security.canonical_digest(
            {"ticket_id": _int(action_id), "decision": ctx.body.get("decision"),
             "note": ctx.body.get("note") or ""})
    elif purpose == "template_sign":
        digest = security.canonical_digest({"procedure_code": action_id})
    elif purpose == "auth_revoke":
        digest = security.canonical_digest({"schedule_id": _int(action_id)})
    else:
        raise ApiError(400, "bad_stepup", "未知 step-up 用途")
    return security.issue_stepup(ctx.conn, ctx.user, purpose, action_id, digest)


# ---------------------------------------------------------------- 工单 / 模板

@route("GET", "/api/tickets")
def ticket_list(ctx) -> dict[str, Any]:
    _allow(ctx.user, "doctor", "nurse", "quality", "manager")
    return {"items": tickets.list_tickets(ctx.conn)}


@route("POST", r"/api/tickets/(?P<tid>\d+)/resolve")
def ticket_resolve(ctx) -> dict[str, Any]:
    _allow(ctx.user, "doctor")
    return tickets.resolve(ctx.conn, _int(ctx.match["tid"]),
                           ctx.body.get("decision", ""), ctx.body.get("note", ""),
                           ctx.body.get("token", ""), ctx.user)


@route("GET", "/api/templates")
def template_list(ctx) -> dict[str, Any]:
    _allow(ctx.user, *ROLES)
    return {"items": templates.list_templates(ctx.conn)}


@route("POST", r"/api/templates/(?P<code>[A-Za-z0-9\-]+)/sign")
def template_sign(ctx) -> dict[str, Any]:
    _allow(ctx.user, "doctor")
    return templates.sign(ctx.conn, ctx.match["code"], ctx.body.get("token", ""), ctx.user)


@route("GET", "/api/rules")
def rule_list(ctx) -> dict[str, Any]:
    _allow(ctx.user, *ROLES)
    return redflag.rule_set_meta()


# ---------------------------------------------------------------- 审计

@route("GET", "/api/audit")
def audit_list(ctx) -> dict[str, Any]:
    _allow(ctx.user, "quality", "doctor", "engineer", "manager")
    limit = min(int(ctx.query.get("limit", "100")), 500)
    event_type = ctx.query.get("type")
    sql = "SELECT * FROM audit_events"
    args: list[Any] = []
    if event_type in audit.EVENT_TYPES:
        sql += " WHERE event_type=?"
        args.append(event_type)
    sql += " ORDER BY seq DESC LIMIT ?"
    args.append(limit)
    rows = []
    for r in ctx.conn.execute(sql, args):
        item = dict(r)
        item["detail"] = json.loads(item.pop("detail_json"))
        item.pop("prev_hash")
        rows.append(item)
    return {"items": rows,
            "note": "质控只读全量；运维不可删改（DB 触发器拒绝 UPDATE/DELETE）"}


@route("POST", "/api/audit/verify")
def audit_verify(ctx) -> dict[str, Any]:
    _allow(ctx.user, "quality", "engineer")
    result = audit.verify_chain(ctx.conn, security.secret())
    with db.transaction(ctx.conn):
        audit.write(ctx.conn, security.secret(), event_type="operation",
                    action="audit.verify", actor_id=ctx.user["user_id"],
                    at=db.now_iso(), outcome="success" if result["ok"] else "failed",
                    detail={"ok": result["ok"], "count": result["count"]})
    return result


@route("GET", "/api/audit/coverage")
def audit_coverage(ctx) -> dict[str, Any]:
    _allow(ctx.user, "quality", "engineer", "manager")
    return audit.coverage(ctx.conn)


# ---------------------------------------------------------------- 传输层

class Handler(BaseHTTPRequestHandler):
    server_version = "FDE-FollowUp/0.1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # 请求日志：不记录 query string、不记录调用方原文标识（日志卫生）
    def log_message(self, fmt, *args):  # noqa: N802
        msg = security.sanitize(fmt % args)
        sys.stderr.write(f"[{db.now_iso()}] {msg}\n")

    def _send(self, status: int, payload: dict[str, Any], close: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.close_connection = self.close_connection or close
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _drain(self, length: int) -> bool:
        """排空已声明但未读取的报文体，消除 413 响应前的 TCP RST 竞态。

        返回是否完整排空；读取设短超时并有 16 MB 上限，避免恶意长度拖垮服务。
        """
        remaining = min(length, DRAIN_LIMIT_BYTES)
        old_timeout = self.connection.gettimeout()
        try:
            self.connection.settimeout(0.5)
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            return False
        finally:
            self.connection.settimeout(old_timeout)
        return length <= DRAIN_LIMIT_BYTES and remaining == 0

    def _log_http(self, status: int, code: str) -> None:
        """4xx 只记摘要与错误码；不落报文体、不打印堆栈（SEC-09/SEC-11）。"""
        sys.stderr.write(f"[{db.now_iso()}] HTTP {status} {code}\n")

    def _static(self, path: str) -> None:
        if path in ("/", "/index.html"):
            path = "/index.html"
        rel = path.lstrip("/")
        target = (FRONTEND / rel)
        if not target.exists() or target.is_dir():
            self.send_error(404)
            return
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}.get(
                     target.suffix, "application/octet-stream")
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if method == "GET" and (path == "/" or path.startswith("/static/")
                                or path in ("/index.html", "/app.js", "/app.css")):
            self._static(path.replace("/static/", "/") if path.startswith("/static/") else path)
            return

        query = {k: v[-1] for k, v in _parse_query(parsed.query).items()}
        conn = None
        close = False
        try:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise ApiError(400, "bad_length", "Content-Length 非法") from exc
            if length > security.MAX_BODY_BYTES:
                close = True
                self._drain(length)
                raise ApiError(413, "body_too_large", "请求体超过大小上限")
            raw = self.rfile.read(length) if length else b""
            try:
                security.check_body(raw)
                body = json.loads(raw) if raw else {}
                if not isinstance(body, (dict, list)):
                    raise ApiError(400, "bad_body", "请求体必须是 JSON 对象")
                if not security.json_depth_within(body):
                    raise ApiError(400, "too_deep", "请求体嵌套层数超过上限")
                if not isinstance(body, dict):
                    raise ApiError(400, "bad_body", "请求体必须是 JSON 对象")
            except ApiError:
                raise
            except (json.JSONDecodeError, ValueError) as exc:
                raise ApiError(400, "bad_json", "请求体不是合法 JSON") from exc

            conn = db.get_db()
            token = self.headers.get("X-Session") or ""
            user = security.resolve_session(conn, token)
            # 鉴权先于语义：未知 /api/* 也不向未认证方泄露路由存在性
            if path.startswith("/api/") and path not in PUBLIC_PATHS and user is None:
                raise ApiError(401, "unauthorized", "未登录或会话已过期")
            for m, pattern, fn in routes:
                if m != method:
                    continue
                match = pattern.match(path)
                if not match:
                    continue
                if user is not None:
                    security.rate_limit(conn, f'{user["user_id"]}|{path}')
                ctx = _Ctx(conn=conn, user=user, body=body, query=query, match=match.groupdict())
                result = fn(ctx)
                self._send(200, {"ok": True, "data": result})
                return
            raise ApiError(404, "not_found", f"未找到接口 {method} {path}")
        except ApiError as exc:
            self._log_http(exc.status, exc.code)
            self._send(exc.status, {"ok": False, "code": exc.code, "error": exc.message},
                       close=close or exc.status == 413)
        except PermissionError as exc:
            self._log_http(403, "forbidden")
            self._send(403, {"ok": False, "code": "forbidden", "error": str(exc)}, close=close)
        except records.DraftConflict as exc:
            self._log_http(409, "draft_conflict")
            self._send(409, {"ok": False, "code": "draft_conflict", "error": str(exc)},
                       close=close)
        except LookupError as exc:
            self._log_http(404, "not_found")
            self._send(404, {"ok": False, "code": "not_found", "error": str(exc)}, close=close)
        except ValueError as exc:
            self._log_http(400, "invalid")
            self._send(400, {"ok": False, "code": "invalid", "error": str(exc)}, close=close)
        except audit.AuditWriteError as exc:
            self._log_http(503, "audit_unavailable")
            self._send(503, {"ok": False, "code": "audit_unavailable",
                             "error": f"留痕写入失败，业务已回滚: {exc}"}, close=close)
        except Exception:  # noqa: BLE001
            # 500 保留堆栈但经 security.sanitize 脱敏后写入应用日志
            sys.stderr.write(f"[{db.now_iso()}] HTTP 500 internal\n"
                             f"{security.sanitize(traceback.format_exc())}\n")
            self._send(500, {"ok": False, "code": "internal", "error": "服务内部错误"},
                       close=close)
        finally:
            if conn is not None:
                conn.close()

    def do_GET(self):  # noqa: N802
        self._handle("GET")

    def do_POST(self):  # noqa: N802
        self._handle("POST")


def _parse_query(qs: str) -> dict[str, list[str]]:
    from urllib.parse import parse_qs
    return parse_qs(qs, keep_blank_values=True)


class _Ctx:
    __slots__ = ("conn", "user", "body", "query", "match")

    def __init__(self, conn, user, body, query, match):
        self.conn, self.user, self.body, self.query, self.match = conn, user, body, query, match


def main() -> None:
    parser = argparse.ArgumentParser(description="出院随访与患者协作 Agent MVP")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    conn = db.get_db()
    conn.close()
    redflag.load_rules()  # 启动时验签，规则被篡改即拒绝启动（fail-closed）
    print(f"随访工作台已启动：http://{args.host}:{args.port}  (数据库 {db.DB_PATH})")
    print("边界：仅监听本机回环、无外部网络出口、不调用院外模型/短信网关（医疗-013）")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
