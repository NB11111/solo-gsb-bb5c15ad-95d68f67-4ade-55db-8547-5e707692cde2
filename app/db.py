"""SQLite 持久层：过敏原字典 + 审查记录。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

from .analysis import missing_allergen_sources, normalize_name
from .recheck import involved_keys, scoped_fingerprint

DB_PATH = os.environ.get("DB_PATH") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "data", "app.db"))

# 可重入锁：dict_lookup -> list_dict 等嵌套调用均在同一线程取锁
_lock = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS allergen_dict (
    ingredient TEXT PRIMARY KEY,
    allergens  TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    declared         TEXT NOT NULL DEFAULT '[]',
    text_a           TEXT NOT NULL,
    text_b           TEXT,
    label_a          TEXT NOT NULL DEFAULT '现版配方',
    label_b          TEXT NOT NULL DEFAULT '新版配方',
    result           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    dict_fingerprint TEXT,
    rechecked_at     TEXT
);
"""


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def init_db() -> None:
    with _lock, _connect() as conn:
        conn.executescript(_SCHEMA)
        # 旧库平滑升级：补齐字典时效复核所需列
        existing = {r["name"] for r in conn.execute(
            "PRAGMA table_info(reviews)").fetchall()}
        if "dict_fingerprint" not in existing:
            conn.execute("ALTER TABLE reviews ADD COLUMN dict_fingerprint TEXT")
        if "rechecked_at" not in existing:
            conn.execute("ALTER TABLE reviews ADD COLUMN rechecked_at TEXT")


# ---------- 过敏原字典 ----------

def list_dict() -> List[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT ingredient, allergens, updated_at FROM allergen_dict "
            "ORDER BY ingredient").fetchall()
    return [{"ingredient": r["ingredient"],
             "allergens": json.loads(r["allergens"]),
             "updated_at": r["updated_at"]} for r in rows]


def dict_lookup() -> Dict[str, Set[str]]:
    """{归一化原料名: 过敏原集合}，供解析传播使用。"""
    return {normalize_name(e["ingredient"]): set(e["allergens"])
            for e in list_dict()}


def upsert_entry(ingredient: str, allergens: List[str]) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO allergen_dict (ingredient, allergens, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(ingredient) DO UPDATE SET "
            "allergens=excluded.allergens, updated_at=excluded.updated_at",
            (ingredient, json.dumps(allergens, ensure_ascii=False), _now()))


def delete_entry(ingredient: str) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM allergen_dict WHERE ingredient = ?",
                     (ingredient,))


def bulk_import(text: str) -> int:
    """按行导入，每行格式：原料名：过敏原1，过敏原2（冒号/逗号中英文均可）。"""
    count = 0
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"[:：]", line, maxsplit=1)
        if len(parts) == 2:
            name, rest = parts[0].strip(), parts[1]
        else:
            # 没有冒号时，第一个空白前的词作为原料名
            tokens = line.split(None, 1)
            name, rest = tokens[0], (tokens[1] if len(tokens) > 1 else "")
        allergens = [a.strip() for a in re.split(r"[,，、;；\s]+", rest) if a.strip()]
        if name:
            upsert_entry(name, allergens)
            count += 1
    return count


# ---------- 审查记录 ----------

def compute_fingerprint(text_a: Optional[str], text_b: Optional[str]) -> str:
    """按配方实际涉及的原料，对当前字典取作用域指纹。"""
    return scoped_fingerprint(dict_lookup(), involved_keys(text_a, text_b))


def create_review(*, name: str, declared: List[str], text_a: str,
                  text_b: Optional[str], label_a: str, label_b: str,
                  result: dict,
                  dict_fingerprint: Optional[str] = None) -> int:
    if dict_fingerprint is None:
        dict_fingerprint = compute_fingerprint(text_a, text_b)
    with _lock, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO reviews (name, declared, text_a, text_b, label_a, "
            "label_b, result, created_at, dict_fingerprint, rechecked_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, json.dumps(declared, ensure_ascii=False), text_a, text_b,
             label_a, label_b, json.dumps(result, ensure_ascii=False), _now(),
             dict_fingerprint, None))
        return cur.lastrowid


