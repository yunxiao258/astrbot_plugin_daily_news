# -*- coding: utf-8 -*-
"""daily_news 插件单元测试：手动推送、文本格式化、定时触发、去重、失败降级、关闭不推送

测试原则：不联网、不写真实 plugin_data。
- 用 mock 替换 _fetch_news（返回固定新闻列表）
- 用内存替身替换持久化（_load_pushed/_save_pushed 置空实现，直接操作 _pushed）
- 用 FakeContext 记录 send_message 调用
- 时钟通过向 _check_and_push 注入 datetime 实现（mock 时钟时间）
"""
import asyncio
import sys
import unittest
from datetime import datetime

sys.path.insert(0, r"D:\astrbot\data\plugins")

from astrbot_plugin_daily_news.main import DailyNewsPlugin  # noqa: E402

# 固定新闻列表（mock 抓取结果）
FAKE_NEWS = [
    {"title": "AI 模型新突破引发关注", "summary": "某公司发布新一代大模型，性能大幅提升。", "source": "36氪"},
    {"title": "央行发布最新政策", "summary": "央行宣布下调存款准备金率。", "source": "cnBeta"},
]

FAKE_EMPTY = []


class FakeContext:
    """内存替身 context：记录所有 send_message 调用（umo -> [text, ...]）"""

    def __init__(self):
        self.sent = {}

    async def send_message(self, umo, chain):
        text = "".join(
            getattr(comp, "text", "") for comp in getattr(chain, "chain", [])
        )
        self.sent.setdefault(umo, []).append(text)


class FakeSession:
    def __init__(self, umo="default:GroupMessage:123"):
        self.umo = umo

    def __str__(self):
        return self.umo


class FakeEvent:
    """最小事件替身：session / get_sender_id / message_str / chain_result"""

    def __init__(self, message_str="/新闻", umo="default:GroupMessage:123", sender="user1"):
        self.session = FakeSession(umo)
        self.message_str = message_str
        self._sender = sender

    def get_sender_id(self):
        return self._sender

    def chain_result(self, chain):
        # 模拟真实 MessageEventResult：带 .chain 组件列表
        return type("FakeResult", (), {"chain": chain})()


def make_plugin(config=None, news=None):
    """构造插件：关闭真实持久化（内存替身），mock 抓取"""
    cfg = {
        "news_push_enable": False,
        "news_push_time": "08:00",
        "news_push_groups": "",
        "news_push_platform": "onebot",
    }
    cfg.update(config or {})
    p = DailyNewsPlugin(FakeContext(), cfg)
    # 内存替身替换持久化：加载置空、保存置空，只操作内存 _pushed
    p._load_pushed = lambda: None
    p._save_pushed = lambda: None
    p._pushed = {}
    if news is not None:
        p._fetch_news = lambda limit=5: news
    return p


class TestFormat(unittest.TestCase):
    """新闻文本格式化：标题/摘要/来源"""

    def test_format_includes_title_source(self):
        p = make_plugin()
        text = p._format_news(FAKE_NEWS, "2026-08-14")
        self.assertIn("2026-08-14", text)
        self.assertIn("AI 模型新突破引发关注", text)
        self.assertIn("36氪", text)
        self.assertIn("央行发布最新政策", text)
        self.assertIn("cnBeta", text)

    def test_format_summary_appears(self):
        text = make_plugin()._format_news(FAKE_NEWS)
        self.assertIn("性能大幅提升", text)

    def test_format_empty_returns_empty(self):
        self.assertEqual(make_plugin()._format_news([]), "")

    def test_format_all_invalid_returns_empty(self):
        p = make_plugin()
        # 全部条目缺标题时视为无效，返回空串
        self.assertEqual(p._format_news([{"title": "  ", "summary": "s", "source": "x"}]), "")

    def test_format_missing_summary_ok(self):
        p = make_plugin()
        text = p._format_news([{"title": "只有标题", "summary": "", "source": "新浪"}], "2026-08-14")
        self.assertIn("只有标题", text)
        self.assertIn("新浪", text)
        # 无摘要时不渲染空白摘要行
        self.assertNotIn("\n   \n", text)


