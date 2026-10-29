"""评测集与知识库的一致性检查（纯数据校验，不调用任何模型）。

为什么需要这条测试：
    评测集里写 `expected_sources: ["XX 制度 v3.2"]`，知识库那边一旦改名或拆分，
    这条用例就会**静默失效**——检索层永远拿不到那个 source，报告却只显示"一条没过"，
    排查方向会跑到 embedding 上去。这类"配置漂移"必须由测试而不是人眼来兜。

    同理，`expected` 里的锚点必须能在对应文档原文里找到；否则答案层测的是
    "模型会不会恰好说到"，而不是"系统有没有把正确的知识用上"。

运行：python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from rag.knowledge_docs import DEFAULT_KNOWLEDGE_DOCS  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover - CI 与本地都装了 PyYAML
    yaml = None

CASES_FILE = SRC / "eval" / "eval_cases.yaml"


@unittest.skipIf(yaml is None, "未安装 PyYAML，跳过评测集一致性检查")
class TestEvalCasesConsistency(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with CASES_FILE.open(encoding="utf-8") as fh:
            cls.cases = yaml.safe_load(fh)["cases"]
        cls.docs = DEFAULT_KNOWLEDGE_DOCS
        cls.sources = {d["source"] for d in cls.docs}

    # ---------------- 结构 ----------------

    def test_case_count_is_13(self):
        """用例条数是承诺值：10 正例 + 1 负例 + 2 路由。"""
        self.assertEqual(len(self.cases), 13)

    def test_ids_are_unique(self):
        ids = [c["id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)), f"重复 id: {ids}")

    def test_every_case_has_query_and_tags(self):
        for case in self.cases:
            with self.subTest(case=case.get("id")):
                self.assertTrue(case.get("query"), "缺少 query")
                self.assertTrue(case.get("tags"), "缺少 tags")

    def test_tags_are_known(self):
        allowed = {"rag_retrieval", "domain_answer", "agent_routing"}
        for case in self.cases:
            with self.subTest(case=case["id"]):
                unknown = set(case["tags"]) - allowed
                self.assertFalse(unknown, f"未知 tag: {unknown}")

    def test_has_negative_case_and_routing_cases(self):
        tags = [set(c["tags"]) for c in self.cases]
        routing = [t for t in tags if "agent_routing" in t]
        self.assertEqual(len(routing), 2, "应有 2 条路由用例")
        # 负例：既不走路由，也没有期望来源
        negatives = [c for c in self.cases if not c.get("expected_sources") and "agent_routing" not in c["tags"]]
        self.assertEqual(len(negatives), 1, "应有 1 条负例（无期望来源）")

    # ---------------- 与知识库对齐 ----------------

    def test_expected_sources_exist_in_knowledge_docs(self):
        for case in self.cases:
            for src in case.get("expected_sources") or []:
                with self.subTest(case=case["id"], source=src):
                    self.assertIn(src, self.sources, "知识库里没有这个 source（改名未同步？）")

    def test_expected_anchor_appears_in_referenced_docs(self):
        """expected 必须是锚点级字符串，且能在它声明的文档原文里找到。"""
        by_source = {}
        for doc in self.docs:
            by_source.setdefault(doc["source"], []).append(doc["content"])

        for case in self.cases:
            expected = case.get("expected")
            case_sources = case.get("expected_sources") or []
            if not expected or not case_sources:
                continue          # 负例 / 路由用例不校验
            text = "\n".join(t for s in case_sources for t in by_source.get(s, []))
            with self.subTest(case=case["id"], expected=expected):
                self.assertIn(
                    expected, text,
                    f"锚点 {expected!r} 不在 {case_sources} 的原文里——"
                    f"要么知识库改了，要么锚点写成了概括词",
                )

    def test_anchor_length_is_reasonable(self):
        """锚点过长会变成"整句背诵"，过短（1 个字）则没有区分度。"""
        for case in self.cases:
            expected = case.get("expected")
            if not expected:
                continue
            with self.subTest(case=case["id"]):
                self.assertGreaterEqual(len(expected), 2, "锚点太短，无区分度")
                self.assertLessEqual(len(expected), 60, "锚点过长，等于要求模型背原文")

    # ---------------- 知识库自身 ----------------

    def test_knowledge_docs_have_both_fields(self):
        for i, doc in enumerate(self.docs):
            with self.subTest(index=i):
                self.assertTrue(doc.get("content"), "缺少 content")
                self.assertTrue(doc.get("source"), "缺少 source")

    def test_knowledge_doc_length_in_range(self):
        """180-320 字是刻意选的区间：太短上下文不足，太长稀释向量语义。"""
        for doc in self.docs:
            with self.subTest(source=doc["source"]):
                self.assertGreaterEqual(len(doc["content"]), 150)
                self.assertLessEqual(len(doc["content"]), 400)


if __name__ == "__main__":
    unittest.main()
