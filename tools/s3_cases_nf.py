"""S3 用例（三）：第七节非功能（性能、并发、容量、备份、稳定性、兼容性）。"""
from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import s2_lib
from s2_lib import DATE, SECRET
from s3_lib import backend_audit, blocked, bulk_audit, bulk_schedules, make_record, row, tokens


def _pct(samples, q):
    ordered = sorted(samples)
    idx = max(0, int(round(q * (len(ordered) - 1))))
    return ordered[idx]


def _p95(samples):
    return _pct(samples, 0.95)


def _bulk_rows(n, department="骨科一病区", prefix="NF"):
    stamp = f"{DATE}T10:00:00"
    return [row(inpatient_no=f"{prefix}{i:04d}", name=f"合成患者{i:04d}",
                department=department, discharge_time=stamp,
                procedure_code="PG-ORTHO-THR", contact_phone=f"1390000{i:04d}",
                contact_type="本人" if i % 2 == 0 else "家属", auth_status="已授权")
            for i in range(n)]


def nf01(ctx, r):
    t = tokens(ctx)
    rows = _bulk_rows(200)
    ctx.import_rows(t["eng"], "NF-LAT-200", rows, exported_at=f"{DATE}T02:10:00")
    ctx.run_schedule(t["n1"])
    board, summary = [], []
    for _ in range(100):
        started = time.perf_counter()
        ctx.ok(ctx.get(f"/api/schedule?date={DATE}", t["n1"]), "排程查询")
        board.append((time.perf_counter() - started) * 1000)
    for _ in range(50):
        started = time.perf_counter()
        ctx.ok(ctx.get("/api/summary", t["qa"]), "汇总查询")
        summary.append((time.perf_counter() - started) * 1000)
    b95, s95 = _p95(board), _p95(summary)
    r.expect(b95 <= 300, f"排程接口 P95={b95:.1f}ms 超过 300ms")
    r.expect(s95 <= 300, f"汇总接口 P95={s95:.1f}ms 超过 300ms")
    r.note(f"200 人排程视图 P95={b95:.1f}ms（P50={_pct(board, 0.5):.1f}ms，n=100）；"
           f"/api/summary P95={s95:.1f}ms（n=50）")


def nf02(ctx, r):
    t = tokens(ctx)
    rows = _bulk_rows(200)
    started = time.perf_counter()
    ctx.import_rows(t["eng"], "NF-SCHED-200", rows, exported_at=f"{DATE}T02:10:00")
    import_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    data = ctx.run_schedule(t["n1"])
    run_ms = (time.perf_counter() - started) * 1000
    r.expect(run_ms <= 3000, f"200 人排程耗时 {run_ms:.0f}ms 超过 3s")
    r.expect(data.get("created", 0) >= 190, f"200 人排程生成数={data.get('created')} {data}")
    r.note(f"导入 200 行 {import_ms:.0f}ms；排程生成 {run_ms:.0f}ms（created={data.get('created')}）")


def nf03(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    targets = [t["n1"]] * 20 + [t["n2"]] * 20 + [t["doc"]] * 20 + [t["mgr"]] * 20 + [t["qa"]] * 20
    wards = {t["n1"]: "骨科一病区", t["n2"]: "骨科二病区"}
    failures, cross = [], []

    def probe(tok):
        status, p = ctx.get(f"/api/schedule?date={DATE}", tok)
        if status != 200:
            return status, tok, None
        items = p["data"]["items"]
        uid = ctx.user_ids.get(tok)
        ward = wards.get(tok)
        if ward:
            for x in items:
                if x["department"] != ward and x.get("nurse_id") != uid:
                    cross.append(f"{uid} 看到非本病区且非本人排程 {x['inpatient_no']}({x['department']})")
        for x in items:
            if not ("*" in x["contact_phone"] and len(x["contact_phone"]) == 9):
                cross.append(f"手机号未脱敏 {x['contact_phone']}")
        return status, tok, len(items)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=20) as ex:
        results = list(ex.map(probe, targets))
    elapsed = (time.perf_counter() - started) * 1000
    bad = [x for x in results if x[0] != 200]
    r.expect(not bad, f"并发查询出现非 200：{bad[:5]}")
    r.expect(not cross, f"并发下出现跨病区/脱敏异常：{cross[:5]}")
    r.note(f"5 会话 × 20 请求（共 {len(targets)} 次）用时 {elapsed:.0f}ms，"
           f"状态码全 200={not bad}，跨病区异常 {len(cross)} 处")


