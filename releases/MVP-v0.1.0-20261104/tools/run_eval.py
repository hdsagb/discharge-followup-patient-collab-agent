"""90 例评估集运行器（正常 30 / 边界 20 / 对抗 20 / 权限 10 / 故障 10）。

依据 docs/06-测试评估.md 的评估集口径与 docs/03-需求定义.md 的验收条款。
每例独立临时数据库，逐例断言；任一失败即以非 0 退出，报告写入 data/eval_report.json。
不调用任何院外服务，纯本地运行。

用法：python tools/run_eval.py
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FDE_SECRET", "fde-eval-local-secret")

from backend.core import (audit, contact, contract, db, records, redflag,  # noqa: E402
                          scheduling, security, templates, tickets)

DATE = "2026-11-04"
REPORT_PATH = ROOT / "data" / "eval_report.json"

CASES: list[tuple[str, str, Callable[[sqlite3.Connection], None]]] = []


def case(category: str, name: str):
    def deco(fn: Callable[[sqlite3.Connection], None]):
        CASES.append((category, name, fn))
        return fn
    return deco


# ---------------------------------------------------------------- 基础设施

def fresh_db(directory: Path, slug: str) -> sqlite3.Connection:
    conn = db.connect(directory / f"{slug}.db")
    db.init_db(conn)
    return conn


def actor(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if row is None:
        raise AssertionError(f"种子用户缺失: {user_id}")
    return {"user_id": row["user_id"], "name": row["name"],
            "role": row["role"], "ward": row["ward"]}


def row(**overrides: Any) -> dict[str, Any]:
    base = {"inpatient_no": "ZYEVAL0001", "discharge_time": "2026-11-02T10:00:00",
            "department": "骨科一病区", "procedure_code": "PG-ORTHO-THR",
            "followup_status": "待随访", "contact_phone": "13800001111",
            "contact_type": "本人", "auth_status": "已授权"}
    base.update(overrides)
    return base


def seed(conn: sqlite3.Connection, rows: list[dict[str, Any]],
         version: str = "HIS-EVAL-v1", exported_at: str | None = None) -> dict[str, Any]:
    return contract.import_batch(conn, rows, version,
                                 exported_at or "2026-11-04T02:00:00",
                                 actor(conn, "u-engineer-01"))


def only_schedule(conn: sqlite3.Connection) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM schedules ORDER BY id DESC LIMIT 1").fetchone()
    if r is None:
        raise AssertionError("未生成排程")
    return r


def issue(conn: sqlite3.Connection, user: dict[str, Any], purpose: str,
          action_id: str, digest: str) -> str:
    return security.issue_stepup(conn, user, purpose, action_id, digest)["token"]


def assert_chain(conn: sqlite3.Connection) -> None:
    result = audit.verify_chain(conn, security.secret())
    assert result["ok"], f"审计链校验失败: {result}"


def normal_flow(conn: sqlite3.Connection, transcript: str, contact_type: str = "本人",
                edit: dict[str, Any] | None = None) -> tuple[sqlite3.Row, dict, dict]:
    """标准流程：排程 → 外呼（录音告知）→ 分析 → 护士签核确认。"""
    seed(conn, [row(contact_type=contact_type)])
    stats = scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    assert stats["created"] == 1, stats
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    contact.start_call(conn, sched["id"], nurse, True)
    result = records.check_and_draft(conn, sched["id"], transcript, nurse)
    assert result["interrupted"] is False, result
    fields = dict(result["draft"]["fields"])
    if edit:
        fields.update(edit)
    token = issue(conn, nurse, "record_confirm", str(result["draft"]["draft_id"]),
                  security.canonical_digest({"draft_id": result["draft"]["draft_id"],
                                             "fields": fields}))
    confirmed = records.confirm(conn, result["draft"]["draft_id"], fields, token, nurse)
    assert confirmed["signature"] == "nurse:u-nurse-01", confirmed
    final = conn.execute("SELECT status FROM schedules WHERE id=?",
                         (sched["id"],)).fetchone()
    assert final["status"] == "completed", final
    assert conn.execute("SELECT COUNT(*) n FROM records").fetchone()["n"] == 1
    assert_chain(conn)
    return sched, result, confirmed


# ---------------------------------------------------------------- 正常 30

NORMAL_TEXTS = [
    "伤口干燥，没有渗血渗液，体温正常。",
    "今天体温36.8度，没有发烧。",
    "呼吸正常，没有胸闷气短。",
    "腿不肿，走路比昨天好一些。",
    "按时吃药，没有漏服。",
    "康复训练每天两次，都做完了。",
    "想下周来复查一次。",
    "刀口有点痒，不疼。",
    "睡眠还可以，就是有点累。",
    "食欲一般，吃得下。",
    "家属帮忙换药，纱布是干的。",
    "没有头晕，也没有恶心。",
    "疼痛评分三分，能忍住。",
    "止痛药按医嘱吃，管用。",
    "今天下床走了十分钟。",
    "没有咳嗽，没有发热。",
    "伤口周围不红不肿。",
    "复查时间还没定，听医生安排。",
    "一切正常，没有不舒服。",
    "体温正常，精神挺好。",
    "训练有点酸，能坚持。",
    "药都按时吃了。",
    "没有气短，说话不费劲。",
    "晚上睡得好，白天有精神。",
    "伤口换药时看着挺干净。",
    "腿没有肿，也不疼。",
    "想问什么时候能拆线。",
    "想约复诊，看看恢复情况。",
    "没有别的问题，都还好。",
    "吃饭睡觉都正常，下周复查。",
]

for _i, _text in enumerate(NORMAL_TEXTS, 1):
    def _normal(conn, text=_text):
        normal_flow(conn, text)
    case("正常", f"N{_i:02d} {_text[:14]}")(_normal)


# ---------------------------------------------------------------- 边界 20

@case("边界", "E01 家属代答排晚间时段")
def e01(conn):
    sched, _, _ = normal_flow(conn, "我是他女儿，术后恢复还可以，没有发烧。", "家属")
    assert sched["slot"] == "evening", sched["slot"]


@case("边界", "E02 方言否定表述不误报")
def e02(conn):
    normal_flow(conn, "刀口干干爽爽的，莫得渗血渗液。")


@case("边界", "E03 方言腿部否定表述不误报")
def e03(conn):
    normal_flow(conn, "腿杆莫得肿，走路还可以。")


@case("边界", "E04 三次未接自动搁置")
def e04(conn):
    seed(conn, [row()])
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    contact.start_call(conn, sched["id"], nurse, True)
    for _ in range(3):
        res = contact.record_attempt(conn, sched["id"], "call", "no_answer", nurse)
    assert res["attempts"] == 3 and res["status"] == "shelved", res
    assert conn.execute("SELECT status FROM schedules WHERE id=?",
                        (sched["id"],)).fetchone()["status"] == "shelved"


@case("边界", "E05 短信发送不计完成率")
def e05(conn):
    seed(conn, [row()])
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    res = contact.record_attempt(conn, sched["id"], "sms", "sent", nurse)
    assert res["counts_toward_completion"] is False, res
    stats = records.stats(conn, DATE)
    assert stats["completed"] == 0 and stats["completion_rate"] == 0.0, stats
    assert stats["sms_attempts_not_counted"] == 1, stats


@case("边界", "E06 号码失效三次转人工清单")
def e06(conn):
    seed(conn, [row()])
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    contact.start_call(conn, sched["id"], nurse, True)
    for _ in range(3):
        res = contact.record_attempt(conn, sched["id"], "call", "invalid_number", nurse)
    assert res["status"] == "shelved", res


@case("边界", "E07 未授权联系人不进入排程")
def e07(conn):
    seed(conn, [row(auth_status="未授权")])
    stats = scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    assert stats["created"] == 0 and stats["excluded_unauthorized"] == 1, stats


@case("边界", "E08 撤回授权不进入排程")
def e08(conn):
    seed(conn, [row(auth_status="撤回")])
    stats = scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    assert stats["created"] == 0 and stats["excluded_unauthorized"] == 1, stats


@case("边界", "E09 术式代码缺失转人工")
def e09(conn):
    seed(conn, [row(procedure_code="")])
    stats = scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    assert stats["created"] == 0 and stats["manual"] == 1, stats
    patient = conn.execute("SELECT manual_reason FROM patients").fetchone()
    assert "术式代码缺失" in patient["manual_reason"], patient


@case("边界", "E10 未签发模板不下发")
def e10(conn):
    seed(conn, [row(procedure_code="PG-ORTHO-FIX")])
    stats = scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    assert stats["created"] == 0 and stats["manual"] == 1, stats
    patient = conn.execute("SELECT manual_reason FROM patients").fetchone()
    assert "模板未签发" in patient["manual_reason"], patient


@case("边界", "E11 同日重复排程幂等")
def e11(conn):
    seed(conn, [row()])
    first = scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    second = scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    assert first["created"] == 1, first
    assert second["created"] == 0 and second["skipped_duplicate"] == 1, second
    assert conn.execute("SELECT COUNT(*) n FROM schedules").fetchone()["n"] == 1


@case("边界", "E12 重复名单按住院号+出院时间合并")
def e12(conn):
    rows = [row(), row(contact_phone="13900002222")]
    result = seed(conn, rows)
    assert result["accepted"] == 1 and result["merged"] == 1, result
    patient = conn.execute("SELECT contact_phone FROM patients").fetchone()
    assert patient["contact_phone"] == "13900002222", patient


@case("边界", "E13 超白名单字段拒收")
def e13(conn):
    result = seed(conn, [row(id_card="310101199001010011", address="某路 1 号")])
    assert result["accepted"] == 1, result
    assert result["dropped_fields"] == ["address", "id_card"], result


@case("边界", "E14 号码过短拒收")
def e14(conn):
    result = seed(conn, [row(contact_phone="1234")])
    assert result["rejected"] == 1 and "联系电话格式无效" in result["reject_reasons"][0], result


@case("边界", "E15 授权状态缺失拒收")
def e15(conn):
    bad = row()
    bad.pop("auth_status")
    result = seed(conn, [bad])
    assert result["rejected"] == 1 and "auth_status" in result["reject_reasons"][0], result


@case("边界", "E16 联系人类型非法拒收")
def e16(conn):
    result = seed(conn, [row(contact_type="同事")])
    assert result["rejected"] == 1 and "联系人类型非法" in result["reject_reasons"][0], result


@case("边界", "E17 本人白天与家属晚间分时段")
def e17(conn):
    seed(conn, [row(inpatient_no="ZYE1701", contact_type="本人"),
                row(inpatient_no="ZYE1702", contact_type="家属",
                    discharge_time="2026-11-02T12:00:00")])
    stats = scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    assert stats["created"] == 2, stats
    slots = {r["contact_type"]: r["slot"] for r in conn.execute(
        "SELECT p.contact_type, s.slot FROM schedules s JOIN patients p ON p.id=s.patient_id")}
    assert slots == {"本人": "daytime", "家属": "evening"}, slots
    board = scheduling.board(conn, DATE, actor(conn, "u-nurse-01"))
    assert all("*" in item["contact_phone"] for item in board["items"]), board


@case("边界", "E18 护士修改草稿字段留痕")
def e18(conn):
    _, result, confirmed = normal_flow(conn, "伤口情况还好，没有发烧。",
                                       edit={"主诉": "护士复核：伤口干燥"})
    assert confirmed["diff"], confirmed
    record = conn.execute("SELECT fields_json FROM records ORDER BY id DESC LIMIT 1").fetchone()
    fields = json.loads(record["fields_json"])
    assert fields["主诉"] == "护士复核：伤口干燥", fields


@case("边界", "E19 护士不确认不归档")
def e19(conn):
    seed(conn, [row()])
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    contact.start_call(conn, sched["id"], nurse, True)
    result = records.check_and_draft(conn, sched["id"], "恢复还行，没有不舒服。", nurse)
    assert result["interrupted"] is False, result
    assert conn.execute("SELECT COUNT(*) n FROM records").fetchone()["n"] == 0
    draft = conn.execute("SELECT status FROM drafts ORDER BY id DESC LIMIT 1").fetchone()
    assert draft["status"] == "draft", draft


@case("边界", "E20 丢弃草稿不归档")
def e20(conn):
    seed(conn, [row()])
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    contact.start_call(conn, sched["id"], nurse, True)
    result = records.check_and_draft(conn, sched["id"], "恢复还行。", nurse)
    records.discard(conn, result["draft"]["draft_id"], nurse)
    draft = conn.execute("SELECT status FROM drafts ORDER BY id DESC LIMIT 1").fetchone()
    assert draft["status"] == "discarded", draft
    assert conn.execute("SELECT COUNT(*) n FROM records").fetchone()["n"] == 0


# ---------------------------------------------------------------- 对抗 20

ADVERSARIAL = [
    ("这两天喘不上气来", "RF-ORTHO-01"),
    ("胸口透不过气", "RF-ORTHO-01"),
    ("气儿不够用，说两句话就累", "RF-ORTHO-01"),
    ("呼哧呼哧的", "RF-ORTHO-01"),
    ("烧到三十八度五了", "RF-ORTHO-02"),
    ("浑身烫，晚上出汗", "RF-ORTHO-02"),
    ("伤口出水了", "RF-ORTHO-03"),
    ("纱布湿透了", "RF-ORTHO-03"),
    ("伤口味道大", "RF-ORTHO-03"),
    ("刀口发红发肿", "RF-ORTHO-03"),
    ("胸口压着疼", "RF-ORTHO-04"),
    ("一条腿粗了", "RF-ORTHO-04"),
    ("小腿发胀发硬", "RF-ORTHO-04"),
    ("眼前一黑", "RF-ORTHO-05"),
    ("站不住，差点没站住", "RF-ORTHO-05"),
    ("晕过去了", "RF-ORTHO-05"),
    ("疼得直冒汗", "RF-ORTHO-06"),
    ("疼得睡不着", "RF-ORTHO-06"),
    ("吃止痛药也不管用", "RF-ORTHO-06"),
    ("喘气时胸口疼", "RF-ORTHO-04"),
]


def adversarial_flow(conn: sqlite3.Connection, transcript: str) -> tuple[dict, sqlite3.Row]:
    seed(conn, [row()])
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    contact.start_call(conn, sched["id"], nurse, True)
    result = records.check_and_draft(conn, sched["id"], transcript, nurse)
    return result, sched


for _j, (_text, _rule) in enumerate(ADVERSARIAL, 1):
    def _adv(conn, text=_text, rule=_rule):
        result, sched = adversarial_flow(conn, text)
        assert result["interrupted"] is True, result
        hit_rules = [h["rule_id"] for h in result["hits"]]
        assert rule in hit_rules, f"期望 {rule}，实际 {hit_rules}"
        assert conn.execute("SELECT COUNT(*) n FROM tickets").fetchone()["n"] >= 1
        assert conn.execute("SELECT status FROM schedules WHERE id=?",
                            (sched["id"],)).fetchone()["status"] == "escalated"
        assert conn.execute("SELECT COUNT(*) n FROM drafts").fetchone()["n"] == 0
        escalation = conn.execute(
            "SELECT COUNT(*) n FROM audit_events WHERE event_type='escalation'").fetchone()["n"]
        assert escalation >= 1
        assert_chain(conn)
    case("对抗", f"A{_j:02d} {_rule} {_text[:12]}")(_adv)


# ---------------------------------------------------------------- 权限 10

@case("权限", "P01 未授权外呼被拒并留痕")
def p01(conn):
    seed(conn, [row()])
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    contact.revoke_by_schedule(conn, sched["id"], nurse)
    try:
        contact.start_call(conn, sched["id"], nurse, True)
    except PermissionError:
        pass
    else:
        raise AssertionError("撤回授权后仍允许外呼")
    denied = conn.execute("SELECT COUNT(*) n FROM audit_events WHERE "
                          "action='call.denied_unauthorized' AND outcome='denied'").fetchone()["n"]
    assert denied == 1


@case("权限", "P02 护士无权处置红旗工单")
def p02(conn):
    adversarial_flow(conn, "喘不上气来")
    tid = conn.execute("SELECT id FROM tickets ORDER BY id DESC LIMIT 1").fetchone()["id"]
    try:
        tickets.resolve(conn, tid, "approved", "", "", actor(conn, "u-nurse-01"))
    except PermissionError:
        pass
    else:
        raise AssertionError("护士成功处置了工单")


@case("权限", "P03 护士无权签发模板")
def p03(conn):
    try:
        templates.sign(conn, "PG-ORTHO-FIX", "", actor(conn, "u-nurse-01"))
    except PermissionError:
        pass
    else:
        raise AssertionError("护士成功签发了模板")


@case("权限", "P04 无 step-up 不能确认记录")
def p04(conn):
    seed(conn, [row()])
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    contact.start_call(conn, sched["id"], nurse, True)
    result = records.check_and_draft(conn, sched["id"], "恢复还行。", nurse)
    try:
        records.confirm(conn, result["draft"]["draft_id"], result["draft"]["fields"], "", nurse)
    except PermissionError:
        pass
    else:
        raise AssertionError("无 step-up 仍写入登记表")


@case("权限", "P05 step-up 凭证防重放")
def p05(conn):
    seed(conn, [row()])
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    contact.start_call(conn, sched["id"], nurse, True)
    result = records.check_and_draft(conn, sched["id"], "恢复还行。", nurse)
    did = result["draft"]["draft_id"]
    digest = security.canonical_digest({"draft_id": did, "fields": result["draft"]["fields"]})
    token = issue(conn, nurse, "record_confirm", str(did), digest)
    records.confirm(conn, did, result["draft"]["fields"], token, nurse)
    try:
        security.verify_stepup(conn, token, nurse, "record_confirm", str(did), digest)
    except PermissionError:
        pass
    else:
        raise AssertionError("同一 step-up 凭证被重复使用")


@case("权限", "P06 step-up 绑定本次草稿内容")
def p06(conn):
    seed(conn, [row(inpatient_no="ZYP0601"), row(inpatient_no="ZYP0602",
                                                discharge_time="2026-11-02T12:00:00")])
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    nurse = actor(conn, "u-nurse-01")
    drafts = []
    for sched in conn.execute("SELECT id FROM schedules ORDER BY id").fetchall():
        contact.start_call(conn, sched["id"], nurse, True)
        drafts.append(records.check_and_draft(conn, sched["id"], "恢复还行。", nurse)["draft"])
    did1, did2 = drafts[0]["draft_id"], drafts[1]["draft_id"]
    digest1 = security.canonical_digest({"draft_id": did1, "fields": drafts[0]["fields"]})
    token = issue(conn, nurse, "record_confirm", str(did1), digest1)
    try:
        records.confirm(conn, did2, drafts[1]["fields"], token, nurse)
    except PermissionError:
        pass
    else:
        raise AssertionError("step-up 未绑定草稿内容仍通过校验")


@case("权限", "P07 step-up 用途不可混用")
def p07(conn):
    seed(conn, [row()])
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    contact.start_call(conn, sched["id"], nurse, True)
    result = records.check_and_draft(conn, sched["id"], "恢复还行。", nurse)
    did = result["draft"]["draft_id"]
    digest = security.canonical_digest({"draft_id": did, "fields": result["draft"]["fields"]})
    token = issue(conn, nurse, "record_confirm", str(did), digest)
    try:
        security.verify_stepup(conn, token, nurse, "ticket_resolve", str(did), digest)
    except PermissionError:
        pass
    else:
        raise AssertionError("step-up 用途混用仍通过校验")


@case("权限", "P08 每分钟请求限流")
def p08(conn):
    at = 1_800_000_000.0
    try:
        for _ in range(security.RATE_LIMIT_PER_MINUTE + 1):
            security.rate_limit(conn, "u-nurse-01|/api/records/confirm", at=at)
    except PermissionError:
        pass
    else:
        raise AssertionError("超过每分钟上限仍未拒绝")


@case("权限", "P09 审计事件禁止更新")
def p09(conn):
    seed(conn, [row()])
    try:
        conn.execute("UPDATE audit_events SET action='tampered' WHERE id=1")
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError("审计事件被成功更新")


@case("权限", "P10 审计事件禁止删除")
def p10(conn):
    seed(conn, [row()])
    try:
        conn.execute("DELETE FROM audit_events WHERE id=1")
    except sqlite3.DatabaseError:
        pass
    else:
        raise AssertionError("审计事件被成功删除")


# ---------------------------------------------------------------- 故障 10

@case("故障", "F01 HIS 超时降级沿用上一版本")
def f01(conn):
    seed(conn, [row()])
    result = contract.simulate_timeout(conn, actor(conn, "u-engineer-01"))
    batch = conn.execute("SELECT status FROM batches WHERE batch_version=?",
                         (result["fallback_batch"],)).fetchone()
    assert batch["status"] == "pending_backfill", batch
    assert conn.execute("SELECT COUNT(*) n FROM audit_events WHERE action='batch.fallback'").fetchone()["n"] == 1


@case("故障", "F02 名单超 24 小时全部转人工")
def f02(conn):
    seed(conn, [row()], exported_at="2026-10-01T08:00:00")
    stats = scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    assert stats["created"] == 0 and stats["stale_manual"] >= 1, stats
    patient = conn.execute("SELECT manual_reason FROM patients").fetchone()
    assert "24 小时" in patient["manual_reason"], patient
    assert conn.execute(
        "SELECT COUNT(*) n FROM audit_events WHERE action='schedule.stale_fallback'").fetchone()["n"] == 1


@case("故障", "F03 模板尚未生效转人工")
def f03(conn):
    conn.execute("UPDATE templates SET effective_date='2027-01-01' "
                 "WHERE procedure_code='PG-ORTHO-THR'")
    seed(conn, [row()])
    stats = scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    assert stats["created"] == 0 and stats["manual"] == 1, stats
    patient = conn.execute("SELECT manual_reason FROM patients").fetchone()
    assert "尚未生效" in patient["manual_reason"], patient


@case("故障", "F04 同一红旗重复分析不重复建单")
def f04(conn):
    seed(conn, [row()])
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    contact.start_call(conn, sched["id"], nurse, True)
    first = records.check_and_draft(conn, sched["id"], "喘不上气来", nurse)
    second = records.check_and_draft(conn, sched["id"], "喘不上气来", nurse)
    assert first["tickets_created"] == 1, first
    assert second["tickets_created"] == 0 and second["deduped"], second
    assert conn.execute("SELECT COUNT(*) n FROM tickets").fetchone()["n"] == 1


@case("故障", "F05 批次重复导入幂等")
def f05(conn):
    first = seed(conn, [row()])
    second = seed(conn, [row()])
    assert first["accepted"] == 1, first
    assert second["idempotent"] is True, second
    assert conn.execute("SELECT COUNT(*) n FROM batches").fetchone()["n"] == 1


@case("故障", "F06 无名单批次拒绝生成排程")
def f06(conn):
    try:
        scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    except ValueError:
        pass
    else:
        raise AssertionError("无名单批次仍生成排程")


@case("故障", "F07 审计链篡改可检出")
def f07(conn):
    seed(conn, [row()])
    last = conn.execute("SELECT seq FROM audit_events ORDER BY seq DESC LIMIT 1").fetchone()
    conn.execute(
        "INSERT INTO audit_events(seq,event_type,action,actor_id,subject,batch_version,"
        "outcome,detail_json,at,prev_hash,entry_hash) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (last["seq"] + 1, "operation", "audit.forged", "u-x", "", "", "success",
         "{}", db.now_iso(), audit.GENESIS, "forged"))
    result = audit.verify_chain(conn, security.secret())
    assert result["ok"] is False, result
    assert result["broken_at_seq"] == last["seq"] + 1, result


@case("故障", "F08 审计覆盖率达标")
def f08(conn):
    normal_flow(conn, "恢复还行。")
    seed(conn, [row(inpatient_no="ZYF0802", discharge_time="2026-11-02T12:00:00")],
         version="HIS-EVAL-v2")
    scheduling.run(conn, DATE, actor(conn, "u-engineer-01"))
    sched = only_schedule(conn)
    nurse = actor(conn, "u-nurse-01")
    contact.start_call(conn, sched["id"], nurse, True)
    records.check_and_draft(conn, sched["id"], "喘不上气来", nurse)
    cov = audit.coverage(conn)
    assert cov["coverage_ok"] is True, cov
    assert cov["records_audited"] >= cov["records"] and cov["tickets_audited"] >= cov["tickets"], cov


@case("故障", "F09 缺出院时间字段拒收")
def f09(conn):
    bad = row()
    bad.pop("discharge_time")
    result = seed(conn, [bad])
    assert result["rejected"] == 1 and "discharge_time" in result["reject_reasons"][0], result


@case("故障", "F10 超深嵌套与超大请求体拒绝")
def f10(conn):
    payload: dict[str, Any] = {}
    node = payload
    for _ in range(40):
        node["n"] = {}
        node = node["n"]
    assert security.json_depth_within(payload) is False
    try:
        security.check_body(b"x" * (security.MAX_BODY_BYTES + 1))
    except ValueError:
        pass
    else:
        raise AssertionError("超大请求体未被拒绝")


# ---------------------------------------------------------------- 运行

def main() -> int:
    parser = argparse.ArgumentParser(description="90 例评估集运行器")
    parser.add_argument("--report", default=str(REPORT_PATH))
    args = parser.parse_args()

    directory = Path(tempfile.mkdtemp(prefix="fde-eval-"))
    results: list[dict[str, Any]] = []
    try:
        for idx, (category, name, fn) in enumerate(CASES):
            conn = None
            try:
                conn = fresh_db(directory, f"c{idx:03d}")
                fn(conn)
                results.append({"category": category, "name": name,
                                "result": "PASS", "error": ""})
            except Exception as exc:  # noqa: BLE001
                results.append({"category": category, "name": name, "result": "FAIL",
                                "error": f"{type(exc).__name__}: {exc}"})
            finally:
                if conn is not None:
                    conn.close()
    finally:
        for leftover in directory.glob("*"):
            try:
                leftover.unlink()
            except OSError:
                pass

    order = ["正常", "边界", "对抗", "权限", "故障"]
    summary = {cat: {"total": 0, "pass": 0, "fail": 0} for cat in order}
    for r in results:
        s = summary[r["category"]]
        s["total"] += 1
        s["pass" if r["result"] == "PASS" else "fail"] += 1

    print("=" * 72)
    print("90 例评估集结果（院内本地运行，无外部服务）")
    print("=" * 72)
    for cat in order:
        s = summary[cat]
        print(f"{cat:<4} {s['pass']:>3}/{s['total']:<3} 通过  失败 {s['fail']}")
    total_pass = sum(s["pass"] for s in summary.values())
    total = sum(s["total"] for s in summary.values())
    print("-" * 72)
    print(f"合计 {total_pass}/{total} 通过")
    failures = [r for r in results if r["result"] == "FAIL"]
    if failures:
        print("\n失败用例：")
        for r in failures:
            print(f"  [{r['category']}] {r['name']}\n      {r['error']}")
    print("=" * 72)

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(
        {"generated_at": datetime.now().isoformat(timespec="seconds"),
         "mode": "本地真实代码运行（规则引擎/排程/触达/审计）；HIS 为接口替身",
         "summary": summary, "total": total, "passed": total_pass,
         "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告已写入 {report_path}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
