import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.hybrid import (
    HybridKnowledgeSearch,
    fuse_rrf,
    keyword_query,
)
from app.providers import ProviderError
from app.rag import PgKnowledgeSearch
from app.schemas import Source


def source(id):
    return Source(
        id=id,
        title=id,
        institution="测试机构",
        url="https://example.org/source",
        published_at=None,
        collected_at="2026-10-06",
        text="测试正文",
        allowed_departments=[],
        kind="official_summary",
        score=0.9,
    )


class HybridTests(unittest.TestCase):
    def test_agreement_promotes_result(self):
        a = source("A")
        b = source("B")
        c = source("C")

        result = fuse_rrf(
            [[a, b], [b, c]],
            3,
        )

        self.assertEqual(result[0].id, "B")

        self.assertEqual(
            {item.id for item in result},
            {"A", "B", "C"},
        )

        self.assertEqual(b.score, 0.9)

    def test_duplicate_does_not_inflate_score(self):
        a = source("A")
        b = source("B")

        self.assertEqual(
            fuse_rrf([[a, a, b]], 2),
            fuse_rrf([[a, b]], 2),
        )

    def test_empty_branch_and_top_k(self):
        result = fuse_rrf(
            [[], [source("A"), source("B")]],
            1,
        )

        self.assertEqual(
            [item.id for item in result],
            ["A"],
        )

    def test_chinese_query_and_punctuation(self):
        query = keyword_query("请介绍抗生素是什么？")

        self.assertIn('"抗生素"', query)
        self.assertNotIn('"请"', query)

        self.assertEqual(
            keyword_query("？！"),
            "",
        )

    def make_search(self):
        search = HybridKnowledgeSearch.__new__(HybridKnowledgeSearch)

        search.mode = "hybrid"
        search.candidates = 10
        search.keyword = Mock()

        return search

    def test_both_branches_are_called_and_fused(self):
        search = self.make_search()

        search.keyword.search.return_value = [source("B")]

        dense = AsyncMock(
            return_value=[
                source("A"),
                source("B"),
            ]
        )

        with patch.object(
            PgKnowledgeSearch,
            "search",
            dense,
        ):
            result = asyncio.run(search.search("测试问题", 1))

        self.assertEqual(result[0].id, "B")

        dense.assert_awaited_once_with(
            "测试问题",
            10,
        )

        search.keyword.search.assert_called_once_with(
            "测试问题",
            10,
        )

    def test_provider_failure_can_use_keyword_results(self):
        search = self.make_search()

        search.keyword.search.return_value = [source("B")]

        dense = AsyncMock(
            side_effect=ProviderError(
                "provider_http",
                "模型服务不可用",
            )
        )

        with patch.object(
            PgKnowledgeSearch,
            "search",
            dense,
        ):
            result = asyncio.run(search.search_detailed("测试问题", 1))

        self.assertEqual(
            result["results"][0].id,
            "B",
        )

        self.assertEqual(
            result["warnings"],
            ["dense: provider_http"],
        )

    def test_programming_error_is_not_hidden(self):
        search = self.make_search()

        search.keyword.search.return_value = [source("B")]

        dense = AsyncMock(side_effect=TypeError("test programming error"))

        with patch.object(
            PgKnowledgeSearch,
            "search",
            dense,
        ):
            with self.assertRaises(TypeError):
                asyncio.run(search.search("测试问题", 1))


if __name__ == "__main__":
    unittest.main()
