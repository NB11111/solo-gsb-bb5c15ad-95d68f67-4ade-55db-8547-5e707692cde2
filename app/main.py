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
from .recheck import build_recheck
from .report import render_html_report, render_markdown_report
from .review import analyze

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


class RecheckRequest(BaseModel):
    # false：仅预览前后差异；true：确认后覆盖结果并更新指纹
    confirm: bool = False


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

@app.post("/api/reviews", status_code=201)
def create_review(payload: ReviewCreate):
    if not payload.text_a.strip():
        raise HTTPException(400, "配方文本不能为空")
    declared = sorted(set(payload.declared))
    result, fingerprint = analyze(
        text_a=payload.text_a, text_b=payload.text_b,
        label_a=payload.label_a, label_b=payload.label_b,
        declared=declared)
    rid = db.create_review(
        name=payload.name.strip() or "未命名审查",
        declared=declared,
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


@app.post("/api/reviews/{rid}/recheck")
def recheck_review(rid: int, payload: RecheckRequest):
    """字典时效复核。

    始终先用当前字典重新解析记录的原始 A/B 配方与标签声明并计算前后差异；
    confirm=false 时仅返回差异供页面预览，不改动记录；
    confirm=true 时确认覆盖结果、刷新字典指纹（含旧记录补录指纹）。
    """
    review = db.get_review(rid)
    if not review:
        raise HTTPException(404, "审查记录不存在")
    outcome = build_recheck(review)
    if payload.confirm:
        db.update_review_result(
            rid, outcome["new_result"], outcome["fingerprint"])
        updated = db.get_review(rid)
        outcome["review"] = updated
    outcome["id"] = rid
    return outcome


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
