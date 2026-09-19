"""字典时效复核测试：作用域指纹、需复核判定、重解析与前后差异。

覆盖要点：
- 指纹仅与“配方实际涉及的原料映射”有关：顺序调整、无关原料变化不误报；
- 把未识别原料补登记为空过敏原条目不改变映射（空集），不触发需复核；
- 涉及原料过敏原增删才使历史记录“需复核”，旧记录无指纹按需复核；
- 复核重解析原始 A/B 配方 + 标签声明，差异覆盖遗漏/多报与来源路径；
- 确认复核后覆盖结果、更新指纹、记录复核时间。
"""
import os
import tempfile
import unittest

_DB_DIR = tempfile.mkdtemp(prefix="allergen-recheck-")
os.environ["DB_PATH"] = os.path.join(_DB_DIR, "recheck.db")

from app import db  # noqa: E402
from app.analysis import normalize_name  # noqa: E402
from app.recheck import (build_result, diff_results, involved_keys,
                         reparse_review, scoped_fingerprint)  # noqa: E402

TEXT_A = ("配料表：小麦粉，巧克力酱（可可脂，奶粉（乳清粉，乳糖），大豆卵磷脂），"
          "鸡蛋，食盐")
TEXT_B = "配料表：小麦粉，白砂糖，巧克力酱（可可脂，椰子粉），鸡蛋，芝麻酱（芝麻），食盐"
DECLARED = ["麸质谷物", "蛋类"]

DICT_TEXT = """\
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
"""


def _lookup(items: dict) -> dict:
    return {normalize_name(k): set(v) for k, v in items.items()}


class FingerprintTest(unittest.TestCase):
    def setUp(self):
        self.dictionary = _lookup({
            "小麦粉": ["麸质谷物"], "乳清粉": ["乳"], "乳糖": ["乳"],
            "奶粉": ["乳"], "可可脂": [], "大豆卵磷脂": ["大豆"],
            "鸡蛋": ["蛋类"], "椰子粉": ["坚果"], "芝麻": ["芝麻"],
            "食盐": [],
        })
        self.keys = involved_keys(TEXT_A, TEXT_B)

    def test_fingerprint_stable_across_dict_order(self):
        keys = involved_keys(TEXT_A, None)
        fp1 = scoped_fingerprint(self.dictionary, keys)
        # 指纹只取决于涉及原料的映射内容，与传入/存储顺序无关
        reordered = {k: self.dictionary[k] for k in reversed(list(self.dictionary))}
        fp2 = scoped_fingerprint(reordered, keys)
        self.assertEqual(fp1, fp2)
        self.assertIsInstance(fp1, str)
        self.assertEqual(len(fp1), 16)

    def test_unrelated_ingredient_change_does_not_move_fingerprint(self):
        keys = involved_keys(TEXT_A, None)
        fp_before = scoped_fingerprint(self.dictionary, keys)
        # 新增与配方无关的原料
        d2 = {**self.dictionary, normalize_name("花生酱"): {"花生"}}
        self.assertEqual(scoped_fingerprint(d2, keys), fp_before)
        # 修改无关原料的过敏原
        d3 = {**self.dictionary, normalize_name("花生酱"): {"花生", "坚果"}}
        self.assertEqual(scoped_fingerprint(d3, keys), fp_before)
        # 删除无关原料
        d4 = {k: v for k, v in self.dictionary.items() if k != normalize_name("椰子粉")}
        # 椰子粉仅出现在 B 版，A 版指纹不受影响
        self.assertEqual(scoped_fingerprint(d4, involved_keys(TEXT_A, None)),
                         fp_before)

    def test_related_ingredient_change_moves_fingerprint(self):
        keys = involved_keys(TEXT_A, None)
        fp_before = scoped_fingerprint(self.dictionary, keys)
        # 配方中原料的过敏原映射被修改
        d2 = {**self.dictionary, normalize_name("乳清粉"): {"乳", "大豆"}}
        self.assertNotEqual(scoped_fingerprint(d2, keys), fp_before)
        # 涉及原料（此前有过敏原）被清空映射，也算映射变化
        d3 = {**self.dictionary, normalize_name("乳清粉"): set()}
        self.assertNotEqual(scoped_fingerprint(d3, keys), fp_before)
        # B 版涉及的原料仅在 A+B 作用域指纹中体现
        fp_ab = scoped_fingerprint(self.dictionary, self.keys)
        d4 = {**self.dictionary, normalize_name("椰子粉"): set()}
        self.assertEqual(scoped_fingerprint(d4, involved_keys(TEXT_A, None)),
                         scoped_fingerprint(self.dictionary,
                                            involved_keys(TEXT_A, None)))
        self.assertNotEqual(scoped_fingerprint(d4, self.keys), fp_ab)

    def test_registering_unknown_ingredient_as_empty_is_not_a_change(self):
        # “白砂糖”未登记（视为空集）；补登记为空过敏原条目，映射仍为空集
        keys = involved_keys(TEXT_A, TEXT_B)
        self.assertIn(normalize_name("白砂糖"), keys)
        fp_before = scoped_fingerprint(self.dictionary, keys)
        d2 = {**self.dictionary, normalize_name("白砂糖"): set()}
        self.assertEqual(scoped_fingerprint(d2, keys), fp_before)
        # 但若赋予过敏原，则属于映射变化
        d3 = {**self.dictionary, normalize_name("白砂糖"): {"亚硫酸盐"}}
        self.assertNotEqual(scoped_fingerprint(d3, keys), fp_before)


class ReviewStalenessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        # 每个用例使用独立临时库，避免相互干扰（db.DB_PATH 在导入时确定，
        # 直接改写模块属性而非环境变量）
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.DB_PATH = self.path
        db.init_db()
        db.bulk_import(DICT_TEXT)
        self.dictionary = db.dict_lookup()

    def _create(self, text_b=TEXT_B):
        result = build_result(text_a=TEXT_A, text_b=text_b,
                              declared=DECLARED, dictionary=self.dictionary)
        return db.create_review(
            name="复核测试", declared=sorted(DECLARED),
            text_a=TEXT_A, text_b=text_b,
            label_a="现版配方", label_b="新版配方", result=result)

    def test_new_record_is_fresh_and_has_fingerprint(self):
        rid = self._create()
        review = db.get_review(rid)
        self.assertTrue(review["dict_fingerprint"])
        self.assertFalse(review["needs_review"])
        self.assertFalse(review["legacy"])
        item = next(r for r in db.list_reviews() if r["id"] == rid)
        self.assertFalse(item["needs_review"])
        self.assertFalse(item["legacy"])

    def test_dict_reorder_and_unrelated_changes_do_not_flag(self):
        rid = self._create()
        # 批量重复导入（updated_at 变化但映射内容不变）不触发
        db.bulk_import(DICT_TEXT)
        self.assertFalse(db.get_review(rid)["needs_review"])
        # 新增/修改/删除与配方无关的条目不触发
        db.upsert_entry("无关花生酱", ["花生"])
        db.upsert_entry("无关花生酱", ["花生", "坚果"])
        db.delete_entry("无关花生酱")
        self.assertFalse(db.get_review(rid)["needs_review"])

    def test_related_mapping_change_flags_needs_review(self):
        rid = self._create()
        db.upsert_entry("乳清粉", ["乳", "大豆"])  # A 版涉及
        self.assertTrue(db.get_review(rid)["needs_review"])
        item = next(r for r in db.list_reviews() if r["id"] == rid)
        self.assertTrue(item["needs_review"])
        self.assertFalse(item["legacy"])

    def test_register_unknown_as_empty_does_not_flag(self):
        rid = self._create()
        db.upsert_entry("白砂糖", [])  # 未识别原料补空登记：映射仍为空
        self.assertFalse(db.get_review(rid)["needs_review"])
        db.upsert_entry("白砂糖", ["亚硫酸盐"])  # 赋予过敏原后才需复核
        self.assertTrue(db.get_review(rid)["needs_review"])

    def test_legacy_record_without_fingerprint_needs_review(self):
        rid = self._create()
        with db._connect() as conn:
            conn.execute("UPDATE reviews SET dict_fingerprint = NULL WHERE id = ?",
                         (rid,))
        review = db.get_review(rid)
        self.assertTrue(review["needs_review"])
        self.assertTrue(review["legacy"])

    def test_reparse_diff_missing_over_reported_and_sources(self):
        rid = self._create(text_b=None)
        review = db.get_review(rid)

        # 字典变化：可可脂（A 版叶子，巧克力酱的子配料）新增过敏原
        db.upsert_entry("可可脂", ["亚硫酸盐"])
        new_result = reparse_review(review, db.dict_lookup())
        d = diff_results(review["result"], new_result)
        self.assertIn("亚硫酸盐", d["missing"]["added"])
        self.assertEqual(d["missing"]["removed"], [])
        self.assertTrue(d["label_changed"])
        self.assertFalse(d["unchanged"])
        # 来源路径：亚硫酸盐来自 巧克力酱 › 可可脂
        self.assertEqual(d["sources"]["亚硫酸盐"]["added"],
                         [["巧克力酱", "可可脂"]])

        # 先只去掉乳清粉的乳：该来源路径消失，但乳仍由奶粉/乳糖携带，
        # 因此遗漏集合不变、只是来源路径少了一条
        db.upsert_entry("乳清粉", [])
        new_result_mid = reparse_review(review, db.dict_lookup())
        d_mid = diff_results(review["result"], new_result_mid)
        self.assertEqual(d_mid["missing"]["removed"], [])
        removed_mid = {tuple(p)
                       for p in d_mid["sources"]["乳"]["removed"]}
        self.assertEqual(removed_mid, {("巧克力酱", "奶粉", "乳清粉")})
        self.assertNotIn("乳", d_mid["missing"]["added"])

        # 再清空其余携带乳的映射（奶粉、乳糖）：乳才从配方过敏原中消失
        db.upsert_entry("奶粉", [])
        db.upsert_entry("乳糖", [])
        new_result2 = reparse_review(review, db.dict_lookup())
        d2 = diff_results(review["result"], new_result2)
        self.assertIn("乳", d2["missing"]["removed"])
        removed_paths = {tuple(p) for p in d2["sources"]["乳"]["removed"]}
        self.assertIn(("巧克力酱", "奶粉", "乳清粉"), removed_paths)
        self.assertIn(("巧克力酱", "奶粉"), removed_paths)
        self.assertIn(("巧克力酱", "奶粉", "乳糖"), removed_paths)

    def test_over_reported_diff(self):
        rid = self._create(text_b=None)
        review = db.get_review(rid)
        # 删除鸡蛋的蛋类映射：蛋类从配方过敏原消失，但标签仍声明 -> 新多报
        db.upsert_entry("鸡蛋", [])
        new_result = reparse_review(review, db.dict_lookup())
        d = diff_results(review["result"], new_result)
        self.assertIn("蛋类", d["over_reported"]["added"])
        # 蛋类仍在标签声明中，因此不会同时成为新增遗漏
        self.assertNotIn("蛋类", d["missing"]["added"])

    def test_no_diff_when_mapping_effectively_unchanged(self):
        rid = self._create(text_b=None)
        review = db.get_review(rid)
        # 食盐在配方中已登记为空；重复空登记映射不变
        db.upsert_entry("食盐", [])
        new_result = reparse_review(review, db.dict_lookup())
        d = diff_results(review["result"], new_result)
        self.assertTrue(d["unchanged"])
        self.assertFalse(db.get_review(rid)["needs_review"])

    def test_confirm_recheck_overwrites_and_refreshes_fingerprint(self):
        rid = self._create(text_b=None)
        old_fp = db.get_review(rid)["dict_fingerprint"]
        db.upsert_entry("可可脂", ["亚硫酸盐"])
        self.assertTrue(db.get_review(rid)["needs_review"])

        # 模拟 main 中的确认流程：重解析 -> 覆盖 -> 更新指纹
        review = db.get_review(rid)
        new_result = reparse_review(review, db.dict_lookup())
        new_fp = scoped_fingerprint(
            db.dict_lookup(), involved_keys(review["text_a"], review["text_b"]))
        db.update_review_result(rid, new_result, new_fp)

        updated = db.get_review(rid)
        self.assertFalse(updated["needs_review"])
        self.assertNotEqual(updated["dict_fingerprint"], old_fp)
        self.assertEqual(updated["dict_fingerprint"], new_fp)
        self.assertTrue(updated["rechecked_at"])
        self.assertIn("亚硫酸盐",
                      updated["result"]["label_check"]["missing"])
        self.assertEqual(
            updated["result"]["label_check"]["missing_sources"]["亚硫酸盐"],
            [["巧克力酱", "可可脂"]])

    def test_recheck_legacy_record_gets_fingerprint(self):
        rid = self._create()
        with db._connect() as conn:
            conn.execute("UPDATE reviews SET dict_fingerprint = NULL WHERE id = ?",
                         (rid,))
        review = db.get_review(rid)
        new_result = reparse_review(review, db.dict_lookup())
        new_fp = scoped_fingerprint(db.dict_lookup(),
                                    involved_keys(review["text_a"],
                                                  review["text_b"]))
        db.update_review_result(rid, new_result, new_fp)
        updated = db.get_review(rid)
        self.assertFalse(updated["needs_review"])
        self.assertFalse(updated["legacy"])
        self.assertTrue(updated["rechecked_at"])


if __name__ == "__main__":
    unittest.main()
