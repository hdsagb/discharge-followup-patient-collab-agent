#!/usr/bin/env python3
"""S2 功能与异常执行器（测试方案 docs/MVP测试方案.md 5.1–5.6、5.8）。

用法：python tools/run_s2.py --snapshot . --round 6
每个用例使用独立临时数据目录与独立端口，只测不改；结果默认写入
data/s{轮次}_s2.json 与同名 .md，全量执行（未指定 --only）时加 _full 后缀。
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
from s2_cases_core import CASES as CORE
from s2_cases_flow import CASES as FLOW

ALL = CORE + FLOW
KNOWN = {}
P0_DEFECTS: set[str] = set()


def run_one(case, index, snapshot):
    cid, title, level, expected, fn = case
    s2_lib.ROOT = snapshot
    port = 8900 + index
    ctx = s2_lib.Ctx(cid, port)
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
            result.notes.append("server.log 尾部：" + log.replace("\n", " | ")[-400:])
        shutil.rmtree(ctx.server.dir, ignore_errors=True)
    payload = result.payload("BLOCKED" if getattr(result, "blocked", False)
                             else ("FAIL" if error else None))
    if error:
        payload["status"] = "ERROR"
    return payload


def baseline(snapshot):
    path = snapshot / "data" / "eval_report.json"
    if not path.exists():
        return {"available": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {"available": True, "summary": data.get("summary", data.get("stats", {}))}
    except Exception as exc:
        return {"available": False, "error": str(exc)}


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


def markdown(snapshot, results, base, round_no):
    s = summarize(results)
    failures = [x for x in results if x["status"] != "PASS"]
    p0_fail = [x for x in failures if x["level"] == "P0"]
    lines = [
        f"# 第 {round_no} 轮 · S2 功能与异常回归报告（自动生成）",
        "",
        f"> 本文件为单次执行产物，默认输出到 `data/s{round_no}_s2[_full].md`；"
        "跨轮次归档见 `docs/MVP测试轮次汇总.md`，修复结论见 `docs/MVP第五轮修复总结报告.md`。",
        "",
        f"- 被测副本：`{snapshot}`",
        f"- 执行时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "- 范围：S1 基线回归 + S2（5.1–5.6、5.8），按约定不含 5.7 权限专项、第六/七节安全与非功能、5.9 浏览器 UAT",
        "- 原则：只测不改；每个用例独立临时库、独立端口，结束后清理运行数据",
        "",
        "## 一、结果总览",
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
    if base.get("available"):
        lines += ["", f"- S1 基线回归：`data/eval_report.json` {json.dumps(base['summary'], ensure_ascii=False)}"]
    lines += ["", "## 二、P0 未通过项", ""]
    if p0_fail:
        lines += ["| 用例 | 标题 | 结果 | 实际 |", "| --- | --- | --- | --- |"]
        for x in p0_fail:
            lines.append(f"| {x['id']} | {x['title']} | {x['status']} | {x['actual']} |")
    else:
        lines.append("无。")
    lines += ["", "## 三、缺陷与风险清单", ""]
    defect_rows = []
    for x in failures:
        defect = KNOWN.get(x["id"])
        if not defect:
            if x["status"] == "BLOCKED":
                defect = "测试阻塞：" + x["actual"]
            elif x["status"] == "ERROR":
                defect = "执行异常，需排查环境或用例前置条件"
            else:
                defect = "实际行为不符合用例预期"
        defect_rows.append((x["id"], x["level"], defect, x["id"] in KNOWN))
    if defect_rows:
        lines += ["| 用例 | 等级 | 缺陷/风险 | 类型 |", "| --- | --- | --- | --- |"]
        for cid, lv, text, known in defect_rows:
            lines.append(f"| {cid} | {lv} | {text} | {'已预判' if known else '本轮新发现'} |")
    else:
        lines.append("无。")
    lines += ["", "## 四、全部用例", "", "| 用例 | 等级 | 标题 | 结果 | 预期 | 实际 |", "| --- | --- | --- | --- | --- | --- |"]
    for x in results:
        actual = x["actual"].replace("|", "/")
        lines.append(f"| {x['id']} | {x['level']} | {x['title']} | {x['status']} | {x['expected']} | {actual} |")
    lines += ["", "## 五、结论", ""]
    if p0_fail:
        lines.append(f"第 {round_no} 轮 S2 未通过。P0 用例有 {len(p0_fail)} 条未通过，存在阻断验收的缺陷；"
                     "建议先修复下列 P0 项后再重新回归。")
        for x in p0_fail:
            lines.append(f"- {x['id']}：{KNOWN.get(x['id'], x['actual'])}")
    elif failures:
        lines.append("P0 全部通过，但仍有 P1/P2 或阻塞项，S2 不能判定为完成。")
    else:
        lines.append("S2 用例全部通过，可进入 S3。")
    lines += ["", "> 说明：RF-10 因缺少 ≥120 条独立标注语料未执行，按测试方案属于阻塞项，"
              "因此即使其余通过也不能给出“红旗命中率≥95%/误报率≤5%”的结论。"]
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=".")
    ap.add_argument("--only", default="")
    ap.add_argument("--round", type=int, default=5,
                    help="轮次号；决定默认产物命名 data/s{N}_s2[_full].json/.md（默认 5）")
    ap.add_argument("--json", help="JSON 输出路径（默认 data/s{N}_s2[_full].json）")
    ap.add_argument("--md", help="Markdown 输出路径（默认 data/s{N}_s2[_full].md）")
    args = ap.parse_args()
    snapshot = Path(args.snapshot).resolve()
    if not (snapshot / "backend" / "server.py").exists():
        raise SystemExit(f"副本无效：{snapshot}")
    only = {x.strip() for x in args.only.split(",") if x.strip()}
    tag = f"s{args.round}_s2{'' if only else '_full'}"
    out_json = Path(args.json) if args.json else ROOT / "data" / f"{tag}.json"
    out_md = Path(args.md) if args.md else ROOT / "data" / f"{tag}.md"
    selected = [c for c in ALL if not only or c[0] in only]
    print(f"目标副本：{snapshot}\n用例：{len(selected)}")
    results = []
    for i, case in enumerate(selected):
        print(f"[{i + 1}/{len(selected)}] {case[0]} {case[1]} ...", flush=True)
        res = run_one(case, i, snapshot)
        print(f"    {res['status']} {res['actual'][:90]}", flush=True)
        results.append(res)
    base = baseline(snapshot)
    summary = summarize(results)
    payload = {"run_at": datetime.now().isoformat(timespec="seconds"),
               "round": args.round, "suite": "s2",
               "snapshot": str(snapshot), "scope": "S2 5.1-5.6 + 5.8",
               "baseline": base, "summary": summary, "cases": results}
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(markdown(snapshot, results, base, args.round), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    print(f"JSON: {out_json}\nMD: {out_md}")


if __name__ == "__main__":
    main()
