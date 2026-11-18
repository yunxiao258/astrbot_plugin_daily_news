# -*- coding: utf-8 -*-
"""daily_news 插件新功能测试（v1.1.0）：分类订阅、关键词过滤、历史回看归档。"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from astrbot_plugin_daily_news.main import (  # noqa: E402
    NEWS_CATEGORIES,
    DailyNewsPlugin,
)


class FakeSession:
    def __init__(self, umo="onebot:GroupMessage:123"):
        self.umo = umo

    def __str__(self):
        return self.umo


class FakeEvent:
    def __init__(self, message_str="/新闻", umo="onebot:GroupMessage:123"):
        self.message_str = message_str
        self.session = FakeSession(umo)

    def get_sender_id(self):
        return "10001"

    def get_group_id(self):
        return "123"

    def get_platform_id(self):
        return "onebot"

    def chain_result(self, chain):
        return chain


def run(coro):
    return asyncio.run(coro)


def chain_text(chain) -> str:
    return chain[0].text


def make_plugin(config=None):
    """完整构造插件并把持久化重定向到临时目录"""
    tmp = tempfile.mkdtemp(prefix="news_extra_test_")
    cfg = dict(config or {})
    p = DailyNewsPlugin.__new__(DailyNewsPlugin)
    p.config = cfg
    p.data_dir = tmp
    p._pushed = {}
    p._group_platforms = {}
    p._task = None
    p._running = False
    return p, tmp


def sample_news():
    return [
        {"title": "AI 大模型发布", "summary": "某公司发布新一代模型", "source": "科技源", "pub_time": None},
        {"title": "股市大涨", "summary": "沪指突破 4000 点", "source": "财经源", "pub_time": None},
        {"title": "世界杯开幕", "summary": "足球盛宴开启", "source": "体育源", "pub_time": None},
    ]


class TestKeywordFilter(unittest.TestCase):
    def test_no_config_no_filter(self):
        p, _ = make_plugin()
        news = sample_news()
        self.assertEqual(len(p._apply_keyword_filters(news)), 3)

    def test_blocklist_removes(self):
        p, _ = make_plugin({"news_filter_keywords": "股市"})
        kept = p._apply_keyword_filters(sample_news())
        titles = [n["title"] for n in kept]
        self.assertNotIn("股市大涨", titles)
        self.assertEqual(len(kept), 2)

    def test_requirelist_keeps_only_hits(self):
        p, _ = make_plugin({"news_required_keywords": "AI,模型"})
        kept = p._apply_keyword_filters(sample_news())
        self.assertEqual([n["title"] for n in kept], ["AI 大模型发布"])

    def test_all_filtered_returns_original(self):
        # 全部命中黑名单时不强制清空，避免推送空白
        p, _ = make_plugin({"news_filter_keywords": "AI,股市,世界杯"})
        kept = p._apply_keyword_filters(sample_news())
        self.assertEqual(len(kept), 3)

    def test_combined_black_and_white(self):
        p, _ = make_plugin({
            "news_required_keywords": "发布",
            "news_filter_keywords": "股市",
        })
        kept = p._apply_keyword_filters(sample_news())
        self.assertEqual([n["title"] for n in kept], ["AI 大模型发布"])

    def test_applied_in_fetch_news(self):
        # 过滤在 _fetch_news 流程内生效（mock 掉 RSS 源避免测试联网）
        p, _ = make_plugin({"news_filter_keywords": "股市"})
        p._fetch_bing = lambda q, c=10: sample_news() if q == "热点" else []
        p._fetch_rss = lambda *a, **kw: []
        news = p._fetch_news(limit=10, bing_query="热点")
        self.assertFalse(any("股市" in n["title"] for n in news))


def _days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


class TestArchive(unittest.TestCase):
    def test_save_and_lookup_latest(self):
        p, tmp = make_plugin()
        d1, d2 = _days_ago(1), _days_ago(0)
        p._save_archive_snapshot(d1, "昨日新闻内容", 5)
        p._save_archive_snapshot(d2, "今日新闻内容", 8)
        date, snap = p._lookup_archive("")
        self.assertEqual(date, d2)
        self.assertIn("今日新闻内容", snap["text"])
        self.assertEqual(snap["count"], 8)

    def test_lookup_by_date(self):
        p, _ = make_plugin()
        d1, d2 = _days_ago(2), _days_ago(0)
        p._save_archive_snapshot(d1, "前日内容", 3)
        p._save_archive_snapshot(d2, "今日内容", 4)
        date, snap = p._lookup_archive(d1)
        self.assertEqual(date, d1)
        self.assertIn("前日内容", snap["text"])

    def test_lookup_missing_date(self):
        p, _ = make_plugin()
        p._save_archive_snapshot(_days_ago(0), "内容", 1)
        date, snap = p._lookup_archive("2020-01-01")
        self.assertIsNone(snap)

    def test_lookup_mmdd_fuzzy(self):
        p, _ = make_plugin()
        d_target = _days_ago(2)
        p._save_archive_snapshot(d_target, "模糊匹配内容", 2)
        date, snap = p._lookup_archive(d_target[5:])
        self.assertIsNotNone(snap)
        self.assertIn("模糊匹配内容", snap["text"])

    def test_expired_cleaned_by_keep_days(self):
        p, _ = make_plugin({"news_archive_days": 7})
        old_d = _days_ago(30)
        fresh_d = _days_ago(0)
        p._save_archive_snapshot(old_d, "过期内容", 1)
        p._save_archive_snapshot(fresh_d, "新鲜内容", 2)
        with open(os.path.join(p.data_dir, "news_archive.json"), encoding="utf-8") as f:
            raw = json.load(f)
        archives = raw["archives"]
        self.assertNotIn(old_d, archives)
        self.assertIn(fresh_d, archives)

    def test_corrupted_archive_reset(self):
        p, tmp = make_plugin()
        with open(os.path.join(tmp, "news_archive.json"), "w", encoding="utf-8") as f:
            f.write("{broken")
        self.assertEqual(p._load_archive(), {})


class TestCommandDispatch(unittest.TestCase):
    def test_categories_command(self):
        p, _ = make_plugin()
        r = run(p.manual_news(FakeEvent("/新闻 分类")))
        text = chain_text(r)
        for cat in ("科技", "财经", "体育"):
            self.assertIn(cat, text)
        self.assertIn("分类", text)

    def test_history_empty_hint(self):
        p, _ = make_plugin()
        r = run(p.manual_news(FakeEvent("/新闻 历史")))
        text = chain_text(r)
        self.assertIn("暂无新闻存档", text)

    def test_history_lookup_after_fetch(self):
        p, _ = make_plugin()

        def fake_fetch(limit=5, bing_query=None):
            return sample_news()

        p._fetch_news = fake_fetch
        run(p.manual_news(FakeEvent("/新闻")))
        r = run(p.manual_news(FakeEvent("/新闻 历史")))
        text = chain_text(r)
        self.assertIn("新闻回看", text)
        self.assertIn("AI 大模型发布", text)

    def test_history_by_date_command(self):
        p, _ = make_plugin()
        target_d = _days_ago(1)
        p._save_archive_snapshot(target_d, "昨日的内容", 4)
        r = run(p.manual_news(FakeEvent(f"/新闻 历史 {target_d}")))
        text = chain_text(r)
        self.assertIn(target_d, text)
        self.assertIn("昨日的内容", text)

    def test_category_search_converts_query(self):
        p, _ = make_plugin()
        captured = {}

        def fake_fetch(limit=5, bing_query=None):
            captured["q"] = bing_query
            return []

        p._fetch_news = fake_fetch
        run(p.manual_news(FakeEvent("/新闻 科技")))
        self.assertEqual(captured.get("q"), NEWS_CATEGORIES["科技"])

    def test_free_keyword_still_works(self):
        p, _ = make_plugin()
        captured = {}

        def fake_fetch(limit=5, bing_query=None):
            captured["q"] = bing_query
            return []

        p._fetch_news = fake_fetch
        run(p.manual_news(FakeEvent("/新闻 人工智能")))
        self.assertEqual(captured.get("q"), "人工智能")

    def test_categories_constant_shape(self):
        # 预置分类不少于 8 个，且值非空
        self.assertGreaterEqual(len(NEWS_CATEGORIES), 8)
        for k, v in NEWS_CATEGORIES.items():
            self.assertTrue(k and v)


if __name__ == "__main__":
    unittest.main(verbosity=1)
