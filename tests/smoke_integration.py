"""端到端集成冒烟测试：不依赖 FastAPI，直接走 db + parser + analysis + report 全链路。"""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "smoke.db")

from app import db
from app.analysis import (attach_allergens, collect_allergens, compare_label,
                          diff_allergens, diff_ingredients, flatten)
from app.parser import parse_recipe
from app.report import render_html_report, render_markdown_report

db.init_db()

# 1. 导入字典
n = db.bulk_import("""\
小麦粉：麸质谷物
乳清粉：乳
乳糖：乳
奶粉：乳
可可脂：
大豆卵磷脂：大豆
鸡蛋：蛋类
椰子粉：坚果
芝麻：芝麻
食盐：
""")
assert n == 10, n
print(f"1. 字典导入 {n} 条 OK")

# 2. 两版配方
TEXT_A = "配料表：小麦粉，白砂糖，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂），鸡蛋，食盐"
TEXT_B = "配料表：小麦粉，白砂糖，巧克力酱（可可脂，椰子粉），鸡蛋，芝麻酱（芝麻），食盐"
DECLARED = ["麸质谷物", "蛋类"]

dictionary = db.dict_lookup()

def build_version(tree):
    return {
        "tree": [x.to_dict() for x in tree],
        "allergens": sorted(collect_allergens(tree)),
        "unknown_ingredients": sorted({x.name for x in flatten(tree) if x.unknown}),
    }

tree_a = parse_recipe(TEXT_A)
attach_allergens(tree_a, dictionary)
va = build_version(tree_a)
tree_b = parse_recipe(TEXT_B)
attach_allergens(tree_b, dictionary)
vb = build_version(tree_b)

assert set(va["allergens"]) == {"麸质谷物", "乳", "大豆", "蛋类"}, va["allergens"]
assert set(vb["allergens"]) == {"麸质谷物", "坚果", "蛋类", "芝麻"}, vb["allergens"]
assert "白砂糖" in va["unknown_ingredients"]  # 未登记原料被标记
print("2. 两版配方解析与过敏原传播 OK")
print("   A 版过敏原:", va["allergens"])
print("   B 版过敏原:", vb["allergens"])

# 3. 标签核对（对照 B 版，含遗漏来源路径）
check = compare_label(vb["allergens"], DECLARED, vb["tree"])
assert check["missing"] == ["坚果", "芝麻"], check
assert check["over_reported"] == [], check
assert check["matched"] == ["蛋类", "麸质谷物"], check
assert check["missing_sources"]["坚果"] == [["巧克力酱", "椰子粉"]], check
assert check["missing_sources"]["芝麻"] == [["芝麻酱", "芝麻"]], check
print("3. 标签核对 OK  遗漏:", check["missing"], " 多报:", check["over_reported"])
print("   来源路径:", check["missing_sources"])

# 3b. 同名原料处于不同分支时来源路径分别保留，不合并
tree_c = parse_recipe("巧克力酱（奶粉（乳清粉）），夹心酱（奶粉（乳清粉））")
attach_allergens(tree_c, dictionary)
vc = build_version(tree_c)
check_c = compare_label(vc["allergens"], [], vc["tree"])
assert check_c["missing_sources"]["乳"] == [
    ["夹心酱", "奶粉"],
    ["夹心酱", "奶粉", "乳清粉"],
    ["巧克力酱", "奶粉"],
    ["巧克力酱", "奶粉", "乳清粉"],
], check_c["missing_sources"]
# 父节点自身标注与子配料传播各产生一条路径；同名分支分别保留、不合并
print("3b. 同名分支路径不合并 / 自身标注+子配料传播 OK")

# 4. 版本对比
diff = diff_allergens(va["allergens"], vb["allergens"])
assert diff["added"] == ["坚果", "芝麻"]
assert diff["removed"] == ["乳", "大豆"]
changes = diff_ingredients(tree_a, tree_b)
names = {c["ingredient"]: c["change"] for c in changes}
assert names.get("巧克力酱") == "modified" and names.get("芝麻酱") == "added", names
print("4. 版本对比 OK  新增:", diff["added"], " 减少:", diff["removed"])

# 5. 入库 + 报告导出
result = {
    "version_a": va, "version_b": vb, "diff": diff,
    "ingredient_changes": changes,
    "label_check": check, "label_check_version": "新版配方",
}
rid = db.create_review(name="巧克力饼干 配方变更审查", declared=DECLARED,
                       text_a=TEXT_A, text_b=TEXT_B,
                       label_a="现版配方", label_b="新版配方", result=result)
