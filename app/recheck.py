"""字典时效复核。

- 过敏原字典的“作用域指纹”：只对审查配方实际涉及的原料映射取摘要，
  字典条目顺序调整、无关原料增删改均不会改变指纹；
- 按当前字典重新解析原始 A/B 配方与标签声明，重建完整审查结果；
- 对比旧结果与新结果：遗漏 / 多报过敏原的集合差异，以及每个遗漏
  过敏原在嵌套配料树中全部来源路径的前后差异。
"""
from __future__ import annotations

import hashlib
import json
from typing import Dict, Iterable, List, Optional, Set

from .analysis import (attach_allergens, collect_allergens, compare_label,
                       diff_allergens, diff_ingredients, flatten,
                       normalize_name)
from .parser import parse_recipe


# ---------- 字典指纹 ----------

def involved_keys(text_a: Optional[str], text_b: Optional[str]) -> Set[str]:
    """两份配方文本中出现过的全部原料节点名（归一化后，含复合原料）。"""
    keys: Set[str] = set()
    for text in (text_a or "", text_b or ""):
        if text and text.strip():
            for node in flatten(parse_recipe(text)):
                keys.add(normalize_name(node.name))
    return keys


def scoped_fingerprint(dictionary: Dict[str, Set[str]],
                       keys: Iterable[str]) -> str:
    """所用字典在“配方实际涉及原料”上的稳定指纹。

    - 仅纳入 ``keys``（配方 A/B 中出现过的原料，含嵌套子配料与复合原料），
      与配方无关的字典条目增删改不影响指纹；
    - 字典中缺失的 key 视为空过敏原集合——“未登记”与“登记为空过敏原”
      的映射同为空集，故把未识别原料补成空过敏原条目不改变指纹；
    - 逐行 ``key<TAB>过敏原,...`` 排序后取 SHA-256 前 16 位，
      与字典条目存储顺序（顺序调整）无关，重复计算结果稳定。
    """
    lines = []
    for key in sorted(set(keys)):
        allergens = ",".join(sorted(dictionary.get(key, set())))
        lines.append(f"{key}\t{allergens}")
    body = "\n".join(lines)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


# ---------- 按当前字典重建审查结果 ----------

def build_version(tree) -> dict:
    return {
        "tree": [node.to_dict() for node in tree],
        "allergens": sorted(collect_allergens(tree)),
        "unknown_ingredients": sorted({n.name for n in flatten(tree) if n.unknown}),
    }


def build_result(*, text_a: str, text_b: Optional[str] = None,
                 label_a: str = "现版配方", label_b: str = "新版配方",
                 declared: Iterable[str] = (),
                 dictionary: Dict[str, Set[str]]) -> dict:
    """解析 A/B 配方、传播过敏原并核对标签声明（与新建审查完全一致的流程）。"""
    tree_a = parse_recipe(text_a)
    attach_allergens(tree_a, dictionary)
    version_a = build_version(tree_a)

    result = {
        "version_a": version_a,
        "version_b": None,
        "diff": None,
        "ingredient_changes": None,
        # 标签默认对照现版配方；若提供了新版配方，则对照新版
        "label_check_version": label_a,
    }
    check_allergens = version_a["allergens"]
    checked_tree = version_a["tree"]

    if text_b and text_b.strip():
        tree_b = parse_recipe(text_b)
        attach_allergens(tree_b, dictionary)
        version_b = build_version(tree_b)
        result["version_b"] = version_b
        result["diff"] = diff_allergens(version_a["allergens"],
                                        version_b["allergens"])
        result["ingredient_changes"] = diff_ingredients(tree_a, tree_b)
        result["label_check_version"] = label_b
        check_allergens = version_b["allergens"]
        checked_tree = version_b["tree"]

    result["label_check"] = compare_label(
        check_allergens, declared, checked_tree)
    return result


