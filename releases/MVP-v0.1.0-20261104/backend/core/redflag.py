"""红旗规则引擎：固定词表（高精度）+ 本地模型替身（覆盖口语/方言变体）。

设计依据：docs/设计方案.md §5.3；docs/借鉴清单.md A10 的规则包格式（版本化 JSON）。
明确不做：不输出诊断、不给用药建议（医疗-009、018）。

匹配在“分句”粒度上进行：否定（没有/没/无/未/不/否认/排除）只抑制它所在分句，
不会跨句掩盖真正的红旗（宁误报不漏报，但不能把正常回答误判成红旗）。
"""
from __future__ import annotations

import difflib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).resolve().parents[1] / "rules" / "redflag.json"
_PUNCT = re.compile(r"[\s，。、；！？：,.!?;:\"'“”‘’()（）\-—…·]+")
_CLAUSE_SPLIT = re.compile(r"[，。、；！？,.!?;:\n]+")
_NEGATORS = ("没有", "没", "无", "未", "不", "否认", "排除", "莫得", "冇")
_VARIANT_NEG = ("不", "没", "无", "未", "莫得", "冇")


@lru_cache(maxsize=8)
def load_rules() -> dict[str, Any]:
    with open(RULES_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _normalize(text: str) -> str:
    """全角转半角、去标点空白，仅用于匹配，不改原文（原文必须原样留痕）。"""
    text = unicodedata.normalize("NFKC", text or "")
    return _PUNCT.sub("", text)


def _clauses(text: str) -> list[str]:
    return [_normalize(c) for c in _CLAUSE_SPLIT.split(text or "") if c.strip()]


def _negated(clause: str, pos: int, lookback: int = 6) -> bool:
    """同一分句内、紧邻 6 字范围内出现否定词即视为否定。"""
    return any(n in clause[max(0, pos - lookback):pos] for n in _NEGATORS)


def _lexicon_hit(clause: str, keyword: str) -> bool:
    target = _normalize(keyword)
    if not target:
        return False
    start = clause.find(target)
    while start != -1:
        if not _negated(clause, start):
            return True
        start = clause.find(target, start + 1)
    return False


def _best_variant_span(clause: str, variant: str) -> tuple[float, str, int]:
    """返回 (相似度, 命中窗口, 窗口起点)；子串命中记 1.0。"""
    target = _normalize(variant)
    if not target:
        return 0.0, "", -1
    pos = clause.find(target)
    if pos != -1:
        return 1.0, target, pos
    best_score, best_window, best_pos = 0.0, "", -1
    span = len(target)
    for size in {max(2, span - 1), span, span + 1}:
        for i in range(0, max(0, len(clause) - size) + 1):
            window = clause[i:i + size]
            ratio = difflib.SequenceMatcher(None, window, target).ratio()
            if ratio > best_score:
                best_score, best_window, best_pos = ratio, window, i
    return round(best_score, 3), best_window, best_pos


def _polarity_conflict(window: str, variant: str) -> bool:
    """变体含否定词（如“不管用”）但文本窗口不含 → 语义反转，不按命中处理。"""
    return (any(n in _normalize(variant) for n in _VARIANT_NEG)
            and not any(n in window for n in _VARIANT_NEG))


def match(text: str) -> list[dict[str, Any]]:
    """返回命中列表：双通道 OR，同 rule 取置信度更高的一条。"""
    rules = load_rules()
    clauses = _clauses(text)
    threshold = float(rules["model_channel"]["min_confidence"])
    hits: dict[str, dict[str, Any]] = {}

    for rule in rules["rules"]:
        best: dict[str, Any] | None = None

        for keyword in rule.get("keywords", []):
            if any(_lexicon_hit(c, keyword) for c in clauses):
                best = {"rule_id": rule["rule_id"], "title": rule["title"],
                        "severity": rule["severity"], "channel": "lexicon",
                        "confidence": 1.0, "evidence": keyword,
                        "escalation": rule["escalation"]}
                break

        if best is None:
            for pattern in rule.get("regex", []):
                if re.search(pattern, text or ""):
                    best = {"rule_id": rule["rule_id"], "title": rule["title"],
                            "severity": rule["severity"], "channel": "lexicon",
                            "confidence": 1.0, "evidence": f"regex:{pattern}",
                            "escalation": rule["escalation"]}
                    break

        model_score, model_evidence = 0.0, ""
        for clause in clauses:
            for variant in rule.get("variants", []):
                score, window, pos = _best_variant_span(clause, variant)
                if score <= model_score or pos < 0:
                    continue
                if (any(window.startswith(n) for n in _NEGATORS)
                        or _negated(clause, pos) or _polarity_conflict(window, variant)):
                    continue
                model_score, model_evidence = score, variant

        # 宁误报不漏报：非否定、非语义反转的相似表述达到阈值即按红旗提示
        if model_score >= threshold and (best is None or best["confidence"] < 1.0):
            best = {"rule_id": rule["rule_id"], "title": rule["title"],
                    "severity": rule["severity"], "channel": "model",
                    "confidence": model_score, "evidence": model_evidence,
                    "escalation": rule["escalation"]}

        if best is not None:
            hits[rule["rule_id"]] = best

    return sorted(hits.values(), key=lambda h: (-h["confidence"], h["rule_id"]))


def rule_set_meta() -> dict[str, Any]:
    rules = load_rules()
    return {
        "rule_set": rules["rule_set_id"],
        "version": rules["version"],
        "effective_date": rules["effective_date"],
        "signed_by": rules["signed_by"],
        "action": rules["action"],
        "model_channel": rules["model_channel"],
        "count": len(rules["rules"]),
        "rules": [
            {"rule_id": r["rule_id"], "title": r["title"], "severity": r["severity"],
             "keywords": r.get("keywords", []), "variants": r.get("variants", []),
             "escalation": r["escalation"]}
            for r in rules["rules"]
        ],
    }