def update_review_result(rid: int, result: dict,
                         dict_fingerprint: Optional[str] = None) -> None:
    """复核确认后覆盖审查结果，并把字典指纹更新到当前字典、记录复核时间。"""
    if dict_fingerprint is None:
        with _lock, _connect() as conn:
            row = conn.execute(
                "SELECT text_a, text_b FROM reviews WHERE id = ?",
                (rid,)).fetchone()
        if row is None:
            return
        dict_fingerprint = compute_fingerprint(row["text_a"], row["text_b"])
    with _lock, _connect() as conn:
        conn.execute(
            "UPDATE reviews SET result = ?, dict_fingerprint = ?, "
            "rechecked_at = ? WHERE id = ?",
            (json.dumps(result, ensure_ascii=False), dict_fingerprint,
             _now(), rid))


def _row_to_review(row: sqlite.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "declared": json.loads(row["declared"]),
        "text_a": row["text_a"],
        "text_b": row["text_b"],
        "label_a": row["label_a"],
        "label_b": row["label_b"],
        "result": json.loads(row["result"]),
        "created_at": row["created_at"],
        "dict_fingerprint": row["dict_fingerprint"],
        "rechecked_at": row["rechecked_at"],
    }


def _annotate_freshness(r: dict,
                        dictionary: Optional[Dict[str, Set[str]]] = None) -> dict:
    """标注记录的字典时效状态。

    - 旧记录无指纹：needs_review=True, legacy=True（按需复核）；
    - 有指纹但与当前字典在“配方实际涉及原料”上的指纹不一致：需复核；
    - 顺序调整、无关原料变化不会改变作用域指纹，因此不会误报。
    """
    stored = r.get("dict_fingerprint")
    if not stored:
        r["needs_review"], r["legacy"] = True, True
        return r
    dictionary = dict_lookup() if dictionary is None else dictionary
    current = scoped_fingerprint(
        dictionary, involved_keys(r.get("text_a"), r.get("text_b")))
    r["needs_review"] = stored != current
    r["legacy"] = False
    return r


def _backfill_missing_sources(r: dict) -> dict:
    """历史记录兼容：补齐旧版结果中缺失的遗漏来源路径（missing_sources）。

    依据记录内已持久化的配料树即时重算，无需修改库内 JSON，
    因此升级前产生的审查记录重新打开时同样可以展开来源路径。
    """
    result = r.get("result") or {}
    check = result.get("label_check")
    if not check or "missing_sources" in check:
        return r
    missing = check.get("missing", [])
    if not missing:
        check["missing_sources"] = {}
        return r
    checked_label = result.get("label_check_version", r.get("label_a"))
    version = (result.get("version_b")
               if result.get("version_b") and checked_label == r.get("label_b")
               else result.get("version_a"))
    tree = (version or {}).get("tree", [])
    check["missing_sources"] = missing_allergen_sources(tree, missing)
    return r


def get_review(rid: int) -> Optional[dict]:
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM reviews WHERE id = ?",
                           (rid,)).fetchone()
    if not row:
        return None
    review = _backfill_missing_sources(_row_to_review(row))
    return _annotate_freshness(review)


def list_reviews() -> List[dict]:
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reviews ORDER BY id DESC").fetchall()
    raw = [_backfill_missing_sources(_row_to_review(row)) for row in rows]
    # 字典只取一次，逐条按作用域指纹判定时效，避免无关字典变化误报
    dictionary = dict_lookup()
    reviews = []
    for r in raw:
        _annotate_freshness(r, dictionary)
        check = r["result"].get("label_check", {})
        reviews.append({
            "id": r["id"], "name": r["name"], "created_at": r["created_at"],
            "has_two_versions": bool(r["text_b"]),
            "missing_count": len(check.get("missing", [])),
            "over_reported_count": len(check.get("over_reported", [])),
            "needs_review": r["needs_review"],
            "legacy": r["legacy"],
            "rechecked_at": r["rechecked_at"],
        })
    return reviews


def delete_review(rid: int) -> None:
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM reviews WHERE id = ?", (rid,))
