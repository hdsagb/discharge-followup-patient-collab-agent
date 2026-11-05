#!/usr/bin/env python3
"""S3 权限·安全·非功能执行器（测试方案 docs/MVP测试方案.md 5.7、第六节、第七节）。

用法：python tools/run_s3.py --snapshot . --round 6
每个用例独立临时数据目录与独立端口，只测不改；结果默认写入
data/s{轮次}_s3.json 与同名 .md，全量执行（未指定 --only）时加 _full 后缀。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from datetime import datetime
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import s2_lib
from s3_cases_nf import CASES as NF
from s3_cases_perm import CASES as PERM
from s3_cases_sec import CASES as SEC

ALL = PERM + SEC + NF

KNOWN = {
    "PM-09": "登录无口令因子（方案已列为待接入院内统一认证的试点缺口）",
}
P0 = {"PM-01", "PM-02", "PM-03", "PM-04", "PM-07", "PM-08",
      "SEC-01", "SEC-02", "SEC-03", "SEC-04", "SEC-05", "SEC-06", "SEC-07", "SEC-08",
      "NF-06"}


def run_one(case, index, snapshot):
    cid, title, level, expected, fn = case
    s2_lib.ROOT = snapshot
    ctx = s2_lib.Ctx(cid, 9100 + index)
    result = s2_lib.Result(cid, title, level, expected)
    error = None
    try:
        ctx.start()
        fn(ctx, result)
    except Exception:
        error = traceback.format_exc()
        result.failures.append(f"用例执行异常：{error.strip().splitlines()[-1]}")
    finally:
        try:
            log = ctx.server.read_log()
        except Exception:
            log = ""
        ctx.stop()
        if error:
            result.notes.append("server.log 尾部：" + log.replace("\n", " | ")[-600:])
        shutil.rmtree(ctx.server.dir, ignore_errors=True)
    blocked = getattr(result, "blocked", False)
    payload = result.payload("BLOCKED" if blocked else ("FAIL" if error else None))
    if error:
        payload["status"] = "ERROR"
    print(f"[{cid}] {payload['status']} {payload['actual'][:110]}", flush=True)
    return payload


def summarize(results):
    out = {"PASS": 0, "FAIL": 0, "ERROR": 0, "BLOCKED": 0}
    for x in results:
        out[x["status"]] = out.get(x["status"], 0) + 1
    by_level = {}
    for x in results:
        lv = x["level"]
        by_level.setdefault(lv, {"total": 0, "pass": 0})
        by_level[lv]["total"] += 1
        if x["status"] == "PASS":
            by_level[lv]["pass"] += 1
    return {"counts": out, "total": len(results), "by_level": by_level}


HISTORY = [
    "## 一、历史轮次结果",
    "",
    "> 历史基线（前三轮累计）已归档至 `docs/MVP测试轮次汇总.md`，"
    "第五轮修复结论见 `docs/MVP第五轮修复总结报告.md`；本文件只保留本次执行结果。",
    "",
]


def markdown(snapshot, results, round_no):
    s = summarize(results)
    failures = [x for x in results if x["status"] != "PASS"]
    executed = [x for x in results if x["status"] != "BLOCKED"]
    p0_fail = [x for x in failures if x["id"] in P0 or x["level"] == "P0"]
    lines = [
        f"# 第 {round_no} 轮 · S3 权限·安全·非功能回归报告（自动生成）",
        "",
        f"> 本文件为单次执行产物，默认输出到 `data/s{round_no}_s3[_full].md`；"
        "跨轮次归档见 `docs/MVP测试轮次汇总.md`，修复结论见 `docs/MVP第五轮修复总结报告.md`。",
        "",
        f"- 被测副本：`{snapshot}`",
        f"- 执行时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "- 范围：S3（5.7 权限、第六节安全、第七节非功能）",
        "- 未含：5.9 浏览器 UAT（UI-01～08，由 tools/uat_ui.py 单独执行）、NF-05 2h 稳定性长窗口",
        "- 原则：只测不改；每个用例独立临时库与端口，结束即清理运行数据",
        "",
    ]
    lines += HISTORY
    lines += [
        f"## 二、第 {round_no} 轮结果总览",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 用例总数 | {s['total']} |",
        f"| 通过 | {s['counts'].get('PASS', 0)} |",
        f"| 失败 | {s['counts'].get('FAIL', 0)} |",
        f"| 执行异常 | {s['counts'].get('ERROR', 0)} |",
        f"| 阻塞未执行 | {s['counts'].get('BLOCKED', 0)} |",
        "",
        "| 等级 | 通过/总数 |",
        "| --- | --- |",
    ]
    for lv in ("P0", "P1", "P2"):
        v = s["by_level"].get(lv)
        if v:
            lines.append(f"| {lv} | {v['pass']}/{v['total']} |")
    lines += [
        "",
        f"- 已执行 {len(executed)} 例，其中通过 {s['counts'].get('PASS', 0)} 例，"
        f"未通过 {s['counts'].get('FAIL', 0) + s['counts'].get('ERROR', 0)} 例。",
        "",
        "## 三、准出建议",
        "",
    ]
    if p0_fail:
        lines.append("**不建议按现状准出。** 需先关闭以下 P0 项，或由产品/安全负责人书面接受风险：")
        lines.append("")
        for x in p0_fail:
            lines.append(f"- {x['id']} {x['title']}：{x['actual']}")
    else:
        lines.append("P0 项全部通过；P1/P2 遗留项见缺陷清单，可按风险接受后准出。")
    lines += ["", "## 四、缺陷与风险清单（本轮新增/复核）", ""]
    merged, seen = [], set()
    for x in failures:
        key = KNOWN.get(x["id"])
        if key:
            if key in seen:
                continue
            seen.add(key)
            merged.append((x["id"], x["level"], key, "已知缺陷（历史轮次已记录）"))
        elif x["status"] == "BLOCKED":
            merged.append((x["id"], x["level"], "测试阻塞：" + x["actual"], "阻塞"))
        elif x["status"] == "ERROR":
            merged.append((x["id"], x["level"], "执行异常，需排查前置条件", "环境/脚本"))
        else:
            merged.append((x["id"], x["level"], "实际行为不符合用例预期", "本轮新增"))
    if merged:
        lines += ["| 用例 | 等级 | 缺陷/风险 | 类型 |", "| --- | --- | --- | --- |"]
        for cid, lv, text, kind in merged:
            lines.append(f"| {cid} | {lv} | {text} | {kind} |")
    else:
        lines.append("无。")
    lines += ["", f"## 五、第 {round_no} 轮逐例结果", "",
              "| 用例 | 等级 | 标题 | 结果 | 实际 |", "| --- | --- | --- | --- | --- |"]
    for x in results:
        actual = x["actual"].replace("|", "｜")[:160]
        lines.append(f"| {x['id']} | {x['level']} | {x['title']} | {x['status']} | {actual} |")
    lines += ["", "## 六、证据与备注", ""]
    for x in results:
        if x["status"] == "PASS":
            continue
        lines.append(f"### {x['id']} {x['title']}（{x['status']}）")
        lines.append("")
        lines.append(f"- 期望：{x['expected']}")
        lines.append(f"- 实际：{x['actual']}")
        for ev in x.get("evidence", []):
            lines.append(f"- 证据：{ev}")
        for note in x.get("notes", []):
            lines.append(f"- 备注：{note}")
        lines.append("")
    lines += [
        "## 七、未执行 / 阻塞",
        "",
        "- NF-05 2 小时混合负载稳定性：需独立长窗口，本轮未执行。",
        "- NF-07 浏览器兼容性：执行器内无浏览器能力，记为 BLOCKED；"
        "已由第五轮真实浏览器 UAT 补做通过（见 `docs/MVP第五轮修复总结报告.md` 第四节）。",
        "- 5.9 UI-01～08：由 `tools/uat_ui.py` 单独执行，第五轮结果 8/8 PASS，执行器不重复执行。",
        "- 说明：SEC-01 零外联用例通过运行期审计钩子采集 socket 事件，"
        "SEC-06 与前轮 EX-02 同源但改走登录链路复核。",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default=".")
    parser.add_argument("--only", help="仅跑指定用例，逗号分隔，如 PM-01,SEC-03")
    parser.add_argument("--round", type=int, default=5,
                        help="轮次号；决定默认产物命名 data/s{N}_s3[_full].json/.md（默认 5）")
    parser.add_argument("--json", help="JSON 输出路径（默认 data/s{N}_s3[_full].json）")
    parser.add_argument("--md", help="Markdown 输出路径（默认 data/s{N}_s3[_full].md）")
    args = parser.parse_args()
    snapshot = (ROOT / args.snapshot).resolve()
    if not snapshot.exists():
        raise SystemExit(f"快照不存在：{snapshot}")
    cases = ALL
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        cases = [c for c in ALL if c[0] in wanted]
    tag = f"s{args.round}_s3{'' if args.only else '_full'}"
    out_json = Path(args.json) if args.json else ROOT / "data" / f"{tag}.json"
    out_md = Path(args.md) if args.md else ROOT / "data" / f"{tag}.md"
    results = []
    for index, case in enumerate(cases):
        results.append(run_one(case, index, snapshot))
    summary = summarize(results)
    report = {"round": args.round, "suite": "s3",
              "snapshot": str(snapshot), "generated_at": datetime.now().isoformat(timespec="seconds"),
              "summary": summary, "results": results, "known": KNOWN}
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(markdown(snapshot, results, args.round), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    print(f"已写出：{out_json}\n已写出：{out_md}")


if __name__ == "__main__":
    main()