def nf04(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    started = time.perf_counter()
    bulk_schedules(ctx, 10000)
    sched_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    bulk_audit(ctx, 10000)
    audit_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    ctx.ok(ctx.get(f"/api/schedule?date={DATE}", t["mgr"]), "万级排程查询")
    board_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    items = ctx.audit_items(t["qa"], limit=500)
    audit_query_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    status, p = ctx.post("/api/audit/verify", t["qa"])
    verify_ms = (time.perf_counter() - started) * 1000
    r.expect(status == 200 and p["data"].get("ok") is True,
             f"1 万审计行验链：{status} {p.get('data')}")
    r.expect(len(items) == 500, f"审计分页返回 {len(items)} 条")
    r.expect(board_ms <= 2000, f"万级排程查询 {board_ms:.0f}ms 超过 2s")
    r.expect(audit_query_ms <= 1000, f"审计分页查询 {audit_query_ms:.0f}ms 超过 1s")
    size = ctx.db_path.stat().st_size if ctx.db_path.exists() else 0
    r.note(f"造数：排程 1 万行 {sched_ms:.0f}ms、审计 1 万行 {audit_ms:.0f}ms；"
           f"查询：排程 {board_ms:.0f}ms、审计 500 条 {audit_query_ms:.0f}ms、"
           f"验链 {verify_ms:.0f}ms（审计总数 {p['data'].get('count')}，库 {size / 1_048_576:.1f}MB）")


def nf05(ctx, r):
    blocked(r, "2 小时混合负载稳定性需独立长窗口执行，本轮按测试方案标记未执行",
            "TRAE-debugger / 独立压测窗口：50 并发持续 2h，每 10min 采样 P95 与错误率，"
            "结束后对齐审计链与表行数；建议在准出前单独排期")


def nf06(ctx, r):
    t = tokens(ctx)
    ctx.import_fixture(t["eng"])
    ctx.run_schedule(t["n1"])
    make_record(ctx, t["n1"], t["eng"], "0101")
    ctx.ok(ctx.post("/api/audit/verify", t["qa"]), "源库验链")
    backup = ctx.server.dir / "backup.db"
    src = sqlite3.connect(ctx.db_path)
    dst = sqlite3.connect(backup)
    started = time.perf_counter()
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    backup_ms = (time.perf_counter() - started) * 1000
    conn = sqlite3.connect(backup)
    conn.row_factory = sqlite3.Row
    try:
        audit_mod = backend_audit()
        data = audit_mod.verify_chain(conn, SECRET.encode())
        n_patients = conn.execute("SELECT COUNT(*) n FROM patients").fetchone()["n"]
        n_audit = conn.execute("SELECT COUNT(*) n FROM audit_events").fetchone()["n"]
    finally:
        conn.close()
    r.expect(data.get("ok") is True, f"备份库验链失败：{data}")
    r.expect(n_patients >= 10, f"备份库患者数={n_patients}")
    r.expect(backup.exists() and backup.stat().st_size > 0, "备份文件未生成")
    r.note(f"sqlite3 .backup {backup_ms:.0f}ms，{backup.stat().st_size / 1024:.0f}KB，"
           f"患者 {n_patients} 条、审计 {n_audit} 条，验链 head={data.get('head_hash','')[:12]}…")


def nf07(ctx, r):
    blocked(r, "执行器无浏览器能力；已由 tools/uat_ui.py 在第五轮真实浏览器 UAT 补做（8/8 PASS）",
            "按 5.9 UI-01～08 用 Playwright 驱动 Edge 分别在 1280×800 与 1920×1080 走查排程、外呼、"
            "草稿确认、红旗工单、撤回与断网场景；记录滚动/换行/按钮可达性")


CASES = [
    ("NF-01", "接口延迟 P95 ≤ 300ms（200 人）", "P1",
     "200 人名单下排程视图与汇总接口 P95 不超过 300ms", nf01),
    ("NF-02", "200 人排程 ≤ 3s", "P1",
     "导入 200 行后一键生成排程在 3 秒内完成且生成数正确", nf02),
    ("NF-03", "5 会话 × 20 并发无错乱", "P1",
     "5 个角色并发 100 次查询全部 200，无跨病区数据、手机号保持脱敏", nf03),
    ("NF-04", "1 万排程 / 1 万审计行可查询", "P1",
     "万级排程与审计行下查询与验链可用，分页正常", nf04),
    ("NF-05", "2 小时混合负载稳定性", "P1",
     "50 并发持续 2 小时无错误率抬升、无审计断链", nf05),
    ("NF-06", "sqlite3 .backup 可恢复且验链通过", "P0",
     "在线备份得到的库可独立打开，审计链完整、业务行数一致", nf06),
    ("NF-07", "浏览器兼容性（1280×800 / 1920×1080）", "P2",
     "Chrome/Edge 两种分辨率下核心页面可完成操作", nf07),
]
