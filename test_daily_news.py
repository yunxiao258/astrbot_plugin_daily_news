# -*- coding: utf-8 -*-
"""每日新闻聚合播报插件测试：时间解析、时效过滤、时间标签、格式化

使用内存替身构造实例，不联网、不写真实 plugin_data。
"""
import asyncio
import sys
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone

sys.path.insert(0, r"D:\astrbot\data\plugins")

from astrbot_plugin_daily_news.main import BING_RSS_URL, CN_TZ, DailyNewsPlugin  # noqa: E402

# 本地时区偏移（用于构造 aware 时间）
LOCAL = datetime.now().astimezone().tzinfo


def make_plugin(config=None):
    """构造未初始化实例（绕过文件 IO 与持久化）"""
    p = DailyNewsPlugin.__new__(DailyNewsPlugin)
    p.config = config or {}
    return p


def aware(hour: int, day_offset: int = 0) -> datetime:
    """构造「今天 ± 偏移天」的本地 aware 时间（时分固定）"""
    dt = datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    return (dt + timedelta(days=day_offset)).replace(tzinfo=LOCAL)


class TestParseTime(unittest.TestCase):
    """RSS/Atom 时间字段解析"""

    def test_rfc822_utc8(self):
        # 带 +0800 偏移的 RFC822 格式（RSS pubDate 常见）
        p = make_plugin()
        dt = p._parse_time("Thu, 14 Aug 2026 08:00:00 +0800")
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)
        # 转为本地 aware 后仍是当天 08:00（本地时区 = UTC+8 时）
        self.assertEqual(dt.strftime("%H:%M"), "08:00")

    def test_iso8601(self):
        # Atom 的 ISO8601 格式
        p = make_plugin()
        dt = p._parse_time("2026-08-14T10:30:00+08:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.strftime("%H:%M"), "10:30")

    def test_iso8601_utc(self):
        p = make_plugin()
        dt = p._parse_time("2026-08-14T00:00:00Z")
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)

    def test_bad_value(self):
        p = make_plugin()
        self.assertIsNone(p._parse_time("不是时间"))
        self.assertIsNone(p._parse_time(""))
        self.assertIsNone(p._parse_time(None))


class TestFilterRecent(unittest.TestCase):
    """时效过滤"""

    def _items(self, *hours_ago):
        now = datetime.now().astimezone()
        return [
            {"title": f"新闻{i}", "pub_time": now - timedelta(hours=h) if h is not None else None}
            for i, h in enumerate(hours_ago)
        ]

    def test_keeps_recent_drops_old(self):
        p = make_plugin({"news_max_age_hours": 24})
        items = self._items(1, 30)  # 1 小时前 + 30 小时前
        kept = p._filter_recent(items, now=datetime.now().astimezone())
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["title"], "新闻0")

    def test_fallback_doubled_hours(self):
        # 严格过滤为空时放宽到 2 倍时长
        p = make_plugin({"news_max_age_hours": 24})
        items = self._items(36)
        kept = p._filter_recent(items, now=datetime.now().astimezone())
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["title"], "新闻0")

    def test_fallback_all_when_empty(self):
        # 2 倍时长仍为空 → 全部保留（避免推送为空）
        p = make_plugin({"news_max_age_hours": 24})
        items = self._items(100)
        kept = p._filter_recent(items, now=datetime.now().astimezone())
        self.assertEqual(len(kept), 1)

    def test_no_time_always_kept(self):
        p = make_plugin({"news_max_age_hours": 24})
        items = self._items(None)
        kept = p._filter_recent(items, now=datetime.now().astimezone())
        self.assertEqual(len(kept), 1)

    def test_disabled(self):
        # 0 或负值 = 不过滤
        p = make_plugin({"news_max_age_hours": 0})
        items = self._items(500, 600)
        self.assertEqual(len(p._filter_recent(items)), 2)
        p.config["news_max_age_hours"] = -5
        self.assertEqual(len(p._filter_recent(items)), 2)

    def test_dirty_value_fallback(self):
        p = make_plugin({"news_max_age_hours": "abc"})
        items = self._items(1)
        self.assertEqual(len(p._filter_recent(items)), 1)


class TestTimeTag(unittest.TestCase):
    """时间标签显示"""

    def test_today_hm(self):
        p = make_plugin()
        tag = p._time_tag(aware(8))
        self.assertEqual(tag, "[08:00] ")

    def test_cross_day_md_hm(self):
        p = make_plugin()
        tag = p._time_tag(aware(8, day_offset=-1))
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%m-%d")
        self.assertEqual(tag, f"[{yesterday} 08:00] ")

    def test_none_empty(self):
        p = make_plugin()
        self.assertEqual(p._time_tag(None), "")

    def test_naive_input(self):
        # naive 输入按本地时区标记，不报错
        p = make_plugin()
        tag = p._time_tag(datetime.now())
        self.assertTrue(tag.startswith("["))


