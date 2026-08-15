# -*- coding: utf-8 -*-
"""AstrBot 每日新闻聚合播报插件：手动/定时抓取并推送今日聚合新闻（标题+摘要+来源+发布时间）"""

import asyncio
import email.utils
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import MessageChain
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

# 插件元数据
PLUGIN_NAME = "astrbot_plugin_daily_news"
PLUGIN_AUTHOR = "云晓"
PLUGIN_DESC = "每日新闻聚合播报"
PLUGIN_VERSION = "1.0.0"

# 后台定时检查间隔（秒）
PUSH_CHECK_INTERVAL = 30
# 单次推送最大新闻条数
DEFAULT_NEWS_LIMIT = 5

# 新闻抓取源：优先国内可访问的免费 RSS，按顺序逐个尝试，全部失败则本次返回空（降级不影响运行）
NEWS_SOURCES = [
    {"name": "36氪", "url": "https://36kr.com/feed"},
    {"name": "cnBeta", "url": "https://www.cnbeta.com.tw/backend.php"},
    {"name": "新浪新闻", "url": "https://rss.sina.com.cn/news/china/focus15.xml"},
]

# Bing 搜索 RSS 输出（无需 API Key），作为关键词新闻源，优先于 RSS 抓取
BING_RSS_URL = "https://cn.bing.com/search?format=rss&count={count}&q={query}"