class TestManualPush(unittest.TestCase):
    """手动 /新闻：抓取、格式化、回复"""

    def test_manual_push_success(self):
        import asyncio
        p = make_plugin(news=FAKE_NEWS)
        event = FakeEvent("/新闻")
        result = asyncio.run(p.manual_news(event))
        text = result.chain[0].text
        self.assertIn("今日新闻聚合播报", text)
        self.assertIn("AI 模型新突破引发关注", text)
        self.assertIn("36氪", text)

    def test_manual_push_fetch_failure(self):
        import asyncio
        p = make_plugin(news=FAKE_EMPTY)
        result = asyncio.run(p.manual_news(FakeEvent("/新闻")))
        self.assertIn("失败或为空", result.chain[0].text)

    def test_manual_push_fetch_raises(self):
        import asyncio
        p = make_plugin()

        def boom(limit=5):
            raise RuntimeError("network down")

        p._fetch_news = boom
        result = asyncio.run(p.manual_news(FakeEvent("/新闻")))
        self.assertIn("出错", result.chain[0].text)


class TestScheduledPush(unittest.TestCase):
    """定时播报：mock 时钟、触发推送、去重、降级、关闭不推送"""

    def _p(self, enable=True, groups="111111111,222222222", time_="08:00", news=None):
        p = make_plugin({
            "news_push_enable": enable,
            "news_push_groups": groups,
            "news_push_time": time_,
        }, news=news)
        p._running = True  # 模拟后台循环运行中
        return p

    def _at(self, hh="08:00", day="2026-08-14"):
        return datetime.strptime(f"{day} {hh}", "%Y-%m-%d %H:%M")

    def test_schedule_arrives_pushes_to_groups(self):
        p = self._p(news=FAKE_NEWS)
        asyncio.run(p._check_and_push(self._at("08:00")))
        # 两个目标群各推一次，且记录去重
        self.assertEqual(len(p.context.sent), 2)
        self.assertEqual(len(p.context.sent["onebot:GroupMessage:111111111"]), 1)
        self.assertEqual(len(p.context.sent["onebot:GroupMessage:222222222"]), 1)
        self.assertIn("2026-08-14", p.context.sent["onebot:GroupMessage:111111111"][0])
        self.assertEqual(p._pushed.get("2026-08-14"), ["111111111", "222222222"])

    def test_schedule_before_time_no_push(self):
        p = self._p(news=FAKE_NEWS)
        asyncio.run(p._check_and_push(self._at("07:59")))
        self.assertEqual(p.context.sent, {})
        self.assertEqual(p._pushed, {})

    def test_same_day_dedup(self):
        p = self._p(news=FAKE_NEWS)
        # 第一次：到达时间，触发推送
        asyncio.run(p._check_and_push(self._at("08:00")))
        # 第二次：同一时刻再检查，同天同群已推送，不再推送
        asyncio.run(p._check_and_push(self._at("08:00")))
        self.assertEqual(len(p.context.sent["onebot:GroupMessage:111111111"]), 1)
        self.assertEqual(len(p.context.sent["onebot:GroupMessage:222222222"]), 1)

    def test_next_day_pushes_again(self):
        p = self._p(news=FAKE_NEWS)
        asyncio.run(p._check_and_push(self._at("08:00", "2026-08-14")))
        asyncio.run(p._check_and_push(self._at("08:00", "2026-08-15")))
        self.assertEqual(len(p.context.sent["onebot:GroupMessage:111111111"]), 2)
        self.assertIn("2026-08-15", p.context.sent["onebot:GroupMessage:111111111"][1])

    def test_fetch_failure_degrades_no_crash(self):
        p = self._p(news=FAKE_EMPTY)
        asyncio.run(p._check_and_push(self._at("08:00")))
        # 抓取为空：不推送、不崩溃、不写去重记录
        self.assertEqual(p.context.sent, {})
        self.assertEqual(p._pushed, {})

    def test_disabled_no_push(self):
        p = self._p(enable=False, news=FAKE_NEWS)
        asyncio.run(p._check_and_push(self._at("08:00")))
        self.assertEqual(p.context.sent, {})

    def test_no_groups_no_push(self):
        p = self._p(groups="", news=FAKE_NEWS)
        asyncio.run(p._check_and_push(self._at("08:00")))
        self.assertEqual(p.context.sent, {})

    def test_closed_no_push(self):
        p = self._p(news=FAKE_NEWS)
        asyncio.run(p.terminate())  # 关闭后 _running=False
        self.assertFalse(p._running)
        asyncio.run(p._check_and_push(self._at("08:00")))
        # 关闭后定时检查直接返回，不推送
        self.assertEqual(p.context.sent, {})

    def test_invalid_push_time_falls_back(self):
        p = self._p(time_="不是时间", news=FAKE_NEWS)
        # 非法时间回退 08:00，因此 08:00 会触发
        self.assertEqual(p._push_time(), "08:00")
        asyncio.run(p._check_and_push(self._at("08:00")))
        self.assertEqual(len(p.context.sent), 2)


