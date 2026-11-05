"""SQLite 连接、schema 与种子数据。

设计依据：docs/设计方案.md §4 分层（接入层/业务层/合规层/人工层）、§6 数据契约、§9 合规。
约束：院内单机部署、无外部网络出口（医疗-013）；仅用 Python 标准库，无第三方依赖。
"""
from __future__ import annotations

import os
import pathlib
import sqlite3
from contextlib import contextmanager
from datetime import datetime


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

ROOT = pathlib.Path(__file__).resolve().parents[2]
DATA_DIR = pathlib.Path(os.environ.get("FDE_DATA_DIR") or (ROOT / "data"))
DB_PATH = pathlib.Path(os.environ.get("FDE_DB") or (DATA_DIR / "followup.db"))

# ---------------------------------------------------------------- schema

SCHEMA = """
-- 人员与会话
CREATE TABLE IF NOT EXISTS users (
    user_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('nurse','doctor','engineer','quality','manager')),
    ward        TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- 名单批次（HIS 只读导出）
CREATE TABLE IF NOT EXISTS batches (
    batch_version TEXT PRIMARY KEY,
    exported_at   TEXT NOT NULL,
    imported_at   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','pending_backfill','superseded')),
    accepted      INTEGER NOT NULL DEFAULT 0,
    rejected      INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT ''
);

-- 患者（最小必要 8 字段，白名单）
CREATE TABLE IF NOT EXISTS patients (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    inpatient_no   TEXT NOT NULL,
    discharge_time TEXT NOT NULL,
    department     TEXT NOT NULL,
    procedure_code TEXT NOT NULL DEFAULT '',
    followup_status TEXT NOT NULL DEFAULT '待随访',
    contact_phone  TEXT NOT NULL,
    contact_type   TEXT NOT NULL CHECK (contact_type IN ('本人','家属')),
    auth_status    TEXT NOT NULL CHECK (auth_status IN ('已授权','未授权','撤回')),
    batch_version  TEXT NOT NULL REFERENCES batches(batch_version),
    manual_reason  TEXT NOT NULL DEFAULT '',
    UNIQUE (inpatient_no, discharge_time)          -- 医疗-010
);

-- 术式问卷模板注册表（专科签发版本，医疗-008）
CREATE TABLE IF NOT EXISTS templates (
    procedure_code TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    version        TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    signed_by      TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('signed','draft')),
    questions_json TEXT NOT NULL
);

-- 当日排程
CREATE TABLE IF NOT EXISTS schedules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id    INTEGER NOT NULL REFERENCES patients(id),
    schedule_date TEXT NOT NULL,
    slot          TEXT NOT NULL CHECK (slot IN ('evening','daytime')),
    nurse_id      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','in_progress','completed','shelved','escalated')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    seq           INTEGER NOT NULL DEFAULT 0,
    batch_version TEXT NOT NULL,
    template_version TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE           -- 住院号|出院时间|日期
);

-- 触达记录（短信不计完成率，医疗-017）
CREATE TABLE IF NOT EXISTS contact_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id),
    channel     TEXT NOT NULL CHECK (channel IN ('call','sms')),
    result      TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    at          TEXT NOT NULL
);

-- 录音登记（须告知并授权，医疗-014）
CREATE TABLE IF NOT EXISTS recordings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id),
    consent     INTEGER NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('active','not_recorded','revoked_do_not_train')),
    at          TEXT NOT NULL
);

-- 红旗升级工单
CREATE TABLE IF NOT EXISTS tickets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id  INTEGER NOT NULL REFERENCES schedules(id),
    patient_id   INTEGER NOT NULL REFERENCES patients(id),
    rule_id      TEXT NOT NULL,
    channel      TEXT NOT NULL CHECK (channel IN ('lexicon','model')),
    confidence   REAL NOT NULL DEFAULT 1.0,
    trigger_text TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected','continued')),
    resolved_by  TEXT NOT NULL DEFAULT '',
    resolved_at  TEXT NOT NULL DEFAULT '',
    resolution   TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS tickets_one_pending
    ON tickets(schedule_id, rule_id) WHERE status = 'pending';   -- 幂等，REQ-02

-- 结构化草稿（模型输出，仅草稿）
CREATE TABLE IF NOT EXISTS drafts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL REFERENCES schedules(id),
    raw_text    TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    generator   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','confirmed','discarded')),
    created_at  TEXT NOT NULL
);

-- 随访登记表（护士签名）
CREATE TABLE IF NOT EXISTS records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id      INTEGER NOT NULL REFERENCES drafts(id),
    schedule_id   INTEGER NOT NULL REFERENCES schedules(id),
    patient_id    INTEGER NOT NULL REFERENCES patients(id),
    fields_json   TEXT NOT NULL,
    diff_json     TEXT NOT NULL,
    nurse_id      TEXT NOT NULL,
    confirmed_at  TEXT NOT NULL,
    batch_version TEXT NOT NULL,
    template_version TEXT NOT NULL,
    signature     TEXT NOT NULL              -- 恒为护士，医疗-017
);

-- 留痕库：只写不改（医疗-015）
CREATE TABLE IF NOT EXISTS audit_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    seq           INTEGER NOT NULL,
    event_type    TEXT NOT NULL CHECK (event_type IN ('operation','escalation','authorization')),
    action        TEXT NOT NULL,
    actor_id      TEXT NOT NULL,
    subject       TEXT NOT NULL DEFAULT '',
    batch_version TEXT NOT NULL DEFAULT '',
    outcome       TEXT NOT NULL DEFAULT 'success',
    detail_json   TEXT NOT NULL DEFAULT '{}',
    at            TEXT NOT NULL,
    prev_hash     TEXT NOT NULL,
    entry_hash    TEXT NOT NULL
);

-- step-up 凭证 nonce（DB 版，防重放）
CREATE TABLE IF NOT EXISTS stepup_nonces (
    nonce      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    purpose    TEXT NOT NULL,
    action_id  TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used_at    TEXT NOT NULL
);

-- 限流（DB 计数，院内无 Redis）
CREATE TABLE IF NOT EXISTS rate_limit (
    bucket_key   TEXT PRIMARY KEY,
    window_start REAL NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0
);

-- append-only 触发器：DB 层拒绝改删（借鉴清单 A3）
CREATE TRIGGER IF NOT EXISTS audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only, UPDATE refused'); END;

CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only, DELETE refused'); END;
"""