# 请求 UA，部分 RSS 源需要伪装浏览器
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 新闻时间统一按北京时间（UTC+8）显示与比较，避免依赖服务器时区导致偏差
CN_TZ = timezone(timedelta(hours=8))


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION)
class DailyNewsPlugin(Star):
    """每日新闻聚合播报：/新闻 手动推送；定时向目标群自动推送"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        # 数据目录（去重推送记录持久化位置）
        self.data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugin_data",
            PLUGIN_NAME,
        )
        os.makedirs(self.data_dir, exist_ok=True)

        # 去重记录：{日期(YYYY-MM-DD): [已推送群号, ...]}
        self._pushed: dict[str, list[str]] = {}
        # 群号 -> 平台实例 ID 映射（从命令事件自动学习，用于定时推送定位平台）
        self._group_platforms: dict[str, str] = {}
        self._load_pushed()

        # 定时任务状态
        self._task: asyncio.Task | None = None
        self._running = False

        logger.info(f"【{PLUGIN_NAME}】插件初始化完成，定时播报: {self.config.get('news_push_enable', False)}")

    # ========== 工具方法 ==========

    @staticmethod
    def _safe_int(value, default: int = 0) -> int:
        """安全转 int：脏值（None/非数字/越界）回退默认值"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_bool(value, default: bool = False) -> bool:
        """安全转 bool：支持 bool/字符串（true/false/1/0），脏值回退默认"""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "1", "yes", "on"):
                return True
            if v in ("false", "0", "no", "off"):
                return False
        return default

    def _now(self) -> datetime:
        """当前时间（独立方法便于测试注入）"""
        return datetime.now()

    def _push_enabled(self) -> bool:
        """定时播报总开关（脏值防御）"""
        return self._safe_bool(self.config.get("news_push_enable"), False)

    def _push_time(self) -> str:
        """读取定时播报时间，校验 HH:MM 格式，非法值回退默认 08:00"""
        target = str(self.config.get("news_push_time", "08:00") or "08:00").strip()
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", target):
            target = "08:00"
        return target

    def _push_groups(self) -> list[str]:
        """解析目标群号列表（英文逗号分隔，脏值防御）"""
        v = self.config.get("news_push_groups", "")
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        if isinstance(v, (list, tuple)):
            return [str(x).strip() for x in v if str(x).strip()]
        return []

    def _learn_platform(self, event) -> None:
        """从消息事件学习「群号 -> 平台实例 ID」映射，供定时推送定位平台使用"""
        try:
            group_id = event.get_group_id()
            if not group_id:
                return
            pid = event.get_platform_id()
            if pid:
                self._group_platforms[str(group_id)] = str(pid)
        except Exception:
            # 学习失败不影响指令本身
            pass

    def _resolve_platform(self, group_id: str) -> str:
        """确定推送目标群所属平台 ID：配置优先，其次自动学习映射，再否则为空"""
        v = str(self.config.get("news_push_platform", "") or "").strip()
        if v:
            return v
        return self._group_platforms.get(str(group_id), "")

    def _target_umo(self, group_id: str) -> str:
        """由群号构造推送目标 UMO（平台ID:GroupMessage:群号）；平台未知返回空串"""
        platform = self._resolve_platform(group_id)
        if not platform:
            return ""
        return f"{platform}:GroupMessage:{group_id}"

    # ========== 去重记录持久化 ==========

    def _pushed_file(self) -> str:
        return os.path.join(self.data_dir, "pushed.json")

    def _load_pushed(self):
        """从磁盘加载去重记录（结构校验，损坏时重置，不影响插件运行）"""
        try:
            path = self._pushed_file()
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # 仅保留合法条目（日期键 + 字符串列表值）
                    self._pushed = {
                        str(k): [str(g) for g in (v if isinstance(v, list) else [])]
                        for k, v in data.items()
                    }
                else:
                    logger.warning("新闻推送去重记录格式异常，已重置")
        except Exception as e:
            logger.warning(f"加载新闻推送去重记录失败: {e}")

    def _save_pushed(self):
        """保存去重记录到磁盘（失败仅告警，不影响运行）"""
        try:
            with open(self._pushed_file(), "w", encoding="utf-8") as f:
                json.dump(self._pushed, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存新闻推送去重记录失败: {e}")

    def _already_pushed(self, date: str, group_id: str) -> bool:
        """同一天同一群是否已推送"""
        return group_id in self._pushed.get(date, [])

    def _mark_pushed(self, date: str, group_id: str):
        """记录某日某群已推送并持久化"""
        self._pushed.setdefault(date, [])
        if group_id not in self._pushed[date]:
            self._pushed[date].append(group_id)
        self._save_pushed()

    # ========== 新闻抓取 ==========

    def _fetch_news(self, limit: int = DEFAULT_NEWS_LIMIT, bing_query: str | None = None) -> list[dict]:
        """抓取今日聚合新闻。

        bing_query 为 None 时使用配置 news_bing_query（空则跳过 Bing）。
        依次尝试 Bing 搜索（若配置关键词）与各 RSS 源（优先国内可访问），解析为
        [{"title", "summary", "source", "pub_time"}, ...]，
        按发布时间倒序排列（无时间字段的排末尾），并按 news_max_age_hours 过滤旧新闻。
        单个源失败自动跳过，全部失败返回空列表（降级，不影响插件运行）。
        """
        limit = max(1, self._safe_int(limit, DEFAULT_NEWS_LIMIT))
        if bing_query is None:
            bing_query = str(self.config.get("news_bing_query", "") or "").strip()
        news: list[dict] = []
        sources: list[tuple[str, str]] = [(s["name"], s["url"]) for s in NEWS_SOURCES]
        if bing_query:
            # Bing 搜索优先（更贴合用户意图，自带时间排序）
            sources.insert(0, ("__bing__", bing_query))
        for src_name, src_url in sources:
            try:
                if src_name == "__bing__":
                    items = self._fetch_bing(bing_query, limit)
                else:
                    items = self._fetch_rss(src_url, src_name)
                news.extend(items)
                logger.info(f"【{PLUGIN_NAME}】抓取 {src_name} 成功: {len(items)} 条")
                if len(news) >= limit:
                    break
            except Exception as e:
                # 单源失败仅告警，继续尝试下一源
                logger.warning(f"【{PLUGIN_NAME}】抓取 {src_name} 失败: {e}")
                continue
        # 发布时间倒序（无时间的排最末，key 用时间戳避免 aware/naive 混比）
        news.sort(key=lambda it: (it.get("pub_time").timestamp() if it.get("pub_time") else 0), reverse=True)
        news = self._filter_recent(news)
        return news[:limit]

    def _fetch_bing(self, query: str, count: int = 10) -> list[dict]:
        """通过 Bing 搜索 RSS 抓取与关键词相关的新闻条目。

        Bing 支持无 API Key 的 RSS 输出（format=rss），复用 _fetch_rss 的标准解析。
        """
        url = BING_RSS_URL.format(
            count=max(1, int(count)),
            query=urllib.parse.quote(query),
        )
        return self._fetch_rss(url, "Bing搜索")

    def _filter_recent(self, items: list[dict], now: datetime | None = None) -> list[dict]:
        """按 news_max_age_hours 过滤旧新闻（0/负值=不过滤）。

        无时间字段的条目不参与过滤（保留，排到末尾）。
        严格过滤结果为空时逐级放宽（2 倍时长 → 全部保留），避免推送为空。
        """
        max_hours = self._safe_int(self.config.get("news_max_age_hours", 24), 24)
        if max_hours <= 0:
            return items
        now = now or datetime.now(CN_TZ)
        if now.tzinfo is None:
            now = now.replace(tzinfo=CN_TZ)
        # 分级放宽：先严格（max_hours），为空再放宽到 2 倍，仍为空则全部保留
        for hours in (max_hours, max_hours * 2):
            kept = [
                it for it in items
                if it.get("pub_time") is None
                or (now - self._as_aware(it["pub_time"])).total_seconds() <= hours * 3600
            ]
            if kept:
                return kept
        return items

    @staticmethod
    def _as_aware(dt: datetime) -> datetime:
        """naive 时间按北京时间标记，返回 aware"""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=CN_TZ)
        return dt

    @staticmethod
    def _parse_time(raw: str) -> datetime | None:
        """解析 RSS/Atom 时间字段为北京时间 aware datetime。

        兼容 RSS2 的 RFC822（pubDate，email.utils.parsedate_tz 全版本可用）
        与 Atom 的 ISO8601（published/updated）。解析失败返回 None。
        """
        if not raw:
            return None
        s = raw.strip()
        try:
            t = email.utils.parsedate_tz(s)
            if t:
                # parsedate_tz 返回 10 元组，末位为 UTC 以东偏移秒数（+0800 → +28800）
                utc_naive = datetime(*t[:6]) - timedelta(seconds=(t[9] or 0))
                return utc_naive.replace(tzinfo=timezone.utc).astimezone(CN_TZ)
        except (TypeError, ValueError, OverflowError):
            pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(CN_TZ)
        except ValueError:
            return None

    def _fetch_rss(self, url: str, source: str) -> list[dict]:
        """抓取单个 RSS/Atom 源并解析出新闻条目（标准库 urllib + ElementTree）"""
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
        root = ET.fromstring(raw)
        items = []
        # 兼容 RSS(<item>) 与 Atom(<entry>)
        for node in root.findall(".//item") + root.findall(".//entry"):
            title = self._node_text(node, "title")
            if not title:
                continue
            summary = (
                self._node_text(node, "description")
                or self._node_text(node, "summary")
                or ""
            )
            # 清洗摘要：去 HTML 标签、压缩空白、截断
            summary = re.sub(r"<[^>]+>", "", summary)
            summary = re.sub(r"\s+", " ", summary).strip()
            if len(summary) > 120:
                summary = summary[:120] + "…"
            # 发布时间：RSS 用 pubDate，Atom 用 published/updated
            pub_raw = (
                self._node_text(node, "pubDate")
                or self._node_text(node, "published")
                or self._node_text(node, "updated")
            )
            items.append({
                "title": title,
                "summary": summary,
                "source": source,
                "pub_time": self._parse_time(pub_raw) if pub_raw else None,
            })
        return items

    @staticmethod
    def _node_text(node: ET.Element, tag: str) -> str:
        """取子节点文本（兼容带命名空间的 RSS），取不到返回空串"""
        text = node.findtext(tag)
        if text is not None:
            return text.strip()
        # 兼容 Atom 命名空间（tag 形如 {namespace}title）
        for child in node:
            m = re.match(r"^\{[^}]*\}(\w+)$", child.tag)
            if m and m.group(1) == tag and child.text:
                return child.text.strip()
        return ""

    # ========== 文本格式化 ==========

    def _time_tag(self, pub_time) -> str:
        """发布时间标签（北京时间）：当天显示 [HH:MM]，跨天显示 [MM-DD HH:MM]，无时间返回空串"""
        if not pub_time:
            return ""
        pub_time = self._as_aware(pub_time)
        now = datetime.now(CN_TZ)
        if pub_time.date() == now.date():
            return f"[{pub_time.strftime('%H:%M')}] "
        return f"[{pub_time.strftime('%m-%d')} {pub_time.strftime('%H:%M')}] "

    def _format_news(self, news: list[dict], date_str: str = "") -> str:
        """把新闻列表格式化为推送文本（时间+标题+摘要+来源），空列表返回空串"""
        if not news:
            return ""
        date_str = date_str or self._now().strftime("%Y-%m-%d")
        lines = [f"📰 今日新闻聚合播报（{date_str}）", "━━━━━━━━━━━━━━━━━━━━━━"]
        for i, item in enumerate(news, start=1):
            title = (item.get("title") or "").strip()
            summary = (item.get("summary") or "").strip()
            source = (item.get("source") or "未知来源").strip()
            if not title:
                continue
            tag = self._time_tag(item.get("pub_time"))
            lines.append(f"{i}. {tag}{title}" if tag else f"{i}. {title}")
            if summary:
                lines.append(f"   {summary}")
            lines.append(f"   来源: {source}")
        if len(lines) <= 2:
            # 没有任何有效条目
            return ""
        return "\n".join(lines)

    # ========== 发送 ==========

    async def _send_text_to(self, umo: str, text: str):
        """向指定 UMO 会话推送纯文本（失败仅告警）"""
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as e:
            logger.warning(f"【{PLUGIN_NAME}】推送新闻到 {umo} 失败: {e}")

    def _send_text(self, event: AstrMessageEvent, text: str):
        """构造命令回复的纯文本结果"""
        return event.chain_result([Plain(text)])

    # ========== 定时播报 ==========

    @filter.on_astrbot_loaded()
    async def _start_push_loop(self):
        """AstrBot 加载完成后启动定时播报任务（仅在启用且未运行时）"""
        if not self._push_enabled():
            return
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._push_loop())
        logger.info(f"【{PLUGIN_NAME}】定时播报已启动，每日 {self._push_time()} 推送")

    async def _push_loop(self):
        """后台循环：每 30 秒检查一次，到达设定时间向目标群推送（同天同群去重）"""
        while self._running:
            try:
                await self._check_and_push(self._now())
            except Exception as e:
                # 单次检查异常不影响后续轮询
                logger.warning(f"【{PLUGIN_NAME}】定时播报检查异常: {e}")
            await asyncio.sleep(PUSH_CHECK_INTERVAL)

    async def _check_and_push(self, now: datetime):
        """单次定时检查：运行中 && 启用 && 到达时间 && 有目标群 && 同天同群未推送 时推送"""
        if not self._running:
            return
        if not self._push_enabled():
            return
        target = self._push_time()
        cur = now.strftime("%H:%M")
        if cur < target:
            return
        today = now.strftime("%Y-%m-%d")
        groups = self._push_groups()
        if not groups:
            return
        # 抓取失败返回空则本次不推送（降级，不影响插件运行）
        news = self._fetch_news()
        text = self._format_news(news, today)
        if not text:
            logger.warning(f"【{PLUGIN_NAME}】{today} 新闻抓取为空，本次跳过推送")
            return
        for group in groups:
            if self._already_pushed(today, group):
                continue
            umo = self._target_umo(group)
            if not umo:
                logger.warning(
                    f"【{PLUGIN_NAME}】群 {group} 的平台 ID 未知"
                    f"（可在 news_push_platform 中指定，或先在该群使用一次 /新闻），本次跳过推送"
                )
                continue
            await self._send_text_to(umo, text)
            self._mark_pushed(today, group)

    # ========== 指令处理 ==========

    @filter.command("新闻")
    async def manual_news(self, event: AstrMessageEvent):
        """手动抓取并推送今日聚合新闻；/新闻 <关键词> 则按关键词 Bing 搜索"""
        try:
            self._learn_platform(event)
            sender = event.get_sender_id()
            logger.info(f"【{PLUGIN_NAME}】收到手动抓取请求，会话: {str(event.session)}, 发送者: {sender}")
            # 解析关键词：/新闻 xxx → Bing 搜索 xxx（兼容唤醒前缀剥离前后的格式）
            raw = event.message_str or ""
            m = re.match(r"^[\\/／]?\s*新闻\s*(.*)$", raw, re.S)
            query = (m.group(1) or "").strip() if m else ""
            news = self._fetch_news(bing_query=query or None)
            text = self._format_news(news)
            if not text:
                if query:
                    return self._send_text(event, f"❌ 未找到与「{query}」相关的新闻，请换关键词重试")
                return self._send_text(event, "❌ 今日新闻抓取失败或为空，请稍后重试")
            return self._send_text(event, text)
        except Exception as e:
            logger.error(f"【{PLUGIN_NAME}】手动抓取新闻异常: {e}")
            return self._send_text(event, f"❌ 抓取新闻时出错: {str(e)}")

    # ========== 生命周期 ==========

    async def terminate(self):
        """插件卸载时取消定时任务"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None