class TestFormatNews(unittest.TestCase):
    """格式化输出"""

    def test_format_with_time(self):
        p = make_plugin()
        text = p._format_news([
            {"title": "标题A", "summary": "摘要", "source": "36氪",
             "pub_time": aware(8)},
            {"title": "标题B", "summary": "", "source": "cnBeta",
             "pub_time": None},
        ])
        self.assertIn("[08:00] 标题A", text)
        self.assertIn("2. 标题B", text)
        self.assertIn("来源: 36氪", text)

    def test_empty(self):
        p = make_plugin()
        self.assertEqual(p._format_news([]), "")

    def test_all_invalid_returns_empty(self):
        p = make_plugin()
        self.assertEqual(p._format_news([{"title": "", "summary": "", "source": ""}]), "")


class TestBingSearch(unittest.TestCase):
    """Bing 搜索集成"""

    def test_bing_url_encoded(self):
        p = make_plugin()
        url = BING_RSS_URL.format(
            count=10,
            query=urllib.parse.quote("科技 新闻"),
        )
        self.assertIn("format=rss", url)
        self.assertIn("count=10", url)
        self.assertIn(urllib.parse.quote("科技 新闻"), url)

    def test_fetch_news_bing_first(self):
        """配置关键词时：Bing 源优先于 RSS 源"""
        p = make_plugin({"news_bing_query": "热点"})
        base = datetime(2026, 8, 14, 12, 0, tzinfo=CN_TZ)
        p._fetch_bing = lambda query, count=10: [{"title": "Bing条目", "pub_time": base}]
        p._fetch_rss = lambda url, source: [{"title": f"RSS:{source}", "pub_time": base - timedelta(hours=1)}]
        p._filter_recent = lambda items, now=None: items
        news = p._fetch_news()
        self.assertEqual(news[0]["title"], "Bing条目")
        # 手动关键词覆盖配置关键词
        p._fetch_bing = lambda query, count=10: [{"title": f"手动:{query}", "pub_time": base}]
        news = p._fetch_news(bing_query="自定义词")
        self.assertEqual(news[0]["title"], "手动:自定义词")

    def test_no_query_skips_bing(self):
        """未配置关键词且无手动关键词：不调用 Bing"""
        p = make_plugin()
        called = []
        p._fetch_bing = lambda query, count=10: called.append(query) or [{"title": "不应出现"}]
        p._fetch_rss = lambda url, source: []
        p._filter_recent = lambda items, now=None: items
        news = p._fetch_news()
        self.assertEqual(called, [])
        self.assertEqual(news, [])

    def test_bing_fail_fallback_rss(self):
        """Bing 失败时降级到 RSS 源（3 个 RSS 源各 1 条）"""
        p = make_plugin({"news_bing_query": "热点"})
        base = datetime(2026, 8, 14, 12, 0, tzinfo=CN_TZ)
        p._fetch_bing = lambda query, count=10: (_ for _ in ()).throw(Exception("Bing 挂了"))
        p._fetch_rss = lambda url, source: [{"title": "RSS兜底", "pub_time": base}]
        p._filter_recent = lambda items, now=None: items
        news = p._fetch_news()
        self.assertEqual(len(news), 3)
        self.assertEqual(news[0]["title"], "RSS兜底")


class TestManualNews(unittest.TestCase):
    """手动 /新闻 命令：关键词正确传给 _fetch_news，无 NameError 回归"""

    def _plugin(self):
        p = make_plugin()
        captured = {}
        p._fetch_news = lambda limit, bing_query=None: captured.update(
            limit=limit, q=bing_query
        ) or [{"title": "t", "url": "http://x", "date": "2026-08-17", "source": "s"}]
        return p, captured

    def test_keyword_passed_and_limit_default(self):
        p, captured = self._plugin()

        class R:
            def __init__(self, c):
                self.text = c

        class E:
            def __init__(self, msg):
                self.message_str = msg
                self.session = None

            def get_sender_id(self):
                return "1"

            def chain_result(self, chain):
                return R(chain[0].text)

        async def run():
            await p.manual_news(E("/新闻 人工智能"))
            first = (captured["limit"], captured["q"])
            await p.manual_news(E("/新闻"))
            second = (captured["limit"], captured["q"])
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first, (5, "人工智能"))
        self.assertEqual(second, (5, None))


if __name__ == "__main__":
    unittest.main(verbosity=1)