def reparse_review(review: dict,
                   dictionary: Dict[str, Set[str]]) -> dict:
    """用当前字典对一条历史审查记录的原始输入重新解析、审查。"""
    return build_result(
        text_a=review["text_a"], text_b=review.get("text_b"),
        label_a=review.get("label_a") or "现版配方",
        label_b=review.get("label_b") or "新版配方",
        declared=review.get("declared") or [],
        dictionary=dictionary)


# ---------- 新旧结果差异 ----------

def _added_removed(old: Iterable[str], new: Iterable[str]) -> dict:
    old_set, new_set = set(old or ()), set(new or ())
    return {"added": sorted(new_set - old_set),
            "removed": sorted(old_set - new_set)}


def _version_diff(old_v: Optional[dict], new_v: Optional[dict]) -> Optional[dict]:
    if not old_v and not new_v:
        return None
    old_v, new_v = old_v or {}, new_v or {}
    allergens = _added_removed(old_v.get("allergens"), new_v.get("allergens"))
    unknown = _added_removed(old_v.get("unknown_ingredients"),
                             new_v.get("unknown_ingredients"))
    return {
        "allergens": allergens,
        "unknown_ingredients": unknown,
        "changed": bool(allergens["added"] or allergens["removed"]
                        or unknown["added"] or unknown["removed"]),
    }


def _sources_delta(old_sources: dict, new_sources: dict) -> dict:
    """每个过敏原的来源路径集合前后差异（新增路径 / 消失路径）。"""
    delta: Dict[str, dict] = {}
    for allergen in sorted(set(old_sources) | set(new_sources)):
        old_paths = {tuple(p) for p in old_sources.get(allergen, [])}
        new_paths = {tuple(p) for p in new_sources.get(allergen, [])}
        if old_paths != new_paths:
            delta[allergen] = {
                "added": [list(p) for p in sorted(new_paths - old_paths)],
                "removed": [list(p) for p in sorted(old_paths - new_paths)],
            }
    return delta


def _stable_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def diff_results(old: dict, new: dict) -> dict:
    """旧结果 vs 按当前字典重算结果的差异。

    missing / over_reported / matched —— 过敏原级别的新增、消除；
    sources                           —— 每个遗漏过敏原来源路径的增减；
    versions.a / versions.b          —— 各版配方过敏原全集与未识别原料变化；
    ingredient_changes_changed        —— 两版配料级变化表是否改变。
    """
    old_check = old.get("label_check") or {}
    new_check = new.get("label_check") or {}
    missing = _added_removed(old_check.get("missing"),
                             new_check.get("missing"))
    over_reported = _added_removed(old_check.get("over_reported"),
                                   new_check.get("over_reported"))
    matched = _added_removed(old_check.get("matched"),
                             new_check.get("matched"))
    sources = _sources_delta(old_check.get("missing_sources") or {},
                             new_check.get("missing_sources") or {})

    versions = {
        "a": _version_diff(old.get("version_a"), new.get("version_a")),
        "b": _version_diff(old.get("version_b"), new.get("version_b")),
    }
    ingredient_changes_changed = _stable_json(
        old.get("ingredient_changes")) != _stable_json(
        new.get("ingredient_changes"))

    label_changed = bool(missing["added"] or missing["removed"]
                         or over_reported["added"] or over_reported["removed"])
    sources_changed = bool(sources)
    versions_changed = bool(versions["a"] and versions["a"]["changed"]
                            or versions["b"] and versions["b"]["changed"])
    unchanged = not (label_changed or sources_changed or versions_changed
                     or ingredient_changes_changed)

    return {
        "label_check_version": {
            "old": old.get("label_check_version"),
            "new": new.get("label_check_version"),
        },
        "missing": missing,
        "over_reported": over_reported,
        "matched": matched,
        "sources": sources,
        "versions": versions,
        "ingredient_changes_changed": ingredient_changes_changed,
        "label_changed": label_changed,
        "sources_changed": sources_changed,
        "versions_changed": versions_changed,
        "unchanged": unchanged,
    }
