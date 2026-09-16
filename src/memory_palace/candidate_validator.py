"""Deterministic Candidate Validator v0.1。

不调用模型的确定性机械检查模块：只按冻结规则解析并检查模型记忆草稿。

公共接口：

    validate_response(content, *, allowed_evidence_lines) -> dict

返回值只含可直接 JSON 序列化的标准 Python 值，固定为：

    {
        "response_errors": [],      # 响应级错误；非空时不检查任何 Candidate
        "response_warnings": [],    # 顶层未知字段警告
        "valid_candidates": [],     # 合法 Candidate 结果对象
        "invalid_candidates": [],   # 非法 Candidate 结果对象
    }

仅使用 Python 标准库，不调用模型、不发网络请求、无第三方依赖。
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import Any

__all__ = ["validate_response"]

# 顶层允许的业务字段；其余按字段名升序产生 unknown_field_ignored 警告。
_RESPONSE_FIELDS = ("candidates",)

# Candidate 冻结业务字段；其余按字段名升序产生 unknown_field_ignored 警告并忽略。
_CANDIDATE_FIELDS = ("title", "summary", "evidence_lines")


def _reject_constant(value: str) -> Any:
    """json.loads 默认接受 NaN/Infinity/-Infinity，均不属于合法 JSON，一律拒绝。"""
    raise ValueError(f"non-standard JSON constant: {value}")


def _is_int(value: Any) -> bool:
    """整数判定：type() is int 排除 True/False（bool 是 int 子类，布尔值不算整数）。"""
    return type(value) is int


def _normalize_text(value: Any) -> Any:
    """字符串只删除首尾 Unicode 空白；非字符串原值保留给验证器报错。"""
    return value.strip() if isinstance(value, str) else value


def _normalize_evidence_lines(value: Any) -> Any:
    """数组则扫描原数组删除重复的合法整数行号（保留第一次出现）；
    布尔值不是合法整数；非整数成员不做猜测转换、不因“看起来重复”而删除。
    非数组时原值保留。"""
    if not isinstance(value, list):
        return value
    seen: set[int] = set()
    out: list[Any] = []
    for item in value:
        if _is_int(item):
            if item in seen:
                continue
            seen.add(item)
        out.append(item)
    return out


def _normalize_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """只保留实际存在的三业务字段，键顺序固定为 title, summary, evidence_lines。"""
    normalized: dict[str, Any] = {}
    for field in _CANDIDATE_FIELDS:
        if field not in candidate:
            continue
        value = candidate[field]
        if field == "evidence_lines":
            normalized[field] = _normalize_evidence_lines(value)
        else:
            normalized[field] = _normalize_text(value)
    return normalized


def _check_text_field(candidate: dict[str, Any], result: dict[str, Any], field: str) -> None:
    """title / summary 的机械检查：缺失 -> 类型 -> 空白。"""
    if field not in candidate:
        result["errors"].append({"code": "missing_field", "field": field})
        return
    value = candidate[field]
    if not isinstance(value, str):
        result["errors"].append({"code": "invalid_type", "field": field, "expected": "string"})
        return
    if value.strip() == "":
        result["errors"].append({"code": "blank_string", "field": field})


def _check_evidence_lines(
    candidate: dict[str, Any], result: dict[str, Any], allowed: set[int]
) -> None:
    """evidence_lines 的机械检查：缺失 -> 类型 -> 空数组 -> 逐项（按 raw 位置）。"""
    field = "evidence_lines"
    if field not in candidate:
        result["errors"].append({"code": "missing_field", "field": field})
        return
    value = candidate[field]
    if not isinstance(value, list):
        result["errors"].append({"code": "invalid_type", "field": field, "expected": "array"})
        return
    if len(value) == 0:
        result["errors"].append({"code": "empty_array", "field": field})
        return
    for item_index, item in enumerate(value):
        if not _is_int(item):
            result["errors"].append(
                {"code": "invalid_evidence_line", "field": field, "item_index": item_index}
            )
        elif item not in allowed:
            result["errors"].append(
                {
                    "code": "evidence_outside_window",
                    "field": field,
                    "item_index": item_index,
                    "value": item,
                }
            )


def _validate_candidate(index: int, candidate: Any, allowed: set[int]) -> dict[str, Any]:
    """单个 Candidate 的隔离验证：一个坏项不能拖死合法邻项。"""
    result = {
        "index": index,
        "raw_candidate": candidate,
        "normalized_candidate": None,
        "errors": [],
        "warnings": [],
    }

    if not isinstance(candidate, dict):
        result["errors"].append({"code": "candidate_not_object"})
        return result

    # 未知字段按字段名升序警告，不构成失败。
    for field in sorted(set(candidate) - set(_CANDIDATE_FIELDS)):
        result["warnings"].append({"code": "unknown_field_ignored", "field": field})

    result["normalized_candidate"] = _normalize_candidate(candidate)

    # 错误顺序固定：形状（上文）-> title -> summary -> evidence_lines。
    _check_text_field(candidate, result, "title")
    _check_text_field(candidate, result, "summary")
    _check_evidence_lines(candidate, result, allowed)
    return result


def validate_response(
    content: str,
    *,
    allowed_evidence_lines: Collection[int],
) -> dict[str, Any]:
    """模型原始文本 -> 响应级检查 -> 逐 Candidate 机械检查 -> 结果对象。"""
    result = {
        "response_errors": [],
        "response_warnings": [],
        "valid_candidates": [],
        "invalid_candidates": [],
    }

    # 只接受整体作为 JSON；不从 Markdown 围栏或说明文字中摘取，也不修复。
    try:
        parsed = json.loads(content, parse_constant=_reject_constant)
    except ValueError:
        result["response_errors"].append({"code": "invalid_json"})
        return result

    if not isinstance(parsed, dict):
        result["response_errors"].append({"code": "top_level_not_object"})
        return result

    # 顶层已解析为对象即保留未知字段警告，即使随后缺少/误写 candidates。
    for field in sorted(set(parsed) - set(_RESPONSE_FIELDS)):
        result["response_warnings"].append({"code": "unknown_field_ignored", "field": field})

    if "candidates" not in parsed:
        result["response_errors"].append({"code": "missing_candidates"})
        return result
    if not isinstance(parsed["candidates"], list):
        result["response_errors"].append({"code": "candidates_not_array"})
        return result
    if len(parsed["candidates"]) == 0:
        result["response_errors"].append({"code": "candidates_empty"})
        return result

    # allowed_evidence_lines 是可信系统输入，这里只做 O(1) 成员检查的对象转换。
    allowed = set(allowed_evidence_lines)

    for index, candidate in enumerate(parsed["candidates"]):
        candidate_result = _validate_candidate(index, candidate, allowed)
        if candidate_result["errors"]:
            result["invalid_candidates"].append(candidate_result)
        else:
            result["valid_candidates"].append(candidate_result)

    return result
