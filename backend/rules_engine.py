"""合规规则引擎。

对采集到的视频内容（文案 / 语音转写 / 画面OCR文字）按规则进行匹配，
判定合规状态与风险等级。

支持：
  - keyword：关键词包含匹配（不区分大小写）
  - regex：正则匹配
  - 预留 semantic_check 钩子，可接入大模型做语义级合规判断
"""
import re
import json
from typing import List, Dict, Any, Optional

_SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "forbidden": 4}
_SEVERITY_REVERSE = {v: k for k, v in _SEVERITY_ORDER.items()}
_SEVERITY_CN = {
    "none": "无", "low": "低风险", "medium": "中风险",
    "high": "高风险", "forbidden": "禁止发布",
}

# 参与匹配的文本字段及其中文名
_TEXT_FIELDS = {
    "caption": "文案",
    "transcript": "语音转写",
    "ocr_text": "画面文字",
}


def _match_rule_in_text(rule: Dict[str, Any], text: str) -> Optional[str]:
    """返回命中的文本片段，未命中返回 None。"""
    if not text:
        return None
    pattern = rule["pattern"]
    if rule["rule_type"] == "regex":
        try:
            m = re.search(pattern, text, re.IGNORECASE)
            return m.group(0) if m else None
        except re.error:
            return None
    # keyword
    if pattern.lower() in text.lower():
        # 截取命中关键词附近的上下文，方便人工复核
        idx = text.lower().find(pattern.lower())
        start = max(0, idx - 15)
        end = min(len(text), idx + len(pattern) + 15)
        snippet = text[start:end]
        return ("…" if start > 0 else "") + snippet + ("…" if end < len(text) else "")
    return None


def analyze_content(
    content: Dict[str, Any],
    rules: List[Dict[str, Any]],
    semantic_hook=None,
) -> Dict[str, Any]:
    """分析单条内容的合规性。

    参数:
        content: 含 caption / transcript / ocr_text 等字段的 dict
        rules:   启用的规则列表
        semantic_hook: 可选，签名 (content, rules) -> List[matched_rule]
                       用于接入 LLM 语义判断
    返回:
        dict: status / risk_level / matched_rules / summary
    """
    matched: List[Dict[str, Any]] = []

    for rule in rules:
        if not rule.get("enabled", 1):
            continue
        for field, field_cn in _TEXT_FIELDS.items():
            snippet = _match_rule_in_text(rule, content.get(field) or "")
            if snippet:
                matched.append(
                    {
                        "rule_id": rule["id"],
                        "rule_name": rule["name"],
                        "category": rule["category"],
                        "severity": rule["severity"],
                        "matched_text": snippet,
                        "field": field_cn,
                    }
                )

    # 可选语义级判断（接入大模型时启用）
    if semantic_hook is not None:
        try:
            matched.extend(semantic_hook(content, rules) or [])
        except Exception:
            pass

    if matched:
        max_sev = max(_SEVERITY_ORDER.get(m["severity"], 0) for m in matched)
        risk_level = _SEVERITY_REVERSE[max_sev]
        status = "violation"
        cats = sorted({m["category"] for m in matched})
        summary = f"命中 {len(matched)} 条规则，涉及：{('、'.join(cats))}；最高风险等级：{_SEVERITY_CN.get(risk_level, risk_level)}。"
    else:
        risk_level = "none"
        status = "compliant"
        summary = "未命中任何合规规则，内容合规。"

    return {
        "status": status,
        "risk_level": risk_level,
        "matched_rules": matched,
        "summary": summary,
    }


def serialize_matched(matched: List[Dict[str, Any]]) -> str:
    return json.dumps(matched, ensure_ascii=False)