review = db.get_review(rid)
html_report = render_html_report(review)
md_report = render_markdown_report(review)
assert "标签遗漏" in html_report and "坚果" in html_report and "芝麻" in html_report
assert "巧克力酱" in html_report and "来源路径" in html_report
assert "巧克力酱 › 椰子粉" in md_report and "芝麻酱 › 芝麻" in md_report
assert "# 食品过敏原标签审查报告" in md_report and "新增过敏原" in md_report
assert len(db.list_reviews()) == 1

# 5b. 模拟升级前的旧记录（结果中无 missing_sources），重新打开时自动补齐
old_result = dict(result)
old_result["label_check"] = {k: v for k, v in check.items()
                             if k != "missing_sources"}
old_rid = db.create_review(name="旧版记录", declared=DECLARED,
                           text_a=TEXT_A, text_b=TEXT_B,
                           label_a="现版配方", label_b="新版配方",
                           result=old_result)
old_review = db.get_review(old_rid)
assert old_review["result"]["label_check"]["missing_sources"]["坚果"] == \
    [["巧克力酱", "椰子粉"]]
print(f"5. 审查记录入库 (#{rid}) 与 HTML/Markdown 报告导出 OK；旧记录回填 OK")

# 6. 字典时效复核：指纹保存、无关变化不误报、涉及映射变化才失效
from app.recheck import (build_result, diff_results, involved_keys,
                         reparse_review, scoped_fingerprint)

review = db.get_review(rid)
fp = review["dict_fingerprint"]
assert fp and not review["needs_review"] and not review["legacy"]
assert scoped_fingerprint(dictionary, involved_keys(TEXT_A, TEXT_B)) == fp
# 字典顺序无关：重排后指纹一致
reordered = {k: dictionary[k] for k in reversed(list(dictionary))}
assert scoped_fingerprint(reordered, involved_keys(TEXT_A, TEXT_B)) == fp
# 无关原料增删改、重复导入不使记录失效
db.upsert_entry("无关花生酱", ["花生"])
db.upsert_entry("无关花生酱", ["花生", "坚果"])
db.delete_entry("无关花生酱")
db.bulk_import("食盐：\n鸡蛋：蛋类\n")
assert db.get_review(rid)["needs_review"] is False
# 未识别原料（白砂糖）补登为空过敏原：映射仍为空集，不失效
db.upsert_entry("白砂糖", [])
assert db.get_review(rid)["needs_review"] is False
# 涉及原料映射变化（可可脂新增亚硫酸盐，A/B 均含）才标“需复核”
db.upsert_entry("可可脂", ["亚硫酸盐"])
assert db.get_review(rid)["needs_review"] is True
item = next(r for r in db.list_reviews() if r["id"] == rid)
assert item["needs_review"] is True and item["legacy"] is False
print("6. 字典指纹：顺序/无关变化不误报，涉及原料映射变化才需复核 OK")

# 7. 一键复核：先预览前后差异（遗漏/多报/来源路径），确认后覆盖并更新指纹
stale = db.get_review(rid)
new_result = reparse_review(stale, db.dict_lookup())
diff = diff_results(stale["result"], new_result)
assert "亚硫酸盐" in diff["missing"]["added"]
assert diff["sources"]["亚硫酸盐"]["added"] == [["巧克力酱", "可可脂"]]
assert diff["versions"]["a"]["allergens"]["added"] == ["亚硫酸盐"]
assert diff["versions"]["b"]["allergens"]["added"] == ["亚硫酸盐"]
new_fp = scoped_fingerprint(db.dict_lookup(), involved_keys(TEXT_A, TEXT_B))
db.update_review_result(rid, new_result, new_fp)
fixed = db.get_review(rid)
assert fixed["needs_review"] is False and fixed["rechecked_at"]
assert fixed["dict_fingerprint"] == new_fp != fp
assert "亚硫酸盐" in fixed["result"]["label_check"]["missing"]
assert render_html_report(fixed).count("字典指纹") == 1
print("7. 一键复核：差异预览（遗漏/来源路径）+ 确认覆盖/更新指纹 OK")

# 8. 旧记录无指纹：按需复核，确认后补获指纹
with db._connect() as conn:
    conn.execute("UPDATE reviews SET dict_fingerprint = NULL, rechecked_at = NULL WHERE id = ?", (rid,))
legacy = db.get_review(rid)
assert legacy["needs_review"] is True and legacy["legacy"] is True
new_result = reparse_review(legacy, db.dict_lookup())
db.update_review_result(rid, new_result,
                        scoped_fingerprint(db.dict_lookup(),
                                           involved_keys(TEXT_A, TEXT_B)))
assert db.get_review(rid)["needs_review"] is False
# 报告导出在复核后仍可用
assert "标签遗漏" in render_html_report(db.get_review(rid))
print("8. 无指纹旧记录按需复核并补获指纹 OK")

print("\n全部集成冒烟测试通过 ✔")
