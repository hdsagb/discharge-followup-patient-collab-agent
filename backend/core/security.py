"""安全基座：会话、step-up 凭证、请求防护、限流、日志卫生。

对应 docs/借鉴清单.md A1/A4/A5/A6/A8 与本项目权限矩阵（docs/设计方案.md §7）。
全部自实现（标准库），未复制上游代码；设计语义保持一致：白名单 scope、DB nonce、常量时间比较。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any

from .db import DATA_DIR

STEP_UP_TTL_SECONDS = 300          # A1：默认 300 s
MAX_BODY_BYTES = 256 * 1024
MAX_JSON_DEPTH = 32                # A5
RATE_LIMIT_PER_MINUTE = 120        # A8

_secret_cache: bytes | None = None


def secret() -> bytes:
    """本地密钥：FDE_SECRET 环境变量优先，否则在 data/.secret 生成一次。"""
    global _secret_cache
    if _secret_cache is None:
        env = os.environ.get("FDE_SECRET")
        if env:
            _secret_cache = env.encode("utf-8")
        else:
            path = DATA_DIR / ".secret"
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(secrets.token_hex(32), encoding="utf-8")
            _secret_cache = path.read_text(encoding="utf-8").strip().encode("utf-8")
    return _secret_cache


# ---------------------------------------------------------------- 会话

def _now() -> datetime:
    return datetime.now()


def login(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    user = conn.execute(
        "SELECT * FROM users WHERE user_id=? AND active=1", (user_id,)
    ).fetchone()
    if user is None:
        raise ValueError("未知账号")
    token = secrets.token_urlsafe(32)
    created = _now()
    expires = created + timedelta(hours=8)
    conn.execute(
        "INSERT INTO sessions(token,user_id,created_at,expires_at) VALUES(?,?,?,?)",
        (token, user_id, created.isoformat(timespec="seconds"),
         expires.isoformat(timespec="seconds")),
    )
    return {
        "token": token,
        "user": {"user_id": user["user_id"], "name": user["name"],
                 "role": user["role"], "ward": user["ward"]},
    }


def resolve_session(conn: sqlite3.Connection, token: str):
    if not token:
        return None
    row = conn.execute(
        "SELECT u.*, s.expires_at FROM sessions s JOIN users u ON u.user_id=s.user_id "
        "WHERE s.token=?", (token,)
    ).fetchone()
    if row is None or row["expires_at"] < _now().isoformat(timespec="seconds"):
        return None
    return {"user_id": row["user_id"], "name": row["name"], "role": row["role"],
            "ward": row["ward"]}


# ---------------------------------------------------------------- step-up

def canonical_digest(payload: Any) -> str:
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def issue_stepup(conn: sqlite3.Connection, user: dict[str, Any], purpose: str,
                 action_id: str, digest: str) -> dict[str, Any]:
    """签发一次性 step-up 凭证；载荷绑定 record_digest + 护士 + action。"""
    if purpose not in STEPUP_PURPOSES:
        raise ValueError("未授权的 step-up 用途")
    exp = int(time.time()) + STEP_UP_TTL_SECONDS
    nonce = secrets.token_hex(8)
    payload = {"uid": user["user_id"], "purpose": purpose, "action": action_id,
               "digest": digest, "nonce": nonce, "exp": exp}
    raw = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    sig = hmac.new(secret(), raw.encode(), hashlib.sha256).hexdigest()
    return {"token": f"{raw}.{sig}", "expires_in": STEP_UP_TTL_SECONDS, "purpose": purpose}


STEPUP_PURPOSES = frozenset({"record_confirm", "ticket_resolve", "template_sign", "auth_revoke"})


def verify_stepup(conn: sqlite3.Connection, token: str, user: dict[str, Any],
                  purpose: str, action_id: str, digest: str) -> dict[str, Any]:
    """校验签名 → 有效期 → 绑定关系 → nonce 一次性（DB 记录，无 Redis）。"""
    if not token or "." not in token:
        raise PermissionError("缺少 step-up 凭证")
    raw, sig = token.split(".", 1)
    expect = hmac.new(secret(), raw.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):          # A6：常量时间比较
        raise PermissionError("step-up 签名无效")
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception as exc:
        raise PermissionError("step-up 载荷损坏") from exc
    if payload.get("exp", 0) < int(time.time()):
        raise PermissionError("step-up 凭证已过期")
    if payload.get("uid") != user["user_id"] or payload.get("purpose") != purpose:
        raise PermissionError("step-up 与当前操作者/用途不匹配")
    if payload.get("action") != action_id or payload.get("digest") != digest:
        raise PermissionError("step-up 未绑定到本次操作内容")
    try:
        conn.execute(
            "INSERT INTO stepup_nonces(nonce,user_id,purpose,action_id,expires_at,used_at) "
            "VALUES(?,?,?,?,?,?)",
            (payload["nonce"], user["user_id"], purpose, action_id,
             float(payload["exp"]), _now().isoformat(timespec="seconds")),
        )
    except sqlite3.IntegrityError as exc:
        raise PermissionError("step-up 凭证已被使用（防重放）") from exc
    return payload


# ---------------------------------------------------------------- 防护

def check_body(raw: bytes) -> None:
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("请求体超过大小上限")


def json_depth_within(obj: Any, limit: int = MAX_JSON_DEPTH, _depth: int = 0) -> bool:
    """有界深度检查：在到达 Python 递归上限前拒绝深嵌套载荷。"""
    if _depth > limit:
        return False
    if isinstance(obj, dict):
        return all(json_depth_within(v, limit, _depth + 1) for v in obj.values())
    if isinstance(obj, list):
        return all(json_depth_within(v, limit, _depth + 1) for v in obj)
    return True


def rate_limit(conn: sqlite3.Connection, bucket: str, at: float | None = None) -> None:
    """DB 令牌桶（窗口计数）：院内无 Redis，按唯一键 upsert 原子累加。"""
    now = at if at is not None else time.time()
    window = int(now // 60)
    conn.execute(
        "INSERT INTO rate_limit(bucket_key,window_start,count) VALUES(?,?,1) "
        "ON CONFLICT(bucket_key) DO UPDATE SET "
        "count = CASE WHEN window_start = excluded.window_start THEN count + 1 ELSE 1 END, "
        "window_start = excluded.window_start",
        (bucket, window),
    )
    row = conn.execute("SELECT count, window_start FROM rate_limit WHERE bucket_key=?",
                       (bucket,)).fetchone()
    if int(row["window_start"]) == window and row["count"] > RATE_LIMIT_PER_MINUTE:
        raise PermissionError("请求频率超限")


# ---------------------------------------------------------------- 日志卫生（A4）

_LOG_LABEL = b"fde-followup-log-id-v1"
_CREDENTIAL_SHAPED = re.compile(
    r"eyJ[A-Za-z0-9_=-]{8,}(?:\.[A-Za-z0-9_=.-]*)?"
    r"|(?<![A-Za-z0-9])(?:sk|rk|pk)[-][A-Za-z0-9_-]{16,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|bearer\s+\S+",
    re.IGNORECASE,
)


def _log_key() -> bytes:
    return hmac.new(secret(), _LOG_LABEL, hashlib.sha256).digest()


def pseudonym(value: str) -> str:
    """日志中的调用方标识一律写短键控哈希，不写原文。"""
    if not value:
        return "-"
    return "t:" + hmac.new(_log_key(), value.encode("utf-8"), hashlib.sha256).hexdigest()[:10]


def sanitize(text: str) -> str:
    return _CREDENTIAL_SHAPED.sub("[redacted]", text or "")
