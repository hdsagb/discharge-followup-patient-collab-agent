"""审计留痕离线校验：哈希链完整性 + 覆盖率 + 日结摘要。

设计依据：docs/设计方案.md §9（append-only、HMAC 哈希链、fail-closed）；
docs/06-测试评估.md 中“保留审计轨迹”的复现要求。

用法：
    python tools/verify_audit.py                # 校验默认 data/followup.db
    python tools/verify_audit.py --db <path>    # 校验指定库
退出码：0 = 链完整且覆盖率达标；1 = 校验失败；2 = 库不存在或无法读取。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.core import audit, db, security  # noqa: E402


def open_readonly(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> int:
    parser = argparse.ArgumentParser(description="审计留痕离线校验")
    parser.add_argument("--db", default=str(db.DB_PATH))
    args = parser.parse_args()

    path = Path(args.db)
    if not path.exists():
        print(f"审计库不存在：{path}")
        return 2
    try:
        conn = open_readonly(path)
        rows = conn.execute("SELECT * FROM audit_events ORDER BY seq").fetchall()
    except sqlite3.DatabaseError as exc:
        print(f"无法读取审计库：{exc}")
        return 2

    if not rows:
        print(f"审计库为空：{path}")
        return 1

    chain = audit.verify_chain(conn, security.secret())
    coverage = audit.coverage(conn)

    by_type = Counter(r["event_type"] for r in rows)
    by_day = Counter(r["at"][:10] for r in rows)
    by_action = Counter(r["action"] for r in rows)
    last = rows[-1]

    print("=" * 72)
    print("审计留痕校验（只读，院内本地）")
    print("=" * 72)
    print(f"库文件       {path}")
    print(f"事件总数     {len(rows)}")
    print(f"链完整性     {'通过' if chain['ok'] else '失败'}"
          + ("" if chain["ok"] else f"（断点 seq={chain.get('broken_at_seq')}：{chain.get('reason', '')}）"))
    print(f"链头哈希     {last['entry_hash']}")
    print(f"最后事件     seq={last['seq']} {last['at']} {last['action']} actor={last['actor_id']}")
    print(f"类型分布     " + "，".join(f"{k} {v}" for k, v in sorted(by_type.items())))
    print(f"覆盖率       "
          + ("达标" if coverage["coverage_ok"] else "未达标")
          + f"（登记表 {coverage['records_audited']}/{coverage['records']}，"
            f"工单 {coverage['tickets_audited']}/{coverage['tickets']}）")
    print("-" * 72)
    print("日结摘要")
    for day in sorted(by_day):
        print(f"  {day}  {by_day[day]} 条")
    print("高频动作")
    for action, count in by_action.most_common(8):
        print(f"  {action:<28} {count}")

    print("-" * 72)
    print(json.dumps({"chain": chain, "coverage": coverage,
                      "by_type": dict(by_type), "by_day": dict(by_day)},
                     ensure_ascii=False, indent=2))
    return 0 if chain["ok"] and coverage["coverage_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