# ---------------------------------------------------------------- 种子

USERS = [
    ("u-nurse-01", "林亦舟", "nurse", "骨科一病区"),
    ("u-nurse-02", "周慕青", "nurse", "骨科二病区"),
    ("u-doctor-01", "唐映秋", "doctor", "骨科"),
    ("u-engineer-01", "秦芮", "engineer", ""),
    ("u-quality-01", "傅景", "quality", ""),
    ("u-manager-01", "叶书", "manager", ""),
]

QUESTIONS_THR = [
    {"id": "q1", "text": "髋部伤口有无渗血、渗液或异味？"},
    {"id": "q2", "text": "是否出现发热（体温 ≥ 38.5℃）？"},
    {"id": "q3", "text": "有无呼吸困难、胸闷或气短？"},
    {"id": "q4", "text": "患肢是否出现单侧肿胀、胀痛或发红？"},
    {"id": "q5", "text": "是否按医嘱服药与完成康复训练？"},
    {"id": "q6", "text": "是否需要安排复诊或与专科沟通？"},
]
QUESTIONS_TKR = [
    {"id": "q1", "text": "膝关节伤口有无渗血、渗液或异味？"},
    {"id": "q2", "text": "是否出现发热（体温 ≥ 38.5℃）？"},
    {"id": "q3", "text": "有无呼吸困难、胸闷或气短？"},
    {"id": "q4", "text": "小腿是否出现单侧肿胀、发硬或压痛？"},
    {"id": "q5", "text": "屈伸训练完成情况与疼痛评分？"},
    {"id": "q6", "text": "是否需要安排复诊或与专科沟通？"},
]
QUESTIONS_FIX = [
    {"id": "q1", "text": "取出内固定的伤口愈合情况？"},
    {"id": "q2", "text": "有无发热或伤口渗液？"},
    {"id": "q3", "text": "活动恢复与负重情况？"},
]

TEMPLATES = [
    ("PG-ORTHO-THR", "全髋关节置换术随访问卷", "1.0", "2026-10-01", "u-doctor-01", "signed", QUESTIONS_THR),
    ("PG-ORTHO-TKR", "全膝关节置换术随访问卷", "1.0", "2026-10-01", "u-doctor-01", "signed", QUESTIONS_TKR),
    # 未签发的模板：用于故障用例（REQ-03：未审核版本不下发）
    ("PG-ORTHO-FIX", "内固定取出术随访问卷", "0.9", "2026-10-20", "", "draft", QUESTIONS_FIX),
]

import json as _json


def connect(path=None) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path or DB_PATH), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    """显式事务：审计与业务状态变更同生共死（fail-closed）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    with transaction(conn):
        for uid, name, role, ward in USERS:
            conn.execute(
                "INSERT OR IGNORE INTO users(user_id,name,role,ward) VALUES(?,?,?,?)",
                (uid, name, role, ward),
            )
        for row in TEMPLATES:
            code, title, ver, eff, signer, status, questions = row
            conn.execute(
                "INSERT OR IGNORE INTO templates(procedure_code,title,version,effective_date,"
                "signed_by,status,questions_json) VALUES(?,?,?,?,?,?,?)",
                (code, title, ver, eff, signer, status,
                 _json.dumps(questions, ensure_ascii=False)),
            )


def get_db() -> sqlite3.Connection:
    conn = connect()
    init_db(conn)
    return conn
