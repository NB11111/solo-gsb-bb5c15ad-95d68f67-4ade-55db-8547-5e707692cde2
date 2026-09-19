"""食品过敏原标签审查台 —— FastAPI 入口。

功能：
- 过敏原字典管理（原料 → 过敏原集合）；
- 解析带嵌套子配料的配方文本，传播并汇总过敏原；
- 对照标签声明，指出遗漏 / 多报的过敏原；
- 支持两版配方对比，输出过敏原变化；
- 生成并导出标签审查报告（HTML / Markdown）。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

from . import db
from .recheck import (build_result, diff_results, reparse_review,
                      scoped_fingerprint, involved_keys)
from .report import render_html_report, render_markdown_report

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 参考 GB 7718 致敏物质推荐目录
PRESET_ALLERGENS = [
    "麸质谷物", "甲壳类", "鱼类", "蛋类", "花生", "大豆", "乳",
    "坚果", "芝麻", "亚硫酸盐", "芹菜", "芥末", "羽扇豆", "软体动物",
]


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="食品过敏原标签审查台", version="1.0.0", lifespan=lifespan)


# ---------- 请求模型 ----------

class DictEntry(BaseModel):
    ingredient: str
    allergens: List[str] = Field(default_factory=list)


class BulkImport(BaseModel):
    text: str


class ReviewCreate(BaseModel):
    name: str = "未命名审查"
    text_a: str
    text_b: Optional[str] = None
    label_a: str = "现版配方"
    label_b: str = "新版配方"
    declared: List[str] = Field(default_factory=list)


# ---------- 页面 ----------

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/preset-allergens")
def preset_allergens():
    return {"allergens": PRESET_ALLERGENS}


# ---------- 过敏原字典 ----------

@app.get("/api/dict")
def get_dict():
    return {"entries": db.list_dict()}


@app.post("/api/dict")
def upsert_entry(entry: DictEntry):
    name = entry.ingredient.strip()
    if not name:
        raise HTTPException(400, "原料名不能为空")
    allergens = sorted({a.strip() for a in entry.allergens if a.strip()})
    db.upsert_entry(name, allergens)
    return {"ok": True}


@app.delete("/api/dict/{ingredient}")
def delete_entry(ingredient: str):
    db.delete_entry(ingredient)
    return {"ok": True}


@app.post("/api/dict/bulk")
def bulk_import(payload: BulkImport):
    imported = db.bulk_import(payload.text)
    return {"ok": True, "imported": imported}


# ---------- 审查 ----------

def _build_result(payload: ReviewCreate) -> dict:
    return build_result(
        text_a=payload.text_a,
        text_b=payload.text_b if payload.text_b and payload.text_b.strip() else None,
        label_a=payload.label_a, label_b=payload.label_b,
        declared=payload.declared, dictionary=db.dict_lookup())


@app.post("/api/reviews", status_code=201)
def create_review(payload: ReviewCreate):
    if not payload.text_a.strip():
        raise HTTPException(400, "配方文本不能为空")
    result = _build_result(payload)
    # 保存本次审查实际使用的字典在“配方涉及原料”上的稳定指纹
    fingerprint = scoped_fingerprint(
        db.dict_lookup(), involved_keys(payload.text_a, payload.text_b))
    rid = db.create_review(
        name=payload.name.strip() or "未命名审查",
        declared=sorted(set(payload.declared)),
        text_a=payload.text_a, text_b=payload.text_b,
        label_a=payload.label_a, label_b=payload.label_b,
        result=result, dict_fingerprint=fingerprint,
    )
    return {"id": rid, **result}


@app.get("/api/reviews")
def list_reviews():
    return {"reviews": db.list_reviews()}


@app.get("/api/reviews/{rid}")
def review_detail(rid: int):
    review = db.get_review(rid)
    if not review:
        raise HTTPException(404, "审查记录不存在")
    return review


@app.delete("/api/reviews/{rid}")
def remove_review(rid: int):
    if not db.get_review(rid):
        raise HTTPException(404, "审查记录不存在")
    db.delete_review(rid)
    return {"ok": True}


# ---------- 字典时效复核 ----------

def _reparse(review: dict) -> tuple[dict, str]:
    """按当前字典重新解析原始 A/B 配方与标签声明，返回 (新结果, 新指纹)。"""
    dictionary = db.dict_lookup()
    new_result = reparse_review(review, dictionary)
    fingerprint = scoped_fingerprint(
        dictionary, involved_keys(review["text_a"], review["text_b"]))
    return new_result, fingerprint


@app.get("/api/reviews/{rid}/recheck")
def preview_recheck(rid: int):
    """一键复核（预览）：用当前字典重算，并先返回遗漏/多报/来源路径的前后差异。"""
    review = db.get_review(rid)
    if not review:
        raise HTTPException(404, "审查记录不存在")
    new_result, fingerprint = _reparse(review)
    diff = diff_results(review["result"], new_result)
    return {
        "id": rid,
        "needs_review": review["needs_review"],
        "legacy": review["legacy"],
        "old_fingerprint": review["dict_fingerprint"],
        "new_fingerprint": fingerprint,
        "diff": diff,
        "new_result": new_result,
    }


@app.post("/api/reviews/{rid}/recheck", status_code=200)
def confirm_recheck(rid: int):
    """确认复核：以当前字典重算的结果覆盖旧结果，并更新指纹与复核时间。"""
    review = db.get_review(rid)
    if not review:
        raise HTTPException(404, "审查记录不存在")
    # 始终按当前字典重算，不采信前端回传的预览结果，确保覆盖内容可追溯
    new_result, fingerprint = _reparse(review)
    db.update_review_result(rid, new_result, fingerprint)
    updated = db.get_review(rid)
    return {"id": rid, **updated}


@app.get("/api/reviews/{rid}/report")
def review_report(rid: int, format: str = Query("html", pattern="^(html|md)$")):
    review = db.get_review(rid)
    if not review:
        raise HTTPException(404, "审查记录不存在")
    if format == "md":
        return Response(
            render_markdown_report(review),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition":
                     f'attachment; filename="allergen_review_{rid}.md"'},
        )
    return HTMLResponse(
        render_html_report(review),
        headers={"Content-Disposition":
                 f'attachment; filename="allergen_review_{rid}.html"'},
    )