class TestConfigDefense(unittest.TestCase):
    """配置脏值防御"""

    def test_safe_bool(self):
        self.assertTrue(DailyNewsPlugin._safe_bool(True))
        self.assertTrue(DailyNewsPlugin._safe_bool("true"))
        self.assertTrue(DailyNewsPlugin._safe_bool("1"))
        self.assertFalse(DailyNewsPlugin._safe_bool("no"))
        self.assertFalse(DailyNewsPlugin._safe_bool(None))
        self.assertFalse(DailyNewsPlugin._safe_bool("garbage"))
        self.assertTrue(DailyNewsPlugin._safe_bool(None, True))

    def test_safe_int(self):
        self.assertEqual(DailyNewsPlugin._safe_int("12"), 12)
        self.assertEqual(DailyNewsPlugin._safe_int("abc"), 0)
        self.assertEqual(DailyNewsPlugin._safe_int(None), 0)
        self.assertEqual(DailyNewsPlugin._safe_int("abc", 5), 5)

    def test_push_groups_dirty(self):
        p = make_plugin({"news_push_groups": " 111, 222 ,,333 "})
        self.assertEqual(p._push_groups(), ["111", "222", "333"])
        p2 = make_plugin({"news_push_groups": None})
        self.assertEqual(p2._push_groups(), [])
        p3 = make_plugin({"news_push_groups": ["a", "b"]})
        self.assertEqual(p3._push_groups(), ["a", "b"])

    def test_platform_dirty(self):
        self.assertEqual(make_plugin({"news_push_platform": "  aiocqhttp "})._push_platform(), "aiocqhttp")
        self.assertEqual(make_plugin({"news_push_platform": None})._push_platform(), "onebot")
        self.assertEqual(make_plugin()._target_umo("123"), "onebot:GroupMessage:123")


class TestPersistence(unittest.TestCase):
    """持久化替身：内存 _pushed 去重标记行为"""

    def test_mark_and_check(self):
        p = make_plugin()
        self.assertFalse(p._already_pushed("2026-08-14", "111"))
        p._mark_pushed("2026-08-14", "111")
        self.assertTrue(p._already_pushed("2026-08-14", "111"))
        self.assertFalse(p._already_pushed("2026-08-14", "222"))
        # 重复标记不产生重复记录
        p._mark_pushed("2026-08-14", "111")
        self.assertEqual(p._pushed["2026-08-14"], ["111"])


if __name__ == "__main__":
    unittest.main(verbosity=2)